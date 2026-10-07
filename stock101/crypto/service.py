import threading
import time
from .multi import MultiEngine as Engine
from . import research,planner,execution_strategies,validation_job
_instances={};_live_instances={};_lock=threading.RLock();_mode_lock=threading.RLock()
def _paper_cycle(instance,before_resume=None):
    try:
        instance.recover_if_due(before_resume)
        return instance.cycle()
    except Exception as error:
        instance.fail('AUTOMATION_CYCLE_FAILED',error)
        return {'status':'HALTED'}
def _live_cycle(instance):
    try:return instance.cycle()
    except Exception:
        # Disarm in memory even when the ledger itself cannot persist the stop.
        instance.stop_event.set();instance._permit=False
        try:instance.fail('LIVE_CYCLE_FAILED')
        except Exception:pass
        raise

def engine(app):
    from .runtime import for_app
    from .storage import guard
    guard(app.DATA/'stock101-coinbase'/'cad-plan.sqlite3')
    key=str(app.DATA)
    with _lock:
        if key not in _instances:
            instance=Engine(app.DATA/'stock101-coinbase'/'cad-plan.sqlite3',strategy_version=execution_strategies.VERSION)
            instance.enable_background();_instances[key]=instance
            from . import agent_research
            manager=agent_research.manager(app);manager.paper=instance;manager.ensure_thread()
            from .quant.manager import manager as quant_manager
            try:quant_manager(instance).ensure_thread()
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Stock101 research supervisor startup failed; core trading continues')
        instance=_instances[key]
        runtime=for_app(app)
        try:
            if instance.learning is None:
                from .learning import Learning
                instance.learning=Learning(instance)
            from .quant.manager import manager as quant_manager
            quant=quant_manager(instance)
            def proposal_sink(spec,key):
                existing=next((row for row in quant.store.list() if (row.get('research') or {}).get('learning_key')==key),None)
                return existing or quant.store.create(spec,source='learning_review',research={'learning_key':key})
            instance.learning.proposal_sink=proposal_sink
            runtime.ensure_worker('paper-learning',instance.learning.cycle,60)
            runtime.init_errors.pop('learning',None)
        except Exception as error:
            runtime.init_errors['learning']=type(error).__name__
        def cycle():
            def ensure_live_off():
                live=live_engine(app)
                if live.enabled() or live.pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
            with _mode_lock:return _paper_cycle(instance,ensure_live_off)
        for_app(app).ensure_worker('coinbase-paper',cycle,2)
        return instance

def live_engine(app):
    from .live import LiveEngine
    from .runtime import for_app
    from .storage import guard
    guard(app.DATA/'stock101-coinbase'/'live.sqlite3')
    key=str(app.DATA)
    with _lock:
        if key not in _live_instances:
            instance=LiveEngine(app.DATA/'stock101-coinbase'/'live.sqlite3',strategy_version=execution_strategies.VERSION)
            instance.enable_background();_live_instances[key]=instance
            from . import agent_research
            agent_research.manager(app).live=instance
        instance=_live_instances[key]
        for_app(app).ensure_worker('live',lambda:_live_cycle(instance),5)
        return instance

def start_supervisor(app):
    from .runtime import for_app
    def initialize(runtime):
        for name,factory in [('paper',engine),('live',live_engine)]:
            try:
                factory(app);runtime.init_errors.pop(name,None)
            except Exception as error:
                runtime.init_errors[name]=type(error).__name__
    return for_app(app).start(initialize)

def live_state(app):
    try:return {**live_engine(app).state(),'ledger_available':True}
    except Exception:
        instance=_live_instances.get(str(app.DATA))
        if instance is not None:
            instance.stop_event.set();instance._permit=False
        return dict(running=False,ledger_available=False,reason='LIVE_LEDGER_UNAVAILABLE',
                    exchange_order_status='UNKNOWN',readiness=dict(ready=False,error='LIVE_LEDGER_UNAVAILABLE'))

