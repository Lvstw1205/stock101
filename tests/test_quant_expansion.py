"""Additive migration, causal research, durable jobs and isolated PAPER execution."""
import copy,json,math,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from stock101.crypto import validation as base,validation_job,execution_strategies as rules
from stock101.crypto.quant import specs,validation,portfolio,research
from stock101.crypto.quant.store import Store,identifier
from stock101.crypto.quant.manager import Manager,code_hash
from stock101.crypto.quant.shadow import ShadowEngine,CachedPublicMarket,observation
from stock101.crypto.quant.worker import main as worker
from stock101.crypto.multi import MultiEngine
from stock101.crypto.paper_fx import CadPaperAdapter
from stock101.crypto.agent_research import atomic,read
from test_execution_strategies import candles,case,RuleMarket,NOW


def spec(**kw):return specs.validate({'strategy_id':'range','products':['BTC-USD'],**kw})
def snapshot(n=350):
    a=dict(hourly=candles(NOW,3600,n),daily=candles(NOW,86400,350),foreign=False,reference_fx=1,
           base_increment='.000001',base_min_size='.000001',base_max_size='100000',quote_min_size='.01',quote_max_size='10000000')
    return dict(assets={'BTC-USD':a},fx_history=None,captured_at=NOW)

class SpecTests(unittest.TestCase):
    def test_closed_schema_rejects_code_unknown_bounds_nonfinite_and_malformed_products(self):
        cases=[dict(code='__import__("os")'),dict(strategy_id='not-a-rule'),dict(strategy_id=[]),dict(parameters={'leverage':10}),dict(parameters={'minimum_net_return':.001}),dict(parameters={'allocation_scale':True}),dict(parameters={'allocation_scale':float('nan')}),dict(products=[{}]),dict(products=['../file']),dict(products=['BTC-USD']*2)]
        for kw in cases:
            with self.subTest(kw=kw),self.assertRaises(ValueError):specs.validate(dict(strategy_id='range',**kw) if 'strategy_id' not in kw else kw)
    def test_neighborhood_bounded_unique_and_input_not_mutated(self):
        s=spec();before=copy.deepcopy(s);variants=specs.neighbors(s)
        self.assertEqual(s,before);self.assertLessEqual(len(variants),9);self.assertEqual(len(variants),len({specs.digest(x) for x in variants}))
        for v in variants:self.assertEqual(specs.validate(v),v)
    def test_evaluator_reuses_rule_and_reduces_size_without_raising_risk(self):
        h,d,b=case('range');s=spec(parameters={'allocation_scale':.5});e=specs.evaluator(s)
        actual=e(h,None,b,daily=d);original=rules.evaluate(h,None,b,daily=d,only_strategy='range')
        self.assertEqual(actual['decision'],original['decision']);self.assertEqual(actual['allocation'],original['allocation']/2)
        self.assertEqual(actual['plan']['research_spec_hash'],specs.digest(s))
    def test_source_templates_and_ai_critic_are_truthful(self):
        r=research.generate(dict(use_ai=False,products=['BTC-USD'],context={}))
        self.assertEqual(len(r['proposals']),3);self.assertEqual(r['source'],'catalog_research_template')
        answer=Mock(side_effect=[dict(hypotheses=[dict(strategy_id='range',hypothesis='시점별 비교',parameters={})]),dict(status='CRITIQUE',text='선택 편향을 확인해야 합니다.')])
        r=research.generate(dict(use_ai=True,products=['BTC-USD'],config={},context={}),completion=answer)
        self.assertEqual(answer.call_count,2);self.assertEqual(r['source'],'local_ai_research');self.assertFalse(r['core_execution_changed'])
    def test_invalid_model_result_and_nonlocal_provider_rejected(self):
        with self.assertRaises(ValueError):research.generate(dict(products=['BTC-USD'],config={}),completion=lambda *a:dict(hypotheses=[dict(strategy_id='range',code='print(1)')]))
        with self.assertRaisesRegex(ValueError,'LOCAL_MODEL'):research.model_json(dict(provider='openai',backend_url='https://example.com'),'test',{})

