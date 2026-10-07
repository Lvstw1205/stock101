"""Durable additive research jobs and independent observation supervisors."""
import hashlib,json,os,subprocess,sys,threading,time,uuid
from pathlib import Path
from .. import agent_research,validation_job,execution_strategies
from ..agent_research import atomic,read
from . import specs,portfolio
from .store import Store,identifier
from .shadow import ShadowEngine,observation

_lock=threading.RLock();_managers={}

def code_hash():
    raw=b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.glob('*.py')))
    return hashlib.sha256(raw+validation_job.fingerprint().encode()).hexdigest()

class Manager:
    def __init__(self,engine):
        self.engine=engine;self.store=Store(engine.repo);self.root=engine.repo.path.parent/'quant'
        self.root.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.stopping=threading.Event()
        self.process=None;self.active=None;self.thread=None;self.shadow_thread=None;self.shadows={};self.shadow_attempted={}
    def folder(self,jid):return self.root/'jobs'/identifier(jid)
    def ensure_thread(self):
        with self.lock:
            if not self.thread or not self.thread.is_alive():
                self.thread=threading.Thread(target=self.loop,name='stock101-quant-research',daemon=True);self.thread.start()
            if not self.shadow_thread or not self.shadow_thread.is_alive():
                self.shadow_thread=threading.Thread(target=self.shadow_loop,name='stock101-quant-observe',daemon=True);self.shadow_thread.start()
    def loop(self):
        while not self.stopping.wait(2):
            try:self.tick()
            except Exception as error:atomic(self.root/'supervisor.json',dict(error=type(error).__name__,at=time.time()))
    def jobs(self):
        jobs=[r for p in (self.root/'jobs').glob('*/job.json') if (r:=read(p))]
        return sorted(jobs,key=lambda j:j['created_at'],reverse=True)
    def save_job(self,row):atomic(self.folder(row['id'])/'job.json',row)
    def generate(self,body):
        question=body.get('question','비용 차감 성과와 재현성을 검증할 전략 후보를 제안해 주세요.')
        if not isinstance(question,str) or len(question)>2000:raise ValueError('QUANT_INVALID_QUESTION')
        products=specs.validate(dict(strategy_id='range',products=body.get('products',['BTC-USD','ETH-USD'])))['products']
        if type(body.get('use_ai',True)) is not bool:raise ValueError('QUANT_INVALID_AI_OPTION')
        with self.lock:
            if sum(j['status'] in ('QUEUED','RUNNING') for j in self.jobs())>=5:raise ValueError('QUANT_QUEUE_FULL')
            intel=self.engine.intelligence;context={'at':time.time(),'markets':[]}
            if intel:
                with intel.lock:
                    for pid in products:
                        h=intel.history.get(pid,{})
                        context['markets'].append(dict(product=pid,hourly_bars=len(h.get('bars',[])),regime=h.get('forecast',{}).get('regime'),news=intel.news.context(intel.products.get(pid,{'base_currency_id':pid.split('-')[0]}))))
            cfg={**agent_research.DEFAULT,**read(self.engine.repo.path.parent/'tradingagents/settings.json',{})}
            jid=uuid.uuid4().hex;folder=self.folder(jid)
            atomic(folder/'request.json',dict(id=jid,kind='research',products=products,question=question,use_ai=body.get('use_ai',True),config={k:cfg[k] for k in ('provider','quick_model','backend_url')},context=context,code_hash=code_hash()))
            job=dict(id=jid,kind='research',status='QUEUED',created_at=time.time(),updated_at=time.time());self.save_job(job)
        self.ensure_thread();return {'ok':True,'job':job}
    def create(self,raw):return {'ok':True,'experiment':self.store.create(raw)}
    def validate(self,eid):
        with self.lock:
            row=self.store.get(eid)
            if row['status'] not in ('PROPOSED','FAILED','INTERRUPTED'):raise ValueError('QUANT_INVALID_TRANSITION')
            if sum(j['status'] in ('QUEUED','RUNNING') for j in self.jobs())>=5:raise ValueError('QUANT_QUEUE_FULL')
            folder=self.folder(eid);request=read(folder/'request.json')
            if request:
                if request['code_hash']!=code_hash():raise ValueError('QUANT_CLONE_FOR_NEW_CODE')
            else:
                snapshot=validation_job.snapshot(self.engine,row['spec']['products'])
                critique=specs.critique(row['spec'],snapshot)
                if not critique['valid']:raise ValueError('QUANT_REQUIRED_DATA_MISSING')
                from .validation import windows
                windows(snapshot)
                atomic(folder/'snapshot.json',snapshot)
                request=dict(id=eid,kind='validation',spec=row['spec'],spec_hash=row['spec_hash'],snapshot_hash=specs.digest(snapshot),code_hash=code_hash())
                atomic(folder/'request.json',request)
            (folder/'cancel').unlink(missing_ok=True)
            self.store.update(eid,dict(status='QUEUED',snapshot_hash=request['snapshot_hash'],code_hash=request['code_hash']),{'PROPOSED','FAILED','INTERRUPTED'})
            job=dict(id=eid,kind='validation',status='QUEUED',created_at=time.time(),updated_at=time.time());self.save_job(job)
        self.ensure_thread();return {'ok':True,'job':job}
    def tick(self):
        with self.lock:
            if self.process:
                if self.process.poll() is None:return
                self.process=None;self.active=None
            jobs=self.jobs()
            for job in jobs:
                if job['status']!='RUNNING':continue
                progress=read(self.folder(job['id'])/'progress.json',{})
                if progress.get('status') in ('COMPLETE','FAILED','CANCELLED'):
                    self.finish(job,progress)
                elif time.time()-progress.get('updated_at',job['updated_at'])>90:
                    job.update(status='INTERRUPTED',updated_at=time.time());self.save_job(job)
                    if job['kind']=='validation':self.store.update(job['id'],dict(status='INTERRUPTED'),{'VALIDATING','QUEUED'})
                else:return
            pending=[j for j in self.jobs() if j['status']=='QUEUED']
            if not pending:return
            job=pending[-1];folder=self.folder(job['id'])
            if job['kind']=='validation':self.store.update(job['id'],dict(status='VALIDATING'),{'QUEUED'})
            job.update(status='RUNNING',updated_at=time.time());self.save_job(job)
            atomic(folder/'progress.json',dict(status='RUNNING',stage='실행 준비',updated_at=time.time(),completed=0,total=1))
            env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LANG','TMPDIR','SSL_CERT_FILE')}
            env.update(PYTHONPATH=str(Path(__file__).resolve().parents[3]),PYTHONUNBUFFERED='1',PYTHON_DOTENV_DISABLED='1')
            try:
                with (folder/'worker.log').open('ab') as log:self.process=subprocess.Popen([sys.executable,'-m','stock101.crypto.quant.worker',str(folder)],cwd=folder,env=env,stdout=log,stderr=log,start_new_session=True)
                self.active=job['id']
            except OSError:self.finish(job,dict(status='FAILED',error='QUANT_WORKER_START_FAILED'))
    def finish(self,job,progress):
        result=read(self.folder(job['id'])/'result.json')
        if progress['status']=='COMPLETE' and result:
            if job['kind']=='research':
                # Stable proposal IDs make supervisor replay idempotent.
                if not job.get('experiments'):
                    existing={e.get('research',{}).get('proposal_key'):e['id'] for e in self.store.list() if isinstance(e.get('research'),dict)}
                    ids=[]
                    for i,p in enumerate(result['proposals']):
                        key=job['id']+':'+str(i)
                        eid=existing.get(key)
                        if not eid:eid=self.store.create(p['spec'],p['source'],dict(job_id=job['id'],proposal_key=key,critic=p['critic']))['id']
                        ids.append(eid)
                    job['experiments']=ids
            else:
                self.store.report(job['id'],result)
                row=self.store.get(job['id'])
                if row['status']!='PAUSED':self.store.update(job['id'],dict(status='VALIDATED',validation_status=result['status']),{'VALIDATING','QUEUED'})
            job.update(status='COMPLETE',updated_at=time.time())
        else:
            job.update(status=progress.get('status','FAILED'),error=progress.get('error','QUANT_RESULT_MISSING'),updated_at=time.time())
            if job['kind']=='validation' and self.store.get(job['id'])['status']!='PAUSED':self.store.update(job['id'],dict(status='FAILED',error=job['error']))
        self.save_job(job)
    def shadow(self,row):
        with self.lock:
            eid=row['id']
            if eid not in self.shadows:
                report=self.store.report(eid)
                self.shadows[eid]=ShadowEngine(self.root/'shadow'/eid/'paper.sqlite3',self.engine,report['locked_spec'])
            return self.shadows[eid]
    def incubate(self,eid,phase='INCUBATING'):
        if phase not in ('INCUBATING','SHADOW_LIVE'):raise ValueError('QUANT_INVALID_PHASE')
        with self.lock:
            row=self.store.get(eid);report=self.store.report(eid)
            if row['status'] not in ('VALIDATED','PAUSED','INCUBATING','SHADOW_LIVE') or not report:raise ValueError('QUANT_VALIDATE_FIRST')
            if report['code_hash']!=code_hash():raise ValueError('QUANT_CLONE_FOR_NEW_CODE')
            active=[e for e in self.store.list() if e['id']!=eid and e['status'] in ('INCUBATING','SHADOW_LIVE')]
            if len(active)>=2:raise ValueError('QUANT_TWO_OBSERVERS_MAX')
            shadow=self.shadow(row);shadow.start_multi()
            row=self.store.update(eid,dict(status=phase,observation_started_at=row.get('observation_started_at',time.time())))
        self.ensure_thread();return {'ok':True,'experiment':row}
    def pause(self,eid):
        with self.lock:
            row=self.store.get(eid)
            if row['status']=='PAUSED':return {'ok':True,'experiment':row}
            if row['status'] in ('QUEUED','VALIDATING'):
                (self.folder(eid)/'cancel').touch(mode=0o600)
                job=read(self.folder(eid)/'job.json')
                if job and job['status']=='QUEUED':job.update(status='CANCELLED',updated_at=time.time());self.save_job(job)
            if eid in self.shadows:self.shadows[eid].halt()
            return {'ok':True,'experiment':self.store.update(eid,dict(status='PAUSED',previous_status=row['status']))}
    def resume(self,eid):
        with self.lock:return self._resume(eid)
    def _resume(self,eid):
        job=read(self.folder(eid)/'job.json',{})
        if job.get('status')=='RUNNING':raise ValueError('QUANT_CANCELLING_WAIT')
        row=self.store.get(eid)
        if row['status']!='PAUSED':raise ValueError('QUANT_INVALID_TRANSITION')
        if row.get('previous_status') in ('INCUBATING','SHADOW_LIVE'):return self.incubate(eid,row['previous_status'])
        self.store.update(eid,dict(status='INTERRUPTED'),{'PAUSED'});return self.validate(eid)
    def resume_job(self,jid):
        with self.lock:
            folder=self.folder(jid);job=read(folder/'job.json')
            if not job or job['kind']!='research' or job['status'] not in ('FAILED','INTERRUPTED','CANCELLED'):raise ValueError('QUANT_INVALID_TRANSITION')
            if read(folder/'request.json')['code_hash']!=code_hash():raise ValueError('QUANT_CLONE_FOR_NEW_CODE')
            (folder/'cancel').unlink(missing_ok=True)
            job.update(status='QUEUED',updated_at=time.time());self.save_job(job)
        self.ensure_thread();return {'ok':True,'job':job}
    def clone(self,eid):
        row=self.store.get(eid)
        return {'ok':True,'experiment':self.store.create(row['spec'],'new_data_clone',{'parent':eid})}
    def shadow_loop(self):
        while not self.stopping.wait(5):
            try:self.observe_once()
            except Exception as error:atomic(self.root/'observer.json',dict(error=type(error).__name__,at=time.time()))
    def observe_once(self):
        for row in self.store.list():
            if row['status'] not in ('INCUBATING','SHADOW_LIVE'):continue
            cadence=15 if row['status']=='SHADOW_LIVE' else 60
            if time.time()-self.shadow_attempted.get(row['id'],0)<cadence:continue
            self.shadow_attempted[row['id']]=time.time()
            try:
                report=self.store.report(row['id'])
                if report['code_hash']!=code_hash():
                    self.pause(row['id']);self.store.update(row['id'],dict(status='NEEDS_REVALIDATION'));continue
                shadow=self.shadow(row)
                if self.store.get(row['id'])['status'] not in ('INCUBATING','SHADOW_LIVE'):continue
                shadow.recover_if_due();shadow.cycle()
                self.store.observation(row['id'],observation(shadow,row['status'],row['observation_started_at']))
            except Exception as error:
                self.store.observation(row['id'],dict(as_of=time.time(),running=False,fresh=False,error=type(error).__name__,phase=row['status'],wallet='ISOLATED_RESEARCH_PAPER',real_orders=False,core_funds_used=False))
    def detail(self,eid):
        row=self.store.get(eid)
        return dict(experiment=row,report=self.store.report(eid),observation=self.store.observation(eid),events=self.store.events(eid),progress=read(self.folder(eid)/'progress.json',{}))
    def portfolio(self):
        rows=self.store.list()[:40]
        return portfolio.analyze(rows,{e['id']:self.store.report(e['id']) for e in rows},{e['id']:self.store.observation(e['id']) for e in rows})
    def state(self):
        self.ensure_thread();rows=self.store.list();digest=code_hash()
        return dict(version=1,experiments=[dict(e,code_current=e.get('code_hash') in (None,digest),observation=self.store.observation(e['id'])) for e in rows[:60]],
                    jobs=[dict(j,progress=read(self.folder(j['id'])/'progress.json',{})) for j in self.jobs()[:20]],
                    catalog=[dict(strategy_id=sid,name=name) for sid,name in execution_strategies.NAMES.items()],
                    parameters=specs.PARAMETERS,supervisor=read(self.root/'supervisor.json',{}),observer=read(self.root/'observer.json',{}),
                    primary_trading_changed=False,live_promotion=False,
                    architecture='기존 전략·백테스트·위험 검사·모의 체결 엔진 재사용',
                    observation_note='실험마다 독립 가상 1,000 CAD를 사용합니다. 홈의 운용 자금·수익과 합산하지 않습니다.')

def manager(engine):
    key=str(engine.repo.path)
    with _lock:
        if key not in _managers:_managers[key]=Manager(engine)
        return _managers[key]
