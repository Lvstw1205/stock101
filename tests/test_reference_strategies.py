"""Video rules, causal indicators and real engine paths with fake exchanges only."""
import copy,datetime as dt,tempfile,threading,unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from stock101.crypto import execution_strategies as bundle,reference_strategies as refs
from stock101.crypto.intelligence import Intelligence
from stock101.crypto.multi import MultiEngine
from stock101.crypto.live import LiveEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from test_execution_strategies import NOW,candles,book,held,entry_fill
from test_crypto import product
from test_live import Broker,FX

def series(prices,step,now=NOW):
 rows=candles(now,step,len(prices))
 for row,p in zip(rows,prices):row.update(open=p,high=p+2,low=p-2,close=p)
 return rows

def benchmark(now=NOW,price=95):
 return dict(product_id='BTC-USD',price=price,timestamp=now,session_open=100,daily=candles(now,86400,30))

def scenario(sid,now=NOW):
 h=candles(now,3600,220);d=candles(now,86400,30);mid=100
 if sid=='supertrend':d=series([100]*59+[120],86400,now);mid=120
 if sid=='hull':d=series([100+i*.2 for i in range(70)],86400,now);mid=d[-1]['close']
 if sid=='ichimoku':d=series([100]*150+[60]*50+[110]*20,86400,now);mid=110
 if sid=='macd_trend':h=series([100]*140+[100+i*.5 for i in range(50)]+[124.5-i*.45 for i in range(29)]+[130],3600,now);mid=130
 if sid=='rsi_cross':h=series([100+i*.15 for i in range(216)]+[90]*3+[160],3600,now);mid=160
 if sid=='rsi_reversion':h=series([100+i*.3 for i in range(180)]+[155]*20+[155-i*.75 for i in range(20)],3600,now);mid=h[-1]['close']
 if sid=='red_day':mid=95
 if sid in ('supertrend','hull','ichimoku'):h=candles(now,3600,220,mid)
 return h,d,book(mid,now),benchmark(now)

def evaluate(sid,position=None,fills=(),now=NOW,**kwargs):
 h,d,b,bm=scenario(sid,now)
 return bundle.evaluate(h,position,b,daily=d,benchmark=bm,fills=fills,anchor=NOW-1,previous_price=float(b['mid']),**kwargs)