class ValidationTests(unittest.TestCase):
    def test_chronological_windows_no_overlap_with_final_holdout(self):
        d,h,folds=validation.windows(snapshot());self.assertLess(d[-1],h[0])
        for train,test in folds:self.assertLess(train[-1],test[0]);self.assertFalse(set(test)&set(h))
        self.assertGreater(len(sum(base.folds_for(snapshot(1700)['assets'],limit=None),[])),720)
        self.assertEqual(len(sum(base.folds_for(snapshot(1700)['assets']),[])),720)
    def test_future_candles_cannot_change_training_selection(self):
        s=spec();a=snapshot();_,h,_=validation.windows(a)
        one=validation.run(a,s);two=copy.deepcopy(a)
        for b in two['assets']['BTC-USD']['hourly']:
            if b['start']>=h[0]:b.update(open=200,close=200,high=205,low=195)
        out=validation.run(two,s)
        self.assertEqual(one['locked_spec'],out['locked_spec']);self.assertEqual(one['robustness'],out['robustness']);self.assertEqual(one['walk_forward'],out['walk_forward'])
    def test_real_simulation_reports_costs_and_never_promotes_small_sample(self):
        s=specs.validate(dict(strategy_id='dca_weekly',products=['BTC-USD']))
        r=validation.run(snapshot(),s)
        self.assertEqual(r['status'],'INSUFFICIENT_SAMPLE');self.assertFalse(r['live_promotion']);self.assertFalse(r['core_execution_changed'])
        self.assertEqual(len(r['walk_forward']),3);self.assertEqual(len(r['stress']),3);self.assertGreater(r['stress']['base']['fees'],0)
        self.assertEqual(r['out_of_sample_days'],len(r['return_series'])/24)
        self.assertIn('INSUFFICIENT_SAMPLE',r['overfitting']['flags'])
    def test_checkpoint_resume_reuses_complete_segments_identically(self):
        cp={};a=snapshot();s=spec()
        def stop(p):
            if p['completed']==3:raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):validation.run(a,s,stop,cp)
        self.assertEqual(len(cp['runs']),3)
        with patch.object(base,'simulate',wraps=base.simulate) as run:result=validation.run(a,s,checkpoint=cp)
        self.assertEqual(run.call_count,len(cp['runs'])-3)
        with patch.object(base,'simulate',side_effect=AssertionError('should reuse')):self.assertEqual(result,validation.run(a,s,checkpoint=cp))
    def test_no_gaps_or_wrong_market_snapshot(self):
        a=snapshot();del a['assets']['BTC-USD']['hourly'][250]
        with self.assertRaises(ValueError):validation.run(a,spec())
        with self.assertRaisesRegex(ValueError,'REQUIRED_DATA'):validation.run(snapshot(),spec(products=['ETH-USD']))

class PortfolioTests(unittest.TestCase):
    def test_aligned_correlation_requires_enough_nonflat_observations(self):
        a=[dict(at=i*3600,value=math.sin(i)) for i in range(100)]
        b=[dict(at=r['at'],value=-r['value']) for r in a]
        self.assertAlmostEqual(portfolio.correlation(a,b)['value'],-1)
        self.assertIsNone(portfolio.correlation(a,b[:71])['value'])
        self.assertIsNone(portfolio.correlation(a,[dict(at=r['at'],value=0) for r in a])['value'])
        self.assertIsNone(portfolio.correlation(a,[dict(at=r['at']+1000000,value=r['value']) for r in b])['value'])
    def test_allocation_requires_both_validation_and_observation_and_stays_within_caps(self):
        es=[dict(id=str(i),spec=dict(products=[f'C{i}-USD'])) for i in range(9)];reports={};obs={}
        for i,e in enumerate(es):
            reports[e['id']]=dict(status='RESEARCH_EVIDENCE_READY',summary=dict(return_ratio=.1,max_drawdown=.02),return_series=[dict(at=t*3600,value=math.sin(t*(i+1))) for t in range(200)])
            obs[e['id']]=dict(as_of=time.time(),fresh=True,days=8,closed_exits=30,realized_cad='10',return_ratio='.1',drawdown=.01)
        self.assertTrue(all(a['weight']==0 for a in portfolio.analyze(es,reports,{})['allocations']))
        p=portfolio.analyze(es,reports,obs);self.assertFalse(p['applied_to_trading']);self.assertGreaterEqual(p['cash_weight'],.4-1e-12)
        self.assertLessEqual(sum(a['weight'] for a in p['allocations']),.60+1e-12);self.assertTrue(all(v<=.1+1e-12 for v in p['asset_weights'].values()))
    def test_loss_and_delayed_feed_visible_without_halting_primary(self):
        h=portfolio.health(dict(id='x'),None,dict(as_of=1,fresh=False,drawdown=.2,realized_cad='-10'),now=1000)
        self.assertEqual(h['status'],'ATTENTION');self.assertFalse(h['base_engine_paused']);self.assertIn('DRAWDOWN_WARNING',h['flags'])

