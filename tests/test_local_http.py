import json,threading,unittest,urllib.request,urllib.error
from local_http import LocalHandler,LocalServer
class HostBoundary(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  class Handler(LocalHandler):
   def route_get(self,path):
    if path=='/api/state':self.send_json({'ok':True});return True
    return False
   def route_post(self,path,body):return {'received':body['value']}
  cls.server=LocalServer(('127.0.0.1',0),Handler);cls.server.session_token='unit-test-session'
  cls.port=cls.server.server_port;cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
  cls.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
 def request(self,path,headers=None,body=None):
  req=urllib.request.Request('http://127.0.0.1:'+str(self.port)+path,headers=headers or {},data=body)
  try:
   with self.opener.open(req) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 def test_bootstrap_and_token(self):
  self.assertEqual(self.request('/api/bootstrap')[1]['token'],'unit-test-session')
  self.assertEqual(self.request('/api/state')[0],401)
  self.assertEqual(self.request('/api/state',{'X-AIRI-Token':'unit-test-session'})[1],{'ok':True})
 def test_origin_host_and_fetch_site(self):
  for headers in ({'Origin':'https://example.com'},{'Host':'example.com'},{'Sec-Fetch-Site':'cross-site'}):
   self.assertEqual(self.request('/api/bootstrap',headers)[0],403)
 def test_post_checks_type_and_size(self):
  headers={'X-AIRI-Token':'unit-test-session','Content-Type':'application/json'}
  self.assertEqual(self.request('/api/action',headers,b'{"value":42}'),(200,{'received':42}))
  self.assertEqual(self.request('/api/action',headers,b'[]')[0],400)
  self.assertEqual(self.request('/api/action',{'X-AIRI-Token':'unit-test-session'},b'text')[0],415)
