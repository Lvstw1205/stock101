"""Causal price/clock scenarios; all orders use temporary ledgers and fake venues."""
import copy, datetime as dt, tempfile, time, unittest, threading
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch
from stock101.crypto import execution_strategies as bundle
from stock101.crypto.multi import MultiEngine
from stock101.crypto.live import LiveEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto.exchange import CoinbaseAdapter, ExchangeError
from test_crypto import product
from test_live import Broker, FX

NOW=1_780_000_000//86400*86400+12*3600

def candles(now,step,count,price=100,spread=2):
 end=int(now)//step*step
 return [dict(start=end-(count-i)*step,open=price,close=price,high=price+spread,low=price-spread,volume=1000) for i in range(count)]

def book(price=100,now=NOW,pid='ALT-USD'):
 return dict(product_id=pid,mid=str(price),timestamp=now,spread_bps='2',
             bids=[dict(price=str(price*.9999),size='10000')],asks=[dict(price=str(price*1.0001),size='10000')])

def case(sid,now=NOW):
 h=candles(now,3600,100);d=candles(now,86400,220);price=100;pid='ALT-USD'
 if sid=='dca_weekly':pid='BTC-USD';d=[]
 if sid=='mayer':pid='BTC-USD';price=75
 if sid=='ma200_swing':
  for b in d[-50:]:b.update(open=105,close=105,high=107,low=103)
  price=105
 if sid=='range':
  for b in h:b.update(high=110,low=90)
  price=92
 if sid=='breakout':price=103
 if sid=='dip_grid':price=89;d=[]
 return h,d,book(price,now,pid)

def signal(sid,position=None,fills=(),now=NOW,**kw):
 h,d,b=case(sid,now)
 return bundle.evaluate(h,position,b,daily=d,previous_price=float(b['mid'])-.1,fills=fills,anchor=NOW-1,**kw)

def held(s,quantity='1',cost=None):
 return dict(product='BTC-USD' if s['strategy_id'] in ('dca_weekly','mayer') else 'ALT-USD',quantity=quantity,
             cost=cost or str(float(s['plan']['current'])*float(quantity)*1.008),entry_plan=bundle.entry_plan(s))

def entry_fill(s,now=NOW):
 return dict(product='BTC-USD' if s['strategy_id'] in ('dca_weekly','mayer') else 'ALT-USD',side='BUY',quantity='1',
             strategy_id=s['strategy_id'],strategy_token=s['plan']['entry_token'],created=now)

