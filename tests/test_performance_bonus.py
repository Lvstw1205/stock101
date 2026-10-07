import concurrent.futures,tempfile,time,unittest
from decimal import Decimal
from pathlib import Path
from stock101.crypto import performance_bonus as bonus
from stock101.crypto.budget_policy import migrate_paper,POLICY_VERSION
from stock101.crypto.multi import MultiEngine,WEEK
from stock101.crypto.live import LiveEngine
from test_multi import CAD
from test_live import Broker,FX,Markets

class BonusTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'paper.db'
  self.e=MultiEngine(self.path,CAD());self.now=time.time()
  with self.e.repo.db() as c:self.anchor=self.e.repo.get(c,'cad_plan')['anchor']
 def result(self):
  with self.e.repo.db() as c:return bonus.state(self.e.repo,c,'paper',self.now)
 def mark(self,assets='1000'):
  with self.e.repo.db() as c:
   c.execute('BEGIN IMMEDIATE');bonus.observe(self.e.repo,c,'paper',self.now,Decimal(assets))
 def realized(self,amount):
  with self.e.repo.db() as c:self.e.repo.put(c,'fills',str(time.time_ns()),dict(side='SELL',realized=amount))
 def account(self):return self.e.plan_state()['account']
 def test_boundary_is_inclusive_realized_after_costs(self):
  self.realized('199.999999');self.mark('9000');self.assertFalse(self.result()['earned'])
  self.realized('.000001');self.mark();s=self.result();self.assertTrue(s['earned']);self.assertTrue(s['active'])
  self.assertEqual(s['net_profit_cad'],'200.000000');self.assertEqual(s['principal_cad'],'1000')
  self.assertEqual(self.account()['cash'],'1020');self.assertEqual(s['next_weekly_cad'],'0')
 def test_unrealized_gains_and_deposits_never_qualify(self):
  self.mark('50000');self.assertFalse(self.result()['earned']);self.assertEqual(self.result()['net_profit_cad'],'0')
  self.assertEqual(self.result()['return_ratio'],'0');self.assertEqual(self.account()['initial'],'1000')
 def test_losses_offset_wins_and_bonus_never_compounds(self):
  self.realized('300');self.realized('-101');self.mark();self.assertFalse(self.result()['earned'])
  self.realized('1');self.mark();award=self.result()['award']
  self.realized('500');self.mark();self.realized('-800');self.mark()
  self.assertEqual(self.result()['award'],award);self.assertEqual(self.account()['cash'],'1020')
  with self.e.repo.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM system_events WHERE id=?',(bonus.KEY,)).fetchone()[0],1)
 def test_elapsed_weeks_never_create_money(self):
  for week in (1,5,100):self.e.contribute(self.anchor+week*WEEK)
  self.assertEqual(self.account()['cash'],'1000');self.assertIsNone(self.e.plan_state()['next_contribution'])
 def test_concurrent_reward_exactly_once(self):
  self.realized('200')
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.mark(),range(4)))
  self.assertEqual(self.account()['cash'],'1020')
 def test_restart_retains_reward_and_no_weekly_catchup(self):
  self.realized('200');self.mark();award=self.result()['award'];self.e=MultiEngine(self.path,CAD())
  self.e.contribute(self.anchor+50*WEEK)
  self.assertEqual(self.result()['award'],award);self.assertEqual(self.account()['cash'],'1020');self.assertFalse(self.e.plan_state()['running'])
 def test_reward_rolls_back_with_failed_fill_transaction(self):
  with self.assertRaises(RuntimeError),self.e.repo.db() as c:
   c.execute('BEGIN IMMEDIATE');self.e.repo.put(c,'fills','rollback',dict(side='SELL',realized='200'))
   bonus.observe(self.e.repo,c,'paper',self.now,Decimal('1200'));raise RuntimeError('rollback')
  self.assertFalse(self.result()['earned']);self.assertEqual(self.account()['cash'],'1000')
 def test_old_weekly_reward_cannot_activate_new_bonus(self):
  with self.e.repo.db() as c:self.e.repo.set(c,'performance_bonus_v1',{'effective_period':1})
  self.mark('2000');self.assertFalse(self.result()['earned']);self.assertEqual(self.account()['cash'],'1000')
 def test_fixed_cap_does_not_grow_over_time(self):
  for weeks in (0,1,500):
   self.assertEqual(bonus.cap(0,weeks*WEEK),1000)
   self.assertEqual(bonus.cap(0,weeks*WEEK,{'bonus_cad':'20'}),1020)
  self.assertEqual(bonus.additions(0,500,{'effective_period':1}),0)
 def test_missing_cost_records_cannot_qualify(self):
  with self.e.repo.db() as c:self.e.repo.put(c,'fills','unknown',dict(side='SELL'))
  self.mark();self.assertFalse(self.result()['verified']);self.assertFalse(self.result()['earned'])
 def test_migration_adds_only_shortfall_and_preserves_held_coins_and_fills(self):
  with self.e.repo.db() as c:
   c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM system_events WHERE id=?',(POLICY_VERSION,))
   self.e.repo.set(c,'cad_plan',{'anchor':self.anchor-30*WEEK,'initial':'100','weekly':'100','enabled':True})
   c.execute("UPDATE paper_accounts SET cash='89',initial='100' WHERE currency='CAD'")
   fill=dict(product='TEST-CAD',currency='CAD',side='BUY',quantity='1',notional='10',fee='1',realized='0')
   self.e.repo.put(c,'paper_orders','original',fill);self.e.repo.put(c,'fills','original',fill)
   self.e.repo.put(c,'positions','TEST-CAD',dict(product='TEST-CAD',currency='CAD',quantity='1',cost='11'))
   original=c.execute("SELECT payload FROM positions").fetchone()[0]
   migrate_paper(self.e.repo,c,self.now);migrate_paper(self.e.repo,c,self.now+50*WEEK)
   self.assertEqual(c.execute('SELECT payload FROM positions').fetchone()[0],original)
   self.assertEqual(c.execute('SELECT COUNT(*) FROM fills').fetchone()[0],1)
   self.e.reconcile_ledger(c)
  self.assertEqual(self.account()['initial'],'1000');self.assertEqual(self.account()['cash'],'989')
 def test_migration_rolls_back_all_changes_together(self):
  with self.e.repo.db() as c:
   c.execute('DELETE FROM system_events WHERE id=?',(POLICY_VERSION,));self.e.repo.set(c,'cad_plan',{'anchor':0,'weekly':'100'})
   c.execute("UPDATE paper_accounts SET cash='90',initial='100'")
  with self.assertRaises(RuntimeError),self.e.repo.db() as c:
   c.execute('BEGIN IMMEDIATE');migrate_paper(self.e.repo,c,self.now);raise RuntimeError('rollback')
  with self.e.repo.db() as c:
   self.assertEqual(c.execute('SELECT cash FROM paper_accounts').fetchone()[0],'90');self.assertNotIn('policy_version',self.e.repo.get(c,'cad_plan'))

class LiveBonusTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.now=1_800_000_000
  market=Markets();self.broker=Broker(market)
  self.e=LiveEngine(Path(self.tmp.name)/'live.db',self.broker,market,FX(),clock=lambda:self.now)
  with self.e.repo.db() as c:self.e.repo.set(c,'live_anchor',self.now)
 def settle(self,key,side,value,qty='1',fee='1',fx='100'):
  row=dict(client_order_id=key,decision_id='d-'+key,product='BTC-USD',base='BTC',quote='USD',side=side,
           size=qty,limit_price=str(Decimal(value)/Decimal(qty)),reserved_cad='500',cad_rate='1',fx_cost_bps=fx,state='PENDING',submitted_at=self.now)
  with self.e.repo.db() as c:self.e.repo.put(c,'orders',key,row)
  order=dict(order_id=key,client_order_id=key,product_id='BTC-USD',side=side,status='FILLED',settled=True,filled_size=qty,filled_value=value,total_fees=fee)
  self.e.settle(row,order)
  if side=='SELL':self.mark()
  return row,order
 def mark(self):self.e.snapshot(self.broker.balances(),Decimal(1),{'quote':'USD','fx_cost_bps':'100'},self.broker.fees())
 def result(self):return self.e.state()['performance_bonus']
 def test_real_fills_costs_and_both_fx_reserves_are_deducted(self):
  self.settle('buy','BUY','100');self.settle('sell','SELL','306')
  s=self.result();self.assertEqual(s['net_profit_cad'],'199.94');self.assertFalse(s['earned'])
  self.assertEqual(self.broker.sent,[]);self.assertFalse(self.e.enabled())
 def test_live_reward_only_adds_twenty_once_without_deposit_or_activation(self):
  self.settle('buy','BUY','100');row,order=self.settle('sell','SELL','307')
  s=self.result();self.assertEqual(s['net_profit_cad'],'200.93');self.assertTrue(s['earned'])
  self.e.settle(row,order);self.assertEqual(self.e.cap(),1020)
  self.now+=50*WEEK;self.mark();self.assertEqual(self.e.cap(),1020);self.assertEqual(self.result()['weekly_cad'],'0')
  self.assertFalse(self.e.enabled());self.assertEqual(self.broker.sent,[]);self.assertFalse(self.e.state()['actual_deposits_automated'])
 def test_partial_sales_allocate_entry_fx_without_double_deduction(self):
  self.settle('buy','BUY','100',qty='2');self.settle('sell-1','SELL','60');self.settle('sell-2','SELL','65')
  self.assertEqual(Decimal(self.result()['net_profit_cad']),Decimal('19.76'))
 def test_missing_cost_records_cannot_qualify(self):
  with self.e.repo.db() as c:self.e.repo.put(c,'fills','unknown',dict(realized_cad='500',side='SELL'))
  self.mark();s=self.result();self.assertFalse(s['verified']);self.assertFalse(s['earned']);self.assertEqual(s['next_weekly_cad'],'0')
 def test_paper_reward_cannot_promote_live_budget(self):
  p=MultiEngine(Path(self.tmp.name)/'paper.db',CAD())
  with p.repo.db() as c:
   p.repo.put(c,'fills','won',dict(realized='200'));bonus.observe(p.repo,c,'paper',self.now,Decimal('1200'))
  self.assertFalse(self.result()['earned']);self.assertEqual(self.e.cap(),1000)
