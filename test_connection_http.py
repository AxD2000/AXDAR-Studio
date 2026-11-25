"""Local HTTP acceptance with mocked external services; no Vast requests."""
import json
import runpy
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock, patch


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = runpy.run_path('studio.pyw')
        cls.app = cls.ctx['app']
        cls.g = cls.app
        cls.server = cls.ctx['StudioServer'](('127.0.0.1', 0), cls.ctx['Handler'])
        cls.app.PORT = cls.server.server_port
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def post(self, obj, token=True, route='/connection'):
        request = Request(
            self.url + route,
            data=json.dumps(obj).encode() if token else b'',
            headers={'Content-Type': 'application/json', 'X-Studio': self.g['TOKEN'] if token else 'invalid'},
        )
        with urlopen(request) as response:
            return json.load(response)

    def test_authentication_required(self):
        with self.assertRaises(HTTPError) as error:
            self.post({'action': 'connect'}, False)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_dispatch_uses_single_connection(self):
        connection = Mock()
        connection.connect.return_value = {'state': 'Connecting'}
        connection.disconnect.return_value = {'state': 'Disconnected'}
        connection.command.return_value = 'done'
        with patch.object(self.app, 'connection', connection):
            self.assertEqual(self.post({'action': 'connect', 'instance': 12})['state'], 'Connecting')
            self.assertEqual(self.post({'action': 'command', 'command': 'echo done'})['output'], 'done')
            self.assertEqual(self.post({'action': 'disconnect'})['state'], 'Disconnected')
            connection.connect.assert_called_once_with(12, False)
            connection.command.assert_called_once_with('echo done')

    def test_manual_ssh_dispatches_to_the_shared_connection(self):
        connection = Mock()
        connection.connect_ssh.return_value = {"state": "Connecting"}
        with patch.object(self.app, "connection", connection):
            self.assertEqual(self.post({"action": "connect-ssh", "ssh": "ssh -p 1234 root@ssh1.vast.ai"})["state"], "Connecting")
        connection.connect_ssh.assert_called_once_with("ssh -p 1234 root@ssh1.vast.ai", False)

    def test_refresh_uses_the_shared_connection(self):
        connection = Mock()
        connection.refresh.return_value = {"state": "Connecting", "instance": 13}
        with patch.multiple(self.app, running=False, connection=connection), \
             patch.object(self.app.workflow_manager, 'busy', return_value=False):
            self.assertEqual(self.post({"action": "refresh"})["instance"], 13)
        connection.refresh.assert_called_once_with(keep_active=False)

    def test_unknown_action_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post({'action': 'unexpected'})
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_new_workspace_archives_tracking_and_keeps_the_shared_connection(self):
        connection = Mock()
        direct = Mock(interrupted=False)
        manager = Mock()
        with patch.multiple(self.app, connection=connection, direct_session=direct, workflow_manager=manager, workspace_started=threading.Event()):
            self.post({'mode': 'new'}, route='/workspace-session')
        direct.archive_for_new_session.assert_called_once_with()
        connection.reconnect.assert_called_once_with(discover_new=True)
        manager.watch.assert_called_once_with()

    def test_resume_workspace_reuses_previous_tracking(self):
        connection = Mock()
        direct = Mock(interrupted=False)
        with patch.multiple(self.app, connection=connection, direct_session=direct, workflow_manager=Mock(),
                            workspace_started=threading.Event(), session={'episode': 'saved-episode'}):
            self.assertEqual(self.post({'mode': 'resume'}, route='/workspace-session')['episode'], 'saved-episode')
            self.post({'mode': 'resume'}, route='/workspace-session')
        connection.reconnect.assert_called_once_with()
        direct.archive_for_new_session.assert_not_called()

    def test_panel_and_static_script_available(self):
        with urlopen(self.url) as response:
            html = response.read().decode()
        self.assertIn('/connection-ui.js', html)
        with urlopen(self.url + '/connection-ui.js') as response:
            script = response.read().decode()
        self.assertIn('Terminal &amp; HF Token', script)
        self.assertIn('type="password"', script)

    def test_status_contains_shared_state(self):
        with urlopen(self.url + '/status') as response:
            status = json.load(response)
        self.assertIn('connection', status)
        self.assertNotIn('production', status)
        self.assertEqual(status['running'], False)
        self.assertTrue(status['restartSupported'])

    def test_restart_is_available_after_active_work_finishes(self):
        requested = threading.Event()
        shutdown = threading.Event()
        with patch.multiple(self.app, restart_requested=requested, running=True), \
             patch.object(self.app.workflow_manager, 'busy', return_value=False), \
             patch.object(self.server, 'shutdown', side_effect=shutdown.set):
            with self.assertRaises(HTTPError) as error:
                self.post({}, route='/studio/restart')
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            self.assertFalse(requested.is_set())
            self.app.running = False
            self.assertEqual(self.post({}, route='/studio/restart'), {'restarting': True})
            self.assertTrue(requested.is_set() and shutdown.wait(2))

    def test_reference_upload_reuses_server_file_and_falls_back_on_its_own_tunnel(self):
        url = 'http://127.0.0.1:19500'
        remote_name = 'axdar_' + 'a' * 64 + '.png'
        connection = Mock(lock=threading.RLock())
        connection.snapshot.return_value = {'url': url}
        connection.target.return_value = {'id': 42, 'host': 'fixture', 'port': 22}
        with tempfile.TemporaryDirectory() as root:
            asset = Path(root) / 'reference.png'
            asset.write_bytes(b'fixture')
            with patch.multiple(self.app, reference_available=Mock(side_effect=[True, False, True]),
                                remote=Mock(side_effect=ConnectionResetError("upload reset")),
                                connection=connection), \
                 patch.object(self.app.ssh_ops, 'stage_reference') as stage:
                self.assertEqual(self.app.upload(url, asset, remote_name), remote_name)
                self.app.remote.assert_not_called()
                self.assertEqual(self.app.upload(url, asset, remote_name), remote_name)
                stage.assert_called_once_with(connection.target.return_value, asset, remote_name)

    def test_second_studio_cannot_listen_on_the_same_address(self):
        with self.assertRaises(OSError):
            self.ctx['StudioServer'](self.server.server_address, self.ctx['Handler'])

    def test_post_requires_matching_host_and_separates_500_from_400(self):
        request = Request(self.url + '/save', data=b'{}', headers={'Content-Type': 'application/json', 'X-Studio': self.g['TOKEN'], 'Host': 'evil.example:18753'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with patch.object(self.g['actions'], 'general_save', side_effect=RuntimeError('boom')):
            with self.assertRaises(HTTPError) as error:
                self.post({}, route='/save')
            self.assertEqual(error.exception.code, 500)
            error.exception.close()

    def test_removed_workspaces_sections_and_routes_are_absent(self):
        with urlopen(self.url + '/workspaces.js') as response:
            ws_js = response.read().decode()
        self.assertIn("title:'Worker',symbol:", ws_js)
        self.assertIn("title:'Workflow',symbol:", ws_js)
        for removed in ('Continuity', 'Video Editor', 'Economics', 'Settings'):
            self.assertNotIn(removed, ws_js)
        with urlopen(self.url) as response:
            html = response.read().decode()
        self.assertIn('function savePayload()', html)
        self.assertIn('saveInFlight', html)
        self.assertIn('/worker-ui.js', html)
        with urlopen(self.url + '/worker-ui.js') as response:
            worker_js = response.read().decode()
        self.assertIn('Worker Execution', worker_js)
        for removed in ('/production-ui.js', '/editor-ui.js', 'Shot history', 'Continuity rules', 'remoteProductionPanel', 'productionServicesPanel'):
            self.assertNotIn(removed, html)
        for removed_asset in ('/production-ui.js', '/editor-ui.js'):
            with self.assertRaises(HTTPError) as err:
                urlopen(self.url + removed_asset)
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        for removed_route in ('/editor', '/production', '/production/clear-history', '/drive/connect'):
            with self.assertRaises(HTTPError) as err:
                self.post({}, route=removed_route)
            self.assertEqual(err.exception.code, 404)
            err.exception.close()
        # Standalone JSON workflow import is rejected; Workflow Package (.zip) import succeeds
        req_json = Request(
            self.url + '/workflow/import',
            data=b'{"1":{"class_type":"X","inputs":{}}}',
            headers={'Content-Type': 'application/octet-stream', 'X-Studio': self.g['TOKEN'], 'X-Filename': 'standalone.json'},
        )
        with self.assertRaises(HTTPError) as err:
            urlopen(req_json)
        self.assertEqual(err.exception.code, 400)
        err.exception.close()
        with self.assertRaises(HTTPError) as err:
            self.post({'name': 'legacy', 'graph': {}}, route='/workflow')
        self.assertEqual(err.exception.code, 400)
        err.exception.close()
        # Real Workflow Package (.zip) uploads and validates through /workflow/import
        pkg_bytes = (Path(__file__).resolve().parent / 'packages' / 'MiniMax_H3.zip').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            import workflow_profiles
            temp_store = workflow_profiles.WorkflowStore(Path(tmp))
            with patch.object(self.app.actions, 'store', temp_store), \
                 patch.multiple(self.app, active_workflow=None, workflow_error=''):
                req_zip = Request(
                    self.url + '/workflow/import',
                    data=pkg_bytes,
                    headers={'Content-Type': 'application/octet-stream', 'X-Studio': self.g['TOKEN'], 'X-Filename': 'MiniMax_H3.zip'},
                )
                with urlopen(req_zip) as response:
                    imported = json.load(response)
                self.assertTrue(imported['profile']['packaged'])
                self.assertEqual(imported['profile']['manifest']['id'], 'minimax-h3-singularity')
                self.assertEqual(imported['active'], imported['profile']['version'])

    def test_files_snapshot_is_isolated_from_in_place_updates(self):
        snap = self.g['files_snapshot']()
        self.assertIsNot(snap, self.g['files'])

    def test_auto_resume_session_has_no_startup_modal_and_defaults_to_resume(self):
        with urlopen(self.url + '/connection-ui.js') as response:
            conn_js = response.read().decode()
        self.assertNotIn("id='workspaceSession'", conn_js)
        self.assertIn("post('/workspace-session',{mode:'resume'})", conn_js)
        self.assertIn('connect-ssh', conn_js)
        self.assertIn('Discover instances', conn_js)
        with urlopen(self.url) as response:
            html = response.read().decode()
        self.assertIn('await resumeWorkspaceSession()', html)
        connection = Mock()
        direct = Mock(interrupted=False)
        with patch.multiple(self.app, connection=connection, direct_session=direct, workflow_manager=Mock(),
                            workspace_started=threading.Event(), session={'episode': 'kept-ep'}):
            self.assertEqual(self.post({}, route='/workspace-session')['episode'], 'kept-ep')
        direct.archive_for_new_session.assert_not_called()

    def test_story_clear_keeps_characters_and_existing_takes(self):
        import story_engine
        from studio_actions import StudioActions
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / 'episodes'
            data.mkdir()
            db_path = data / 'project.json'
            ep = story_engine.new_episode('Ep 1')
            scene = story_engine.new_scene('Scene 1')
            shot = {'id': '22222222-2222-4222-8222-222222222222', 'revision': 'r2', 'scene_id': scene['id'], 'action': 'B', 'prompt': 'B', 'takes': []}
            ep.update(story='Original story', scenes=[scene], shots=[shot], story_imports=[{'original': 'Original story'}])
            db = story_engine.new_project()
            db['episodes'] = [ep]
            db['characters'] = [{'id': 'c1', 'name': 'Hero', 'aliases': [], 'images': [], 'voices': []}]
            db['_edit'] = 'e1'
            db_path.write_text(json.dumps(db), 'utf-8')
            take_dir = data / ep['id'] / shot['id'] / '1'
            take_dir.mkdir(parents=True)
            (take_dir / 'job.json').write_text(json.dumps({'shot': shot['id'], 'status': 'done', 'revision': 'r2'}), 'utf-8')
            with patch.multiple(self.app, DATA=data, DB=db_path, running=False):
                actions = StudioActions(self.app)
                with patch.object(self.app, 'actions', actions):
                    res = self.post({'episode': ep['id'], 'edit': 'e1'}, route='/story/clear')
                    cleared_ep = res['project']['episodes'][0]
                    self.assertEqual(cleared_ep['story'], '')
                    self.assertEqual(cleared_ep['scenes'], [])
                    self.assertEqual(cleared_ep['shots'], [])
                    self.assertEqual(cleared_ep['story_imports'], [])
                    self.assertEqual(len(res['project']['characters']), 1)
                    self.assertTrue((take_dir / 'job.json').is_file())

    def test_workflow_reset_install_clears_state_and_keeps_script_and_blocks_when_busy(self):
        from workflow_manager import WorkflowManager
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            data = base / 'episodes'
            data.mkdir()
            version = 'a' * 64
            conn = Mock()
            conn.target.return_value = {'id': 99, 'host': 'h', 'port': 22}
            conn.ssh_target.return_value = {'id': 99, 'host': 'h', 'port': 22}
            conn.snapshot.return_value = {'instance': 99}
            ssh_ops = Mock()
            ctx = {'BASE': base, 'DATA': data, 'LOCK': threading.RLock(), 'connection': conn}
            mgr = WorkflowManager(ctx, ssh_ops=ssh_ops)
            mgr.save_profile(version, script='echo keep', instances={'99': {'status': 'ready', 'report': {'models': []}}})
            mgr.records['99'] = {'id': 'b' * 32, 'version': version, 'instance': '99', 'status': 'running', 'started': 1.0, 'mode': 'install'}
            with self.assertRaises(ValueError):
                mgr.action({'action': 'reset-install', 'instance': '99', 'version': version})
            mgr.records['99']['status'] = 'failed'
            result = mgr.action({'action': 'reset-install', 'instance': '99', 'version': version})
            self.assertTrue(result['ok'])
            self.assertTrue(result['remoteCleaned'])
            self.assertNotIn('99', mgr.records)
            prof = mgr.profile(version)
            self.assertEqual(prof.get('script'), 'echo keep')
            self.assertNotIn('99', prof.get('instances', {}))
            self.assertEqual(ssh_ops.ssh.call_count, 2)


if __name__ == '__main__':
    unittest.main()
