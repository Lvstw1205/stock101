import concurrent.futures
import copy
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from stock101.crypto.exchange import CoinbaseAdapter, ExchangeError, StreamHealth
from stock101.crypto.engine import Engine
from stock101.crypto import strategy,risk,research

PID='TEST-USD'
def product():return {'product_id':PID,'base_currency_id':'TEST','quote_currency_id':'USD','product_type':'SPOT','status':'online','base_increment':'0.0001','quote_increment':'0.01','base_min_size':'0.0001','base_max_size':'1000000','quote_min_size':'1','quote_max_size':'1000000','tradable':True}
def bars():
 end=int(time.time())//3600*3600
 return [{'start':end-(100-i)*3600,'open':100*(1.003**i),'close':100*(1.003**i)*1.001,'high':100*(1.003**i)*1.005,'low':100*(1.003**i)*.995,'volume':100} for i in range(100)]
def book(size='1000'):
 return {'product_id':PID,'timestamp':time.time(),'received_at':time.time(),'bids':[{'price':'134','size':size}],'asks':[{'price':'134.1','size':size}],'mid':'134.05','spread_bps':'7.46'}
class Fake:
 def get_products(self):return [product()]
 def get_product(self,p):return product()
 def get_candles(self,p,*args):return bars()
 def get_orderbook(self,p):return book()
class Ledger(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'paper.db';self.e=Engine(self.path,Fake());self.e.release(PID)
 def tearDown(self):self.tmp.cleanup()
 def test_simulation_passes_full_chain(self):
  r=self.e.decide(PID);self.assertEqual(r['status'],'SIMULATED_FILLED');self.assertEqual(r['risk']['status'],'PASS')
  self.assertLessEqual(Decimal(r['execution']['notional']),Decimal('200'));self.assertTrue(r['decision_id'].startswith('ST101-'))
 def test_duplicate_and_restart(self):
  a=self.e.decide(PID);b=self.e.decide(PID);self.assertEqual(a['decision_id'],b['decision_id']);self.assertTrue(b['duplicate'])
  e=Engine(self.path,Fake());self.assertTrue(e.state()['halted']);self.assertIsNone(e.state()['armed']);e.release(PID)
  self.assertEqual(e.decide(PID)['decision_id'],a['decision_id'])
 def test_concurrent_double_click(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(lambda _:self.e.decide(PID),range(4)))
  self.assertEqual(len({r['decision_id'] for r in rows}),1)
  with self.e.repo.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],1)
 def test_kill_switch_blocks_but_keeps_monitoring(self):
  self.e.halt()
  with self.assertRaises(ValueError):self.e.decide(PID)
  self.assertIn('analysis',self.e.analyze(PID));self.assertTrue(self.e.state()['halted'])
 def test_arm_waits_for_new_bar(self):
  self.e.arm(PID,'balanced_v1',True);self.assertEqual(self.e.cycle()['status'],'WAITING_FOR_NEW_HOURLY_BAR')
 def test_no_arm_without_confirmation(self):
  with self.assertRaises(ValueError):self.e.arm(PID,'balanced_v1',False)
 def test_live_disabled(self):
  with self.assertRaises(ValueError):self.e.mode('LIVE')
  with self.assertRaises(ValueError):CoinbaseAdapter().place_order({})
  with self.assertRaises(ValueError):CoinbaseAdapter().cancel_order('x')
 def test_backtest_cannot_paper_fill(self):
  self.e.mode('BACKTEST')
  with self.assertRaises(ValueError):self.e.decide(PID)
 def test_insufficient_funds(self):
  with self.e.repo.db() as c:c.execute("UPDATE paper_accounts SET cash='0'")
  r=self.e.decide(PID);self.assertIn(r['risk']['status'],('BLOCK','HALT'));self.assertIsNone(r['execution'])
 def test_stale_book_fail_closed(self):
  with patch.object(self.e.adapter,'get_orderbook',return_value={**book(),'timestamp':time.time()-90}):r=self.e.decide(PID)
  self.assertEqual(r['risk']['status'],'HALT');self.assertIsNone(r['execution'])
 def test_partial_fill(self):
  with patch.object(self.e.adapter,'get_orderbook',return_value=book('.2')):r=self.e.decide(PID)
  self.assertEqual(r['status'],'SIMULATED_PARTIAL_IOC');self.assertEqual(Decimal(r['execution']['quantity']),Decimal('.2'))
 def test_rejected_market(self):
  with patch.object(self.e.adapter,'get_product',return_value={**product(),'tradable':False}):r=self.e.decide(PID)
  self.assertEqual(r['risk']['status'],'BLOCK');self.assertIsNone(r['execution'])
 def test_database_failure_rolls_back(self):
  original=self.e.repo.put
  def fail(c,table,*a,**kw):
   if table=='trade_decisions':raise RuntimeError('disk failure')
   return original(c,table,*a,**kw)
  with patch.object(self.e.repo,'put',side_effect=fail):
   with self.assertRaises(ValueError):self.e.decide(PID)
  with self.e.repo.db() as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT cash FROM paper_accounts').fetchone()[0],'10000')
  self.assertTrue(self.e.state()['halted'])
 def test_rest_timeout(self):
  with patch.object(self.e.adapter,'get_orderbook',side_effect=TimeoutError('secret should not appear')):
   with self.assertRaisesRegex(ValueError,'EXECUTION_HALTED'):self.e.decide(PID)
  self.assertTrue(self.e.state()['halted']);self.assertNotIn('secret',json.dumps(self.e.state()))
 def test_unknown_product(self):
  with self.assertRaises(ValueError):self.e.decide('HALLUCINATED-USD')
 def test_strategy_cannot_override_risk(self):
  s=strategy.evaluate(bars());s['allocation']=1
  with patch('stock101.crypto.strategy.evaluate',return_value=s):r=self.e.decide(PID)
  self.assertEqual(r['risk']['status'],'REDUCE');self.assertLessEqual(Decimal(r['execution']['notional']),Decimal('200'))
 def test_experiment_immutable(self):
  a=research.experiment(self.e,{'name':'candidate','rsi_entry':31});b=research.experiment(self.e,{'name':'candidate','rsi_entry':30})
  self.assertNotEqual(a['version'],b['version'])
  with self.e.repo.db() as c:self.assertEqual(self.e.repo.one(c,'strategy_versions',a['version'])['rsi_entry'],31)
