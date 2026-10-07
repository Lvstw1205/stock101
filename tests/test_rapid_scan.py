"""Real strategy calculations on temporary ledgers; no exchange writes or LLMs."""
import copy
import io
import json
import datetime as dt
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from stock101.crypto import agent_bridge, daily_target, execution_strategies, risk, sizing
from stock101.crypto.agent_research import atomic, DEFAULT, Manager
from stock101.crypto.multi import MultiEngine
from stock101.crypto.rapid_scan import RapidScanner, screening_book
from stock101.crypto.rapid_review import RapidReviewer, validate_selection, decode_indices
from test_execution_strategies import case, book, held, signal, NOW, RuleMarket
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto.intelligence import Intelligence
from concurrent.futures import ThreadPoolExecutor


class FullScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.adapter=Mock();self.adapter.fx.return_value={'rate':'1'}
        self.e=MultiEngine(Path(self.tmp.name)/'paper.sqlite3',self.adapter,execution_strategies.VERSION)
        self.e.start_multi();self.e.validation_evidence=lambda pid:{}
        h,d,b=case('range',NOW)
        self.products={f'C{i}-USD':dict(product_id=f'C{i}-USD',base_currency_id=f'C{i}') for i in range(160)}
        self.ticks={pid:dict(price=b['mid'],timestamp=NOW) for pid in self.products}
        self.e.intelligence=SimpleNamespace(lock=threading.RLock(),products=self.products,
            history={pid:dict(bars=h,fx_rate=1) for pid in self.products},
            daily={pid:dict(bars=d,fx_rate=1) for pid in self.products},
            stream=SimpleNamespace(snapshot=lambda:dict(self.ticks)),
            news=SimpleNamespace(context=lambda p:dict(status='CURRENT',allocation_multiplier=1,articles=[])),
            bitcoin_context=lambda now:None,fx_context=lambda now:None)
        self.scan=RapidScanner(self.e);self.e.rapid=self.scan
    def scan_now(self):
        with patch('time.time',return_value=NOW):self.scan.scan_once(NOW)
    def test_entire_universe_no_network_orders_or_per_coin_ledger_writes(self):
        self.scan_now();state=self.scan.state()
        self.assertEqual(state['universe'],160);self.assertEqual(state['evaluated'],160)
        self.assertEqual(len(self.scan.snapshot()),160)
        self.adapter.get_orderbook.assert_not_called();self.adapter.get_candles.assert_not_called()
        with self.e.repo.db() as c:
            self.assertEqual(self.e.repo.rows(c,'fills',10),[])
            self.assertEqual(self.e.repo.rows(c,'trade_decisions',10),[])
        self.assertTrue(all(len(r['strategies'])==16 for r in self.scan.snapshot().values()))
    def test_stale_missing_data_is_explicit_and_does_not_exclude_other_coins(self):
        self.ticks['C0-USD']['timestamp']=NOW-301;del self.ticks['C1-USD']
        del self.e.intelligence.history['C2-USD'];self.scan_now()
        self.assertEqual(self.scan.summary['evaluated'],157)
        self.assertEqual(self.scan.summary['skipped'],{'WAITING_FRESH_PRICE':2,'HISTORY_REFRESH_PENDING':1})
        self.assertEqual(self.scan.summary['universe'],160)
    def test_no_fabricated_fresh_time_and_screen_book_cannot_fill(self):
        b=screening_book('C0-USD',dict(price=100,timestamp=NOW-60),1,NOW)
        self.assertEqual(b['timestamp'],NOW-60);self.assertFalse(b['executable'])
        with self.assertRaisesRegex(ValueError,'SCREENING_PRICE_NOT_EXECUTABLE'):
            self.e.fill(None,None,{},b,None,'BUY',None,None,None)
    def test_real_rule_failure_is_visible_not_counted_as_success(self):
        with patch('stock101.crypto.execution_strategies.evaluate',side_effect=RuntimeError):self.scan_now()
        self.assertEqual(self.scan.summary['evaluated'],0)
        self.assertEqual(self.scan.summary['skipped'],{'SCREEN_ANALYSIS_RETRY':160})
    def test_queue_prioritizes_exits_and_rotates_blocked_entries(self):
        rows={}
        for i in range(20):
            pid=f'C{i}-USD';rows[pid]=dict(product=pid,status='SCREENED',decision='BUY',at=NOW,score=1-i/100)
        rows['C19-USD']['decision']='SELL';self.scan.rows=rows
        first=self.e.rapid_batch(list(rows),[],NOW)
        self.assertEqual(first[0],'C19-USD');self.assertLessEqual(len(first),8)
        for pid in first:self.e.latest[pid]={'at':NOW}
        second=self.e.rapid_batch(list(rows),[],NOW+4)
        self.assertTrue(set(second)-set(first))
        self.assertEqual(self.e.rapid_batch(list(rows),[],NOW+31),[])
    def test_scan_prices_rescale_saved_fx_before_strategy_evaluation(self):
        self.adapter.fx.return_value={'rate':'2'};captured=[]
        original=execution_strategies.evaluate
        def check(h,pos,b,**kw):
            captured.append((h[-1]['close'],float(b['mid']),kw['daily'][-1]['close']))
            return original(h,pos,b,**kw)
        with patch('stock101.crypto.execution_strategies.evaluate',side_effect=check):self.scan_now()
        self.assertEqual(captured[0],(200,184,200))
    def test_stopped_and_expired_scan_do_not_claim_running(self):
        self.scan_now()
        with patch('time.time',return_value=NOW+31):self.assertEqual(self.scan.state()['status'],'DELAYED')
        self.e.halt();self.assertEqual(self.scan.state()['status'],'STOPPED')


class RapidAITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=MultiEngine(Path(self.tmp.name)/'p.db',Mock(),execution_strategies.VERSION);self.e.start_multi()
        self.scan=RapidScanner(self.e);self.root=self.e.repo.path.parent/'tradingagents'
        atomic(self.root/'settings.json',{**DEFAULT,'auto_enabled':True})
        self.now=time.time();self.s=signal('range',now=self.now);p=self.s['plan'];pid='ALT-USD'
        self.scan.rows={pid:dict(product=pid,status='SCREENED',decision='BUY',at=self.now,price_at=self.now,
                                plan=p,strategies=self.s['strategy_candidates'],score=.7)}
        self.completion=Mock(return_value={'selected':[{'product':pid,'strategy_id':'range'}],'avoid':[]})
        self.review=RapidReviewer(self.scan,self.completion);self.scan.reviewer=self.review
    def test_valid_batch_writes_scoped_opinion_and_applies_to_qualified_entry(self):
        self.review.review_once(self.now)
        b=book(92,self.now,'ALT-USD')
        self.assertEqual(agent_bridge.selected_entry(self.s,None,b,self.e.repo.path.parent,self.now+1),'range')
        out=agent_bridge.apply(self.s,None,b,self.e.repo.path.parent,now=self.now+1)
        self.assertEqual(out['decision'],'BUY');self.assertTrue(out['plan']['ai_team']['applied'])
        self.assertIn('AGENTS_SELECTED_ENTRY',out['reason_codes'])
        self.assertEqual(self.review.priorities(self.now+1),{'ALT-USD':0})
    def test_ai_cannot_select_unknown_strategy_duplicate_or_symbol(self):
        entries=[dict(product='ALT-USD',choices=[dict(strategy_id='range')])]
        for payload in ({'selected':[dict(product='ALT-USD',strategy_id='invented')],'avoid':[]},
                        {'selected':[dict(product='ALT-USD',strategy_id='range')],'avoid':['ALT-USD']},
                        {'selected':[],'avoid':['../SECRET']}, {'selected':[]}, {'selected':None,'avoid':[]}):
            with self.subTest(payload=payload),self.assertRaises(ValueError):validate_selection(payload,entries)
    def test_index_selection_rejects_unknown_duplicate_and_boolean_ids(self):
        entries=[dict(product='ALT-USD',choices=[dict(strategy_id='range')]),dict(product='BTC-USD',choices=[dict(strategy_id='mayer')])]
        options=[dict(product='ALT-USD',strategy_id='range'),dict(product='BTC-USD',strategy_id='mayer')]
        self.assertEqual(decode_indices({'selected':[1],'avoid':[0]},entries,options),{'selected':[options[1]],'avoid':['ALT-USD']})
        for response in ({'selected':[2],'avoid':[]},{'selected':[True],'avoid':[]},{'selected':[0,0],'avoid':[]},{'selected':[0],'avoid':[0]}):
            with self.assertRaises(ValueError):decode_indices(response,entries,options)
    def test_model_conflicting_buys_are_removed_without_inventing_recommendations(self):
        from stock101.crypto.rapid_review import normalize_choices
        entries=[dict(product='ALT-USD',choices=[dict(strategy_id='range'),dict(strategy_id='mayer')]),dict(product='BTC-USD',choices=[dict(strategy_id='mayer')])]
        options=[dict(product='ALT-USD',strategy_id='range'),dict(product='ALT-USD',strategy_id='mayer'),dict(product='BTC-USD',strategy_id='mayer')]
        out=normalize_choices({'selected':[0,1,2],'avoid':[]},entries,options)
        self.assertEqual(out,dict(selected=[options[2]],avoid=[]))
        out=normalize_choices({'selected':[0,2],'avoid':[0,0]},entries,options)
        self.assertEqual(out,dict(selected=[options[2]],avoid=['ALT-USD']))
        for payload in ({'selected':[3],'avoid':[]},{'selected':[True],'avoid':[]},{'selected':[0],'avoid':[6]}):
            with self.assertRaises(ValueError):normalize_choices(payload,entries,options)
    def test_failed_batch_does_not_starve_next_candidates(self):
        self.completion.side_effect=ValueError('RAPID_AI_UNKNOWN_CANDIDATE')
        with self.assertRaises(ValueError):self.review.review_once(self.now)
        self.assertEqual(self.review.attempted['ALT-USD'],self.now)
        self.review.review_once(self.now+1)
        self.assertEqual(self.completion.call_count,1)
    def test_current_cost_gate_token_age_and_price_must_still_match(self):
        self.review.review_once(self.now);b=book(92,self.now,'ALT-USD')
        for mutation in ('rule','token','price','age'):
            s=copy.deepcopy(self.s);quote=copy.deepcopy(b);now=self.now+1
            if mutation=='rule':
                for c in s['strategy_candidates']:c['decision']='WAIT'
            if mutation=='token':
                for c in s['strategy_candidates']:c['entry_token']='different'
            if mutation=='price':quote['mid']='200'
            if mutation=='age':now=self.now+181
            self.assertIsNone(agent_bridge.selected_entry(s,None,quote,self.e.repo.path.parent,now))
        waiting={'decision':'WAIT','reason_codes':['COST_CHECK'],'plan':{}}
        self.assertEqual(agent_bridge.apply(waiting,None,b,self.e.repo.path.parent,now=self.now+1)['decision'],'WAIT')
        self.assertEqual(agent_bridge.apply(self.s,None,b,self.e.repo.path.parent,'live',self.now+1),self.s)
    def test_full_report_busy_never_invokes_extra_model_and_disabled_priority_is_empty(self):
        atomic(self.root/'jobs/job/status.json',dict(status='RUNNING',updated_at=self.now))
        self.review.review_once(self.now);self.completion.assert_not_called()
        self.assertEqual(self.review.state()['status'],'WAITING_DEEP_REPORT')
        self.review.selections={'ALT-USD':(self.now,0)}
        atomic(self.root/'settings.json',{**DEFAULT,'auto_enabled':False})
        self.assertEqual(self.review.priorities(self.now),{})
    def test_timeout_or_bad_json_never_changes_ledger(self):
        self.completion.side_effect=TimeoutError
        with self.assertRaises(TimeoutError):self.review.review_once(self.now)
        self.assertFalse((self.root/'fast_opinions/paper/ALT-USD.json').exists())
        with self.e.repo.db() as c:self.assertEqual(self.e.repo.rows(c,'fills'),[])
    def test_hourly_automatic_report_replaced_manual_start_remains_available(self):
        manager=Manager(SimpleNamespace(ROOT=Path(self.tmp.name),DATA=Path(self.tmp.name)))
        manager.config=lambda:{**DEFAULT,'auto_enabled':True};manager.start=Mock();self.e.rapid=self.scan
        manager.maybe_auto(self.e);manager.start.assert_not_called()
    def test_ai_selected_alternative_flows_through_real_paper_fill_and_token_guard(self):
        market=RuleMarket('mayer')
        adapter=CadPaperAdapter(market,lambda:dict(rate='1',date=dt.date.today().isoformat()))
        engine=MultiEngine(Path(self.tmp.name)/'entry.db',adapter,execution_strategies.VERSION)
        engine.start_multi();pid=market.pid;quote=adapter.get_orderbook(pid)
        with engine.repo.db() as c:anchor=engine.repo.get(c,'cad_plan')['anchor']
        original=execution_strategies.evaluate(adapter.get_candles(pid),None,quote,daily=adapter.get_daily_candles(pid),anchor=anchor)
        self.assertEqual(original['strategy_id'],'mayer')
        choice=next(c for c in original['strategy_candidates'] if c['strategy_id']=='dca_weekly')
        self.assertEqual(choice['decision'],'BUY')
        atomic(self.root/'fast_opinions/paper'/f'{pid}.json',dict(ticker=pid,asset_type='crypto',mode='paper',source='rapid_candidate_review',
            date=dt.date.today().isoformat(),created_at=self.now,reference_at=quote['timestamp'],reference_price=quote['mid'],
            rating='Buy',strategy_id='dca_weekly',entry_token=choice['entry_token']))
        result=engine.decide(pid,execution_strategies.VERSION)
        self.assertEqual(result['analysis']['strategy_id'],'dca_weekly')
        self.assertTrue(result['analysis']['plan']['ai_team']['applied'])
        self.assertEqual(result['execution']['side'],'BUY')
        self.assertLessEqual(float(result['execution']['notional']),2)
        again=engine.decide(pid,execution_strategies.VERSION)
        self.assertTrue(again.get('duplicate'))
        with engine.repo.db() as c:self.assertEqual(len(engine.repo.rows(c,'fills')),1)


