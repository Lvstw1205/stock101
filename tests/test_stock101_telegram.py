import contextlib,copy,importlib.util,json,sqlite3,sys,tempfile,time,types,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from stock101.crypto import telegram_view as view,execution_strategies as rules,service
from stock101.crypto.multi import MultiEngine
from stock101.crypto.live import LiveEngine
from test_execution_strategies import RuleMarket
from test_live import Broker,FX

def state():
 return dict(mode='paper',running=True,reason=None,equity='121.21',cash='90',capital='100',realized='10.1',unrealized='11.11',
  marked_at=time.time(),completed_at=time.time(),last_assessment_at=time.time(),last_product='BTC-USD',last_reason='STRATEGY_HOLD',
  positions=[],fills=[],fill_count=0,now=time.time())

class ViewTests(unittest.TestCase):
 def test_every_screen_is_bounded_and_buttons_are_read_only(self):
  for mode in view.MODES:
   for page in view.VIEWS:
    s=state();s['mode']=mode;r=view.render(s,page)
    self.assertLess(len(r['text'].encode('utf-16-le'))//2,3400);self.assertTrue(r['protect_content'])
    for row in r['reply_markup']['inline_keyboard']:
     for b in row:
      self.assertRegex(b['callback_data'],r'^st101:(paper|live):(status|positions|fills|strategies)$')
      self.assertLessEqual(len(b['callback_data'].encode()),64)
 def test_snapshot_is_read_only_and_never_calls_market_or_orders(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();e=MultiEngine(Path(tmp)/'p.db',m,strategy_version=rules.VERSION)
   with e.repo.db() as c:
    c.execute("INSERT INTO paper_accounts VALUES('CAD','100','100') ON CONFLICT(currency) DO NOTHING")
    e.repo.put(c,'portfolio_snapshots','mark',dict(equity='100',cash='100',unrealized='0',positions=[]))
    before=list(c.iterdump())
   with patch.object(m,'get_orderbook',side_effect=AssertionError('market called')):
    r=view.response(e,'paper');self.assertIn('100.00 CAD',r['text']);self.assertIn('모의매매',r['text'])
   with e.repo.db() as c:self.assertEqual(list(c.iterdump()),before)
 def test_live_offline_view_does_not_check_credentials_or_activate(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=RuleMarket();br=Broker(m);e=LiveEngine(Path(tmp)/'live.db',br,m,FX(),strategy_version=rules.VERSION)
   with patch.object(br,'permissions',side_effect=AssertionError('credentials called')):
    r=view.response(e,'live');self.assertIn('비활성',r['text']);self.assertIn('예산 한도는 Coinbase 잔액이 아닙니다',r['text'])
   self.assertFalse(e.enabled());self.assertEqual(br.sent,[])
 def test_financial_values_are_distinct_and_stale_is_visible(self):
  s=state();s['marked_at']=time.time()-900;s['running']=False;s['reason']='SECRET_should_not_leak'
  text=view.render(s)['text'];self.assertIn('확정 손익: 10.10 CAD',text);self.assertIn('평가 손익: 11.11 CAD',text)
  self.assertIn('누적 가상 입금: 100.00 CAD',text);self.assertIn('최신 평가액을 확인하지 못했습니다',text);self.assertNotIn('SECRET',text)
 def test_trade_shows_fee_inclusive_debit_and_net_credit(self):
  s=state();s['fills']=[dict(product='BTC-USD',side=side,notional='10',fee='.1',created=time.time(),currency='CAD',strategy_id='dca_weekly') for side in ('BUY','SELL')]
  text=view.render(s,'fills')['text'];self.assertIn('총 지출 10.10 CAD',text);self.assertIn('순 수령 9.90 CAD',text)
 def test_invalid_mode_or_view_never_reads_ledger(self):
  e=Mock()
  for mode,page in [('../../data','status'),('paper','buy'),('live','start')]:
   with self.assertRaises(ValueError):view.response(e,mode,page)
  e.repo.db.assert_not_called()
 def test_service_route_uses_existing_engine_without_action(self):
  e=Mock()
  with patch.object(service,'engine',return_value=e),patch.object(view,'response',return_value={'text':'safe'}) as render,patch.object(service,'live_engine') as live:
   self.assertEqual(service.get(None,'telegram',dict(mode='paper',view='fills')),{'text':'safe'})
   render.assert_called_once_with(e,'paper','fills');live.assert_not_called()

