import copy,datetime as dt,email.utils,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
from test_paper_execution import Markets
from stock101.crypto import planner
from stock101.crypto.multi import MultiEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto.news import parse,relevant,NewsResearch
from stock101.crypto.realtime import MarketStream
from stock101.crypto.intelligence import Intelligence

class HistoricalMarket(Markets):
 def __init__(self):
  super().__init__();self.history=super().get_candles('AAA-USD')
 def get_candles(self,p):return copy.deepcopy(self.history)

class PlanningTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.m=HistoricalMarket()
  self.a=CadPaperAdapter(self.m,lambda:{'rate':'1.37','date':dt.datetime.now(dt.timezone.utc).date().isoformat()})
  self.e=MultiEngine(Path(self.tmp.name)/'p.db',self.a);self.e.start_multi()
 def tearDown(self):self.tmp.cleanup()
 def decision(self):return self.e.decide('AAA-USD',planner.VERSION)
 def test_same_hour_new_price_can_enter_and_exit(self):
  self.m.factor=.97;first=self.decision();self.assertIsNone(first['execution'])
  self.m.factor=1;second=self.decision();self.assertEqual(second['execution']['side'],'BUY')
  self.assertEqual(first['signal_bar'],second['signal_bar']);self.assertNotEqual(first['decision_id'],second['decision_id'])
  self.m.factor=.90;third=self.decision();self.assertEqual(third['execution']['side'],'SELL')
  self.assertEqual(third['analysis']['reason_codes'],['PLAN_INVALIDATED'])
  with self.e.repo.db() as c:self.e.reconcile_ledger(c)
 def test_identical_quote_does_not_duplicate_order(self):
  stamp=time.time()
  original=self.a.get_orderbook
  with patch.object(self.a,'get_orderbook',side_effect=lambda p:{**original(p),'timestamp':stamp}):
   a=self.decision();b=self.decision()
  self.assertEqual(a['decision_id'],b['decision_id']);self.assertTrue(b['duplicate']);self.assertEqual(self.e.plan_state()['trade_count'],1)
 def test_stop_arriving_during_market_read_prevents_fill(self):
  original=self.a.get_orderbook
  def stopped(p):
   row=original(p);self.e._paper_stop.set();return row
  with patch.object(self.a,'get_orderbook',side_effect=stopped):r=self.decision()
  self.assertIsNone(r['execution']);self.assertEqual(self.e.plan_state()['trade_count'],0)
 def test_news_cannot_create_positive_edge(self):
  bars=self.a.get_candles('AAA-USD');book=self.a.get_orderbook('AAA-USD')
  good=planner.evaluate(bars,None,book,news={'status':'CURRENT','allocation_multiplier':1,'articles':[]})
  event=planner.evaluate(bars,None,book,news={'status':'CURRENT','allocation_multiplier':.5,'articles':[{'title':'hack'}]})
  self.assertEqual(good['net_edge'],event['net_edge']);self.assertEqual(event['allocation'],good['allocation']/2)
 def test_high_cost_blocks_even_with_uptrend(self):
  book=self.a.get_orderbook('AAA-USD');book['asks'][0]['price']=str(float(book['asks'][0]['price'])*1.2)
  signal=planner.evaluate(self.a.get_candles('AAA-USD'),None,book)
  self.assertEqual(signal['decision'],'WAIT');self.assertIn('NET_EDGE_TOO_SMALL',signal['reason_codes'])
 def test_model_has_prefix_only_nonoverlapping_validation(self):
  rows=self.a.get_candles('AAA-USD');original=planner.prediction;lengths=[]
  def record(closes,model,horizon=6):lengths.append(len(closes));return original(closes,model,horizon)
  with patch.object(planner,'prediction',side_effect=record):f=planner.forecast(rows)
  prefixes=sorted(set(lengths)-{len(rows)})
  self.assertTrue(prefixes);self.assertTrue(all(n<=len(rows)-6 for n in prefixes))
  self.assertTrue(all(b-a==6 for a,b in zip(prefixes,prefixes[1:])))
  self.assertGreaterEqual(f['validation']['samples'],4);self.assertLess(f['lower'],f['center']);self.assertGreater(f['upper'],f['center'])
 def test_no_blanket_conflict_or_volatility_veto(self):
  rows=self.a.get_candles('AAA-USD')
  for b in rows:b.update(high=b['close']*1.06,low=b['close']*.94)
  signal=planner.evaluate(rows,None,self.a.get_orderbook('AAA-USD'))
  self.assertLess(signal['allocation'],.02);self.assertNotIn('VOLATILITY_VETO',signal['reason_codes']);self.assertNotIn('STRATEGY_DISAGREEMENT',signal['reason_codes'])
  self.assertIn('target',signal['plan']);self.assertIn('invalidation',signal['plan'])
 def test_whole_universe_not_top_twenty(self):
  def products():return [{**self.m.get_product(f'C{i:03d}-USD'),'base_currency_id':f'C{i:03d}'} for i in range(45)]
  with patch.object(self.m,'get_products',side_effect=products):
   for _ in range(12):self.e.cycle()
  self.assertEqual(self.e.scan['eligible'],45);self.assertEqual(len(self.e.latest),45)
 def test_old_forecast_available_for_exit_but_not_new_entry(self):
  intelligence=Intelligence(self.a,self.e.repo);rows=self.a.get_candles('AAA-USD')
  for b in rows:b['start']-=3600
  intelligence.history['AAA-USD']={'bars':rows,'forecast':planner.forecast(rows)}
  self.assertIsNone(intelligence.get('AAA-USD'));self.assertIsNotNone(intelligence.get('AAA-USD',allow_stale=True))
 def test_analysis_uses_selected_planner_not_legacy_votes(self):
  data=self.e.analyze('AAA-USD',planner.VERSION);self.assertEqual(data['analysis']['strategy_version'],planner.VERSION);self.assertIn('plan',data['analysis'])
 def test_position_keeps_original_plan(self):
  result=self.decision()
  with self.e.repo.db() as c:pos=self.e.repo.one(c,'positions','AAA-USD')
  self.assertEqual(pos['entry_plan']['target'],result['analysis']['plan']['target'])
 def test_profit_exit_can_close_whole_position_above_entry_cap(self):
  self.decision();self.m.factor=1.10
  result=self.decision();self.assertEqual(result['execution']['side'],'SELL')
  with self.e.repo.db() as c:
   pos=self.e.repo.one(c,'positions','AAA-USD');self.assertEqual(Decimal(pos['quantity']),0);self.e.reconcile_ledger(c)