class EntryRateTests(unittest.TestCase):
    def test_paper_sells_do_not_use_entry_quota_live_defaults_unchanged(self):
        context=dict(equity='1000',cash='500',invested='500',asset_value='50',halted=False,age=0,
                     spread_bps='2',daily_loss='0',drawdown='0',consecutive_losses=0,since_loss=99999,
                     trades_hour=40,buys_hour=30,deployed_today='0')
        limits={**sizing.LIMITS,'trades_per_hour':30,'entry_rate_only':True}
        self.assertEqual(risk.assess(30,'SELL',context,limits,paper_observation=True)['status'],'PASS')
        self.assertEqual(risk.assess(30,'BUY',context,limits,paper_observation=True)['reason'],'TRADE_FREQUENCY_LIMIT')
        self.assertEqual(risk.assess(30,'SELL',context,sizing.LIMITS)['reason'],'TRADE_FREQUENCY_LIMIT')
        context.update(buys_hour=0,cash='400',invested='600')
        self.assertEqual(risk.assess(30,'BUY',context,limits,paper_observation=True)['reason'],'EXPOSURE_OR_CASH_LIMIT')


class HistoryRefreshTests(unittest.TestCase):
    def test_parallel_tail_fetch_preserves_history_and_isolates_bad_coin(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=Mock();adapter.fx_loader=None
            engine=MultiEngine(Path(tmp)/'p.db',adapter)
            intel=Intelligence(adapter,engine.repo);now=time.time();hour=int(now)//3600*3600
            h=case('range',now)[0];previous=h[:-1]
            intel.products={f'C{i}-CAD':{} for i in range(5)}
            for pid in intel.products:intel.history[pid]={'bars':previous,'fx_rate':1}
            barrier=threading.Barrier(4);calls=[]
            def fetch(pid,**kw):
                calls.append((pid,kw));barrier.wait(timeout=3)
                if pid=='C1-CAD':raise ValueError('STALE_CANDLES')
                return h[-4:]
            adapter.get_candles.side_effect=fetch
            with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(intel.refresh_history_batch(pool),4)
            self.assertEqual(len(calls),4)
            self.assertTrue(all(k['start']==previous[-1]['start']-7200 and k['end']==hour for _,k in calls))
            self.assertEqual(intel.history['C0-CAD']['bars'],h)
            self.assertEqual(intel.errors['C1-CAD']['reason'],'STALE_CANDLES')
            self.assertEqual(intel.history['C1-CAD']['bars'],previous)
            self.assertNotIn('C4-CAD',intel.attempted)
    def test_scalar_index_preserves_exact_accounting_values_and_avoids_payload_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=MultiEngine(Path(tmp)/'p.db',Mock())
            with engine.repo.db() as c:
                value='987.123456789123456789123456789'
                engine.repo.put(c,'portfolio_snapshots','one',{'currency':'CAD','equity':value,'initial':'1000','positions':[{'large':'x'*10000}]})
                rows=engine.repo.equity_series(c,'CAD')
                self.assertEqual(rows[0]['equity'],value)
                query="SELECT created,json_extract(payload,'$.equity'),json_extract(payload,'$.initial') FROM portfolio_snapshots WHERE json_extract(payload,'$.currency')=? ORDER BY created DESC LIMIT 100000"
                plan=' '.join(str(tuple(r)) for r in c.execute('EXPLAIN QUERY PLAN '+query,('CAD',)))
                self.assertIn('portfolio_risk_scalars',plan);self.assertNotIn('TEMP B-TREE',plan)
                # SQLite may omit the word COVERING for expression indexes;
                # its bytecode must still read scalars solely from index cursor 2.
                program=list(c.execute('EXPLAIN '+query,('CAD',)))
                self.assertFalse(any(r[1]=='Function' and 'json_extract' in str(r[5]) for r in program))


class DailyTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.e=MultiEngine(Path(self.tmp.name)/'p.db',Mock())
        self.now=dt.datetime(2026,10,2,12,tzinfo=daily_target.ZONE).timestamp()
    def test_first_midday_measurement_is_partial_reward_excluded_no_fake_profit(self):
        with self.e.repo.db() as c:
            daily_target.observe(self.e.repo,c,'paper',self.now,1000,self.now,0)
            daily_target.observe(self.e.repo,c,'paper',self.now+5,1020,self.now+5,20)
            s=daily_target.state(self.e.repo,c,'paper',self.now+5)
            self.assertTrue(s['partial_day']);self.assertEqual(s['net_profit_cad'],'0')
            self.assertFalse(s['reached']);self.assertFalse(s['guaranteed'])
            daily_target.observe(self.e.repo,c,'paper',self.now+10,1120,self.now+10,20)
            s=daily_target.state(self.e.repo,c,'paper',self.now+10)
            self.assertTrue(s['reached']);self.assertEqual(s['target_profit_cad'],'100.00')
    def test_vancouver_day_rollover_has_correct_baseline_and_stale_mark_is_rejected(self):
        midnight=dt.datetime(2026,10,3,tzinfo=daily_target.ZONE).timestamp()
        with self.e.repo.db() as c:
            daily_target.observe(self.e.repo,c,'paper',midnight-5,1000,midnight-5,0)
            daily_target.observe(self.e.repo,c,'paper',midnight+5,990,midnight+5,0)
            s=daily_target.state(self.e.repo,c,'paper',midnight+5)
            self.assertFalse(s['partial_day']);self.assertEqual(s['net_profit_cad'],'-10')
            daily_target.observe(self.e.repo,c,'paper',midnight+100,1100,midnight+5,0)
            self.assertEqual(daily_target.state(self.e.repo,c,'paper',midnight+100)['net_profit_cad'],'-10')
            self.assertFalse(daily_target.state(self.e.repo,c,'paper',midnight+100)['fresh'])


if __name__=='__main__':unittest.main()