class Adapter(unittest.TestCase):
 def test_market_discovery_not_hardcoded(self):
  a=CoinbaseAdapter(transport=lambda *args:{'products':[product()]});self.assertEqual(a.get_products()[0]['product_id'],PID)
 def test_invalid_response(self):
  for payload in ([],{'error':'bad'},None):
   with self.assertRaises(ExchangeError):CoinbaseAdapter(transport=lambda *args:payload).get_products()
 def test_nan_rejected(self):
  p=product();p['base_increment']='NaN'
  with self.assertRaises(ExchangeError):CoinbaseAdapter(transport=lambda *args:p).get_product(PID)
 def test_wrong_product_response(self):
  with self.assertRaises(ExchangeError):CoinbaseAdapter(transport=lambda *args:product()).get_product('OTHER-USD')
 def test_book_timestamp_and_inconsistency(self):
  for stamp,bid,ask in ((time.time()-60,'10','11'),(time.time(),'12','11')):
   data={'pricebook':{'product_id':PID,'time':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat(),'bids':[{'price':bid,'size':'1'}],'asks':[{'price':ask,'size':'1'}]}}
   with self.assertRaises(ExchangeError):CoinbaseAdapter(transport=lambda *args:data).get_orderbook(PID)
 def test_private_keys_not_frontend_or_errors(self):
  with patch.dict('os.environ',{'ST101_COINBASE_KEY_NAME':'secret-name','ST101_COINBASE_PRIVATE_KEY':'secret-value'}):
   with self.assertRaisesRegex(ExchangeError,'AUTH_CONFIGURATION_INVALID'):CoinbaseAdapter().get_accounts()
 def test_websocket_disconnect_and_gap(self):
  s=StreamHealth();self.assertTrue(s.observe({'channel':'ticker','sequence_num':0}));self.assertFalse(s.observe({'channel':'ticker','sequence_num':2}));self.assertFalse(s.connected)
  self.assertFalse(s.observe({'type':'error'}));self.assertFalse(s.disconnect())
class StrategyTests(unittest.TestCase):
 def test_decline_alone_not_buy(self):
  b=bars()
  for i in range(75,100):
   for k in ('open','high','low','close'):b[i][k]*=(1-(i-74)*.012)
  self.assertNotEqual(strategy.evaluate(b)['decision'],'BUY')
 def test_extreme_volatility_veto(self):
  b=bars()
  for i in range(70,100):
   for k in ('open','high','low','close'):b[i][k]*=(1.1 if i%2 else .9)
  self.assertEqual(strategy.evaluate(b)['decision'],'NO_TRADE')
 def test_malformed_llm_and_hallucinated_asset(self):
  for v in (None,'{bad',{'product':'FAKE-USD','decision':'BUY'}, {'product':PID,'decision':'EXECUTE'}):self.assertEqual(strategy.advisory(v,[PID])['decision'],'NO_TRADE')
  self.assertFalse(strategy.advisory({'product':PID,'decision':'BUY'},[PID])['executable'])
 def test_backtest_next_open_no_future_signal(self):
  b=bars();r=research.backtest(b,strategy.DEFAULT)
  self.assertGreater(r['trade_count'],0);self.assertGreaterEqual(r['trades'][0]['time'],b[60]['start'])
  cutoff=research.backtest(b[:80],strategy.DEFAULT)
  self.assertEqual(cutoff['equity'],r['equity'][:80])
 def test_profit_taking_is_partial(self):
  b=bars();p={'quantity':'1','cost':'100','peak':'134','profit_taken':False}
  r=strategy.evaluate(b,p);self.assertEqual(r['decision'],'REDUCE');self.assertLess(r['sell_fraction'],1)
 def test_low_history_unknown(self):self.assertEqual(strategy.evaluate(bars()[:10])['regime']['name'],'UNKNOWN')