class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=MultiEngine(Path(self.tmp.name)/'core.sqlite3',Mock(),rules.VERSION);self.q=Manager(self.base)
        self.q.ensure_thread=Mock();self.addCleanup(self.q.stopping.set)
    def test_additive_migration_preserves_accounts_settings_and_history(self):
        with self.base.repo.db() as c:
            self.base.repo.set(c,'keep_setting',{'a':123});self.base.repo.set(c,'multi_running',True)
            before=[tuple(r) for r in c.execute('SELECT * FROM paper_accounts')]
            settings=[tuple(r) for r in c.execute('SELECT * FROM settings ORDER BY key')]
        Store(self.base.repo)
        with self.base.repo.db() as c:
            self.assertEqual(before,[tuple(r) for r in c.execute('SELECT * FROM paper_accounts')])
            self.assertEqual(settings,[tuple(r) for r in c.execute('SELECT * FROM settings ORDER BY key')])
        e=self.q.store.create(spec());self.assertEqual(self.q.store.get(e['id']),e)
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):self.q.store.update(e['id'],{'spec':{}})
        with self.assertRaises(ValueError):identifier('../core.sqlite3')
    def test_durable_validation_run_worker_and_supervisor_replay(self):
        e=self.q.store.create(spec())
        with patch.object(validation_job,'snapshot',return_value=snapshot()):self.q.validate(e['id'])
        job=read(self.q.folder(e['id'])/'job.json');self.assertEqual(job['status'],'QUEUED')
        self.q.store.update(e['id'],{'status':'VALIDATING'});worker(self.q.folder(e['id']))
        p=read(self.q.folder(e['id'])/'progress.json');self.assertEqual(p['status'],'COMPLETE',p)
        self.q.finish(job,p);self.assertEqual(self.q.store.get(e['id'])['status'],'VALIDATED')
        r=self.q.store.report(e['id']);self.assertEqual(r['code_hash'],code_hash());self.assertEqual(r['spec_hash'],e['spec_hash'])
    def test_worker_checks_input_hash_and_does_not_mutate_core(self):
        e=self.q.store.create(spec())
        with patch.object(validation_job,'snapshot',return_value=snapshot()):self.q.validate(e['id'])
        folder=self.q.folder(e['id']);a=read(folder/'snapshot.json');a['captured_at']=0;atomic(folder/'snapshot.json',a)
        worker(folder);self.assertEqual(read(folder/'progress.json')['error'],'QUANT_INPUT_HASH_MISMATCH')
        with self.base.repo.db() as c:self.assertEqual(self.base.repo.rows(c,'fills'),[])
    def test_queued_pause_and_resume_without_resetting_immutable_input(self):
        e=self.q.store.create(spec())
        with patch.object(validation_job,'snapshot',return_value=snapshot()):self.q.validate(e['id'])
        old=(self.q.folder(e['id'])/'snapshot.json').read_bytes();self.q.pause(e['id']);self.q.resume(e['id'])
        self.assertEqual(self.q.store.get(e['id'])['status'],'QUEUED');self.assertEqual(old,(self.q.folder(e['id'])/'snapshot.json').read_bytes())
        j=read(self.q.folder(e['id'])/'job.json');j['status']='RUNNING';self.q.save_job(j);self.q.store.update(e['id'],{'status':'VALIDATING'});self.q.pause(e['id'])
        with self.assertRaisesRegex(ValueError,'CANCELLING'):self.q.resume(e['id'])
    def test_research_result_replay_does_not_duplicate_proposals(self):
        jid='a'*32;folder=self.q.folder(jid);atomic(folder/'result.json',research.generate(dict(products=['BTC-USD'],use_ai=False)))
        job=dict(id=jid,kind='research',status='RUNNING',created_at=1)
        self.q.finish(job.copy(),{'status':'COMPLETE'});self.q.finish(job.copy(),{'status':'COMPLETE'})
        self.assertEqual(len(self.q.store.list()),3)
    def test_cannot_incubate_unvalidated_or_modified_code(self):
        e=self.q.store.create(spec())
        with self.assertRaisesRegex(ValueError,'VALIDATE_FIRST'):self.q.incubate(e['id'])
        self.q.store.update(e['id'],{'status':'VALIDATED'});self.q.store.report(e['id'],{'code_hash':'old'})
        with self.assertRaisesRegex(ValueError,'NEW_CODE'):self.q.incubate(e['id'])

