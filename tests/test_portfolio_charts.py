import datetime as dt
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from stock101.crypto.repository import Repository
from stock101.crypto import portfolio_charts as charts, service


def epoch(iso):return dt.datetime.fromisoformat(iso).timestamp()
NOW=epoch('2026-10-02T20:00:00+00:00')

class PortfolioChartsTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.repo=Repository(Path(self.tmp.name)/'paper.db');self.n=0
 def put(self,table,payload,created):
  self.n+=1
  with self.repo.db() as c:
   c.execute(f'INSERT INTO {table} VALUES(?,?,?)',(str(self.n),created,json.dumps(payload)))
 def mark(self,equity,initial,created,currency='CAD'):
  self.put('portfolio_snapshots',dict(equity=equity,initial=initial,currency=currency),created)
 def sell(self,net,created,currency='CAD'):
  self.put('fills',dict(side='SELL',product='BTC-USD',quantity='.1',notional='101',fee='1',realized=net,currency=currency),created)
 def test_positive_realized_does_not_mask_negative_account_return(self):
  self.mark('967.49','1000',NOW-5);self.sell('2.02',NOW-10)
  v=charts.view(self.repo,now=NOW)
  self.assertEqual(v['history'][0]['net_profit_cad'],'-32.51')
  self.assertEqual(v['history'][0]['return_ratio'],'-0.03251')
  self.assertEqual(v['daily'][-1]['net_profit_cad'],'2.02')
  self.assertEqual(v['daily'][-1]['cost_basis_cad'],'97.98')
 def test_cash_injections_and_bonus_are_not_profit(self):
  self.mark('102','100',NOW-20);self.mark('1002','1000',NOW-10);self.mark('1022','1020',NOW-5)
  v=charts.view(self.repo,now=NOW)
  self.assertEqual([D(r['net_profit_cad']) for r in v['history']],[D(2)]*3)
  self.assertTrue(all(r['net_profit_cad'] is None for r in v['daily']))
 def test_empty_and_invalid_marks_are_unknown_not_zero(self):
  v=charts.view(self.repo,now=NOW);self.assertEqual(v['history'],[]);self.assertIsNone(v['last_at'])
  for value in [None,'NaN','Infinity','bad']:self.mark(value,'1000',NOW-5)
  self.mark('1000','0',NOW-4)
  v=charts.view(self.repo,now=NOW);self.assertEqual(v['invalid_marks'],5);self.assertEqual(v['history'],[])
 def test_window_currency_order_and_future_filter(self):
  self.mark('900','1000',NOW-86500);self.mark('1000','1000',NOW-500);self.mark('1100','1000',NOW-10)
  self.mark('5000','5000',NOW-5,'USD');self.mark('1200','1000',NOW+2);self.sell('40',NOW+1)
  v=charts.view(self.repo,now=NOW)
  self.assertEqual([r['equity'] for r in v['history']],['1000','1100'])
  self.assertEqual(v['daily'][-1]['status'],'before_records')
  self.assertEqual(charts.view(self.repo,'7',now=NOW)['record_count'],3)
 def test_daily_timezone_and_cost_weighted_returns(self):
  self.sell('2',epoch('2026-10-01T23:59:59+00:00'));self.sell('-4',epoch('2026-10-02T00:00:00+00:00'))
  self.sell('1',NOW-10)
  v=charts.view(self.repo,now=NOW)
  self.assertEqual(v['daily'][-2]['date'],'2026-10-01');self.assertEqual(v['daily'][-2]['net_profit_cad'],'2')
  today=v['daily'][-1];self.assertEqual(today['net_profit_cad'],'-3');self.assertEqual(today['sell_count'],2)
  self.assertEqual(D(today['return_ratio']),D(-3)/D(203));self.assertEqual(today['status'],'today')
 def test_unknown_exit_does_not_become_zero_profit(self):
  self.sell('1',NOW-10,'USD');v=charts.view(self.repo,now=NOW)
  self.assertIsNone(v['daily'][-1]['net_profit_cad']);self.assertFalse(v['daily'][-1]['verified'])
 def test_downsample_preserves_peaks_endpoints_and_gap(self):
  points=[dict(timestamp=float(i),equity=str(1000+i%30),initial='1000',net_profit_cad=str(i%30)) for i in range(10000)]
  points[24]['equity']='600';points[24]['net_profit_cad']='-400'
  points[25]['equity']='1400';points[25]['net_profit_cad']='400'
  sampled=charts.downsample(points,0,10000)
  self.assertLessEqual(len(sampled),600)
  for p in [points[0],points[-1],points[24],points[25]]:self.assertIn(p,sampled)
  points=[dict(timestamp=i,equity='1000',initial='1000',net_profit_cad='0') for i in [0,1,1000,1001]]
  sampled=charts.downsample(points,0,1001);self.assertTrue(sampled[2]['gap_before'])
 def test_truncation_explains_missing_period(self):
  for i in range(8):self.mark(str(1000+i),'1000',NOW-100+i)
  with patch.object(charts,'MAX_RECORDS',3):v=charts.view(self.repo,now=NOW)
  self.assertTrue(v['truncated']);self.assertEqual(v['record_count'],3)
  self.assertEqual(v['history'][0]['equity'],'1005')
 def test_read_only_ledger_and_api_never_touch_live_or_orders(self):
  self.mark('967.49','1000',NOW-1);before=self.repo.path.read_bytes()
  with patch.object(service,'engine',return_value=SimpleNamespace(repo=self.repo)),patch.object(service,'live_engine',side_effect=AssertionError('LIVE accessed')):
   v=service.get(None,'portfolio-charts',{'days':'7'})
  self.assertEqual(v['source'],'primary_paper_ledger');self.assertFalse(v['includes_research_accounts'])
  self.assertEqual(before,self.repo.path.read_bytes())
 def test_no_unbounded_or_sql_range(self):
  for days in ['0','2','365','-1',"1' OR 1=1",None,[],1.5]:
   with self.subTest(days=days),self.assertRaises(ValueError):charts.view(self.repo,days,now=NOW)