class IndicatorTests(unittest.TestCase):
 def test_indicators_known_values_and_prefix_invariance(self):
  self.assertEqual(refs.ema([1,2,3,4,5],3),[None,None,2,3,4])
  self.assertAlmostEqual(refs.wma([1,2,3,4],3)[-1],20/6)
  self.assertAlmostEqual(refs.rma([1,2,3,4,5],3)[-1],31/9)
  self.assertEqual(refs.rsi([1]*30,9)[-1],50)
  self.assertEqual(refs.rsi(list(range(1,30)),9)[-1],100)
  self.assertEqual(refs.rsi(list(range(30,0,-1)),9)[-1],0)
  data=[100+i*.3 for i in range(220)]
  for indicator in (lambda x:refs.ema(x,35),lambda x:refs.rsi(x,9),refs.hma):
   self.assertEqual(indicator(data),indicator(data+[1,1000])[:-2])
  h=series(data,3600);self.assertEqual(refs.supertrend(h),refs.supertrend(h+[dict(h[-1],close=1,low=1)])[:-1])
 def test_supertrend_flips_only_after_band_cross(self):
  h=series([100]*30+[115,115,80],86400);s=refs.supertrend(h)
  self.assertFalse(s[-4]['up']);self.assertTrue(s[-3]['up']);self.assertTrue(s[-2]['up']);self.assertFalse(s[-1]['up'])
 def test_eight_hour_blocks_are_complete_utc_blocks(self):
  h=candles(NOW,3600,350);out=refs.aggregate_8h(h,NOW)
  self.assertTrue(all(r['start']%28800==0 for r in out));self.assertEqual(out[-1]['start']+28800,NOW//28800*28800)
  current=[b for b in h if b['start']>=NOW//28800*28800]
  for b in current:b['close']=1000000
  self.assertEqual(refs.aggregate_8h(h,NOW),out)
  self.assertEqual(refs.aggregate_8h(h[:-10],NOW),[])
  del h[-20];self.assertEqual(refs.aggregate_8h(h,NOW),[])

class CandidateTests(unittest.TestCase):
 def test_all_seven_are_executable_and_identified(self):
  for sid in refs.IDS:
   with self.subTest(sid=sid):
    s=evaluate(sid)
    self.assertEqual(s['strategy_id'],sid,[(r['strategy_id'],r['decision']) for r in s['strategy_candidates']])
    self.assertEqual(s['decision'],'BUY');self.assertEqual(len(s['strategy_candidates']),len(bundle.CATALOG))
    self.assertTrue(s['net_edge']['passed']);self.assertTrue(s['plan']['entry_token'])
    self.assertGreater(s['plan']['target'],s['plan']['current']);self.assertLess(s['plan']['invalidation'],s['plan']['current'])
 def test_cost_gate_and_filled_token_apply_to_all_seven(self):
  for sid in refs.IDS:
   with self.subTest(sid=sid):
    s=evaluate(sid)
    out=evaluate(sid,fills=[entry_fill(s)])
    row=next(r for r in out['strategy_candidates'] if r['strategy_id']==sid)
    self.assertNotEqual(row['decision'],'BUY');self.assertEqual(row['reason'],'INSTALLMENT_ALREADY_FILLED')
    out=evaluate(sid,fee_bps=5000)
    row=next(r for r in out['strategy_candidates'] if r['strategy_id']==sid)
    self.assertNotEqual(row['decision'],'BUY');self.assertEqual(row['reason'],'TARGET_NET_TOO_SMALL')
 def test_incomplete_future_stale_and_gapped_data_cannot_buy(self):
  for sid in refs.IDS-{'red_day'}:
   h,d,b,bm=scenario(sid);use_daily=sid in ('supertrend','hull','ichimoku');original=d if use_daily else h;step=86400 if use_daily else 3600
   for rows in (original[:-1],original[:10]+original[11:],original[-10:]):
    out=bundle.evaluate(h if use_daily else rows,None,b,daily=rows if use_daily else d,benchmark=bm)
    self.assertNotEqual(next(r for r in out['strategy_candidates'] if r['strategy_id']==sid)['decision'],'BUY')
   future=dict(original[-1],start=original[-1]['start']+step,close=1000000)
   a=bundle.evaluate(h,None,b,daily=d,benchmark=bm)
   z=bundle.evaluate(h if use_daily else h+[future],None,b,daily=d+[future] if use_daily else d,benchmark=bm)
   self.assertEqual(a,z)
 def test_intrabar_extension_blocks_entries_but_preserves_exits(self):
  for sid in refs.TREND_IDS:
   h,d,b,bm=scenario(sid);b=book(float(b['mid'])*2)
   out=bundle.evaluate(h,None,b,daily=d,benchmark=bm)
   self.assertNotEqual(next(r for r in out['strategy_candidates'] if r['strategy_id']==sid)['decision'],'BUY')
 def test_original_owner_and_stop_survive_new_chart(self):
  for sid in refs.IDS:
   s=evaluate(sid);pos=held(s);h,d,b,bm=scenario('hull')
   out=bundle.evaluate(h,pos,book(s['plan']['invalidation']-.1),daily=d,benchmark=bm)
   self.assertEqual(out['strategy_id'],sid);self.assertEqual(out['reason_codes'],['STRATEGY_STOP'])
   self.assertEqual(out['plan']['invalidation'],s['plan']['invalidation'])
 def test_opposite_trend_can_exit_loss_without_opening_short(self):
  for sid in refs.TREND_IDS:
   pos=held(evaluate(sid),cost='1000');pos['entry_plan']['invalidation']=1
   h=series([150-i*.2 for i in range(250)],3600);d=series([150-i*.2 for i in range(250)],86400)
   out=bundle.evaluate(h,pos,book(100),daily=d)
   self.assertEqual(out['decision'],'SELL');self.assertEqual(out['reason_codes'],['REFERENCE_TREND_EXIT'])
   self.assertIn(out['reason_codes'][0],bundle.PROTECTIVE);self.assertLess(Decimal(out['plan']['position_net_exit']),0)
   no_position=bundle.evaluate(h,None,book(100),daily=d)
   self.assertNotIn(no_position['decision'],('SELL','REDUCE'))
 def test_rsi_recovery_exit_requires_profit_and_uses_same_owner(self):
  pos=held(evaluate('rsi_reversion'),cost='150');pos['entry_plan'].update(target=200,invalidation=1)
  h=series([100+i*.1 for i in range(201)]+[150]*19,3600);d=candles(NOW,86400,30)
  out=bundle.evaluate(h,pos,book(150),daily=d);self.assertEqual(out['decision'],'HOLD')
  pos['cost']='120';out=bundle.evaluate(h,pos,book(150),daily=d);self.assertEqual(out['reason_codes'],['RSI_RECOVERY_EXIT'])

class RedDayTests(unittest.TestCase):
 def test_ladder_all_levels_once_across_assets(self):
  fills=[]
  for level in (.047,.07,.095,.12,.15,.18):
   reason,token,f=refs.red_day_trigger(benchmark(price=100*(1-level)),NOW,fills,NOW-1)
   self.assertEqual(reason,'BTC_INTRADAY_DIP_BUY');self.assertTrue(token)
   fills.append(dict(product='DIFFERENT-USD',side='BUY',quantity='1',strategy_token=token))
   again=refs.red_day_trigger(benchmark(price=100*(1-level)),NOW,fills,NOW-1)
   self.assertIsNone(again[1])
 def test_daily_close_and_three_small_red_days(self):
  bm=benchmark(price=100);bm['daily'][-1]['close']=96.7
  reason,token,_=refs.red_day_trigger(bm,NOW,[],NOW-1);self.assertEqual(reason,'BTC_RED_DAY_BUY')
  bm=benchmark(price=100)
  for b,p in zip(bm['daily'][-3:],[98,96.1,95]):b.update(open=p/0.99,close=p)
  self.assertEqual(refs.red_day_trigger(bm,NOW,[],NOW-1)[0],'BTC_THREE_RED_BUY')
  bm['daily'][-2]['open']=110
  self.assertIsNone(refs.red_day_trigger(bm,NOW,[],NOW-1)[1])
 def test_weekly_cap_missing_btc_and_currency_scale(self):
  fills=[dict(side='BUY',quantity='1',strategy_token=f'red_day:0:old:{i}') for i in range(15)]
  self.assertEqual(refs.red_day_trigger(benchmark(),NOW,fills,NOW-1)[0],'RED_DAY_WEEKLY_LIMIT')
  bm=benchmark();bm['timestamp']-=16
  self.assertIsNone(refs.red_day_trigger(bm,NOW,[],NOW-1)[1])
  bm=benchmark();bm['daily'].pop();self.assertIsNone(refs.red_day_trigger(bm,NOW,[],NOW-1)[1])
  bm=benchmark();bm['product_id']='OTHER-USD';self.assertIsNone(refs.red_day_trigger(bm,NOW,[],NOW-1)[1])
  a=refs.red_day_trigger(benchmark(),NOW,[],NOW-1);bm=benchmark(price=95*1.37);bm['session_open']=137
  for b in bm['daily']:
   for k in ('open','high','low','close'):b[k]*=1.37
  self.assertEqual(a[:2],refs.red_day_trigger(bm,NOW,[],NOW-1)[:2])
 def test_btc_and_eth_are_signals_not_red_day_targets(self):
  h,d,b,bm=scenario('red_day')
  for base in ('BTC','ETH'):
   b['product_id']=base+'-USD';s=bundle.evaluate(h,None,b,daily=d,benchmark=bm)
   self.assertEqual(next(r for r in s['strategy_candidates'] if r['strategy_id']=='red_day')['reason'],'ALT_ONLY')
 def test_intelligence_unscales_each_history_and_rejects_stale_tick(self):
  obj=object.__new__(Intelligence);obj.lock=threading.RLock();obj.products={'BTC-USD':{}}
  obj.daily={'BTC-USD':dict(bars=candles(NOW,86400,30,137),fx_rate=1.37)}
  obj.history={'BTC-USD':dict(bars=candles(NOW,3600,100,140),fx_rate=1.40)}
  obj.stream=SimpleNamespace(snapshot=lambda:{'BTC-USD':dict(price='95',timestamp=NOW)})
  result=obj.bitcoin_context(NOW);self.assertEqual(result['session_open'],100)
  self.assertAlmostEqual(result['daily'][-1]['close'],100);self.assertEqual(result['price'],'95')
  self.assertIsNone(obj.bitcoin_context(NOW+16))

class Market:
 def __init__(self,sid):self.sid=sid;self.now=NOW;self.price=None
 def get_product(self,pid):return {**product(),'product_id':pid,'base_currency_id':pid.split('-')[0],'quote_volume_24h':'10000000'}
 def get_products(self):return [self.get_product('ALT-USD')]
 def get_orderbook(self,pid):return book(self.price or float(scenario(self.sid,self.now)[2]['mid']),self.now,pid)
 def get_candles(self,pid):return scenario(self.sid,self.now)[0]
 def get_daily_candles(self,pid):return scenario(self.sid,self.now)[1]

class History:
 """Deterministic cached input provider, without background threads/network."""
 def __init__(self,adapter):
  self.adapter=adapter;self.products={};self.stream=SimpleNamespace(snapshot=lambda:{})
  self.news=SimpleNamespace(context=lambda p:None)
 def set_products(self,products):self.products={p['product_id']:p for p in products}
 def get(self,pid,allow_stale=False):return dict(bars=self.adapter.get_candles(pid))
 def get_daily(self,pid):return self.adapter.get_daily_candles(pid)
 def bitcoin_context(self,now):return benchmark(now)
 def state(self):return {}

class EngineTests(unittest.TestCase):
 def test_six_opposite_trends_reach_paper_and_fake_live_loss_exits(self):
  for mode in ('paper','live'):
   for sid in refs.TREND_IDS:
    with self.subTest(mode=mode,sid=sid),tempfile.TemporaryDirectory() as tmp:
     m=Market(sid);a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()));broker=Broker(m)
     with patch('time.time',side_effect=lambda:m.now):
      if mode=='paper':
       e=MultiEngine(Path(tmp)/'p.db',a,strategy_version=bundle.VERSION);e.start_multi();e.intelligence=History(a)
      else:
       e=LiveEngine(Path(tmp)/'l.db',broker,m,FX(),clock=lambda:m.now,strategy_version=bundle.VERSION);e.intelligence=History(m)
       e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True));e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True))
      e.cycle()
      if mode=='live':self.assertTrue(e.reconcile())
      original=float(m.get_orderbook('ALT-USD')['mid']);m.now+=86400;m.price=original*.999
      with patch.object(m,'get_candles',side_effect=lambda pid:series([150-i*.2 for i in range(250)],3600,m.now)),patch.object(m,'get_daily_candles',side_effect=lambda pid:series([150-i*.2 for i in range(250)],86400,m.now)):
       e.cycle()
       if mode=='live':self.assertTrue(e.reconcile())
      with e.repo.db() as c:
       fills=e.repo.rows(c,'fills');self.assertEqual(len(fills),2)
       f=fills[0];self.assertEqual(f['side'],'SELL');self.assertIn('REFERENCE_TREND_EXIT',f['reason_codes']);self.assertLess(Decimal(f['realized']),0)
       if mode=='paper':e.reconcile_ledger(c)
      if mode=='live':self.assertTrue(e.audit_ledger())
 def test_seven_paper_entries_costs_ledger_and_restart_idempotency(self):
  for sid in refs.IDS:
   with self.subTest(sid=sid),tempfile.TemporaryDirectory() as tmp,patch('time.time',return_value=NOW):
    m=Market(sid);a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
    path=Path(tmp)/'paper.db';e=MultiEngine(path,a,strategy_version=bundle.VERSION)
    e.start_multi();e.intelligence=History(a);e.cycle()
    with e.repo.db() as c:
     fills=e.repo.rows(c,'fills');self.assertEqual(len(fills),1,e.repo.rows(c,'trade_decisions'))
     f=fills[0];self.assertEqual(f['strategy_id'],sid);self.assertLessEqual(Decimal(f['notional'])+Decimal(f['fee']),100)
     self.assertGreater(Decimal(f['fee']),0);e.reconcile_ledger(c)
    e=MultiEngine(path,a,strategy_version=bundle.VERSION);e.start_multi();e.intelligence=History(a);m.now+=10;e.cycle()
    with e.repo.db() as c:self.assertEqual(len(e.repo.rows(c,'fills')),1)
 def test_seven_fake_live_ioc_settlement_and_loss_exit(self):
  for sid in refs.IDS:
   with self.subTest(sid=sid),tempfile.TemporaryDirectory() as tmp,patch('time.time',return_value=NOW):
    m=Market(sid);broker=Broker(m);e=LiveEngine(Path(tmp)/'live.db',broker,m,FX(),clock=lambda:m.now,strategy_version=bundle.VERSION)
    e.intelligence=History(m);e.cycle();self.assertEqual(broker.sent,[])
    e.configure(dict(quote='USD',costs_confirmed=True,enable_live=True));e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True));e.cycle()
    self.assertEqual(len(broker.sent),1,e.state()['decisions']);self.assertTrue(e.reconcile())
    with e.repo.db() as c:
     f=e.repo.rows(c,'fills')[0];pos=e.repo.one(c,'positions','ALT-USD');self.assertEqual(f['strategy_id'],sid)
     self.assertEqual(pos['entry_plan']['strategy_id'],sid);self.assertLessEqual((Decimal(f['notional'])+Decimal(f['fee']))*Decimal('1.37'),100)
    self.assertTrue(e.audit_ledger());m.now+=10;m.price=float(pos['entry_plan']['invalidation'])-.1
    with patch('time.time',return_value=m.now):
     e.cycle();self.assertEqual(len(broker.sent),2,e.state()['decisions']);self.assertEqual(broker.sent[-1]['side'],'SELL');self.assertTrue(e.reconcile());self.assertTrue(e.audit_ledger())

if __name__=='__main__':unittest.main()
