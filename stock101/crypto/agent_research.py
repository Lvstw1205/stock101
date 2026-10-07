"""Durable, isolated TradingAgents jobs. No broker or order APIs in this module."""
import contextlib,copy,datetime as dt,hashlib,json,os,re,subprocess,threading,time,uuid
from pathlib import Path
from urllib.parse import urlsplit

UPSTREAM={'name':'TradingAgents','version':'0.5.2','commit':'8b22d43d01d9ddda5d686d093d5385884622f3de','url':'https://github.com/TauricResearch/TradingAgents','license':'Apache-2.0'}
ROLES={'market':'기술 분석','social':'시장 심리','news':'뉴스·거시경제','fundamentals':'기초 분석'}
PROVIDERS=['ollama','openai','anthropic','google','xai','deepseek','qwen','qwen-cn','glm','glm-cn','minimax','minimax-cn','openrouter','mistral','kimi','groq','nvidia','azure','bedrock','openai_compatible']
KEY_NAMES={'OPENAI_API_KEY','ANTHROPIC_API_KEY','GOOGLE_API_KEY','XAI_API_KEY','DEEPSEEK_API_KEY','DASHSCOPE_API_KEY','DASHSCOPE_CN_API_KEY','ZHIPU_API_KEY','ZHIPU_CN_API_KEY','MINIMAX_API_KEY','MINIMAX_CN_API_KEY','OPENROUTER_API_KEY','MISTRAL_API_KEY','MOONSHOT_API_KEY','GROQ_API_KEY','NVIDIA_API_KEY','OPENAI_COMPATIBLE_API_KEY','FRED_API_KEY','ALPHA_VANTAGE_API_KEY','TYPESAFE_API_KEY','AZURE_OPENAI_API_KEY','AZURE_OPENAI_ENDPOINT','AZURE_OPENAI_API_VERSION','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN','AWS_DEFAULT_REGION','AWS_BEARER_TOKEN_BEDROCK'}
FEATURES=[('analysts','4개 분석가 병렬 실행'),('debate','상승·하락 연구원 토론'),('trader','매매 시점·크기 제안'),('risk','공격·중립·보수 위험 토론'),('portfolio','포트폴리오 담당 최종 5단계 판단'),('holdings','현재 보유량·현금 반영'),('memory','과거 판단·결과·회고 기억'),('checkpoint','분석 중단 후 이어서 실행'),('reports','단계별 보고서·전체 보고서 내보내기'),('backtest','여러 종목·날짜별 AI 판단 검증'),('point_in_time','분석일 이후 데이터 차단'),('markets','코인·미국·캐나다·해외 주식 분석'),('vendors','Coinbase·Yahoo·SEC·Alpha Vantage'),('macro','FRED 거시경제·Polymarket 예측시장'),('sentiment','뉴스·Reddit·StockTwits 심리·선택적 Jev 필터'),('providers','로컬·클라우드 모델과 한국어 보고서'),('cli','원본 CLI·Python API·Docker 구성 포함')]
DEFAULT={'provider':'ollama','quick_model':'qwen3:4b','deep_model':'qwen3:4b','backend_url':'http://127.0.0.1:11434/v1','language':'Korean','analysts':list(ROLES),'debate_rounds':1,'risk_rounds':1,'tool_rounds':3,'max_tokens':1200,'retries':1,'checkpoint':True,'auto_enabled':False,'auto_minutes':60,'apply_to_execution':True,'data_vendors':{'core_stock_apis':'yfinance','technical_indicators':'yfinance','fundamental_data':'sec_edgar,yfinance','news_data':'yfinance','macro_data':'fred','prediction_markets':'polymarket'},'benchmark':'','holding_days':5,'temperature':0.2,'news_limit':12,'macro_news_limit':8,'macro_lookback_days':7,'tool_vendors':{},'reasoning_effort':None,'thinking_level':None,'anthropic_effort':None}
DEFAULT['rapid_enabled']=True
_LOCK=threading.RLock();_managers={}

