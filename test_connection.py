import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from vast_connection import Connection,ssh_address

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

    def test_fresh_session_preserves_manual_ssh_and_saved_auto_setting(self):
        settings={"ssh":{"host":"ssh1.vast.ai","port":1234},"auto":False}
        self.c.settings=settings.copy()
        original=json.dumps(settings,indent=2).encode()
        self.c.path.write_bytes(original)
        with patch.object(self.c,"_run"),patch.object(self.c,"refresh") as discover:
            self.c.reconnect(discover_new=True)
            self.c.thread.join()
        self.assertEqual(self.c.path.read_bytes(),original)
        self.assertEqual(self.c.settings,settings)
        discover.assert_not_called()

    def test_fresh_automatic_vast_session_discoveries_do_not_use_old_endpoint(self):
        self.c.settings={"instance":12,"auto":True}
        self.c.endpoint={"id":12,"host":"ssh1.vast.ai","port":1111}
        with patch.object(self.c,"refresh") as refresh:
            self.c.reconnect(discover_new=True)
        refresh.assert_called_once_with()
        self.assertIsNone(self.c.endpoint)
        self.assertTrue(self.c.settings["auto"])

    def test_refresh_discovers_newest_running_instance_and_keeps_active_submission(self):
        self.discover.return_value += [
            dict(id=31,host='ssh2.vast.ai',port=22,status='stopped'),
            dict(id=19,host='ssh3.vast.ai',port=4321,status='running'),
        ]
        with patch.object(self.c,"connect") as connect:
            self.c.refresh(keep_active=True)
            connect.assert_not_called()
            self.c.refresh()
            connect.assert_called_once_with(19,auto=True)
        self.assertEqual(self.discover.call_count,2)

    def test_auto_mode_switches_immediately_from_stopped_saved_instance_to_new_running_one(self):
        self.c.settings = dict(instance=12, auto=True)
        self.discover.return_value = [
            dict(id=12, host='ssh1.vast.ai', port=1234, status='exited'),
            dict(id=99, host='ssh9.vast.ai', port=9999, status='running'),
        ]
        target = self.c._resolve_active()
        self.assertEqual(target['id'], 99)
        self.assertEqual(self.c.settings, dict(instance=99, auto=True))
        self.assertEqual(json.loads(self.c.path.read_text()), dict(instance=99, auto=True))

    def test_first_login_verifies_host_and_attaches_key_when_needed(self):
        self.remote.side_effect = [OSError("Host key verification failed"),
                                   OSError("Permission denied (publickey)."), b"8188"]
        with patch("vast_ssh_setup.trust_host") as trust, patch("vast_ssh_setup.attach_key") as attach:
            target = self.discover.return_value[0]
            self.assertEqual(self.c._remote_port(target),8188)
            trust.assert_called_once_with(target)
            attach.assert_called_once_with(target)

    def test_attach_key_retries_briefly_while_vast_propagates_key(self):
        self.remote.side_effect = [OSError("Permission denied (publickey)."),
                                   OSError("Permission denied (publickey)."), b"8188"]
        with patch("vast_ssh_setup.attach_key") as attach, patch.object(self.c.stop, "wait", return_value=False) as wait:
            target = self.discover.return_value[0]
            self.assertEqual(self.c._remote_port(target), 8188)
            attach.assert_called_once_with(target)
            wait.assert_called_once_with(2)

    def test_identity_options_uses_saved_axdar_key_even_for_direct_ssh(self):
        import vast_credentials
        with tempfile.TemporaryDirectory() as tmp:
            fake_key = Path(tmp) / "id_axdar_vast"
            fake_key.write_text("key", encoding="utf-8")
            with patch.object(vast_credentials, "VAST_IDENTITY", fake_key):
                opts = vast_credentials.identity_options({"host": "ssh1.vast.ai", "port": 1234})
                self.assertEqual(opts, ["-i", str(fake_key)])

    def test_ensure_account_ssh_key_registers_missing_key_on_vast_account(self):
        import vast_ssh_setup
        pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKeyForUnitTestOnly"
        with patch.object(vast_ssh_setup, "ensure_local_key", return_value=pub), \
             patch.object(vast_ssh_setup, "vast", side_effect=[[], {"success": True}]) as vast_call:
            self.assertTrue(vast_ssh_setup.ensure_account_ssh_key())
            self.assertEqual(vast_call.call_args_list[0].args, ("ssh/",))
            self.assertEqual(vast_call.call_args_list[1].args, ("ssh/", {"ssh_key": pub}))
        with patch.object(vast_ssh_setup, "ensure_local_key", return_value=pub), \
             patch.object(vast_ssh_setup, "vast", return_value=[{"public_key": pub}]) as vast_call:
            self.assertFalse(vast_ssh_setup.ensure_account_ssh_key())
            vast_call.assert_called_once_with("ssh/")

    def test_running_comfyui_service_gets_time_to_expose_its_api(self):
        self.remote.side_effect=[b'',b'pending',b'',b'',b'18188']
        with patch.object(self.c.stop,"wait",return_value=False) as wait:
            self.assertEqual(self.c._remote_port(self.discover.return_value[0]),18188)
        self.assertEqual(wait.call_count,3)
        service=self.remote.call_args_list[1].args[1]
        self.assertIn('RUNNING',service)
        self.assertIn('STARTING',service)

    def test_discovery_reports_loading_instance_and_connects_when_it_runs(self):
        self.discover.return_value=[dict(id=23,host='ssh2.vast.ai',port=5678,status='loading')]
        with patch.object(self.c,"connect") as connect:
            waiting=self.c.refresh()
            self.assertEqual(waiting['state'],'Vast starting')
            self.assertIn('Instance #23',waiting['error'])
            connect.assert_not_called()
            self.discover.return_value[0]['status']='running'
            self.c.refresh()
            connect.assert_called_once_with(23,auto=True)

    def test_changed_host_is_not_replaced(self):
        self.remote.side_effect = OSError("REMOTE HOST IDENTIFICATION HAS CHANGED: Host key verification failed.")
        with patch("vast_ssh_setup.trust_host") as trust, self.assertRaises(OSError):
            self.c._remote_port(self.discover.return_value[0])
        trust.assert_not_called()

    def test_stopped_and_unsafe_targets(self):
        for changes in [dict(status='stopped'),dict(host='-oProxyCommand=bad'),dict(port=0)]:
            self.discover.return_value=[dict(id=12,host='ssh1.vast.ai',port=22,status='running',**{})|changes]
            with self.assertRaises(ValueError):self.c.resolve(12)
        self.remote.assert_not_called()

    def test_pasted_vast_ssh_command_extracts_only_the_endpoint(self):
        for value in ("ssh -p 1234 root@ssh1.vast.ai -L 8080:localhost:8080\n",
                      "ssh root@ssh1.vast.ai -p 1234", "ssh://root@ssh1.vast.ai:1234"):
            self.assertEqual(ssh_address(value),{"host":"ssh1.vast.ai","port":1234})
        for value in ("ssh -o ProxyCommand=cmd root@ssh1.vast.ai","ssh root@ssh1.vast.ai whoami",
                      "ssh -p 0 root@ssh1.vast.ai","ssh://root@ssh1.vast.ai:0","ssh root@ssh1.vast.ai; test",
                      "ssh -p 22 root@ssh1.vast.ai\nwhoami","-----BEGIN OPENSSH PRIVATE KEY-----"):
            with self.subTest(value=value),self.assertRaises(ValueError):
                ssh_address(value)

    def test_manual_ssh_connects_without_vast_api(self):
        address={"host":"ssh1.vast.ai","port":1234}
        with patch("vast_connection.threading.Thread"):
            self.c.connect_ssh("ssh -p 1234 root@ssh1.vast.ai",True)
        self.assertEqual(json.loads(self.c.path.read_text()),{"ssh":address,"auto":True})
        self.c._open=Mock()
        self.c._probe=Mock(return_value=True)
        def wait(seconds):
            if seconds==5:
                self.assertEqual(self.c.ssh_target(),address)
                self.c.stop.set()
                return True
            return False
        self.c.stop.wait=wait
        self.c._run()
        self.c._open.assert_called_once_with(address,8188)
        self.discover.assert_not_called()

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
            process.poll.return_value=None
            opened.append(target['port']);self.c.process=process;self.c.url='http://127.0.0.1:22222'
        self.c._open=open_tunnel
        self.c._probe=Mock(side_effect=[True,False,True])
        def wait(seconds):
            if seconds==5:
                observed.append(self.c.state)
                if len(observed)==1:
                    self.discover.return_value[0]['port']=5678
                    process.poll.return_value=1
                else:self.c.stop.set();return True
            return False
        self.c.stop.wait=wait
        self.c._run()
        self.assertEqual(opened,[1234,5678]);self.assertEqual(observed,['Ready','Ready'])
        self.assertEqual(process.terminate.call_count,2)
        self.assertEqual(self.c.state,'Disconnected')
        self.assertTrue(all('prompt' not in str(call) for call in self.remote.call_args_list))

    def test_one_missed_comfy_check_keeps_the_tunnel_alive(self):
        process=Mock();process.poll.return_value=None
        self.c.process=process
        self.c._open=Mock()
        self.c._probe=Mock(side_effect=[True,False,True])
        states=[]
        def wait(seconds):
            self.assertEqual(seconds,5)
            states.append(self.c.state)
            return len(states)==3
        self.c.stop.wait=wait
        self.c._run()
        self.assertEqual(states,['Ready','ComfyUI Starting','Ready'])
        self.c._open.assert_called_once()
        process.terminate.assert_called_once()

    def test_retry_only_restarts_the_saved_instance(self):
        self.c._state('Failed')
        with patch.object(self.c,'_start') as start:
            self.c.retry_saved('13')
            start.assert_not_called()
            self.c.retry_saved('12')
            start.assert_called_once_with(self.c.settings)
            self.c.stop.set()
            self.c.retry_saved('12')
            start.assert_called_once()

    def test_tunnel_security_and_local_binding(self):
        with patch('vast_connection.subprocess.Popen') as popen:
            self.c._open(self.discover.return_value[0],8188)
            args=popen.call_args.args[0]
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('ExitOnForwardFailure=yes',args)
            self.assertEqual(args[args.index('ConnectTimeout=30')-1],'-o')
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
    def test_ssh_sets_thirty_second_login_timeout(self):
        from vast_ssh_ops import ssh
        with patch('vast_ssh_ops.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'18188',b'')) as run:
            self.assertEqual(ssh({'host':'fixture','port':22},'probe',timeout=45),b'18188')
        args=run.call_args.args[0]
        self.assertEqual(args[args.index('ConnectTimeout=30')-1],'-o')
        self.assertEqual(run.call_args.kwargs['timeout'],45)

    def test_direct_mapping_and_proxy_fallback(self):
        from vast_ssh_ops import instance_target
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
