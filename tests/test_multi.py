import tempfile,unittest,concurrent.futures
from pathlib import Path
from unittest.mock import patch
from stock101.crypto.multi import MultiEngine,WEEK
from test_crypto import Fake,product,book,bars
class CAD(Fake):
 def get_products(self):return [{**product(),'product_id':'TEST-CAD','quote_currency_id':'CAD'}]
 def get_product(self,p):return self.get_products()[0]
 def get_orderbook(self,p):return {**book(),'product_id':p}
class MultiTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'cad.db';self.e=MultiEngine(self.path,CAD());self.anchor=self.e.plan_state()['plan']['anchor']
 def tearDown(self):self.tmp.cleanup()
 def test_initial_is_1000_cad(self):
  self.assertEqual(self.e.plan_state()['account']['cash'],'1000');self.assertFalse(self.e.plan_state()['running'])
 def test_week_boundary_and_idempotency(self):
  self.e.contribute(self.anchor+WEEK-1);self.assertEqual(self.e.plan_state()['account']['cash'],'1000')
  for i in range(3):self.e.contribute(self.anchor+WEEK)
  self.assertEqual(self.e.plan_state()['account']['cash'],'1000')
 def test_catchup_and_concurrency(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.e.contribute(self.anchor+3*WEEK),range(4)))
  self.assertEqual(self.e.plan_state()['account']['cash'],'1000')
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_restart_no_duplicate_or_auto_resume(self):
  self.e.contribute(self.anchor+WEEK);self.e.start_multi();e=MultiEngine(self.path,CAD())
  self.assertEqual(e.plan_state()['account']['cash'],'1000');self.assertFalse(e.plan_state()['running'])
 def test_foreign_currency_never_funded(self):
  with self.e.repo.db() as c:
   with self.assertRaisesRegex(ValueError,'FX_COST'):self.e.initialize_currency(c,'USD')
 def test_unverified_fx_waits(self):
  self.e.adapter=Fake();self.e.start_multi();self.assertEqual(self.e.cycle()['status'],'NO_ELIGIBLE_PRODUCTS')
  self.assertEqual(self.e.plan_state()['account']['cash'],'1000')
 def test_strategy_buy_not_profit_evidence(self):
  self.e.start_multi();r=self.e.decide('TEST-CAD')
  self.assertIn('NET_EDGE_TOO_SMALL',r['analysis']['reason_codes']);self.assertIsNone(r['execution'])
  self.assertEqual(self.e.plan_state()['account']['cash'],'1000')
 def test_stop_prevents_scan(self):
  self.e.start_multi();self.e.halt();self.assertEqual(self.e.cycle()['status'],'STOPPED')
 def test_weekly_funding_is_not_profit(self):
  self.e.portfolio();self.e.contribute(self.anchor+WEEK)
  p=self.e.portfolio()['portfolios'][0]
  self.assertEqual(p['equity'],'1000');self.assertEqual(p['today_pnl'],'0');self.assertEqual(p['drawdown'],'0')
 def test_live_cap_cannot_create_money(self):
  from stock101.crypto.budget_policy import live_cap
  self.assertEqual(live_cap('75','100',0,604800*4),75)
  self.assertEqual(live_cap('900','900',0,604800),900)
  self.assertEqual(live_cap('500','100',0,604800*4),100)
 def test_live_policy_does_not_enable_orders(self):
  from stock101.crypto.budget_policy import description
  d=description();self.assertFalse(d['live_execution_enabled']);self.assertFalse(d['real_auto_deposit_enabled'])