def get(app,path,query):
    from .runtime import for_app
    if path=='runtime':return for_app(app).snapshot()
    e=engine(app)
    if path=='learning':return e.learning.state() if e.learning else {'status':'UNAVAILABLE'}
    if path.startswith('quant'):
        from .quant.manager import manager
        q=manager(e)
        if path=='quant':return q.state()
        if path=='quant/experiment':return q.detail(query.get('id'))
        if path=='quant/portfolio':return q.portfolio()
        raise ValueError('UNKNOWN_QUANT_ENDPOINT')
    if path.startswith('agents'):
        from . import agent_research
        manager=agent_research.manager(app)
        if path=='agents':return manager.state()
        if path=='agents/job':return manager.detail(query.get('id'))
        if path=='agents/report':return manager.report(query.get('id'),query.get('ticker',''))
        if path=='agents/memory':return manager.memory()
        raise ValueError('UNKNOWN_AGENTS_ENDPOINT')
    if path=='telegram':
        from . import telegram_view
        mode=query.get('mode','paper');view=query.get('view','status')
        if mode not in telegram_view.MODES or view not in telegram_view.VIEWS:raise ValueError('지원하지 않는 조회 화면입니다.')
        return telegram_view.response(e if mode=='paper' else live_engine(app),mode,view)
    if path=='calendar':
        from . import profit_calendar
        mode=query.get('mode','paper')
        if mode not in ('paper','live'):raise ValueError('INVALID_CALENDAR_MODE')
        return profit_calendar.view(e.repo if mode=='paper' else live_engine(app).repo,mode,query.get('month'))
    if path=='state':return {**e.state(compact=query.get('view')=='summary'),'cad_plan':e.plan_state(),'live':live_state(app),'background':for_app(app).snapshot()}
    if path=='plan':return e.inspect_plan(query.get('product',''))
    if path=='products':return {'products':e.discover(),'eligibility':'Public list only; Canadian account eligibility not verified.'}
    if path=='analysis':return e.analyze(query.get('product',''),query.get('version','balanced_v1'))
    if path=='portfolio':return e.portfolio()
    if path=='portfolio-charts':
        from . import portfolio_charts
        return portfolio_charts.view(e.repo,query.get('days','1'))
    if path=='analytics':return research.analytics(e)
    if path=='explain':
        if query.get('id','').startswith('ST101-LIVE-'):
            live=live_engine(app)
            with live.repo.db() as c:
                record=live.repo.decision_detail(c,query['id'],query.get('record_id'))
                orders=live.repo.decision_related(c,'orders',query['id'])
                fills=live.repo.decision_related(c,'fills',query['id'])
            if not record:raise ValueError('DECISION_NOT_FOUND')
            return {'source':'live_trade_journal','record':record,'orders':orders,'fills':fills,'explanation':f"{record['decision_id']}: {record['product']} {record['signal']['decision']}. Reasons: {', '.join(record['signal']['reason_codes'])}. Risk: {record['risk']['status']} ({record['risk']['reason']}). Exchange-confirmed fills: {len(fills)}."}
        with e.repo.db() as c:
            record=e.repo.decision_detail(c,query.get('id'),query.get('record_id'))
        if not record:raise ValueError('DECISION_NOT_FOUND')
        return {'source':'immutable_trade_journal','record':record,'explanation':f"{record['decision_id']}: {record['analysis']['decision']}. Reasons: {', '.join(record['analysis']['reason_codes'])}. Risk: {record['risk']['status']} ({record['risk']['reason']}). Outcome: {record['status']}."}
    if path=='validation':return validation_job.state(e.repo.path.parent)
    if path=='research':
        with e.repo.db() as c:return {'backtests':e.repo.rows(c,'backtest_runs',25),'experiments':e.repo.rows(c,'strategy_experiments',50),'post_trade':e.repo.rows(c,'post_trade_analysis',100)}
    raise ValueError('UNKNOWN_CRYPTO_ENDPOINT')
