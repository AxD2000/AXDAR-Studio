"""Local HTTP acceptance with mocked external services; no Vast requests."""
import json
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock,patch

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx=runpy.run_path('studio.pyw')
        cls.app=cls.ctx['app']
        cls.g=cls.app
        cls.server=cls.ctx['StudioServer'](('127.0.0.1',0),cls.ctx['Handler'])
        cls.app.PORT=cls.server.server_port
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def post(self,obj,token=True,route='/connection'):
        request=Request(self.url+route,data=json.dumps(obj).encode() if token else b'',headers={'Content-Type':'application/json','X-Studio':self.g['TOKEN'] if token else 'invalid'})
        with urlopen(request) as response:return json.load(response)
    def test_authentication_required(self):
        with self.assertRaises(HTTPError) as error:self.post({'action':'connect'},False)
        self.assertEqual(error.exception.code,403);error.exception.close()
    def test_dispatch_uses_single_controller_connection(self):
        connection=Mock();connection.connect.return_value={'state':'Connecting'}
        connection.disconnect.return_value={'state':'Disconnected'};connection.command.return_value='done'
        with patch.object(self.g['controller'],'connection',connection):
            self.assertEqual(self.post({'action':'connect','instance':12})['state'],'Connecting')
            self.assertEqual(self.post({'action':'command','command':'echo done'})['output'],'done')
            self.assertEqual(self.post({'action':'disconnect'})['state'],'Disconnected')
            connection.connect.assert_called_once_with(12,False)
            connection.command.assert_called_once_with('echo done')
    def test_manual_ssh_dispatches_to_the_shared_controller(self):
        connection=Mock()
        connection.connect_ssh.return_value={"state":"Connecting"}
        with patch.object(self.g["controller"],"connection",connection):
            self.assertEqual(self.post({"action":"connect-ssh","ssh":"ssh -p 1234 root@ssh1.vast.ai"})["state"],"Connecting")
        connection.connect_ssh.assert_called_once_with("ssh -p 1234 root@ssh1.vast.ai",False)

    def test_refresh_uses_the_shared_connection(self):
        connection=Mock()
        connection.refresh.return_value={"state":"Connecting","instance":13}
        with patch.multiple(self.app,running=False,production_busy=lambda:False), \
             patch.object(self.app.workflow_manager,'busy',return_value=False), \
             patch.object(self.app.controller,"connection",connection):
            self.assertEqual(self.post({"action":"refresh"})["instance"],13)
        connection.refresh.assert_called_once_with(keep_active=False)

    def test_unknown_action_rejected(self):
        with self.assertRaises(HTTPError) as error:self.post({'action':'unexpected'})
        self.assertEqual(error.exception.code,400);error.exception.close()

    def test_new_workspace_archives_tracking_and_keeps_the_shared_connection(self):
        controller=Mock()
        controller.current.return_value={}
        direct=Mock(interrupted=False)
        manager=Mock()
        with patch.multiple(self.app,controller=controller,direct_session=direct,workflow_manager=manager,workspace_started=threading.Event()):
            self.post({'mode':'new'},route='/workspace-session')
        controller.archive_for_new_session.assert_called_once_with()
        direct.archive_for_new_session.assert_called_once_with()
        controller.connection.reconnect.assert_called_once_with(discover_new=True)
        manager.watch.assert_called_once_with()

    def test_resume_workspace_reuses_previous_tracking(self):
        controller=Mock()
        controller.current.return_value={'episode':'saved-episode'}
        direct=Mock(interrupted=False)
        with patch.multiple(self.app,controller=controller,direct_session=direct,workflow_manager=Mock(),
                            workspace_started=threading.Event(),session={}):
            self.assertEqual(self.post({'mode':'resume'},route='/workspace-session')['episode'],'saved-episode')
            self.post({'mode':'resume'},route='/workspace-session')
        controller.connection.reconnect.assert_called_once_with()
        controller.reconnect.assert_called_once_with()
        direct.archive_for_new_session.assert_not_called()
    def test_panel_and_static_script_available(self):
        with urlopen(self.url) as response:html=response.read().decode()
        self.assertIn('/connection-ui.js',html)
        with urlopen(self.url+'/connection-ui.js') as response:script=response.read().decode()
        self.assertIn('Terminal &amp; HF Token',script)
        self.assertIn('type="password"',script)
    def test_status_contains_shared_state(self):
        with urlopen(self.url+'/status') as response:status=json.load(response)
        self.assertIn('connection',status);self.assertEqual(status['running'],False)
        self.assertTrue(status['restartSupported'])

    def test_restart_is_available_after_active_work_finishes(self):
        requested=threading.Event()
        shutdown=threading.Event()
        with patch.multiple(self.app,restart_requested=requested,running=True,production_busy=lambda:False), \
             patch.object(self.app.workflow_manager,'busy',return_value=False), \
             patch.object(self.server,'shutdown',side_effect=shutdown.set):
            with self.assertRaises(HTTPError) as error:
                self.post({},route='/studio/restart')
            self.assertEqual(error.exception.code,400);error.exception.close()
            self.assertFalse(requested.is_set())
            self.app.running=False
            self.assertEqual(self.post({},route='/studio/restart'),{'restarting':True})
            self.assertTrue(requested.is_set() and shutdown.wait(2))

    def test_reference_upload_reuses_server_file_and_falls_back_on_its_own_tunnel(self):
        url='http://127.0.0.1:19500'
        remote_name='axdar_'+'a'*64+'.png'
        connection=Mock(lock=threading.RLock())
        connection.snapshot.return_value={'url':url}
        connection.target.return_value={'id':42,'host':'fixture','port':22}
        with tempfile.TemporaryDirectory() as root:
            asset=Path(root)/'reference.png'
            asset.write_bytes(b'fixture')
            with patch.multiple(self.app,reference_available=Mock(side_effect=[True,False,True]),
                                remote=Mock(side_effect=ConnectionResetError("upload reset"))), \
                 patch.object(self.app.controller,'connection',connection), \
                 patch.object(self.app.vast_deploy,'stage_reference') as stage:
                self.assertEqual(self.app.upload(url,asset,remote_name),remote_name)
                self.app.remote.assert_not_called()
                self.assertEqual(self.app.upload(url,asset,remote_name),remote_name)
                stage.assert_called_once_with(connection.target.return_value,asset,remote_name)

    def test_second_studio_cannot_listen_on_the_same_address(self):
        with self.assertRaises(OSError):
            self.ctx['StudioServer'](self.server.server_address,self.ctx['Handler'])

    def test_post_requires_matching_host_and_separates_500_from_400(self):
        request=Request(self.url+'/save',data=b'{}',headers={'Content-Type':'application/json','X-Studio':self.g['TOKEN'],'Host':'evil.example:18753'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code,403);error.exception.close()
        with patch.object(self.g['actions'],'general_save',side_effect=RuntimeError('boom')):
            with self.assertRaises(HTTPError) as error:
                self.post({},route='/save')
            self.assertEqual(error.exception.code,500);error.exception.close()

    def test_workspaces_icons_and_slim_pagehide_save(self):
        with urlopen(self.url+'/workspaces.js') as response:
            js=response.read().decode()
        self.assertIn("title:'Workflow',symbol:",js)
        with urlopen(self.url) as response:
            html=response.read().decode()
        self.assertIn('function savePayload()',html)
        self.assertIn('saveInFlight',html)
        self.assertIn("'Content-Type':'application/json'",html)

    def test_files_snapshot_is_isolated_from_in_place_updates(self):
        snap=self.g['files_snapshot']()
        self.assertIsNot(snap,self.g['files'])

    def test_layer_d_production_ui_and_service_status_endpoint(self):
        with urlopen(self.url+'/production-ui.js') as response:
            prod_js=response.read().decode()
        self.assertIn('Autonomous Vast production (Layer D)',prod_js)
        self.assertIn('driveClientFile',prod_js)
        self.assertIn('cancel-countdown',prod_js)
        with urlopen(self.url+'/connection-ui.js') as response:
            conn_js=response.read().decode()
        self.assertIn('connect-ssh',conn_js)
        self.assertIn('Discover instances',conn_js)
        status=self.post({'action':'service-status'},route='/production')
        self.assertEqual(set(status.keys()),{'driveConnected','webhookSaved','visualSaved'})
        self.assertTrue(all(isinstance(v,bool) for v in status.values()))

if __name__=='__main__':unittest.main()