class SixStrategies(unittest.TestCase):
 def test_each_rule_independently_produces_buy_and_persists_owner(self):
  for sid in ('dca_weekly','ma200_swing','range','breakout','dip_grid','mayer'):
   with self.subTest(strategy=sid):
    s=signal(sid);self.assertEqual(s['strategy_id'],sid);self.assertEqual(s['decision'],'BUY')
    self.assertEqual(len(s['strategy_candidates']),len(bundle.CATALOG));self.assertTrue(s['plan']['entry_token'])
    self.assertEqual(bundle.entry_plan(s)['strategy_id'],sid)
    self.assertTrue(sid=='dca_weekly' or s['net_edge']['passed'])
 def test_dca_once_weekly_and_cost_loss_is_not_sell_signal(self):
  s=signal('dca_weekly');pos=held(s);f=entry_fill(s)
  again=signal('dca_weekly',pos,[f],now=NOW+60)
  self.assertEqual(again['decision'],'HOLD');self.assertLess(Decimal(again['plan']['position_net_exit']),0)
  self.assertEqual(signal('dca_weekly',pos,[f],now=NOW+604800)['decision'],'BUY')
  self.assertEqual(s['requested_cash_cad'],'2');self.assertIsNone(s['plan']['target'])
 def test_expensive_dca_does_not_bypass_cost_guard(self):
  h,d,b=case('dca_weekly');b['asks'][0]['price']='110'
  s=bundle.evaluate(h,None,b,daily=d)
  self.assertNotEqual(s['decision'],'BUY')
  self.assertEqual(next(r for r in s['strategy_candidates'] if r['strategy_id']=='dca_weekly')['reason'],'DCA_COST_LIMIT')
 def test_daily_ma_never_uses_hourly_or_future_or_gapped_bars(self):
  h,d,b=case('ma200_swing')
  for rows in (h,d[:-1],d[:100]+d[101:],d[-100:],candles(NOW+86400,86400,201)):
   with self.subTest(last=rows[-1]['start'],count=len(rows)):
    s=bundle.evaluate(h,None,b,daily=rows)
    self.assertNotEqual(next(r for r in s['strategy_candidates'] if r['strategy_id']=='ma200_swing')['decision'],'BUY')
 def test_downtrend_does_not_satisfy_200ma_entry(self):
  h,d,b=case('ma200_swing')
  for row in d[-50:]:row.update(open=90,close=90,high=92,low=88)
  s=bundle.evaluate(h,None,book(90),daily=d)
  self.assertEqual(next(r for r in s['strategy_candidates'] if r['strategy_id']=='ma200_swing')['reason'],'ABOVE_200MA_REQUIRED')
 def test_range_needs_recovery_and_sells_at_original_resistance(self):
  h,d,b=case('range');s=bundle.evaluate(h,None,b,daily=d,previous_price=95)
  self.assertNotEqual(s['decision'],'BUY')
  s=signal('range');pos=held(s)
  out=bundle.evaluate(h,pos,book(s['plan']['target']+1),daily=d)
  self.assertEqual(out['reason_codes'],['STRATEGY_TARGET']);self.assertEqual(out['sell_fraction'],1)
 def test_original_atr_stop_survives_later_chart_changes(self):
  s=signal('ma200_swing');pos=held(s);h,d,b=case('ma200_swing')
  out=bundle.evaluate(h,pos,book(s['plan']['invalidation']-1),daily=d)
  self.assertEqual(out['reason_codes'],['STRATEGY_STOP'])
  self.assertEqual(out['plan']['invalidation'],s['plan']['invalidation'])
 def test_breakout_expiry_waits_for_cost_recovery_and_preserves_stop(self):
  s=signal('breakout');self.assertEqual(s['plan']['facts']['trigger'],102)
  out=signal('breakout',held(s,cost='108'),[entry_fill(s)],now=NOW+86400)
  self.assertEqual(out['reason_codes'],['BREAKOUT_COST_RECOVERY_HOLD']);self.assertEqual(out['decision'],'HOLD')
  self.assertLess(Decimal(out['plan']['position_net_exit']),0)
  self.assertEqual(out['plan']['invalidation'],s['plan']['invalidation'])
  h,d,b=case('breakout',NOW+86400)
  out=bundle.evaluate(h,held(s,cost='108'),book(s['plan']['invalidation']-1,NOW+86400),daily=d)
  self.assertEqual(out['reason_codes'],['STRATEGY_STOP']);self.assertEqual(out['decision'],'SELL')
 def test_breakout_expiry_exits_only_with_positive_net_and_execution_cost_guard(self):
  s=signal('breakout');out=signal('breakout',held(s,cost='102.3'),now=NOW+86400)
  self.assertEqual(out['reason_codes'],['BREAKOUT_NET_SESSION_EXIT'])
  self.assertGreater(Decimal(out['plan']['position_net_exit']),0)
  self.assertNotIn('BREAKOUT_NET_SESSION_EXIT',bundle.PROTECTIVE)
  self.assertFalse(bundle.profit_policy.exit_allowed('99','100',out,'.005'))
  self.assertTrue(bundle.profit_policy.exit_allowed('100.01','100',out,'.005'))
 def test_live_bundle_preserves_cad_entry_cost_and_unknown_costs_hold(self):
  s=signal('breakout');pos=held(s,cost='90');pos['cost_cad']='140'
  negative=signal('breakout',pos,now=NOW+86400,cad_rate='1.3',cost_basis='140.35',fx_bps=25)
  self.assertEqual(negative['decision'],'HOLD')
  self.assertEqual(negative['plan']['profit_management']['cost_basis_cad'],'140.35')
  self.assertLess(Decimal(negative['plan']['position_net_exit']),0)
  unknown=signal('breakout',pos,now=NOW+86400,cad_rate='1.3',fx_bps=25)
  self.assertEqual(unknown['decision'],'HOLD');self.assertIsNone(unknown['plan']['position_net_exit'])
  positive=signal('breakout',pos,now=NOW+86400,cad_rate='1.4',cost_basis='140.35',fx_bps=25)
  self.assertEqual(positive['reason_codes'],['BREAKOUT_NET_SESSION_EXIT'])
 def test_no_fake_utc_open_before_first_completed_hour(self):
  now=NOW//86400*86400+1800;h,d,b=case('breakout',now)
  s=bundle.evaluate(h,None,b,daily=d)
  rows={r['strategy_id']:r for r in s['strategy_candidates']}
  self.assertEqual(rows['breakout']['reason'],'UTC_SESSION_OPEN_REQUIRED')
  self.assertEqual(rows['dip_grid']['reason'],'UTC_SESSION_OPEN_REQUIRED')
 def test_grid_stages_once_with_real_price_cap_and_stop_precedence(self):
  s=signal('dip_grid');self.assertEqual(s['plan']['entry_limit'],90)
  pos=held(s);f=entry_fill(s);h,d,b=case('dip_grid')
  out=bundle.evaluate(h,pos,book(87),daily=d,fills=[f],previous_price=87)
  self.assertEqual(out['decision'],'BUY');self.assertEqual(out['plan']['entry_limit'],87.5)
  self.assertNotEqual(out['plan']['entry_token'],s['plan']['entry_token'])
  last=entry_fill(out);out=bundle.evaluate(h,pos,book(84),daily=d,fills=[f,last],previous_price=84)
  self.assertEqual(out['reason_codes'],['STRATEGY_STOP'])
 def test_grid_never_buys_above_limit_including_slippage(self):
  h,d,b=case('dip_grid');s=bundle.evaluate(h,None,book(90),daily=d)
  self.assertNotEqual(next(r for r in s['strategy_candidates'] if r['strategy_id']=='dip_grid')['decision'],'BUY')
 def test_mayer_partial_exit_then_final_on_next_day(self):
  s=signal('mayer');pos=held(s);h,d,b=case('mayer')
  out=bundle.evaluate(h,pos,book(241,pid='BTC-USD'),daily=d)
  self.assertEqual(out['sell_fraction'],.5);self.assertIn('PARTIAL_PROFIT_TAKING',out['reason_codes'])
  sold=dict(product='BTC-USD',side='SELL',quantity='.5',strategy_id='mayer',created=NOW)
  pos.update(quantity='.5',cost=str(float(pos['cost'])/2),profit_taken=True)
  self.assertEqual(bundle.evaluate(h,pos,book(241,pid='BTC-USD'),daily=d,fills=[sold])['decision'],'HOLD')
  h,d,b=case('mayer',NOW+86400)
  out=bundle.evaluate(h,pos,book(241,NOW+86400,'BTC-USD'),daily=d,fills=[sold])
  self.assertEqual(out['reason_codes'],['MAYER_FINAL_EXIT']);self.assertEqual(out['sell_fraction'],1)
 def test_other_strategies_cannot_sell_dca_holding(self):
  s=signal('dca_weekly');pos=held(s);h,d,b=case('mayer')
  out=bundle.evaluate(h,pos,b,daily=d,fills=[entry_fill(s)],anchor=NOW-1)
  self.assertEqual(out['strategy_id'],'dca_weekly');self.assertEqual(out['decision'],'HOLD')
  self.assertTrue(all(r['reason']=='OTHER_STRATEGY_OWNS_POSITION' for r in out['strategy_candidates'] if r['strategy_id']!='dca_weekly'))
 def test_filled_token_blocks_reentry_even_after_full_exit(self):
  for sid in ('dca_weekly','ma200_swing','range','breakout','dip_grid','mayer'):
   s=signal(sid);out=signal(sid,fills=[entry_fill(s)])
   selected=next(r for r in out['strategy_candidates'] if r['strategy_id']==sid)
   self.assertNotEqual(selected['decision'],'BUY')

