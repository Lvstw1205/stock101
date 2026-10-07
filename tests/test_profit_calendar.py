import datetime as dt,json,tempfile,unittest
from decimal import Decimal as D
from pathlib import Path
from stock101.crypto.repository import Repository
from stock101.crypto import profit_calendar,performance_bonus,service
from unittest.mock import patch

def epoch(iso):return dt.datetime.fromisoformat(iso).timestamp()
NOW=epoch('2026-09-30T20:00:00+00:00')
class CalendarTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.repo=Repository(Path(self.tmp.name)/'ledger.db');self.n=0
 def tearDown(self):self.tmp.cleanup()
 def add(self,side,notional,fee,realized,when,*,pid='AAA-USD',qty='1',rate=None,fx='25',currency='CAD'):
  self.n+=1;key=str(self.n);f=dict(product=pid,side=side,quantity=qty,notional=notional,fee=fee,realized=realized,currency=currency)
  if rate:f.update(cad_rate=rate,realized_cad=realized)
  with self.repo.db() as c:
   self.repo.put(c,'fills',key,f);c.execute('UPDATE fills SET created=? WHERE id=?',(epoch(when),key))
   if rate and fx is not None:self.repo.put(c,'orders',key,dict(fx_cost_bps=fx))
  return key
 def view(self,mode='paper',month='2026-09',now=NOW):return profit_calendar.view(self.repo,mode,month,now)
 def test_empty_days_do_not_invent_zero_percent_or_trades(self):
  view=self.view();self.assertEqual(len(view['days']),30);self.assertEqual(view['weekday_offset'],2)
  self.assertEqual(view['summary']['net_profit_cad'],'0');self.assertIsNone(view['summary']['return_ratio'])
  self.assertTrue(all(d['net_profit_cad'] is None for d in view['days']))
  self.assertEqual(self.view(month='2026-10')['days'][0]['status'],'future')
 def test_positive_and_negative_exits_use_weighted_sold_cost(self):
  self.add('BUY','100','1','0','2026-09-28T20:00:00+00:00')
  self.add('SELL','60','.6','8.9','2026-09-29T20:00:00+00:00',qty='.5')
  self.add('SELL','40','.4','-10.9','2026-09-29T21:00:00+00:00',qty='.5')
  v=self.view();day=v['days'][28]
  self.assertEqual(D(day['net_profit_cad']),D('-2'))
  self.assertEqual(D(day['cost_basis_cad']),D('101'))
  self.assertEqual(D(day['return_ratio']),D('-2')/101)
  self.assertEqual(v['days'][27]['net_profit_cad'],'0')
  self.assertIsNone(v['days'][27]['return_ratio'])
  self.assertEqual(v['days'][29]['status'],'today');self.assertEqual(v['days'][29]['sell_count'],0)
 def test_deposits_and_unrealized_snapshots_never_become_profit(self):
  with self.repo.db() as c:
   c.execute("INSERT INTO paper_accounts VALUES('CAD','5000','5000')")
   self.repo.put(c,'portfolio_snapshots','x',dict(equity='6000',unrealized='1000'))
   self.repo.put(c,'system_events','deposit',dict(amount='100',category='funding'))
  v=self.view();self.assertEqual(v['summary']['net_profit_cad'],'0')
  self.assertFalse(v['includes_deposits']);self.assertFalse(v['includes_unrealized'])
 def test_utc_midnight_and_month_boundary(self):
  self.add('SELL','11','.1','.9','2026-09-30T23:59:59+00:00')
  self.add('SELL','12','.1','1.9','2026-10-01T00:00:00+00:00')
  now=epoch('2026-10-02T12:00:00+00:00')
  self.assertEqual(self.view(now=now)['summary']['net_profit_cad'],'0.9')
  self.assertEqual(self.view(month='2026-10',now=now)['summary']['net_profit_cad'],'1.9')
 def test_dst_repeated_hour_counted_once_each(self):
  self.add('SELL','11','0','1','2026-11-01T08:30:00+00:00')
  self.add('SELL','11','0','1','2026-11-01T09:30:00+00:00')
  v=self.view(month='2026-11',now=epoch('2026-11-02T12:00:00+00:00'))
  self.assertEqual(v['days'][0]['sell_count'],2);self.assertEqual(v['days'][0]['net_profit_cad'],'2')
 def test_no_latest_100_limit_and_read_is_idempotent(self):
  for _ in range(125):self.add('SELL','11','.1','.9','2026-09-29T20:00:00+00:00')
  before=self.repo.path.read_bytes();first=self.view();second=self.view()
  self.assertEqual(first,second);self.assertEqual(first['summary']['sell_count'],125)
  self.assertEqual(D(first['summary']['net_profit_cad']),D('112.5'))
  self.assertEqual(before,self.repo.path.read_bytes())
 def test_live_fx_partial_cost_basis_and_bonus_reconcile(self):
  self.add('BUY','100','1','0','2026-09-28T20:00:00+00:00',qty='2',rate='1.4')
  self.add('SELL','60','.6','9.49','2026-09-29T20:00:00+00:00',qty='1',rate='1.35')
  self.add('SELL','55','.55','0.085','2026-09-30T20:00:00+00:00',qty='1',rate='1.3')
  v=self.view('live');basis=D('101')*D('1.4')/2*D('1.0025')
  sale=v['days'][28]['trades'][0]
  net=D('9.49')-D('70.7')*D('.0025')-D('59.4')*D('1.35')*D('.0025')
  self.assertEqual(D(sale['cost_basis_cad']),basis)
  self.assertEqual(D(sale['net_profit_cad']),net)
  self.assertEqual(D(sale['sell_proceeds_cad'])-D(sale['cost_basis_cad']),net)
  with self.repo.db() as c:bonus=performance_bonus.realized_net(self.repo,c,'live')
  self.assertEqual(D(v['summary']['net_profit_cad']),bonus)
 def test_live_missing_entry_fx_is_unknown_not_zero(self):
  self.add('BUY','100','1','0','2026-09-28T20:00:00+00:00',rate='1.4',fx=None)
  self.add('SELL','110','1.1','11.06','2026-09-29T20:00:00+00:00',rate='1.4')
  v=self.view('live');self.assertFalse(v['summary']['verified'])
  self.assertIsNone(v['summary']['net_profit_cad']);self.assertIsNone(v['days'][28]['return_ratio'])
 def test_non_cad_legacy_paper_not_counted_as_cad(self):
  self.add('SELL','11','.1','.9','2026-09-29T20:00:00+00:00',currency='USD')
  self.assertFalse(self.view()['summary']['verified'])
 def test_parameters_cannot_select_sql_or_unbounded_dates(self):
  for month in ('2026-00','2026-13','2026-9','1999-01','2101-01',"2026-09'",'x',[]):
   with self.subTest(month=month),self.assertRaises(ValueError):self.view(month=month)
  with self.assertRaises(ValueError):self.view(mode='LIVE')
 def test_calendar_api_separates_ledgers_without_order_actions(self):
  class E:pass
  e=E();e.repo=self.repo
  with patch.object(service,'engine',return_value=e),patch.object(service,'live_engine',side_effect=AssertionError('LIVE touched')):
   v=service.get(None,'calendar',{'mode':'paper','month':'2026-09'})
   self.assertEqual(v['source'],'confirmed_fill_ledger')
   with self.assertRaises(ValueError):service.get(None,'calendar',{'mode':'both'})