class RiskTests(unittest.TestCase):
 def context(self):return {'equity':'10000','cash':'10000','invested':'0','asset_value':'0','halted':False,'age':0,'spread_bps':'5','daily_loss':'0','drawdown':'0','consecutive_losses':0,'since_loss':999999,'trades_hour':0,'deployed_today':'0'}
 def test_reduce(self):self.assertEqual(risk.assess('500','BUY',self.context())['approved_notional'],'200.00')
 def test_independent_gates(self):
  for field,value in [('halted',True),('age',30),('daily_loss','.04'),('drawdown','.12'),('consecutive_losses',3),('spread_bps','100'),('signal_deviation','.04'),('trades_hour',6),('since_loss',1),('deployed_today','1000'),('asset_value','1000'),('invested','5000')]:
   c={**self.context(),field:value};self.assertIn(risk.assess('100','BUY',c)['status'],('BLOCK','HALT'),field)
 def test_failed_calculation(self):self.assertEqual(risk.assess('100','BUY',{})['status'],'HALT')
 def test_nan(self):self.assertEqual(risk.assess('NaN','BUY',self.context())['status'],'HALT')
if __name__=='__main__':unittest.main()

class RecoveryAndSignals(unittest.TestCase):
 def test_corrupt_ledger_cannot_recover(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp)/'paper.db',Fake());e.release(PID);e.decide(PID)
   with e.repo.db() as c:c.execute("UPDATE paper_accounts SET cash='999999'")
   with self.assertRaises(ValueError):e.release(PID)
   self.assertTrue(e.state()['halted'])
 def test_strategy_disagreement_waits(self):
  b=bars()
  for i in range(100):
   for k in ('open','high','low','close'):b[i][k]*=1.003**i
  for i in range(93,100):
   for k in ('open','high','low','close'):b[i][k]=b[92][k]*(.997**(i-92))
  s=strategy.evaluate(b)
  self.assertTrue(s['conflict']);self.assertEqual(s['decision'],'WAIT')
 def test_llm_timeout_has_no_execution_path(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp)/'paper.db',Fake());e.release(PID)
   with patch('stock101.crypto.strategy.advisory',side_effect=TimeoutError('LLM unavailable')):
    r=e.decide(PID)
   self.assertEqual(r['risk']['status'],'PASS')
   self.assertFalse(r['airi_recommendation']['executable'])
 def test_exchange_maintenance_stops_automation(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp)/'paper.db',Fake());e.release(PID);e.arm(PID,'balanced_v1',True)
   with patch.object(e.adapter,'get_candles',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
    with self.assertRaises(ValueError):e.cycle()
   self.assertTrue(e.state()['halted']);self.assertIsNone(e.state()['armed'])
 def test_invalid_candle_rejected(self):
  sample=bars()[-1];sample['high']=1
  with self.assertRaises(ExchangeError):CoinbaseAdapter(transport=lambda *args:{'candles':[sample]}).get_candles(PID)
 def test_client_payload_does_not_define_side_or_limits(self):
  from stock101.crypto import service
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp)/'paper.db',Fake());e.release(PID)
   with patch.object(service,'engine',return_value=e),patch.object(service,'live_engine') as live:
    live.return_value.enabled.return_value=False;live.return_value.pending.return_value=[]
    r=service.action(None,{'action':'crypto_evaluate','product':PID,'side':'BUY','allocation':1,'risk':{'status':'PASS'},'limit':100000000})
   self.assertLessEqual(Decimal(r['execution']['notional']),Decimal('200'))

class WebSocketSequence(unittest.TestCase):
 def test_connection_sequence_includes_subscriptions(self):
  health=StreamHealth()
  for seq,channel in enumerate(('subscriptions','ticker','subscriptions','ticker','heartbeats')):
   self.assertTrue(health.observe({'channel':channel,'sequence_num':seq}))
  self.assertTrue(health.connected)

class SellReconciliation(unittest.TestCase):
 def test_full_exit_reconciles_decimal_cost(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Engine(Path(tmp)/'paper.db',Fake());e.release(PID)
   older=bars()
   for row in older:row['start']-=3600
   with patch.object(e.adapter,'get_candles',return_value=older):first=e.decide(PID)
   signal=strategy.evaluate(bars());signal.update(decision='SELL',sell_fraction=1,reason_codes=['TEST_EXIT'])
   with patch('stock101.crypto.strategy.evaluate',return_value=signal):second=e.decide(PID)
   self.assertIsNotNone(second['execution'])
   e.release(PID)
   with e.repo.db() as c:e.reconcile_ledger(c)
