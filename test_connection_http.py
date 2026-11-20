"""Local HTTP acceptance with mocked external services; no Vast requests."""
import json
import runpy
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import Mock,patch

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx=runpy.run_path('studio.pyw')
        cls.g=cls.ctx['Handler'].do_POST.__globals__
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),cls.ctx['Handler'])
        cls.g['PORT']=cls.server.server_port
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def post(self,obj,token=True):
        request=Request(self.url+'/connection',data=json.dumps(obj).encode() if token else b'',headers={'Content-Type':'application/json','X-Studio':self.g['TOKEN'] if token else 'invalid'})
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
    def test_unknown_action_rejected(self):
        with self.assertRaises(HTTPError) as error:self.post({'action':'unexpected'})
        self.assertEqual(error.exception.code,400);error.exception.close()
    def test_panel_and_static_script_available(self):
        with urlopen(self.url) as response:html=response.read().decode()
        self.assertIn('/connection-ui.js',html)
        with urlopen(self.url+'/connection-ui.js') as response:script=response.read().decode()
        self.assertIn('Terminal &amp; HF Token',script)
        self.assertIn('type="password"',script)
    def test_status_contains_shared_state(self):
        with urlopen(self.url+'/status') as response:status=json.load(response)
        self.assertIn('connection',status);self.assertEqual(status['running'],False)

if __name__=='__main__':unittest.main()