class RuleMarket:
 def __init__(self,sid='dca_weekly'):self.sid=sid;self.price=None;self.now=time.time()
 def get_products(self):return [self.get_product(self.pid)]
 @property
 def pid(self):return case(self.sid,self.now)[2]['product_id']
 def get_product(self,pid):return {**product(),'product_id':pid,'base_currency_id':pid.split('-')[0],'quote_volume_24h':'10000000'}
 def get_orderbook(self,pid):
  b=case(self.sid,self.now)[2];return book(self.price or float(b['mid']),self.now,pid)
 def get_candles(self,pid):return case(self.sid,self.now)[0]
 def get_daily_candles(self,pid):return case(self.sid,self.now)[1]

class ExecutionIntegration(unittest.TestCase):
 def test_portfolio_display_failure_does_not_stop_execution(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
   e=MultiEngine(Path(tmp)/'p.db',a,strategy_version=bundle.VERSION);e.start_multi();e.cycle()
   with patch.object(a,'get_orderbook',side_effect=ExchangeError('EXCHANGE_UNAVAILABLE')):
    cached=e.portfolio()['portfolios'][0]
    self.assertEqual(cached['mark_status'],'STALE');self.assertLessEqual(cached['mark_as_of'],time.time())
   self.assertTrue(e.plan_state()['running']);self.assertEqual(e.plan_state()['trade_count'],1)
   with patch.object(a,'get_orderbook',return_value=book(now=time.time()-60,pid=m.pid)):
    self.assertEqual(e.portfolio()['portfolios'][0]['mark_status'],'STALE')
   self.assertTrue(e.plan_state()['running'])
   with patch.object(a,'get_orderbook',side_effect=RuntimeError('unexpected programming error')):
    with self.assertRaisesRegex(ValueError,'PORTFOLIO_MARK_FAILED'):e.portfolio()
   self.assertFalse(e.plan_state()['running'])
 def test_status_read_does_not_wait_for_execution_network_lock(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=MultiEngine(Path(tmp)/'p.db',RuleMarket(),strategy_version=bundle.VERSION)
   done=threading.Event();results=[]
   def read():results.append(e.plan_state());done.set()
   e.lock.acquire();t=threading.Thread(target=read);t.start()
   try:self.assertTrue(done.wait(1),'Status blocked behind execution lock')
   finally:e.lock.release();t.join(2)
   self.assertEqual(results[0]['strategy_version'],bundle.VERSION)
 def test_independent_held_quotes_are_read_concurrently(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();e=MultiEngine(Path(tmp)/'p.db',m,strategy_version=bundle.VERSION)
   pids=['AAA-USD','BBB-USD','CCC-USD'];barrier=threading.Barrier(3,timeout=1)
   with e.repo.db() as c:
    for pid in pids:e.repo.put(c,'positions',pid,dict(product=pid,currency='CAD',quantity='1',cost='100'))
   def quote(pid):barrier.wait();return book(pid=pid,now=time.time())
   with patch.object(m,'get_orderbook',side_effect=quote):rows=e.books_for('CAD')
   self.assertEqual(set(rows),set(pids))
 def test_v3_readonly_live_key_still_cannot_activate(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();br=Broker(m);br.trade=False;e=LiveEngine(Path(tmp)/'live.db',br,m,FX(),strategy_version=bundle.VERSION)
   e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True))
   with self.assertRaises(ValueError):e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True))
   e.cycle();self.assertEqual(br.sent,[]);self.assertFalse(e.state()['running'])
 def test_paper_dca_fill_survives_restart_once_per_week_with_costs(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
   path=Path(tmp)/'paper.db';e=MultiEngine(path,a,strategy_version=bundle.VERSION);e.start_multi();e.cycle()
   with e.repo.db() as c:
    fills=e.repo.rows(c,'fills');self.assertEqual(len(fills),1);f=fills[0]
    self.assertEqual(f['strategy_id'],'dca_weekly');self.assertEqual(f['strategy_token'],'dca:0')
    self.assertLessEqual(Decimal(f['notional'])+Decimal(f['fee']),2);self.assertGreater(Decimal(f['fee']),0)
    e.reconcile_ledger(c)
   e=MultiEngine(path,a,strategy_version=bundle.VERSION);self.assertFalse(e.plan_state()['running'])
   e.start_multi();m.now+=10
   with patch('time.time',return_value=m.now):e.cycle();self.assertEqual(e.plan_state()['trade_count'],1)
   m.now+=604800
   with patch('time.time',return_value=m.now):
    e.contribute();e.cycle()
    self.assertEqual(e.plan_state()['trade_count'],2);self.assertEqual(Decimal(e.plan_state()['account']['initial']),1000)
   with e.repo.db() as c:e.reconcile_ledger(c)
 def test_all_six_route_through_paper_order_and_ledger(self):
  for sid in ('dca_weekly','ma200_swing','range','breakout','dip_grid','mayer'):
   with self.subTest(strategy=sid),tempfile.TemporaryDirectory() as tmp:
    m=RuleMarket(sid)
    # Stable midday fixture is independent of the time the test is run.
    m.now=NOW
    with patch('time.time',return_value=NOW):
     a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
     e=MultiEngine(Path(tmp)/'p.db',a,strategy_version=bundle.VERSION);e.start_multi()
     e.latest[m.pid]={'plan':{'current':float(m.get_orderbook(m.pid)['mid'])*1.37-.1}}
     e.cycle()
     with e.repo.db() as c:
      rows=e.repo.rows(c,'fills');self.assertEqual(len(rows),1,e.repo.rows(c,'trade_decisions')[0])
      self.assertEqual(rows[0]['strategy_id'],sid);e.reconcile_ledger(c)
 def test_all_six_fake_live_ioc_and_settlement_carry_strategy(self):
  for sid in ('dca_weekly','ma200_swing','range','breakout','dip_grid','mayer'):
   with self.subTest(strategy=sid),tempfile.TemporaryDirectory() as tmp:
    m=RuleMarket(sid);m.now=NOW;br=Broker(m)
    with patch('time.time',return_value=NOW):
     e=LiveEngine(Path(tmp)/'live.db',br,m,FX(),clock=lambda:NOW,strategy_version=bundle.VERSION)
     e.cycle();self.assertEqual(br.sent,[])
     e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True))
     e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True));e.latest[m.pid]=float(m.get_orderbook(m.pid)['mid'])-.1
     e.cycle();self.assertEqual(len(br.sent),1,e.state()['decisions']);self.assertTrue(e.reconcile())
     with e.repo.db() as c:
      f=e.repo.rows(c,'fills')[0];self.assertEqual(f['strategy_id'],sid);self.assertTrue(f['strategy_token'])
      self.assertEqual(e.repo.one(c,'positions',m.pid)['entry_plan']['strategy_id'],sid)
     self.assertTrue(e.audit_ledger())
     m.now+=10;e.clock=lambda:NOW+10;e.cycle();self.assertEqual(len(br.sent),1)
 def test_daily_adapter_requests_daily_granularity_and_cad_conversion(self):
  now=time.time();seen=[]
  def transport(path,query,headers):
   seen.append(query);return {'candles':[{k:str(v) for k,v in b.items()} for b in candles(now,86400,220)]}
  a=CoinbaseAdapter(transport=transport);rows=a.get_daily_candles('BTC-USD')
  self.assertEqual(seen[0]['granularity'],'ONE_DAY');self.assertEqual(len(rows),220)
  cad=CadPaperAdapter(a,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
  self.assertAlmostEqual(cad.get_daily_candles('BTC-USD')[0]['close'],137)

if __name__=='__main__':unittest.main()
