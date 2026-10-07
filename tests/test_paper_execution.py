import datetime as dt,tempfile,time,unittest,copy
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from stock101.crypto.multi import MultiEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto import net_edge
from stock101.crypto.exchange import ExchangeError
from test_crypto import product,book,bars
class Markets:
 def __init__(self):self.factor=1.;self.hour=0
 def get_products(self):return [self.get_product(p) for p in ['AAA-USD','BBB-USD']]
 def get_product(self,p):return {**product(),'product_id':p,'base_currency_id':p.split('-')[0],'quote_volume_24h':'1000000'}
 def get_orderbook(self,p):
  b=book();b.update(product_id=p,mid=str(134.05*self.factor))
  for side in ['asks','bids']:
   b[side]=[{**x,'price':str(float(x['price'])*self.factor)} for x in b[side]]
  return b
 def get_candles(self,p):
  rows=bars()
  for i,r in enumerate(rows):
   price=134.05*(1.01**(i-99))*self.factor
   r.update(open=price,close=price,high=price*1.005,low=price*.995,start=r['start']+self.hour*3600)
  return rows
class PaperExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.source=Markets()
  self.adapter=CadPaperAdapter(self.source,lambda:{'rate':'1.37','date':dt.datetime.now(dt.timezone.utc).date().isoformat(),'source':'synthetic test'})
  self.e=MultiEngine(Path(self.tmp.name)/'paper.db',self.adapter)
 def tearDown(self):self.tmp.cleanup()
 def test_multiple_assets_one_budget_and_round_trip(self):
  self.e.start_multi()
  with self.e.repo.db() as c:self.e.repo.set(c,'multi_after',0)
  self.e.cycle();state=self.e.plan_state()
  self.assertEqual(state['trade_count'],2);self.assertLess(Decimal(state['account']['cash']),1000)
  self.assertGreaterEqual(Decimal(state['account']['cash']),Decimal('950'))
  with self.e.repo.db() as c:
   self.e.reconcile_ledger(c);fills=self.e.repo.rows(c,'paper_orders');self.assertEqual({x['product'] for x in fills},{'AAA-USD','BBB-USD'})
   self.assertTrue(all(x['currency']=='CAD' and Decimal(x['fee'])>0 for x in fills))
  self.e.cycle();self.assertEqual(self.e.plan_state()['trade_count'],2)
  self.source.hour=1;self.source.factor=1.10;self.e.cycle()
  with self.e.repo.db() as c:
   self.e.reconcile_ledger(c);sold=[x for x in self.e.repo.rows(c,'paper_orders') if x['side']=='SELL']
   self.assertTrue(sold);self.assertTrue(all(Decimal(x['realized'])>0 for x in sold))
 def test_fx_markup_is_in_both_sides_and_not_at_par(self):
  b=self.adapter.get_orderbook('AAA-USD')
  self.assertGreater(Decimal(b['asks'][0]['price']),Decimal('134.1')*Decimal('1.37'))
  self.assertLess(Decimal(b['bids'][0]['price']),Decimal('134')*Decimal('1.37'))
 def test_stale_fx_fails_closed(self):
  a=CadPaperAdapter(self.source,lambda:{'rate':'1.37','date':'2000-01-01'})
  with self.assertRaises(ExchangeError):a.get_products()
 def test_net_gate_rejects_flat_and_high_cost(self):
  b=self.adapter.get_orderbook('AAA-USD');r=self.adapter.get_candles('AAA-USD')
  self.assertTrue(net_edge.estimate(r,b)['passed'])
  flat=[{**x,'close':100} for x in r];self.assertFalse(net_edge.estimate(flat,b)['passed'])
  expensive=copy.deepcopy(b);expensive['asks'][0]['price']=str(Decimal(expensive['asks'][0]['price'])*2)
  self.assertFalse(net_edge.estimate(r,expensive)['passed'])
 def test_no_live_order_path(self):
  from stock101.crypto.exchange import CoinbaseAdapter
  with patch.object(CoinbaseAdapter,'place_order',side_effect=AssertionError('LIVE CALLED')):
   self.e.start_multi()
   with self.e.repo.db() as c:self.e.repo.set(c,'multi_after',0)
   self.e.cycle();self.assertEqual(self.e.plan_state()['trade_count'],2)
 def test_deep_book_cost_can_cancel_buy_before_ledger_change(self):
  self.e.start_multi()
  b=self.adapter.get_orderbook('AAA-USD');r=self.adapter.get_candles('AAA-USD');edge=net_edge.estimate(r,b)
  with self.assertRaisesRegex(ValueError,'NET_EDGE_LOST_TO_BOOK_DEPTH'):
   self.e.validate_execution('BUY',Decimal('1'),Decimal('1000'),Decimal('6'),Decimal('0'),Decimal('0'),{'net_edge':edge})
  self.assertEqual(self.e.plan_state()['trade_count'],0)
 def test_unprofitable_normal_sale_rejected_but_protection_allowed(self):
  args=('SELL',Decimal('1'),Decimal('99'),Decimal('1'),Decimal('100'),Decimal('1'))
  with self.assertRaisesRegex(ValueError,'NET_EXIT_PROFIT_TOO_SMALL'):self.e.validate_execution(*args,{'reason_codes':['NET_PROFIT_TAKE']})
  self.e.validate_execution(*args,{'reason_codes':['TRAILING_PROTECTION']})
 def test_service_disarm_stops_multi_loop(self):
  from stock101.crypto import service
  self.e.start_multi()
  with patch.object(service,'engine',return_value=self.e):service.action(None,{'action':'crypto_disarm'})
  self.assertFalse(self.e.plan_state()['running']);self.assertEqual(self.e.cycle()['status'],'STOPPED')
  self.assertEqual(self.e.plan_state()['trade_count'],0)
 def test_failure_stops_and_restart_reconciles_without_duplicates(self):
  self.e.start_multi();self.e.cycle();self.assertEqual(self.e.plan_state()['trade_count'],2)
  with patch.object(self.adapter,'get_orderbook',side_effect=ExchangeError('NETWORK_UNAVAILABLE')):
   with self.assertRaises(Exception):self.e.cycle()
  self.assertFalse(self.e.plan_state()['running'])
  restarted=MultiEngine(self.e.repo.path,self.adapter)
  self.assertFalse(restarted.plan_state()['running'])
  restarted.start_multi();restarted.cycle()
  self.assertEqual(restarted.plan_state()['trade_count'],2)
  with restarted.repo.db() as c:restarted.reconcile_ledger(c)
 def test_partial_fill_and_total_funding_bound(self):
  original=self.source.get_orderbook
  def thin(pid):
   b=original(pid);b['asks'][0]['size']='0.008';return b
  with patch.object(self.source,'get_orderbook',side_effect=thin):
   self.e.start_multi();self.e.cycle()
  with self.e.repo.db() as c:
   fills=self.e.repo.rows(c,'paper_orders');self.e.reconcile_ledger(c)
   spent=sum((Decimal(x['notional'])+Decimal(x['fee']) for x in fills),Decimal(0))
   self.assertTrue(fills);self.assertLessEqual(spent,Decimal('100'));self.assertTrue(all(x['status']=='SIMULATED_PARTIAL_IOC' for x in fills))