class StreamAndNewsTests(unittest.TestCase):
 def test_history_can_use_real_contiguous_tail_without_filling_gaps(self):
  from stock101.crypto.exchange import CoinbaseAdapter,ExchangeError
  end=int(time.time())//3600*3600
  rows=[{'start':str(end-i*3600),'open':'100','high':'101','low':'99','close':'100','volume':'1'} for i in range(1,351) if i!=150]
  a=CoinbaseAdapter(transport=lambda *args:{'candles':rows})
  with self.assertRaises(ExchangeError):a.get_candles('BTC-USD')
  tail=a.get_candles('BTC-USD',contiguous_tail=True)
  self.assertEqual(len(tail),149);self.assertEqual(tail[0]['start'],end-149*3600)
  self.assertTrue(all(b['start']-x['start']==3600 for x,b in zip(tail,tail[1:])))
 def test_live_profit_exit_does_not_apply_buy_cash_reserve(self):
  from test_live import Broker,FX
  from stock101.crypto.live import LiveEngine
  with tempfile.TemporaryDirectory() as tmp:
   market=HistoricalMarket();broker=Broker(market);e=LiveEngine(Path(tmp)/'live.db',broker,market,FX())
   e.configure({'quote':'USD','costs_confirmed':True,'enable_live':True});e.activate({'confirmation':'실제 돈으로 자동매매 시작','confirmed':True})
   e.cycle();e.reconcile();market.factor=1.1;e.cycle();e.reconcile()
   with e.repo.db() as c:self.assertEqual(Decimal(e.repo.one(c,'positions','AAA-USD')['quantity']),0)
   self.assertTrue(e.audit_ledger())
 def test_ticks_use_exchange_time_and_drop_old_updates(self):
  s=MarketStream();s.products={'BTC-USD'};now=time.time()
  def msg(stamp,price):return {'channel':'ticker_batch','timestamp':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat(),'events':[{'tickers':[{'product_id':'BTC-USD','price':price}]}]}
  s.observe(msg(now,'100'),now);s.observe(msg(now-1,'90'),now)
  self.assertEqual(s.snapshot()['BTC-USD']['price'],'100')
  with self.assertRaisesRegex(ValueError,'STALE'):s.observe(msg(now-60,'10'),now)
 def test_unsubscribed_products_never_schedule_execution(self):
  s=MarketStream();s.products={'ETH-USD'};now=time.time()
  s.observe({'channel':'ticker_batch','timestamp':dt.datetime.fromtimestamp(now,dt.timezone.utc).isoformat(),'events':[{'tickers':[{'product_id':'BTC-USD','price':'100'}]}]},now)
  self.assertEqual(s.snapshot(),{})
 def feed(self,stamp,title='Bitcoin exchange hack',link='https://example.com/news'):
  return f'<rss><channel><item><title>{title}</title><link>{link}</link><pubDate>{email.utils.formatdate(stamp,usegmt=True)}</pubDate></item></channel></rss>'.encode()
 def test_freshness_and_safe_links(self):
  now=time.time();rows=parse(self.feed(now-120),'test',now);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['event_keywords'],['hack'])
  self.assertEqual(parse(self.feed(now-90000),'test',now),[])
  self.assertEqual(parse(self.feed(now+60),'test',now),[])
  self.assertEqual(parse(self.feed(now-10,link='javascript:alert(1)'),'test',now),[])
  with self.assertRaises(ValueError):parse(b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss/>','test',now)
 def test_coin_relevance_and_ambiguous_tickers(self):
  self.assertTrue(relevant({'title':'Bitcoin adoption rises'},'BTC'))
  self.assertFalse(relevant({'title':'One step forward for crypto'},'ONE'))
  self.assertTrue(relevant({'title':'Harmony (ONE) outage'},'ONE'))
 def test_failed_news_source_is_visible(self):
  from stock101.crypto.repository import Repository
  with tempfile.TemporaryDirectory() as tmp:
   n=NewsResearch(Repository(Path(tmp)/'p.db'),lambda _:(_ for _ in ()).throw(TimeoutError()))
   n.collect('test','https://example.com');self.assertFalse(n.state()['sources']['test']['ok'])
   ctx=n.context({'base_currency_id':'BTC'});self.assertEqual(ctx['status'],'UNAVAILABLE_OR_STALE');self.assertEqual(ctx['allocation_multiplier'],.75)

if __name__=='__main__':unittest.main()
