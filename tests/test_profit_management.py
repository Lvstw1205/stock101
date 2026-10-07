import copy,datetime as dt,tempfile,time,unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from stock101.crypto import profit_policy as policy, execution_strategies as rules, profit_calendar
from stock101.crypto.multi import MultiEngine
from stock101.crypto.live import LiveEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from test_execution_strategies import candles,book,NOW,RuleMarket
from test_live import Broker,FX


def signal():
 return dict(decision='HOLD',reason_codes=['STRATEGY_HOLD'],sell_fraction=0,strategy_id='range',plan={})

def position(cost='90',**kw):
 return dict(product='ALT-USD',quantity='1',cost=cost,**kw)

def falling(now=NOW):
 rows=candles(now,3600,100)
 for i,b in enumerate(rows):
  value=132-i*.3;b.update(open=value,close=value,high=value+1,low=value-1)
 return rows

def rising(now=NOW):
 rows=candles(now,3600,60)
 for i,b in enumerate(rows):
  value=80+i*.3;b.update(open=value,close=value,high=value+1,low=value-1)
 return rows

class ProfitJudgment(unittest.TestCase):
 def apply(self,h=None,p=None,b=None,s=None,**kw):
  return policy.apply(s or signal(),h if h is not None else falling(),p or position(),b or book(),fee_bps=60,slippage_bps=5,**kw)
 def test_positive_profit_is_not_an_unconditional_sell(self):
  out=self.apply(rising(),position('80'),book(100))
  self.assertEqual(out['decision'],'HOLD')
  self.assertEqual(out['plan']['profit_management']['status'],'HOLD_STRONG_TREND')
  self.assertGreater(D(out['plan']['profit_management']['net_profit_cad']),0)
 def test_small_positive_profit_can_exit_on_weakening_trend(self):
  proceeds=D('99.99')*D('.9995')*D('.994')
  out=self.apply(p=position(str(proceeds/D('1.001'))))
  self.assertEqual(out['reason_codes'],['PROFIT_TREND_WEAKENED'])
  self.assertEqual(out['sell_fraction'],1)
  self.assertLess(D(out['plan']['profit_management']['net_return']),D('.005'))
 def test_gross_profit_but_negative_after_cost_holds(self):
  out=self.apply(p=position('100'),b=book(100.2))
  self.assertEqual(out['decision'],'HOLD')
  self.assertEqual(out['plan']['profit_management']['status'],'NET_NOT_POSITIVE')
 def test_zero_net_is_not_eligible(self):
  cost=D('99.99')*D('.9995')*D('.994')
  out=self.apply(p=position(str(cost)))
  self.assertEqual(out['decision'],'HOLD');self.assertEqual(D(out['plan']['profit_management']['net_profit_cad']),0)
 def test_all_bid_depth_is_used_not_just_best_quote(self):
  b=book(110);b['bids']=[dict(price='110',size='.1'),dict(price='90',size='1')]
  out=self.apply(p=position('100'),b=b)
  self.assertEqual(out['decision'],'HOLD');self.assertLess(D(out['plan']['profit_management']['net_profit_cad']),0)
  b['bids']=b['bids'][:1]
  self.assertEqual(self.apply(b=b)['plan']['profit_management']['status'],'COSTS_UNVERIFIED')
 def test_resistance_partial_only_once_per_position(self):
  h=candles(NOW,3600,60);b=book(102)
  out=self.apply(h,position(),b)
  self.assertEqual(out['sell_fraction'],.5);self.assertIn('PARTIAL_PROFIT_TAKING',out['reason_codes'])
  out=self.apply(h,position(profit_taken=True),b)
  self.assertEqual(out['decision'],'HOLD')
 def test_pullback_preserves_profit_but_uptrend_can_hold(self):
  h=candles(NOW,3600,60)
  out=self.apply(h,position(peak='108'),book(101))
  self.assertEqual(out['reason_codes'],['PROFIT_PULLBACK_EXIT'])
  self.assertEqual(self.apply(rising(),position('80',peak='105'),book(100))['decision'],'HOLD')
 def test_positive_without_history_is_not_an_excuse_to_sell(self):
  out=self.apply([])
  self.assertEqual(out['decision'],'HOLD');self.assertEqual(out['plan']['profit_management']['status'],'HISTORY_REQUIRED')
 def test_original_protective_exit_is_never_removed(self):
  s=signal();s.update(decision='SELL',reason_codes=['STRATEGY_STOP'],sell_fraction=1)
  out=self.apply(rising(),position('120'),book(100),s)
  self.assertEqual(out['reason_codes'],['STRATEGY_STOP']);self.assertEqual(out['decision'],'SELL')
 def test_cad_profit_uses_historic_entry_rate_and_both_fx_sides(self):
  p=position('90',cost_cad='126')
  negative=self.apply(p=p,fx_bps=25,cad_rate='1.2',cost_basis='126.315')
  self.assertEqual(negative['decision'],'HOLD')
  positive=self.apply(p=p,fx_bps=25,cad_rate='1.4',cost_basis='126.315')
  self.assertEqual(positive['decision'],'SELL')
  expected=D('99.99')*D('.9995')*D('.994')*D('.9975')*D('1.4')-D('126.315')
  self.assertEqual(D(positive['plan']['profit_management']['net_profit_cad']),expected)
  self.assertEqual(self.apply(p=p,cad_rate='1.4')['plan']['profit_management']['status'],'COSTS_UNVERIFIED')
 def test_bundle_applies_common_judgment_to_dca_and_legacy(self):
  for owner in ('dca_weekly','mayer','range',None):
   with self.subTest(owner=owner):
    p=position('90',entry_plan={'strategy_id':owner,'target':130,'invalidation':70})
    out=rules.evaluate(falling(),p,book(),daily=[])
    self.assertEqual(out['decision'],'SELL')
    self.assertIn('PROFIT_TREND_WEAKENED',out['reason_codes'])
 def test_final_fill_guard_rejects_zero_and_negative(self):
  s={'reason_codes':['PROFIT_TREND_WEAKENED']}
  self.assertTrue(policy.exit_allowed('100.0001','100',s,'.005'))
  self.assertFalse(policy.exit_allowed('100','100',s,'.005'))
  self.assertFalse(policy.exit_allowed('99.99','100',s,'.005'))
  self.assertFalse(policy.exit_allowed('100.1','100',{'reason_codes':['STRATEGY_TARGET']},'.005'))

