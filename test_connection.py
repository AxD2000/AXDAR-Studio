import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from vast_connection import Connection

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        token_patch=patch('vast_connection.hf_token',return_value=None);token_patch.start();self.addCleanup(token_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.discover = Mock(return_value=[dict(id=12, host='ssh1.vast.ai', port=1234, status='running')])
        self.remote = Mock(return_value=b'8188\n')
        self.c = Connection(Path(self.tmp.name)/'connection.json', self.discover, self.remote)
        self.c.settings = dict(instance=12, auto=False)

    def test_resolve_refreshes_changed_port(self):
        self.assertEqual(self.c.resolve(12)['port'],1234)
        self.discover.return_value[0]['port']=5678
        self.assertEqual(self.c.resolve(12)['port'],5678)

    def test_stopped_and_unsafe_targets(self):
        for changes in [dict(status='stopped'),dict(host='-oProxyCommand=bad'),dict(port=0)]:
            self.discover.return_value=[dict(id=12,host='ssh1.vast.ai',port=22,status='running',**{})|changes]
            with self.assertRaises(ValueError):self.c.resolve(12)
        self.remote.assert_not_called()

    def test_bounded_retries_cleanup_and_rediscovery(self):
        self.c._open=Mock()
        self.c._probe=Mock(return_value=False)
        self.c.stop.wait=Mock(return_value=False)
        self.c._run()
        self.assertEqual(self.c.state,'Failed')
        self.assertEqual(self.c._open.call_count,3)
        self.assertEqual(self.discover.call_count,3)
        self.assertIsNone(self.c.process)

    def test_ready_and_reconnect_no_submission(self):
        process=Mock();process.poll.return_value=None
        opened=[];observed=[]
        def open_tunnel(target,port):
            opened.append(target['port']);self.c.process=process;self.c.url='http://127.0.0.1:22222'
        self.c._open=open_tunnel
        self.c._probe=Mock(side_effect=[True,False,True])
        def wait(seconds):
            if seconds==5:
                observed.append(self.c.state)
                if len(observed)==1:
                    self.discover.return_value[0]['port']=5678
                else:self.c.stop.set();return True
            return False
        self.c.stop.wait=wait
        self.c._run()
        self.assertEqual(opened,[1234,5678]);self.assertEqual(observed,['Ready','Ready'])
        self.assertEqual(process.terminate.call_count,2)
        self.assertEqual(self.c.state,'Disconnected')
        self.assertTrue(all('prompt' not in str(call) for call in self.remote.call_args_list))

    def test_tunnel_security_and_local_binding(self):
        with patch('vast_connection.subprocess.Popen') as popen:
            self.c._open(self.discover.return_value[0],8188)
            args=popen.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('ExitOnForwardFailure=yes',args)
            self.assertRegex(args[args.index('-L')+1],r'^127\.0\.0\.1:\d+:127\.0\.0\.1:8188$')
            self.c._cleanup();popen.return_value.terminate.assert_called_once()

    def test_cleanup_kills_only_owned_process_on_timeout(self):
        own=Mock();own.wait.side_effect=[subprocess.TimeoutExpired('ssh',5),0]
        self.c.process=own;self.c._cleanup();own.kill.assert_called_once()
        self.assertIsNone(self.c.process)

    def test_connect_persists_identity_and_deduplicates(self):
        with patch('vast_connection.threading.Thread') as thread:
            thread.return_value.is_alive.return_value=True
            self.c.connect(12,True);self.c.connect(12,True)
            thread.assert_called_once()
            self.assertEqual(json.loads(self.c.path.read_text()),dict(instance=12,auto=True))
            with self.assertRaises(ValueError):self.c.connect(13)

    def test_probe_requires_comfy_json_and_alive_process(self):
        self.c.process=Mock();self.c.process.poll.return_value=None;self.c.url='http://127.0.0.1:11111'
        with patch('vast_connection.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value=b'{"system":{}}'
            self.assertTrue(self.c._probe())
            request.return_value.__enter__.return_value.read.return_value=b'{"unrelated":1}'
            self.assertFalse(self.c._probe())
            self.c.process.poll.return_value=1
            self.assertFalse(self.c._probe())

    def test_terminal_once_shared_target_and_size_limits(self):
        self.c._state('Ready');self.c.endpoint=self.discover.return_value[0]
        self.remote.return_value=b'hello'
        self.assertEqual(self.c.command('echo hello'),'hello')
        self.remote.assert_called_once();self.assertEqual(self.remote.call_args.args[0]['id'],12)
        self.assertIn('head -c 65536',self.remote.call_args.args[1])
        with self.assertRaises(ValueError):self.c.command('x'*4097)
        self.c._state('Disconnected')
        with self.assertRaises(ValueError):self.c.command('echo hello')

    def test_service_start_only_uses_verified_stopped_supervisor(self):
        self.remote.side_effect=[b'',b'',b'18188']
        self.c.stop.wait=Mock(return_value=False)
        self.assertEqual(self.c._remote_port(self.discover.return_value[0]),18188)
        cmd=self.remote.call_args_list[1].args[1]
        self.assertIn('supervisorctl status comfyui',cmd);self.assertIn('STOPPED',cmd)
        self.assertNotIn('main.py',cmd)

    def test_windows_vault_backend(self):
        from vast_connection import api_key
        with patch('keyring.backends.Windows.WinVaultKeyring') as vault, patch.dict('os.environ',{},clear=True):
            vault.return_value.get_password.return_value='test-only'
            self.assertEqual(api_key('test-only'),'test-only')
            vault.return_value.set_password.assert_called_once()

class DiagnosticTests(unittest.TestCase):
    def test_direct_mapping_and_proxy_fallback(self):
        from vast_deploy import instance_target
        row=dict(id=1,ssh_host='ssh5.vast.ai',ssh_port=123,actual_status='running',public_ipaddr='192.0.2.1',ports={'22/tcp':[{'HostPort':'50860'}]})
        self.assertEqual(instance_target(row)['host'],'192.0.2.1')
        self.assertEqual(instance_target(row)['port'],50860)
        row['ports']={}
        self.assertEqual(instance_target(row)['host'],'ssh5.vast.ai')
    def test_safe_diagnostic_classification(self):
        from vast_connection import connection_error
        for message,expected in [('Host key verification failed.','fingerprint'),('Permission denied (publickey).','authentication'),('Connection closed by remote host','before login'),('Connection refused','refused')]:
            self.assertIn(expected,connection_error(OSError(message)))
        self.assertNotIn('secret',connection_error(OSError('unknown secret raw-output')))

class WorkflowToolTests(unittest.TestCase):
    def test_hf_token_is_stdin_only_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, patch('vast_connection.hf_token',return_value='hf_testsecret'):
            remote=Mock(return_value=b'got hf_testsecret')
            c=Connection(Path(tmp)/'state.json',Mock(),remote);c.endpoint={'id':1,'host':'example.com','port':22};c._state('Ready')
            self.assertEqual(c.command('echo $HF_TOKEN'),'got [REDACTED]')
            self.assertNotIn('hf_testsecret',remote.call_args.args[1])
            self.assertEqual(remote.call_args.kwargs['data'],b'hf_testsecret')
            self.assertNotIn('hf_testsecret',json.dumps(c.snapshot()))
    def test_indicators_are_grey_then_api_fail_is_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=Connection(Path(tmp)/'state.json',Mock(side_effect=OSError('network')),Mock())
            self.assertTrue(all(v=='grey' for v in c.snapshot()['indicators'].values()))
            with self.assertRaises(OSError):c.resolve(1)
            self.assertEqual(c.snapshot()['indicators']['api'],'red')
            self.assertEqual(c.snapshot()['indicators']['ssh'],'grey')
            c.disconnect();self.assertTrue(all(v=='grey' for v in c.snapshot()['indicators'].values()))
    def test_gpu_pass_requires_comfy_device(self):
        with tempfile.TemporaryDirectory() as tmp, patch('vast_connection.urlopen') as request:
            c=Connection(Path(tmp)/'state.json',Mock(),Mock());c.process=Mock();c.process.poll.return_value=None;c.url='http://127.0.0.1:1'
            request.return_value.__enter__.return_value.read.return_value=b'{"system":{},"devices":[{"type":"cuda"}]}'
            self.assertTrue(c._probe());self.assertEqual(c.snapshot()['indicators']['gpu'],'green')
            request.return_value.__enter__.return_value.read.return_value=b'{"system":{},"devices":[{"type":"cpu"}]}'
            self.assertTrue(c._probe());self.assertEqual(c.snapshot()['indicators']['gpu'],'red')
            request.side_effect=OSError('offline');self.assertFalse(c._probe());self.assertEqual(c.snapshot()['indicators']['comfy'],'red')

if __name__=='__main__':unittest.main()
