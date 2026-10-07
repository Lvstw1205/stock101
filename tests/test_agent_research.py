import copy,datetime as dt,json,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from stock101.crypto import agent_bridge as bridge
from stock101.crypto.agent_research import Manager,DEFAULT,atomic,read,validate_config,tickers,valid_date

class ManagerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.m=Manager(SimpleNamespace(ROOT=self.root,DATA=self.root/'data'));self.m.python=Path(__file__)
  self.m.ensure_thread=lambda:None
 def test_tickers_and_dates_reject_paths_and_future(self):
  self.assertEqual(tickers('btc-usd, SHOP.TO,btc-usd'),['BTC-USD','SHOP.TO'])
  for v in ('../../x','BTC/../../x','<script>'):
   with self.assertRaises(ValueError):tickers(v)
  with self.assertRaises(ValueError):valid_date('2999-01-01')
 def test_provider_change_clears_previous_endpoint(self):
  self.assertEqual(validate_config({'provider':'openai'},DEFAULT)['backend_url'],'')
 def test_invalid_config_rejected_without_changing_saved_settings(self):
  self.m.save_config({'auto_minutes':30})
  for change in ({'auto_minutes':0},{'auto_enabled':'true'},{'analysts':[]},{'backend_url':'https://key:secret@example.com'},{'max_tokens':-10}):
   with self.assertRaises(ValueError):self.m.save_config(change)
  self.assertEqual(self.m.config()['auto_minutes'],30)
 def test_keys_private_and_never_echoed(self):
  r=self.m.save_keys({'FRED_API_KEY':'test-not-a-real-key'})
  self.assertNotIn('test-not-a-real-key',json.dumps(r));self.assertEqual((self.m.root/'credentials.json').stat().st_mode&0o777,0o600)
  self.assertNotIn('test-not-a-real-key',json.dumps(self.m.state()))
 def test_durable_queue_cancel_and_resume(self):
  r=self.m.start({'kind':'analysis','asset_type':'stock','ticker':'SHOP.TO','mode':'research'},None)
  jid=r['job']['id'];self.assertEqual(self.m.detail(jid)['job']['status'],'QUEUED')
  self.m.cancel(jid);self.assertEqual(self.m.detail(jid)['job']['status'],'CANCELLED')
  self.m.resume(jid);self.assertEqual(self.m.detail(jid)['job']['status'],'QUEUED')
  reopened=Manager(SimpleNamespace(ROOT=self.root,DATA=self.root/'data'));self.assertEqual(reopened.jobs()[0]['id'],jid)
 def test_historical_analysis_never_captures_current_account(self):
  r=self.m.start({'ticker':'BTC-USD','date':'2020-01-01'},None)
  req=read(self.m.job_path(r['job']['id'])/'request.json');self.assertEqual(req['snapshots'],{});self.assertIsNone(req['portfolio'])
 def test_backtest_grid_bounded_and_resumable_when_partial(self):
  with self.assertRaisesRegex(ValueError,'GRID_TOO_LARGE'):self.m.start({'kind':'backtest','start':'2020-01-01','end':'2025-01-01','every':1},None)
  r=self.m.start({'kind':'backtest','tickers':'BTC-USD,ETH-USD','start':'2020-01-01','end':'2020-01-15','every':7},None)
  self.assertEqual(r['job']['total'],6);j=self.m.job_path(r['job']['id']);s=read(j/'status.json');s['status']='PARTIAL';atomic(j/'status.json',s);self.m.resume(s['id']);self.assertEqual(read(j/'status.json')['status'],'QUEUED')
 def test_expired_orphan_does_not_block_next_jobs(self):
  jid='a'*32;atomic(self.m.job_path(jid)/'status.json',dict(id=jid,created_at=1,updated_at=1,status='RUNNING'))
  self.m.tick();self.assertEqual(self.m.jobs()[0]['status'],'INTERRUPTED')
 def test_job_path_never_escapes_root(self):
  with self.assertRaises(ValueError):self.m.detail('../credentials')

class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.now=time.time()
  atomic(self.root/'tradingagents/settings.json',{'apply_to_execution':True})
  self.row={'ticker':'BTC-USD','date':dt.date.today().isoformat(),'created_at':self.now,'reference_at':self.now,'reference_price':100,'rating':'Sell','asset_type':'crypto','mode':'paper','job_id':'a'*32}
  self.book={'product_id':'BTC-USD','mid':'100'};self.position={'quantity':'1','cost':'90'}
  self.signal={'decision':'BUY','reason_codes':['STRATEGY_BUY'],'plan':{'profit_management':{'net_profit_cad':'9'}}}
 def apply(self,signal=None,mode='paper'):
  atomic(self.root/'tradingagents/opinions'/mode/'BTC-USD.json',self.row)
  return bridge.apply(signal or self.signal,self.position,self.book,self.root,mode,self.now)
 def test_sell_opinion_allows_exit_only_with_positive_net(self):
  r=self.apply();self.assertEqual(r['decision'],'SELL');self.assertEqual(r['sell_fraction'],1)
  self.signal['plan']['profit_management']['net_profit_cad']='-1';r=self.apply();self.assertEqual(r['decision'],'HOLD')
 def test_underweight_reduces_once(self):
  self.row['rating']='Underweight';r=self.apply();self.assertEqual(r['sell_fraction'],.5)
  self.position['profit_taken']=True;r=self.apply();self.assertNotEqual(r['decision'],'SELL')
 def test_existing_protective_exit_cannot_be_overruled(self):
  self.row['rating']='Buy';r=self.apply({'decision':'SELL','reason_codes':['STOP_LOSS'],'plan':{}})
  self.assertEqual(r['reason_codes'],['STOP_LOSS'])
 def test_ai_buy_does_not_create_unqualified_entry(self):
  self.row['rating']='Buy';r=self.apply({'decision':'WAIT','reason_codes':['COST_CHECK'],'plan':{}});self.assertEqual(r['decision'],'WAIT')
 def test_stale_date_or_quote_and_changed_price_cannot_apply(self):
  for change in ({'date':'2020-01-01'},{'created_at':self.now-3601},{'reference_at':self.now-3601},{'reference_price':200},{'mode':'live'},{'rating':'REVIEW'}):
   original=copy.deepcopy(self.row);self.row.update(change);r=self.apply();self.assertEqual(r['decision'],'BUY');self.assertFalse(r['plan']['ai_team']['applied']);self.row=original
 def test_malformed_cache_never_halts_execution(self):
  for change in ({'created_at':'bad'},{'reference_at':[]},{'reference_price':0}):
   self.row.update(change);self.assertEqual(self.apply()['decision'],'BUY')
  atomic(self.root/'tradingagents/opinions/paper/BTC-USD.json',[])
  self.assertEqual(bridge.apply(self.signal,self.position,self.book,self.root,now=self.now),self.signal)
 def test_paper_opinion_cannot_cross_to_live(self):
  self.apply();r=bridge.apply(self.signal,self.position,self.book,self.root,'live',self.now);self.assertEqual(r,self.signal)
 def test_bridge_preserves_original_signal(self):
  original=copy.deepcopy(self.signal);self.apply();self.assertEqual(self.signal,original)
 def test_disabled_opinion_cannot_affect_orders(self):
  atomic(self.root/'tradingagents/settings.json',{'apply_to_execution':False});self.assertEqual(self.apply(),self.signal)