class ProfitExecution(unittest.TestCase):
 def test_real_paper_fill_path_records_sale_and_calendar(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();a=CadPaperAdapter(m,lambda:dict(rate='1.37',date=dt.datetime.now(dt.timezone.utc).date().isoformat()))
   e=MultiEngine(Path(tmp)/'p.db',a,strategy_version=rules.VERSION);e.start_multi();e.cycle()
   with e.repo.db() as c:before=D(e.repo.rows(c,'paper_orders')[0]['notional']);self.assertEqual(len(e.repo.rows(c,'fills')),1)
   m.price=104;m.now=time.time()+1
   with patch.object(m,'get_candles',return_value=falling(m.now)):
    e.cycle()
   with e.repo.db() as c:
    sold=[f for f in e.repo.rows(c,'fills') if f['side']=='SELL']
    self.assertEqual(len(sold),1);self.assertIn('PROFIT_TREND_WEAKENED',sold[0]['reason_codes'])
    self.assertGreater(D(sold[0]['realized']),0);e.reconcile_ledger(c)
   view=profit_calendar.view(e.repo)
   self.assertEqual(view['summary']['sell_count'],1)
   self.assertEqual(D(view['summary']['net_profit_cad']),D(sold[0]['realized']))
 def test_paper_final_cost_guard_before_any_write(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=MultiEngine(Path(tmp)/'p.db',RuleMarket(),strategy_version=rules.VERSION)
   args=('SELL',D('1'),D('100'),D('.6'),D('99.5'),D('1'))
   with self.assertRaisesRegex(ValueError,'NET_EXIT_PROFIT_TOO_SMALL'):
    e.validate_execution(*args,{'reason_codes':['PROFIT_TREND_WEAKENED']})
   e.validate_execution('SELL',D('1'),D('100'),D('.6'),D('99.3'),D('1'),{'reason_codes':['PROFIT_TREND_WEAKENED']})
   with e.repo.db() as c:self.assertEqual(e.repo.rows(c,'fills'),[])
 def setup_live(self,tmp):
  m=RuleMarket();b=Broker(m);e=LiveEngine(Path(tmp)/'l.db',b,m,FX(),strategy_version=rules.VERSION)
  e.configure(dict(quote='USD',minimum_net='.005',fx_cost_bps='25',costs_confirmed=True,enable_live=True))
  e.activate(dict(confirmation='실제 돈으로 자동매매 시작',confirmed=True));e.cycle();e.reconcile()
  return e,m,b
 def test_fake_live_discretionary_sell_settles_and_never_duplicates(self):
  with tempfile.TemporaryDirectory() as tmp:
   e,m,b=self.setup_live(tmp);self.assertEqual(len(b.sent),1)
   m.price=104;m.now=time.time()+1
   with patch.object(m,'get_candles',return_value=falling(m.now)):e.cycle()
   self.assertEqual(len(b.sent),2);self.assertEqual(b.sent[-1]['side'],'SELL')
   e.reconcile();e.reconcile()
   v=profit_calendar.view(e.repo,'live');self.assertEqual(v['summary']['sell_count'],1)
   self.assertGreater(D(v['summary']['net_profit_cad']),0)
 def test_fake_live_preview_fx_can_reverse_quote_currency_profit(self):
  with tempfile.TemporaryDirectory() as tmp:
   e,m,b=self.setup_live(tmp);m.price=104;m.now=time.time()+1
   with patch.object(m,'get_candles',return_value=falling(m.now)),patch.object(e,'quote_rate',return_value=D('1.1')):e.cycle()
   self.assertEqual(len(b.sent),1,'USD profit must not liquidate at a CAD loss')
 def test_fake_live_preview_fee_spike_rejects_discretionary_sell(self):
  with tempfile.TemporaryDirectory() as tmp:
   e,m,b=self.setup_live(tmp);m.price=102;m.now=time.time()+1;original=b.preview
   def high(payload):
    p=original(payload);p['commission_total']=str(D(p['base_size'])*D(payload['order_configuration']['sor_limit_ioc']['limit_price'])*D('.04'));return p
   with patch.object(m,'get_candles',return_value=falling(m.now)),patch.object(b,'preview',side_effect=high):e.cycle()
   self.assertEqual(len(b.sent),1)