def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (FileNotFoundError,json.JSONDecodeError):return copy.deepcopy(default)

def atomic(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,allow_nan=False)
    tmp.replace(path)

def valid_date(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('AGENTS_INVALID_DATE')
    try:d=dt.date.fromisoformat(value)
    except ValueError:raise ValueError('AGENTS_INVALID_DATE') from None
    if not dt.date(2000,1,1)<=d<=dt.date.today():raise ValueError('AGENTS_INVALID_DATE')
    return value

def tickers(values):
    if isinstance(values,str):values=[v.strip().upper() for v in values.split(',') if v.strip()]
    if not isinstance(values,list) or not 1<=len(values)<=20:raise ValueError('AGENTS_TICKERS_REQUIRED')
    out=[]
    for v in values:
        if not isinstance(v,str) or not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=\-]{0,31}',v) or '..' in v:raise ValueError('AGENTS_INVALID_TICKER')
        if v not in out:out.append(v)
    return out

def validate_config(body,old):
    cfg=copy.deepcopy(old)
    if not isinstance(body,dict) or any(k not in DEFAULT for k in body):raise ValueError('AGENTS_UNKNOWN_SETTING')
    cfg.update(body)
    if cfg['provider'] not in PROVIDERS:raise ValueError('AGENTS_INVALID_PROVIDER')
    for k in ('quick_model','deep_model'):
        if not isinstance(cfg[k],str) or not 1<=len(cfg[k])<=160 or any(ord(c)<32 for c in cfg[k]):raise ValueError('AGENTS_INVALID_MODEL')
    if body.get('provider',old['provider'])!=old['provider'] and 'backend_url' not in body:cfg['backend_url']=''
    url=cfg['backend_url']
    if not isinstance(url,str):raise ValueError('AGENTS_INVALID_ENDPOINT')
    if url:
        u=urlsplit(url)
        if u.username or u.password or u.query or u.fragment or not u.hostname or not (u.scheme=='https' or u.scheme=='http' and u.hostname in ('127.0.0.1','localhost','::1')):raise ValueError('AGENTS_INVALID_ENDPOINT')
    for k,low,high in [('debate_rounds',1,5),('risk_rounds',1,5),('tool_rounds',1,20),('max_tokens',512,8192),('retries',0,3),('auto_minutes',15,1440),('holding_days',1,90),('news_limit',1,40),('macro_news_limit',1,25),('macro_lookback_days',1,30)]:
        if type(cfg[k]) is not int or not low<=cfg[k]<=high:raise ValueError('AGENTS_INVALID_LIMIT')
    for k in ('checkpoint','auto_enabled','apply_to_execution','rapid_enabled'):
        if type(cfg[k]) is not bool:raise ValueError('AGENTS_INVALID_SETTING')
    if not isinstance(cfg['analysts'],list) or not cfg['analysts'] or len(cfg['analysts'])!=len(set(cfg['analysts'])) or any(a not in ROLES for a in cfg['analysts']):raise ValueError('AGENTS_INVALID_ANALYST')
    if cfg['language'] not in ('Korean','English','Japanese','French','Chinese','Spanish','German','Portuguese'):raise ValueError('AGENTS_INVALID_LANGUAGE')
    if not isinstance(cfg['temperature'],(int,float)) or isinstance(cfg['temperature'],bool) or not 0<=cfg['temperature']<=1:raise ValueError('AGENTS_INVALID_SETTING')
    if cfg['benchmark']:tickers([cfg['benchmark']])
    categories=DEFAULT['data_vendors'];vendors={'yfinance','alpha_vantage','sec_edgar','fred','polymarket'}
    if not isinstance(cfg['data_vendors'],dict) or set(cfg['data_vendors'])!=set(categories):raise ValueError('AGENTS_INVALID_VENDOR')
    for v in cfg['data_vendors'].values():
        if not isinstance(v,str) or not set(v.split(','))<=vendors:raise ValueError('AGENTS_INVALID_VENDOR')
    if not isinstance(cfg['tool_vendors'],dict) or any(not re.fullmatch('get_[a-z_]+',k) or not isinstance(v,str) or not set(v.split(','))<=vendors for k,v in cfg['tool_vendors'].items()):raise ValueError('AGENTS_INVALID_VENDOR')
    for k in ('reasoning_effort','thinking_level','anthropic_effort'):
        if cfg[k] not in (None,'minimal','low','medium','high','xhigh','max'):raise ValueError('AGENTS_INVALID_SETTING')
    return cfg

