import json
import unittest
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric import ec,ed25519
from cryptography.hazmat.primitives import serialization
from stock101.crypto import credentials
from stock101.crypto.exchange import CoinbaseAdapter,ExchangeError

def keydata(curve=True):
 key=ec.generate_private_key(ec.SECP256R1()) if curve else ed25519.Ed25519PrivateKey.generate()
 return {'name':'organizations/test-organization/apiKeys/test-key','privateKey':key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()}
class ConnectionTests(unittest.TestCase):
 def adapter(self,permissions=None,accounts=None):
  a=CoinbaseAdapter();a._jwt=lambda path=None:'test-token'
  perms={'can_view':True,'can_trade':False,'can_transfer':False} if permissions is None else permissions
  a.transport=lambda path,params,headers: perms if path.endswith('key_permissions') else accounts or {'accounts':[]}
  return a
 def test_valid_ecdsa(self):
  d=keydata();self.assertEqual(credentials.validate(d)[0],d['name'])
 def test_pasted_single_line_pem(self):
  d=keydata();expected=d['privateKey'];d['privateKey']=expected.replace('\n',' ')
  self.assertEqual(credentials.validate(d)[1],expected)
 def test_pasted_escaped_newlines(self):
  d=keydata();expected=d['privateKey'];d['privateKey']=expected.replace('\n','\\n')
  self.assertEqual(credentials.validate(d)[1],expected)
 def test_reject_wrong_key_type(self):
  with self.assertRaisesRegex(credentials.CredentialError,'ECDSA_KEY_JSON_REQUIRED'):credentials.validate(keydata(False))
 def test_no_secret_in_error(self):
  with self.assertRaises(credentials.CredentialError) as ctx:credentials.validate({'name':'secret-identifier','privateKey':'do-not-print'})
  self.assertNotIn('secret',str(ctx.exception));self.assertNotIn('do-not-print',str(ctx.exception))
 def test_readonly_connection(self):
  result=self.adapter().check_readonly_connection()
  self.assertTrue(result['connected']);self.assertFalse(result['live_enabled']);self.assertNotIn('key',result)
 def test_reject_excess_permissions(self):
  for perms in ({'can_view':True,'can_trade':True,'can_transfer':False},{'can_view':True,'can_trade':False,'can_transfer':True},{'can_view':True},{'can_view':False,'can_trade':False,'can_transfer':False}):
   with self.assertRaisesRegex(ExchangeError,'VIEW_ONLY_KEY_REQUIRED'):self.adapter(perms).check_readonly_connection()
 def test_missing_key_does_not_create_credentials(self):
  with patch.dict('os.environ',{},clear=True),patch.object(credentials,'read',return_value=None),patch.object(credentials,'save') as save:
   with self.assertRaisesRegex(ExchangeError,'READ_ONLY_CREDENTIALS_NOT_CONFIGURED'):CoinbaseAdapter().get_accounts()
   save.assert_not_called()
 def test_keychain_signing_and_no_secret_in_result(self):
  d=keydata();seen=[]
  def transport(path,params,headers):seen.append(headers['Authorization']);return {'accounts':[]}
  with patch.dict('os.environ',{},clear=True),patch.object(credentials,'read',return_value=(d['name'],d['privateKey'])):
   result=CoinbaseAdapter(transport=transport).get_accounts()
  self.assertTrue(seen[0].startswith('Bearer '));self.assertNotIn(d['privateKey'],json.dumps(result))
 def test_real_balance_not_paper(self):
  accounts={'accounts':[{'uuid':'hidden-id','name':'hidden-name','currency':'USD','available_balance':{'currency':'USD','value':'12.34'},'hold':{'currency':'USD','value':'0'}}]}
  r=self.adapter(accounts=accounts).readonly_balances()
  self.assertEqual(r['balances'],[{'currency':'USD','available':'12.34','held':'0'}]);self.assertFalse(r['paper_funds_included']);self.assertNotIn('hidden',json.dumps(r))
 def test_invalid_balance_fail_closed(self):
  accounts={'accounts':[{'currency':'USD','available_balance':{'currency':'USD','value':'NaN'},'hold':{'currency':'USD','value':'0'}}]}
  with self.assertRaises(ExchangeError):self.adapter(accounts=accounts).readonly_balances()
 def test_private_balance_permission_checked_first(self):
  a=self.adapter({'can_view':True,'can_trade':True,'can_transfer':False});seen=[]
  original=a.transport
  a.transport=lambda path,params,headers:(seen.append(path) or original(path,params,headers))
  with self.assertRaises(ExchangeError):a.readonly_balances()
  self.assertEqual(seen,['/api/v3/brokerage/key_permissions'])