def action(app,body):
    e=engine(app);op=body.get('action','').removeprefix('crypto_')
    if op.startswith('quant_'):
        from .quant.manager import manager
        q=manager(e);eid=body.get('id')
        if op=='quant_generate':return q.generate(body)
        if op=='quant_create':return q.create(body.get('spec'))
        if op=='quant_validate':return q.validate(eid)
        if op=='quant_incubate':return q.incubate(eid,body.get('phase','INCUBATING'))
        if op=='quant_pause':return q.pause(eid)
        if op=='quant_resume':return q.resume(eid)
        if op=='quant_resume_job':return q.resume_job(eid)
        if op=='quant_clone':return q.clone(eid)
        raise ValueError('UNKNOWN_QUANT_ACTION')
    if op.startswith('agents_'):
        from . import agent_research
        manager=agent_research.manager(app)
        if op=='agents_start':return manager.start(body,live_engine(app) if body.get('mode')=='live' else e)
        if op=='agents_configure':return manager.save_config(body.get('config',{}))
        if op=='agents_keys':return manager.save_keys(body.get('keys',{}))
        if op=='agents_resume':return manager.resume(body.get('id'))
        if op=='agents_cancel':return manager.cancel(body.get('id'))
        raise ValueError('UNKNOWN_AGENTS_ACTION')
    if op=='live_status':return live_engine(app).readiness()
    if op=='live_configure':return live_engine(app).configure(body)
    if op=='live_reconcile':return {'ok':live_engine(app).reconcile(),'state':live_engine(app).state()}
    if op=='live_start':
        with _mode_lock:
            return live_engine(app).activate(body,before_start=e.halt)
    if op=='live_stop':return live_engine(app).stop()
    if op=='multi_start':
        with _mode_lock:
            live=live_engine(app)
            if live.enabled() or live.pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
            return e.start_multi()
    if op=='multi_stop':return e.halt()
    if op=='monitor':
        pid=body.get('product','');e.analyze(pid);e.monitor.start(pid);return {'ok':True}
    if op=='connection_setup':
        import subprocess
        helper=app.ROOT/'Stock101 Connect.app'/'Contents'/'MacOS'/'Stock101Connect'
        if not helper.is_file():raise ValueError('터미널에서 python -m stock101.crypto.connect_local --json-file /path/to/key.json 을 실행하세요. 실거래 키는 --live를 추가합니다.')
        subprocess.Popen(['/usr/bin/open','-a',str(helper.parents[2])],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'ok':True}
    if op=='real_balances':return e.adapter.readonly_balances()
    if op=='connection_check':return e.adapter.check_readonly_connection()
    if op=='halt':
        live=live_engine(app).stop();paper=e.halt();return {'ok':True,'paper':paper,'live':live}
    if op=='mode':
        with _mode_lock:
            if live_engine(app).enabled() or live_engine(app).pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
            return e.mode(body.get('mode'))
    if op=='recover':
        if live_engine(app).enabled() or live_engine(app).pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
        return e.release(body.get('product',''))
    if op=='arm':raise ValueError('USE_MULTI_ASSET_START')
    if op=='disarm':return e.halt()
    if op=='evaluate':
        with _mode_lock:
            if live_engine(app).enabled() or live_engine(app).pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
            return e.decide(body.get('product',''),body.get('version','balanced_v1'))
    if op=='validate_strategies':return validation_job.start(e)
    if op=='experiment':return research.experiment(e,body.get('config',{}))
    if op=='backtest':
        if body.get('version') in (planner.VERSION,'multi_strategy_v3','multi_strategy_v4',execution_strategies.VERSION):raise ValueError('USE_PLAN_WALK_FORWARD_DIAGNOSTICS')
        if live_engine(app).enabled() or live_engine(app).pending():raise ValueError('STOP_AND_RECONCILE_LIVE_FIRST')
        e.mode('BACKTEST')
        with e.repo.db() as c:
            if e.repo.get(c,'mode')!='BACKTEST':raise ValueError('BACKTEST_MODE_REQUIRED')
            config=e.repo.one(c,'strategy_versions',body.get('version','balanced_v1'))
        if not config:raise ValueError('UNKNOWN_STRATEGY_VERSION')
        p,bars,book=e.market(body.get('product',''))
        result=research.backtest(bars,config)
        with e.repo.db() as c:e.repo.put(c,'backtest_runs',str(time.time_ns()),{'product':p['product_id'],'result':result})
        return result
    if op=='post_trade':return research.post_trade(e)
    raise ValueError('UNKNOWN_CRYPTO_ACTION')
