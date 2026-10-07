"""Regression tests for an unavailable asset stopping the whole PAPER worker."""
import json,time,tempfile,unittest,sqlite3
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from test_intelligence import HistoricalMarket
from stock101.crypto.multi import MultiEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto.intelligence import Intelligence
from stock101.crypto.exchange import ExchangeError
from stock101.crypto import planner,service
from stock101.crypto.news import relevant
import datetime as dt

class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.market=HistoricalMarket()
  self.adapter=CadPaperAdapter(self.market,lambda:{'rate':'1.37','date':dt.datetime.now(dt.timezone.utc).date().isoformat()})
  self.e=MultiEngine(Path(self.tmp.name)/'paper.db',self.adapter)
  self.e.intelligence=Intelligence(self.adapter,self.e.repo)
  for p in self.adapter.get_products():
   pid=p['product_id'];bars=self.adapter.get_candles(pid)
   self.e.intelligence.products[pid]=p
   self.e.intelligence.history[pid]={'bars':bars,'forecast':planner.forecast(bars)}
  self.background=patch.object(self.e.intelligence,'set_products');self.background.start()
  self.e.start_multi()
 def tearDown(self):self.background.stop();self.tmp.cleanup()
 def test_transient_asset_failure_recovers_and_other_assets_continue(self):
  self.market.factor=.97
  original=self.adapter.get_orderbook
  def flaky(pid):
   if pid=='AAA-USD':raise ExchangeError('EXCHANGE_UNAVAILABLE')
   return original(pid)
  with patch.object(self.adapter,'get_orderbook',side_effect=flaky):
   service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  self.assertIsNone(self.e.latest['AAA-USD']['plan'])
  self.assertIn('decision_id',self.e.latest['BBB-USD'])
  # The minute retry delay elapses; the same missing-plan row is encountered.
  self.e._unavailable['AAA-USD']=time.time()-61;self.market.factor=1
  service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  self.assertIsNotNone(self.e.latest['AAA-USD']['plan'])
  self.assertNotIn('AAA-USD',self.e._unavailable)
  with self.e.repo.db() as c:
   self.e.reconcile_ledger(c)
   fills=self.e.repo.rows(c,'fills');events=self.e.repo.rows(c,'system_events')
   self.assertEqual({r['product'] for r in fills},{'AAA-USD','BBB-USD'})
   self.assertTrue(any(r['code']=='PAPER_MARKET_DATA_RECOVERED' for r in events if 'code' in r))
   count=len(fills)
  # Subsequent cycles cannot duplicate the entry, including across quote buckets.
  for _ in range(4):service._paper_cycle(self.e)
  self.assertEqual(self.e.plan_state()['trade_count'],count)
  self.assertGreaterEqual(Decimal(self.e.plan_state()['account']['cash']),96)
 def test_missing_plan_detail_is_not_an_internal_error(self):
  self.e.latest['MISSING-USD']={'plan':None}
  with self.assertRaisesRegex(ValueError,'PLAN_NOT_READY'):self.e.inspect_plan('MISSING-USD')
  self.e.latest['AAA-USD']={'plan':None}
  self.assertIn('forecast',self.e.inspect_plan('AAA-USD'))
 def test_global_network_failure_recovers_after_ledger_and_market_checks(self):
  self.e.fail('PRODUCT_DISCOVERY_FAILED',ExchangeError('EXCHANGE_UNAVAILABLE'))
  self.assertEqual(self.e.recovery['status'],'RECOVERING');self.e.recovery['next_retry_at']=0
  with patch.object(self.adapter,'get_products',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):service._paper_cycle(self.e)
  self.assertFalse(self.e.plan_state()['running']);self.assertGreater(self.e.recovery['next_retry_at'],time.time())
  self.e.recovery['next_retry_at']=0;service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running']);self.assertEqual(self.e.recovery['status'],'MONITORING')
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_restart_resumes_only_previously_requested_paper_after_checks(self):
  e=MultiEngine(self.e.repo.path,self.adapter);self.assertFalse(e.plan_state()['running'])
  e.recovery['next_retry_at']=0;e.recover_if_due();self.assertTrue(e.plan_state()['running'])
  e.halt();stopped=MultiEngine(e.repo.path,self.adapter);stopped.recover_if_due()
  self.assertFalse(stopped.plan_state()['running']);self.assertFalse(stopped._resume_requested)
 def test_pending_quotes_retry_before_expiry_and_reuse_product_discovery(self):
  self.e.fail('PRODUCT_DISCOVERY_FAILED');self.e.recovery['next_retry_at']=0
  with patch.object(self.e,'books_for',side_effect=ExchangeError('MARKET_DATA_REFRESH_PENDING')):
   self.e.recover_if_due()
  self.assertFalse(self.e.plan_state()['running'])
  self.assertLessEqual(self.e.recovery['next_retry_at']-time.time(),2)
  self.e.recovery['next_retry_at']=0
  with patch.object(self.adapter,'get_products',side_effect=AssertionError('should reuse known products')):
   self.e.recover_if_due()
  self.assertTrue(self.e.plan_state()['running'])
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_recovery_never_rewrites_bad_ledger_or_ignores_live_guard(self):
  self.e.fail('EXECUTION_OR_DATABASE_FAILURE');self.e.recovery['next_retry_at']=0
  with self.e.repo.db() as c:c.execute("UPDATE paper_accounts SET cash='75' WHERE currency='CAD'")
  self.e.recover_if_due();self.assertFalse(self.e.plan_state()['running'])
  with self.e.repo.db() as c:
   self.assertEqual(c.execute("SELECT cash FROM paper_accounts WHERE currency='CAD'").fetchone()[0],'75')
   c.execute("UPDATE paper_accounts SET cash='100' WHERE currency='CAD'")
  self.e.recovery['next_retry_at']=0
  def live_on():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
  self.e.recover_if_due(live_on);self.assertFalse(self.e.plan_state()['running'])
 def test_user_stop_during_recovery_cancels_resume(self):
  self.e.fail('PRODUCT_DISCOVERY_FAILED');self.e.recovery['next_retry_at']=0
  self.e.recover_if_due(self.e.halt)
  self.assertFalse(self.e.plan_state()['running']);self.assertFalse(self.e._resume_requested)
 def test_user_requested_paper_observation_recovers_legacy_loss_halts(self):
  for reason in ('LOSS_LIMIT','CONSECUTIVE_LOSSES'):
   self.e.fail(reason);self.e.recovery['next_retry_at']=0;service._paper_cycle(self.e)
   self.assertTrue(self.e.plan_state()['running'])
   self.assertEqual(self.e.plan_state()['activity']['loss_observation']['mode'],'CONTINUOUS_PAPER_OBSERVATION')
   with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_risk_calculation_failure_cannot_resume_even_after_restart(self):
  self.e.fail('RISK_CALCULATION_FAILED');self.e.recovery['next_retry_at']=0;service._paper_cycle(self.e)
  self.assertFalse(self.e.plan_state()['running']);self.assertEqual(self.e.recovery['status'],'ACTION_REQUIRED')
  restarted=MultiEngine(self.e.repo.path,self.adapter);restarted.recovery['next_retry_at']=0
  restarted.recover_if_due()
  self.assertFalse(restarted.plan_state()['running'])
  self.assertEqual(restarted.plan_state()['activity']['halt_reason'],'RISK_CALCULATION_FAILED')
 def test_database_outage_does_not_kill_supervisor_or_fabricate_repair(self):
  with patch.object(self.e.repo,'db',side_effect=sqlite3.OperationalError('database locked')):
   self.assertEqual(service._paper_cycle(self.e)['status'],'HALTED')
  self.assertTrue(self.e._fatal);self.assertEqual(self.e.recovery['status'],'RECOVERING')
  self.e.recovery['next_retry_at']=0;service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_quote_expiring_during_portfolio_read_blocks_only_this_attempt(self):
  original=self.e.books_for
  def delayed(currency,initial=None):
   books=original(currency,initial)
   if 'AAA-USD' in books:books['AAA-USD']['timestamp']=time.time()-16
   return books
  with patch.object(self.e,'books_for',side_effect=delayed):service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  self.assertEqual(self.e.latest['AAA-USD']['reason'],'STALE_MARKET_DATA')
  self.assertIn('AAA-USD',self.e._unavailable)
  with self.e.repo.db() as c:
   self.assertNotIn('AAA-USD',{f['product'] for f in self.e.repo.rows(c,'fills')})
   self.assertIn('BBB-USD',{f['product'] for f in self.e.repo.rows(c,'fills')})
   self.e.reconcile_ledger(c)
  self.e._unavailable['AAA-USD']=time.time()-61;service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  with self.e.repo.db() as c:self.assertIn('AAA-USD',{f['product'] for f in self.e.repo.rows(c,'fills')})
 def test_stale_held_valuation_rolls_back_and_fresh_quote_recovers(self):
  self.e.decide('AAA-USD',planner.VERSION);count=self.e.plan_state()['trade_count']
  original=self.adapter.get_orderbook
  with patch.object(self.adapter,'get_orderbook',side_effect=lambda p:{**original(p),'timestamp':time.time()-16}):
   r=self.e.decide('AAA-USD',planner.VERSION)
  self.assertEqual(r['status'],'MARKET_DATA_UNAVAILABLE');self.assertIsNone(r['execution'])
  self.assertTrue(self.e.plan_state()['running']);self.assertEqual(self.e.plan_state()['trade_count'],count)
  self.market.factor=1.1;r=self.e.decide('AAA-USD',planner.VERSION)
  self.assertEqual(r['execution']['side'],'SELL')
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_unexpected_failure_is_not_hidden_as_a_quote_retry(self):
  with patch.object(self.adapter,'get_orderbook',side_effect=TypeError('secret should never appear')):
   service._paper_cycle(self.e)
  self.assertFalse(self.e.plan_state()['running'])
  with self.e.repo.db() as c:
   events=self.e.repo.rows(c,'system_events')
   failure=next(r for r in events if r.get('code')=='AUTOMATION_CYCLE_FAILED')
  self.assertEqual(failure['diagnostic']['type'],'TypeError')
  self.assertNotIn('secret should never appear',json.dumps(events))
  self.assertEqual(self.e.plan_state()['trade_count'],0)
 def test_original_execution_failure_survives_daemon_wrapper(self):
  with patch.object(self.e,'evaluate_signal',side_effect=AttributeError('sensitive detail')):
   service._paper_cycle(self.e)
  state=self.e.plan_state();self.assertFalse(state['running'])
  self.assertEqual(state['activity']['halt_reason'],'EXECUTION_OR_DATABASE_FAILURE')
  failures=[r for r in state['activity']['events'] if r['category']=='risk']
  self.assertEqual(len(failures),1)
  self.assertEqual(failures[0]['diagnostic']['type'],'AttributeError')
  at=state['activity']['halt_at'];count=state['activity']['assessment_count']
  for _ in range(4):service._paper_cycle(self.e)
  after=self.e.plan_state()
  self.assertEqual(after['activity']['halt_at'],at)
  self.assertEqual(after['activity']['assessment_count'],count)
  self.assertFalse(after['running'])
 def test_stopped_worker_does_not_advance_assessment_or_cycle_clock(self):
  self.market.factor=.97;service._paper_cycle(self.e)
  before=self.e.plan_state();self.e.halt()
  for _ in range(4):service._paper_cycle(self.e)
  after=self.e.plan_state()
  for key in ('last_assessment_at','last_cycle_completed_at','assessment_count'):
   self.assertEqual(before['activity'][key],after['activity'][key])
  self.assertEqual(before['last_cycle_at'],after['last_cycle_at'])
  self.assertEqual(after['activity']['halt_reason'],'USER_KILL_SWITCH')
  self.e.start_multi();service._paper_cycle(self.e)
  self.assertTrue(self.e.plan_state()['running'])
  self.assertGreater(self.e.plan_state()['activity']['started_at'],after['activity']['halt_at'])
 def test_optional_coin_name_does_not_stop_news_assessment(self):
  self.assertTrue(relevant({'title':'Bitcoin adoption'},'BTC',None))
 def test_entry_cost_loss_is_held_until_target_or_invalidation(self):
  original=self.adapter.get_orderbook;stamp=time.time()
  with patch.object(self.adapter,'get_orderbook',side_effect=lambda p:{**original(p),'timestamp':stamp-6}):
   bought=self.e.decide('AAA-USD',planner.VERSION)
  self.assertEqual(bought['execution']['side'],'BUY')
  self.assertLess(Decimal(bought['portfolio_after']['equity']),Decimal('1000'))
  with patch.object(self.adapter,'get_orderbook',side_effect=lambda p:{**original(p),'timestamp':stamp}):
   held=self.e.decide('AAA-USD',planner.VERSION)
  self.assertEqual(held['analysis']['decision'],'HOLD')
  self.assertIsNone(held['execution'])
  self.assertEqual(held['analysis']['reason_codes'],['MANAGING_ENTRY_PLAN'])
  self.market.factor=1.10
  sold=self.e.decide('AAA-USD',planner.VERSION)
  self.assertEqual(sold['execution']['side'],'SELL')
  self.assertGreater(Decimal(sold['execution']['realized']),0)
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)

if __name__=='__main__':unittest.main()
