"""Isolated, resumable-on-request research jobs. No trading-mode changes."""
import copy,hashlib,json,os,subprocess,sys,threading,time
from pathlib import Path
from . import execution_strategies as rules,fx_reserve,validation

_jobs={};_lock=threading.RLock();_evidence_cache={}
FILES=('profit_policy.py','execution_strategies.py','reference_strategies.py','market_strategies.py','fx_reserve.py','sizing.py','risk.py','validation.py')

def fingerprint():
    return hashlib.sha256(b''.join((Path(__file__).parent/f).read_bytes() for f in FILES)).hexdigest()

def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False));tmp.replace(path)

def read(path):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return None

def state(directory):
    directory=Path(directory)/'validation';progress=read(directory/'progress.json') or dict(status='NOT_RUN')
    if progress.get('status')=='RUNNING' and time.time()-progress.get('updated_at',0)>180:
        progress={**progress,'status':'INTERRUPTED','reason':'VALIDATION_WORKER_NO_PROGRESS'}
    result=read(directory/'report.json')
    if result and (result.get('strategy_version')!=rules.VERSION or result.get('code_hash')!=fingerprint()):
        result={**result,'outdated':True}
    return dict(progress=progress,result=result)

def evidence_for(directory,pid):
    path=Path(directory)/'validation'/'report.json'
    try:stamp=path.stat().st_mtime_ns
    except OSError:return {}
    key=str(path);cached=_evidence_cache.get(key)
    if not cached or cached[0]!=stamp:
        report=read(path) or {}
        valid=report.get('code_hash')==fingerprint() and report.get('strategy_version')==rules.VERSION
        data={p:{r['strategy_id']:r['evidence'][p] for r in report.get('entries',[]) if r['strategy_id']!='combined' and p in r.get('evidence',{})} for p in report.get('products',[])} if valid else {}
        _evidence_cache[key]=(stamp,data)
    return _evidence_cache[key][1].get(pid,{})

def snapshot(engine,products=None):
    if not engine.intelligence:raise ValueError('VALIDATION_HISTORY_NOT_READY')
    intel=engine.intelligence;assets={};missing=[]
    with intel.lock:
        requested=products
        products=dict(intel.products)
        if requested is not None and (not isinstance(requested,list) or not 1<=len(requested)<=4 or any(p not in products for p in requested)):raise ValueError('VALIDATION_INVALID_PRODUCTS')
        for base in (requested if requested is not None else ('BTC','ETH','SOL','USDC')):
            pid=(base if base in intel.history and base in intel.daily else None) if requested is not None else next((p for p in products if p.split('-')[0]==base and p in intel.history and p in intel.daily),None)
            if not pid or len(intel.history[pid]['bars'])<273:missing.append(base);continue
            h=copy.deepcopy(intel.history[pid]);d=copy.deepcopy(intel.daily[pid]);p=products[pid]
            def native(row):
                rate=float(row.get('fx_rate',1));return [{**b,**{k:b[k]/rate for k in ('open','high','low','close')}} for b in row['bars']]
            foreign=pid.endswith('-USD');rate=float(h.get('fx_rate',1)) if foreign else 1.
            # Public cached PAPER product metadata is CAD-valued; restore native minima.
            assets[pid]=dict(hourly=native(h),daily=native(d),foreign=foreign,reference_fx=rate,
                             **{k:p[k] for k in ('base_increment','base_min_size','base_max_size')},
                             quote_min_size=str(float(p['quote_min_size'])/rate),quote_max_size=str(float(p['quote_max_size'])/rate))
    if not assets:raise ValueError('VALIDATION_HISTORY_NOT_READY')
    with engine.repo.db() as c:fx=engine.repo.get(c,fx_reserve.KEY)
    if not fx_reserve.context(fx,time.time()):fx=None
    payload=dict(assets=assets,fx_history=fx,missing_assets=missing,captured_at=time.time(),strategy_version=rules.VERSION,code_hash=fingerprint())
    validation.folds_for(assets)
    return payload

def start(engine):
    directory=engine.repo.path.parent/'validation';key=str(directory)
    with _lock:
        active=_jobs.get(key)
        if active and active.poll() is None:return state(engine.repo.path.parent)
        prior=state(engine.repo.path.parent)
        if prior['progress'].get('status')=='RUNNING':return prior
        payload=snapshot(engine);directory.mkdir(parents=True,exist_ok=True)
        atomic(directory/'input.json',payload)
        atomic(directory/'progress.json',dict(status='RUNNING',started_at=time.time(),updated_at=time.time(),completed=0,total=153))
        try:
            process=subprocess.Popen([sys.executable,'-m','stock101.crypto.validation_worker',str(directory)],
                                     cwd=str(Path(__file__).resolve().parents[2]),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            _jobs[key]=process
        except Exception:
            atomic(directory/'progress.json',dict(status='FAILED',reason='VALIDATION_WORKER_START_FAILED',updated_at=time.time()));raise ValueError('VALIDATION_WORKER_START_FAILED') from None
        return state(engine.repo.path.parent)