class Manager:
    def __init__(self,app):
        self.app=app;self.root=Path(app.DATA)/'stock101-coinbase'/'tradingagents';self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.process=None;self.active_id=None;self.stopping=threading.Event()
        self.python=Path(app.ROOT)/'.runtime/tradingagents-venv/bin/python';self.worker=Path(__file__).with_name('agent_worker.py')
        self.thread=None
    def config(self):return {**copy.deepcopy(DEFAULT),**read(self.root/'settings.json',{})}
    def save_config(self,body):
        with self.lock:
            cfg=validate_config(body,self.config());atomic(self.root/'settings.json',cfg)
        return {'ok':True,'config':cfg}
    def save_keys(self,body):
        if not isinstance(body,dict) or any(k not in KEY_NAMES for k in body):raise ValueError('AGENTS_INVALID_CREDENTIAL')
        with self.lock:
            current=read(self.root/'credentials.json',{})
            for k,v in body.items():
                if not isinstance(v,str) or not 0<len(v)<=4096 or '\n' in v or '\r' in v:raise ValueError('AGENTS_INVALID_CREDENTIAL')
                current[k]=v.strip()
            atomic(self.root/'credentials.json',current)
        return {'ok':True,'configured_keys':sorted(current)}
    def job_path(self,jid):
        if not isinstance(jid,str) or not re.fullmatch('[0-9a-f]{32}',jid):raise ValueError('AGENTS_INVALID_JOB')
        return self.root/'jobs'/jid
    def jobs(self):
        jobs=[r for p in (self.root/'jobs').glob('*/status.json') if (r:=read(p))]
        return sorted(jobs,key=lambda r:r['created_at'],reverse=True)
    def capture(self,engine,symbols,mode,date):
        """Only completed locally stored market facts; no private keys or broker calls."""
        if date!=dt.date.today().isoformat():return {},None
        snapshots={};now=time.time()
        with engine.repo.db() as c:
            saved_plans={}
            for row in engine.repo.rows(c,'trade_decisions',500):
                saved_plans.setdefault(row.get('product'),(row.get('signal') or row.get('analysis') or {}).get('plan') or {})
            for symbol in symbols:
                h=engine.repo.one(c,'market_history',symbol);d=engine.repo.one(c,'daily_history',symbol)
                if not h or not d:continue
                def bars(row):
                    rate=float(row.get('fx_rate',1))
                    return [{**b,**{k:b[k]/rate for k in ('open','high','low','close')}} for b in row['bars'] if b['start']+(86400 if row is d else 3600)<=now]
                news=engine.intelligence.news.context({'base_currency_id':symbol.split('-')[0]}) if engine.intelligence else {}
                latest=engine.latest.get(symbol,{}) or {};p=(latest.get('plan') if isinstance(latest,dict) else None) or saved_plans.get(symbol,{})
                snapshots[symbol]={'ticker':symbol,'source':'Coinbase','currency':symbol.split('-')[-1], 'hourly':bars(h)[-350:],'daily':bars(d)[-350:],
                 'fx_rate':h.get('fx_rate',1),'captured_at':now,'price_for_execution':p.get('current'),
                 'price_evaluated_at':p.get('evaluated_at'), 'articles':news.get('articles',[])[:25], 'news_status':news.get('status'),'news_as_of':now}
        portfolio=None
        # CAD accounting values are explicitly converted to analysis USD units.
        # LIVE balances are never read or transmitted merely by opening this page.
        if mode=='paper' and snapshots and all(p.endswith('-USD') for p in symbols):
            rate=float(next(iter(snapshots.values()))['fx_rate'])
            if rate>0:
                with engine.repo.db() as c:
                    account=c.execute("SELECT cash FROM paper_accounts WHERE currency='CAD'").fetchone()
                    positions=engine.repo.rows(c,'positions',10000)
                portfolio={'currency':'USD','cash':float(account['cash'])/rate if account else None,'positions':[{'ticker':p['product'],'quantity':float(p['quantity']),'average_price':float(p['cost'])/float(p['quantity'])/rate} for p in positions if float(p['quantity'])>0]}
        if mode=='live' and snapshots and len({s.split('-')[-1] for s in symbols})==1 and (engine.config() or {}).get('quote')==symbols[0].split('-')[-1]:
            quote=symbols[0].split('-')[-1]
            with engine.repo.db() as c:
                positions=engine.repo.rows(c,'positions',10000);marks=engine.repo.rows(c,'portfolio_snapshots',1)
            summary=marks[0] if marks and 0<=now-marks[0]['created']<=120 else {}
            portfolio={'currency':quote,'cash':None,'positions':[{'ticker':p['product'],'quantity':float(p['quantity']),'average_price':float(p['cost'])/float(p['quantity'])} for p in positions if float(p['quantity'])>0 and p.get('quote')==quote]}
            # Only this app's last confirmed allocation is used, never the user's
            # entire exchange wallet. Do not fetch broker balances for research.
            if quote=='CAD' and summary.get('available_cad') is not None:portfolio['cash']=float(summary['available_cad'])
        return snapshots,portfolio
    def start(self,body,engine):
        if not self.python.is_file():raise ValueError('AGENTS_RUNTIME_MISSING')
        symbols=tickers(body.get('tickers',body.get('ticker','BTC-USD')))
        kind=body.get('kind','analysis');mode=body.get('mode','paper');asset=body.get('asset_type','crypto')
        if kind not in ('analysis','backtest') or mode not in ('paper','live','research') or asset not in ('stock','crypto'):raise ValueError('AGENTS_INVALID_REQUEST')
        date=valid_date(body.get('date',dt.date.today().isoformat()));dates=[]
        if kind=='backtest':
            start=dt.date.fromisoformat(valid_date(body.get('start','')));end=dt.date.fromisoformat(valid_date(body.get('end','')));step=body.get('every',7)
            if type(step) is not int or not 1<=step<=90 or end<start:raise ValueError('AGENTS_INVALID_DATE_GRID')
            while start<=end:dates.append(start.isoformat());start+=dt.timedelta(days=step)
            if len(dates)*len(symbols)>60:raise ValueError('AGENTS_GRID_TOO_LARGE')
        snapshots,portfolio=self.capture(engine,symbols,mode,date) if asset=='crypto' and kind=='analysis' else ({},None)
        cfg=self.config();jid=uuid.uuid4().hex;folder=self.job_path(jid);now=time.time()
        from . import evaluation_policy
        evaluation=evaluation_policy.description()
        if engine is not None and mode in ('paper','live') and kind=='analysis' and date==dt.date.today().isoformat():
            with engine.repo.db() as c:evaluation=evaluation_policy.state(engine.repo,c,mode,now)
        with self.lock:
            if sum(j['status'] in ('QUEUED','RUNNING') for j in self.jobs())>=10:raise ValueError('AGENTS_QUEUE_FULL')
            request={'id':jid,'kind':kind,'tickers':symbols,'asset_type':asset,'mode':mode,'date':date,'dates':dates,'config':cfg,'snapshots':snapshots,'portfolio':portfolio,'created_at':now,'automatic':bool(body.get('automatic',False)),'upstream':UPSTREAM}
            request['evaluation_policy']=evaluation
            atomic(folder/'request.json',request);atomic(folder/'status.json',{'id':jid,'kind':kind,'tickers':symbols,'date':date,'created_at':now,'updated_at':now,'status':'QUEUED','stage':'대기','completed':0,'total':len(dates)*len(symbols) if dates else len(symbols),'attempt':0})
        self.ensure_thread();return {'ok':True,'job':read(folder/'status.json')}
    def resume(self,jid):
        folder=self.job_path(jid)
        with self.lock:
            s=read(folder/'status.json')
            if not s or s['status'] not in ('FAILED','INTERRUPTED','CANCELLED','PARTIAL'):raise ValueError('AGENTS_NOT_RESUMABLE')
            (folder/'cancel').unlink(missing_ok=True);s.update(status='QUEUED',stage='저장 지점부터 재개 대기',error=None,updated_at=time.time());atomic(folder/'status.json',s)
        self.ensure_thread();return {'ok':True}
    def cancel(self,jid):
        folder=self.job_path(jid)
        with self.lock:
            s=read(folder/'status.json')
            if not s:raise ValueError('AGENTS_JOB_NOT_FOUND')
            if s['status']=='QUEUED':s.update(status='CANCELLED',updated_at=time.time());atomic(folder/'status.json',s)
            elif s['status']=='RUNNING':(folder/'cancel').touch(mode=0o600)
        return {'ok':True}
    def ensure_thread(self):
        with self.lock:
            if self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self.loop,name='stock101-tradingagents',daemon=True);self.thread.start()
    def loop(self):
        while not self.stopping.wait(2):
            try:self.tick()
            except Exception as error:
                atomic(self.root/'supervisor.json',{'error':type(error).__name__,'at':time.time()})
    def tick(self):
        with self.lock:
            if self.process:
                if self.process.poll() is None:return
                folder=self.job_path(self.active_id);s=read(folder/'status.json')
                if s and s['status']=='RUNNING':s.update(status='INTERRUPTED',stage='분석 프로세스 중단 · 재개 가능',updated_at=time.time());atomic(folder/'status.json',s)
                self.process=None;self.active_id=None
            jobs=self.jobs()
            for s in jobs:
                if s['status']=='RUNNING':
                    if time.time()-s.get('heartbeat_at',s['updated_at'])<=60:return
                    s.update(status='INTERRUPTED',stage='응답 끊김 · 저장 지점에서 재개 가능',updated_at=time.time());atomic(self.job_path(s['id'])/'status.json',s)
            pending=[s for s in jobs if s['status']=='QUEUED']
            if not pending:
                if getattr(self,'live',None) and self.live.enabled():self.maybe_auto(self.live,'live')
                elif getattr(self,'paper',None):self.maybe_auto(self.paper)
                return
            s=pending[-1];jid=s['id'];folder=self.job_path(jid)
            env={k:v for k,v in os.environ.items() if k in ('PATH','HOME','USER','LANG','LC_ALL','TMPDIR','SSL_CERT_FILE')}
            env.update(PYTHON_DOTENV_DISABLED='1',PYTHONUNBUFFERED='1',PYTHONPATH=str(Path(__file__).resolve().parents[2]),LANGSMITH_TRACING='false',LANGCHAIN_TRACING_V2='false')
            # SDK configuration stays in the child process environment, never in reports.
            env.update(read(self.root/'credentials.json',{}))
            s.update(status='RUNNING',stage='실행 준비',attempt=s.get('attempt',0)+1,updated_at=time.time(),heartbeat_at=time.time());atomic(folder/'status.json',s)
            try:
                with open(folder/'worker.log','ab') as log:self.process=subprocess.Popen([str(self.python),str(self.worker),'--job',str(folder)],cwd=folder,env=env,stdout=log,stderr=log,start_new_session=True)
                os.chmod(folder/'worker.log',0o600);self.active_id=jid
            except OSError:s.update(status='FAILED',error='WORKER_START_FAILED',updated_at=time.time());atomic(folder/'status.json',s)
    def state(self):
        self.ensure_thread();jobs=self.jobs();cfg=self.config();keys=read(self.root/'credentials.json',{})
        rapid=getattr(getattr(self,'paper',None),'rapid',None)
        return {'upstream':UPSTREAM,'runtime_ready':self.python.is_file(),'config':cfg,'roles':ROLES,'providers':PROVIDERS,'configured_keys':sorted(keys),'key_names':sorted(KEY_NAMES),
          'rapid_scan':rapid.state() if rapid else None,
          'features':[{'id':k,'name':v} for k,v in FEATURES],'jobs':jobs[:30],'automatic_execution':'fresh current-date crypto opinions only; existing budget/cost/quote gates remain mandatory',
          'supervisor':read(self.root/'supervisor.json',{}),'data_status':{'FRED':'configured' if keys.get('FRED_API_KEY') else 'key_required','Alpha Vantage':'configured' if keys.get('ALPHA_VANTAGE_API_KEY') else 'key_required','Jev':'configured' if keys.get('TYPESAFE_API_KEY') else 'optional_key_required','SEC EDGAR':'stock_only','Coinbase':'existing_market_cache','Yahoo / Reddit / StockTwits / Polymarket':'public_sources_may_be_unavailable'}}
    def detail(self,jid):
        folder=self.job_path(jid);s=read(folder/'status.json')
        if not s:raise ValueError('AGENTS_JOB_NOT_FOUND')
        return {'job':s,'results':read(folder/'results.json',[]),'partial':read(folder/'partial.json',{}),'backtest':read(folder/'backtest.json'),'settings':read(folder/'request.json')['config']}
    def report(self,jid,ticker):
        tickers([ticker]);folder=self.job_path(jid);req=read(folder/'request.json')
        if not req or ticker not in req['tickers']:raise ValueError('AGENTS_REPORT_NOT_FOUND')
        p=folder/'reports'/ticker/'complete_report.md'
        if not p.is_file():raise ValueError('AGENTS_REPORT_NOT_READY')
        return {'filename':ticker+'-'+req['date']+'.md','text':p.read_text()}
    def memory(self):
        p=self.root/'memory.json';return {'entries':read(p,[]),'note':'분석 뒤 보유 기간이 지난 판단은 원본 엔진이 벤치마크 대비 결과와 회고를 기록합니다. 계좌의 확정 수익과 다릅니다.'}
    def maybe_auto(self,engine,mode='paper'):
        cfg=self.config()
        # Continuous local candidate comparison replaces hourly single-coin
        # automatic reports. Manual full-team analysis remains available.
        if mode=='paper' and cfg['rapid_enabled'] and cfg['provider']=='ollama' and getattr(engine,'rapid',None):return
        if not cfg['auto_enabled'] or any(s['status'] in ('QUEUED','RUNNING') for s in self.jobs()):return
        last=read(self.root/'auto.json',{})
        if time.time()-last.get('attempted_at',0)<cfg['auto_minutes']*60:return
        with engine.repo.db() as c:
            if mode=='paper' and (not engine.repo.get(c,'multi_running') or engine.repo.get(c,'halted')):return
            if mode=='live' and not engine.enabled():return
            held=[p['product'] for p in engine.repo.rows(c,'positions',10000) if float(p['quantity'])>0]
        ready=list(engine.intelligence.history) if engine.intelligence else []
        candidates=list(dict.fromkeys(held+ready))
        candidates=[p for p in candidates if p.endswith('-USD')]
        if not candidates:return
        index=last.get('cursor',0)%len(candidates);symbol=candidates[index]
        atomic(self.root/'auto.json',{'attempted_at':time.time(),'cursor':index+1})
        self.start({'ticker':symbol,'automatic':True,'mode':mode},engine)

def manager(app):
    with _LOCK:
        key=str(app.DATA)
        if key not in _managers:_managers[key]=Manager(app)
        return _managers[key]