class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.market=RuleMarket('range');self.market.now=time.time()
        adapter=CadPaperAdapter(self.market,lambda:dict(rate='1',date=time.strftime('%Y-%m-%d',time.gmtime())))
        self.base=MultiEngine(Path(self.tmp.name)/'core.sqlite3',adapter,rules.VERSION)
        h,d,b=case('range',self.market.now);pid=self.market.pid
        self.base.intelligence=SimpleNamespace(get=lambda p:dict(bars=h,fx_rate=1),get_daily=lambda p:d,news=SimpleNamespace(context=lambda p:dict(status='CURRENT',allocation_multiplier=1)),products={pid:{'base_currency_id':'ALT'}},bitcoin_context=lambda t:None,fx_context=lambda t:None)
        self.shadow=ShadowEngine(Path(self.tmp.name)/'trial/paper.sqlite3',self.base,spec(products=[pid]))
    def test_actual_rule_and_fill_use_separate_ledger_only(self):
        with self.base.repo.db() as c:original=[tuple(r) for r in c.execute('SELECT * FROM paper_accounts')]
        self.shadow.start_multi();self.shadow.cycle()
        self.market.price=92.2;self.market.now=time.time()+20
        with patch('time.time',return_value=self.market.now):self.shadow.cycle()
        with self.shadow.repo.db() as c:
            fills=self.shadow.repo.rows(c,'fills');self.shadow.reconcile_ledger(c)
        self.assertEqual(len(fills),1);self.assertEqual(fills[0]['side'],'BUY')
        with patch('time.time',return_value=self.market.now):out=observation(self.shadow,'SHADOW_LIVE',self.market.now-60)
        self.assertTrue(out['fresh']);self.assertEqual(out['fills'],1);self.assertLess(float(out['net_profit_cad']),0)
        self.assertFalse(out['real_orders']);self.assertFalse(out['core_funds_used'])
        with self.base.repo.db() as c:self.assertEqual(self.base.repo.rows(c,'fills'),[]);self.assertEqual(original,[tuple(r) for r in c.execute('SELECT * FROM paper_accounts')])
    def test_shadow_cannot_replace_primary_subscriptions_or_call_broker(self):
        with patch.object(self.base.adapter,'set_held_products',create=True) as f:self.shadow.adapter.set_held_products(['BTC-USD']);f.assert_not_called()
        self.assertFalse(hasattr(self.shadow.adapter,'place_order'))
        with self.assertRaisesRegex(Exception,'OUTSIDE_SCOPE'):self.shadow.adapter.get_product('BTC-USD')
    def test_stale_cached_candles_do_not_fill(self):
        self.base.intelligence.get=lambda p:None;self.shadow.start_multi()
        with self.assertRaises(Exception):self.shadow.cycle()
        with self.shadow.repo.db() as c:self.assertEqual(self.shadow.repo.rows(c,'fills'),[])

if __name__=='__main__':unittest.main()
