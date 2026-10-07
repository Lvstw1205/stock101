"""Isolated CAD paper plan: contributions never touch a real account.
Foreign-quote products fail closed until executable FX pricing is implemented.
Experimental trend-based net return scenario gates paper buys; no live execution.
"""
import time,threading,hashlib,json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from .engine import Engine
from .paper_fx import CadPaperAdapter
from . import strategy,net_edge,risk,planner,execution_strategies,performance_bonus,sizing
from .exchange import ExchangeError,number

from .budget_policy import INITIAL_CAD,WEEKLY_CAD,WEEK_SECONDS,description,migrate_paper
WEEK=WEEK_SECONDS
class MultiEngine(Engine):
    def __init__(self,path,adapter=None,strategy_version=planner.VERSION):
        super().__init__(path,adapter or CadPaperAdapter())
        if strategy_version not in (planner.VERSION,execution_strategies.VERSION):raise ValueError('UNKNOWN_STRATEGY_VERSION')
        self.active_version=strategy_version
        self.last_cycle_at=None;self.last_cycle_completed_at=None;self.cursor=0;self.scan={'status':'STOPPED','eligible':0,'excluded_fx':0}
        self.intelligence=None;self.rapid=None;self._quote_pool=None;self.latest={};self._prepared={};self._unavailable={};self._paper_stop=threading.Event();self._paper_stop.set()
        self.recovery={'status':'IDLE','attempts':0,'next_retry_at':None};self._stable_cycles=0;self._recovery_integrity_checked=False
        self.suspended_products={};self._missing_product_checks={};self._suspension_lock=threading.RLock()
        self.last_progress_at=None;self.market_wait=None
        self.learning=None
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.repo.one(c,'strategy_versions',planner.VERSION):self.repo.put(c,'strategy_versions',planner.VERSION,{**strategy.DEFAULT,'name':planner.VERSION})
            if not self.repo.one(c,'strategy_versions',execution_strategies.VERSION):self.repo.put(c,'strategy_versions',execution_strategies.VERSION,{**strategy.DEFAULT,'name':execution_strategies.VERSION,'strategies':execution_strategies.CATALOG})
            migrate_paper(self.repo,c,time.time())
            self.repo.set(c,'multi_running',False)
            self._resume_requested=bool(self.repo.get(c,'paper_resume_requested',False))
        if self._resume_requested:self.recovery.update(status='RECOVERING',next_retry_at=time.time()+2,reason='STARTUP_RECONCILIATION_REQUIRED')
        self.contribute()
    def initialize_currency(self,c,currency):
        if currency!='CAD':raise ValueError('FX_COST_NOT_VERIFIED')
        if not c.execute("SELECT 1 FROM paper_accounts WHERE currency='CAD'").fetchone():raise ValueError('CAD_PLAN_MISSING')
    def contribute(self,now=None):
        # Funding was applied atomically at setup/migration. Elapsed weeks never
        # create additional money, even after a long shutdown.
        return
    def start_multi(self,recovered=False):
        self.contribute()
        with self.lock,self.repo.db() as c:
            if recovered and not self._resume_requested:return {'ok':False}
            c.execute('BEGIN IMMEDIATE');self.reconcile_ledger(c)
            self.repo.set(c,'paper_resume_requested',True);self._resume_requested=True
            self.repo.set(c,'mode','PAPER');self.repo.set(c,'multi_running',True)
            self.repo.set(c,'halted',False);self.repo.set(c,'halt_reason',None);self.repo.set(c,'armed',None)
            self.repo.set(c,'halt_at',None)
            if not recovered:self.repo.set(c,'paper_started_at',time.time())
            self.repo.put(c,'system_events',str(time.time_ns()),{'category':'execution','code':'PAPER_AUTO_RECOVERED' if recovered else 'PAPER_STARTED'})
            self.repo.set(c,'multi_after',int(time.time())//3600*3600-3601)
        self._fatal=False;self._paper_stop.clear();self.last_cycle_at=None;self.last_cycle_completed_at=None
        self._stable_cycles=0
        self.recovery.update(status='MONITORING',next_retry_at=None,recovered_at=time.time() if recovered else None)
        if not recovered:self.recovery['attempts']=0
        self.scan={'status':'SCANNING','eligible':0,'excluded_fx':0}
        return {'ok':True}
    def halt(self):
        self._resume_requested=False;self._paper_stop.set();self.recovery.update(status='USER_STOPPED',next_retry_at=None)
        with self.lock,self.repo.db() as c:self.repo.set(c,'multi_running',False);self.repo.set(c,'paper_resume_requested',False)
        return super().halt()
    def fail(self,code,error=None):
        self._paper_stop.set()
        if self._fatal:return
        self._recovery_integrity_checked=False
        try:
            with self.repo.db() as c:self.repo.set(c,'multi_running',False)
        except Exception:pass
        super().fail(code,error)
        if self._resume_requested:
            blocked=code=='RISK_CALCULATION_FAILED'
            self.recovery.update(status='ACTION_REQUIRED' if blocked else 'RECOVERING',reason=code,
                                 next_retry_at=None if blocked else time.time()+min(300,15*2**min(self.recovery['attempts'],5)))
    def recover_if_due(self,before_resume=None):
        if not self._resume_requested:return
        with self.repo.db() as c:
            running=self.repo.get(c,'multi_running') and not self.repo.get(c,'halted') and not self._fatal
            reason=self.repo.get(c,'halt_reason')
        if running:return
        if reason=='RISK_CALCULATION_FAILED':
            self.recovery.update(status='ACTION_REQUIRED',reason=reason,next_retry_at=None);return
        if self.recovery['status']!='RECOVERING':self.recovery.update(status='RECOVERING',reason=reason,next_retry_at=time.time()+15)
        if time.time()<(self.recovery.get('next_retry_at') or 0):return
        self.recovery['attempts']+=1
        self.recovery.update(next_retry_at=time.time()+min(300,15*2**min(self.recovery['attempts'],5)),last_attempt_at=time.time())
        try:
            with self.lock:
                if not self._resume_requested:return
                with self.repo.db() as c:
                    # Scan the database once per recovery episode. Quote warmup
                    # retries still reconcile all balances/fills on every attempt.
                    if not self._recovery_integrity_checked:
                        self.recovery['phase']='CHECKING_LEDGER'
                        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('LEDGER_INTEGRITY_FAILED')
                        self._recovery_integrity_checked=True
                    self.reconcile_ledger(c)
                self.recovery['phase']='CHECKING_MARKET'
                # Rebuild market caches, then verify every held asset with fresh
                # books. Never fabricate data or rewrite a damaged ledger.
                # Keep product discovery while asynchronous quote reads warm up.
                # Rediscovering on each attempt ages the books just completed by
                # the previous attempt and can starve recovery with many holdings.
                if self.products and time.time()-self._products_at<300:products=self.products
                else:
                    products=self.adapter.get_products();self.products=products;self._products_at=time.time()
                if not any(p['tradable'] for p in products):raise ValueError('NO_ELIGIBLE_PRODUCTS')
                books=self.books_for('CAD')
                with self.repo.db() as c:self.value(c,'CAD',books)
                if before_resume:before_resume()
                if not self._resume_requested:return
                self.products=products;self._products_at=time.time();self._prepared.clear();self._unavailable.clear()
                self.start_multi(recovered=True)
        except Exception as error:
            self.recovery['last_error']=str(error) if isinstance(error,(ExchangeError,ValueError)) and len(str(error))<90 and all(c.isupper() or c.isdigit() or c=='_' for c in str(error)) else 'RECOVERY_CHECK_FAILED'
            self.recovery['pending_products']=getattr(error,'products',[])
            if isinstance(error,ExchangeError) and str(error) in ('MARKET_DATA_REFRESH_PENDING','STALE_MARKET_DATA','PORTFOLIO_MARK_STALE'):
                # Pending requests are coalesced by PaperBooks. Check the same
                # work promptly, before its fresh quotes expire. Network/rate
                # failures keep the normal backoff; stale quotes cannot trade.
                self.recovery.update(next_retry_at=time.time()+2,pending_products=getattr(error,'products',[]))
    def mode(self,mode):
        if mode=='BACKTEST':self.halt()
        return super().mode(mode)
    def enable_background(self):
        from .intelligence import Intelligence
        if callable(getattr(self.adapter,'enable_book_cache',None)):self.adapter.enable_book_cache()
        self.intelligence=Intelligence(self.adapter,self.repo)
        if self.active_version==execution_strategies.VERSION:
            from .rapid_scan import RapidScanner
            self.rapid=RapidScanner(self)
            self._quote_pool=ThreadPoolExecutor(max_workers=4,thread_name_prefix='stock101-candidate-quotes')
    def execution_limits(self):
        return {**sizing.LIMITS,'trades_per_hour':30,'entry_rate_only':True} if self.active_version==execution_strategies.VERSION else risk.DEFAULTS
    def books_for(self,currency,initial=None):
        from . import paper_suspensions
        return paper_suspensions.books_for(self,currency,initial)
    def value(self,c,currency,books):
        from . import paper_suspensions
        return paper_suspensions.value(self,c,currency,books)
    def observe_paper_losses(self):return True
    def execution_stopped(self):return self._paper_stop.is_set()
    def retry_stale_books(self):return True
    def require_bar_boundary(self,version):return version not in (planner.VERSION,execution_strategies.VERSION)
    def decision_key(self,pid,version,bars,book):
        if version not in (planner.VERSION,execution_strategies.VERSION):return super().decision_key(pid,version,bars,book)
        # Same quote within a five-second window is idempotent. A changed quote
        # gets a new assessment without waiting for another completed hourly bar.
        raw=f'PAPER:{version}:{pid}:{bars[-1]["start"]}:{int(book["timestamp"])//5}:{book["bids"][0]["price"]}:{book["asks"][0]["price"]}'
        return hashlib.sha256(raw.encode()).hexdigest()
    def market(self,pid):
        if not self.intelligence:return super().market(pid)
        if pid in self._prepared:return self._prepared.pop(pid)
        cached=self.intelligence.get(pid,allow_stale=True)
        if not cached:
            # A missing historical cache uses the same transient read retry path
            # as cached assets, including assets already held by the portfolio.
            return self.adapter.get_product(pid),self.adapter.get_candles(pid),self.adapter.get_orderbook(pid)
        p=self.adapter.get_product(pid);book=self.adapter.get_orderbook(pid)
        self.health={'state':'HEALTHY','code':'FRESH_REST_WITH_HISTORICAL_PLAN','checked_at':time.time()}
        return p,cached['bars'],book
    def stale_book_wait(self,pid,reason,unavailable_products=None):
        self.market_wait={'product':pid,'reason':reason,'at':time.time(),'unavailable_products':unavailable_products or []}
        return super().stale_book_wait(pid,reason,unavailable_products)
    def validation_evidence(self,pid):
        from .validation_job import evidence_for
        return evidence_for(self.repo.path.parent,pid)
    def evaluate_signal(self,bars,pos,config,book):
        if config.get('name')==execution_strategies.VERSION:
            pid=book['product_id'];previous=(self.latest.get(pid,{}).get('plan') or {}).get('current')
            daily=self.intelligence.get_daily(pid) if self.intelligence else self.adapter.get_daily_candles(pid)
            product=self.intelligence.products.get(pid,{'base_currency_id':pid.split('-')[0]}) if self.intelligence else {'base_currency_id':pid.split('-')[0]}
            news=self.intelligence.news.context(product) if self.intelligence else None
            with self.repo.db() as c:
                fills=self.repo.rows(c,'fills',10000);anchor=self.repo.get(c,'cad_plan')['anchor']
            benchmark=self.intelligence.bitcoin_context(book['timestamp']) if self.intelligence else None
            from . import agent_bridge
            options=dict(daily=daily,news=news,previous_price=previous,fills=fills,anchor=anchor,benchmark=benchmark,fx_context=self.intelligence.fx_context(book['timestamp']) if self.intelligence and hasattr(self.intelligence,'fx_context') else None,evidence=self.validation_evidence(pid))
            signal=execution_strategies.evaluate(bars,pos,book,**options)
            # The AI can select among current rule-qualified entry strategies.
            # Recalculate against this real book, never execute a screening price
            # or let an LLM turn an unqualified WAIT into an order.
            selected=agent_bridge.selected_entry(signal,pos,book,self.repo.path.parent)
            if selected and selected!=signal.get('strategy_id'):
                chosen=execution_strategies.evaluate(bars,pos,book,only_strategy=selected,**options)
                if chosen['decision']=='BUY':
                    chosen['strategy_candidates']=signal['strategy_candidates'];signal=chosen
            signal=agent_bridge.apply(signal,pos,book,self.repo.path.parent,'paper')
            return self.learning.apply(signal) if self.learning else signal
        if config.get('name')==planner.VERSION:
            pid=book['product_id'];previous=(self.latest.get(pid,{}).get('plan') or {}).get('current')
            model=None;news=None
            if self.intelligence:
                cached=self.intelligence.get(pid,allow_stale=True);model=cached['forecast'] if cached else None
                product=self.intelligence.products.get(pid,{'base_currency_id':pid.split('-')[0]})
                news=self.intelligence.news.context(product)
            signal=planner.evaluate(bars,pos,book,model=model,news=news,previous_price=previous)
            if self.intelligence and not self.intelligence.get(pid) and not (pos and number(pos['quantity'])>0):
                signal.update(decision='WAIT',reason_codes=['HISTORY_REFRESH_PENDING'])
            if signal['decision']=='BUY':
                with self.repo.db() as c:
                    last=next((r for r in self.repo.rows(c,'paper_orders',10000) if r['product']==pid and r['side']=='BUY'),None)
                if last and time.time()-last['created']<3600:
                    signal.update(decision='WAIT',reason_codes=['ENTRY_COOLDOWN'])
                    signal['plan']['next_entry_after']=last['created']+3600
            return signal
        signal=strategy.evaluate(bars,pos,config)
        edge=net_edge.estimate(bars,book);signal['net_edge']=edge
        if signal['decision']=='BUY' and not edge['passed']:
            signal.update(decision='WAIT',reason_codes=[edge['reason']])
        if pos and number(pos.get('quantity','0'))>0:
            quantity=number(pos['quantity']);cost=number(pos['cost'])
            exit_value=quantity*number(book['bids'][0]['price'])*(1-number(risk.DEFAULTS['slippage_bps'])/10000)*(1-number(risk.DEFAULTS['fee_bps'])/10000)
            protective=set(signal['reason_codes']) & {'TRAILING_PROTECTION','PORTFOLIO_REBALANCE','TREND_AND_MOMENTUM_EXIT'}
            if cost>0 and exit_value/cost-1>=net_edge.MIN_NET and not protective:
                signal.update(decision='SELL',sell_fraction=1,reason_codes=['NET_PROFIT_TAKE'])
        return signal
    def validate_execution(self,side,quantity,notional,fee,cost,held,signal):
        if side=='BUY':
            edge=signal.get('net_edge',{})
            if execution_strategies.scheduled_dca(signal):
                if quantity*number(edge['immediate_exit_per_unit_after_cost'])/(notional+fee)-1<Decimal('-.03'):raise ValueError('DCA_COST_LIMIT')
                return
            if not edge.get('passed'):raise ValueError('NET_EDGE_TOO_SMALL')
            projected=quantity*number(edge['scenario_exit_per_unit_after_cost'])
            if projected/(notional+fee)-1<net_edge.MIN_NET:raise ValueError('NET_EDGE_LOST_TO_BOOK_DEPTH')
        elif side=='SELL' and not (set(signal['reason_codes']) & (planner.PROTECTIVE|execution_strategies.PROTECTIVE)):
            removed=cost*quantity/held
            if not execution_strategies.profit_policy.exit_allowed(notional-fee,removed,signal,net_edge.MIN_NET):raise ValueError('NET_EXIT_PROFIT_TOO_SMALL')
    def fill(self,c,key,p,book,pos,side,check,before,signal):
        if p.get('product_id') in self.suspended_products:raise ValueError('PRODUCT_NOT_FOUND')
        if side=='BUY' and self.suspended_products:raise ValueError('SUSPENDED_ASSET_ENTRY_HOLD')
        if book.get('executable') is False or book.get('source')=='INDICATIVE_TICKER_SCREEN':raise ValueError('SCREENING_PRICE_NOT_EXECUTABLE')
        if p['quote_currency_id']!='CAD':raise ValueError('FX_COST_NOT_VERIFIED')
        liquidity=None
        if side=='BUY' and self.learning:
            from .learning import exit_liquidity
            liquidity=exit_liquidity(book,pos['quantity'] if pos else '0',check['approved_notional'])
            signal.setdefault('plan',{})['exit_liquidity']=liquidity
            if not liquidity['passed']:raise ValueError(liquidity['reason'])
        result=super().fill(c,key,p,book,pos,side,check,before,signal)
        result.update(valuation_currency='CAD',fx_rate=book.get('fx_rate','1'),fx_markup_bps=book.get('fx_markup_bps','0'),fx_reference=book.get('fx_reference'),net_edge=signal.get('net_edge'),cost_model='PAPER assumed fees and FX; not actual Coinbase fee tier')
        if self.learning:
            from .learning import VERSION
            plan=signal.get('plan') or {}
            result.update(learning_policy=VERSION,execution_version=signal.get('strategy_version'),
                          learning=plan.get('learning'),exit_liquidity=liquidity,
                          entry_thesis={k:plan.get(k) for k in ('strategy_id','strategy_name','target','invalidation','exit_at','entry_token')} if side=='BUY' else None)
        for table in ('paper_orders','fills'):self.repo.put(c,table,key,result,replace=True)
        return result
    def after_valuation(self,c,portfolio,books):
        if portfolio.get('mark_status')=='PARTIAL':
            performance_bonus.observe(self.repo,c,'paper',time.time(),None,portfolio.get('mark_as_of'))
            return
        net=number(portfolio['cash']);stamp=time.time()
        for p in portfolio['positions']:
            book=books[p['product']];stamp=min(stamp,book['timestamp'])
            value=performance_bonus.liquidation_value(p['quantity'],book,number(risk.DEFAULTS['fee_bps'])/10000,number(risk.DEFAULTS['slippage_bps'])/10000)
            if value is None:net=None;break
            net+=value  # CAD book bids already include the adverse FX markup.
        performance_bonus.observe(self.repo,c,'paper',time.time(),net,stamp)
    def cycle(self):
        self.contribute()
        with self.lock:
            with self.repo.db() as c:
                if not self.repo.get(c,'multi_running') or self.repo.get(c,'halted') or self._fatal:return {'status':'STOPPED'}
                after=self.repo.get(c,'multi_after')
            self.last_cycle_at=time.time()
            self.last_progress_at=self.last_cycle_at
            universe=self.discover()
            tradable=[p for p in universe if p['tradable'] and p['quote_currency_id']=='CAD']
            # One quote per base asset avoids duplicate USD/CAD exposures.
            with self.repo.db() as c:held=[p['product'] for p in self.positions(c) if number(p['quantity'])>0]
            unique={}
            for p in sorted(tradable,key=lambda x:(x['product_id'] not in held,x.get('native_quote_currency_id','CAD')!='CAD',-number(x.get('quote_volume_24h','0')))):
                unique.setdefault(p['base_currency_id'],p)
            candidates=[p['product_id'] for p in unique.values()]
            if self.intelligence:self.intelligence.set_products(list(unique.values()))
            if self.rapid:self.rapid.ensure_thread()
            with self.repo.db() as c:held=[p['product'] for p in self.positions(c) if number(p['quantity'])>0]
            held=[pid for pid in held if pid not in self.suspended_products]
            self.scan={'status':'RUNNING','eligible':len(candidates),'excluded_fx':0,'checked_at':self.scan.get('checked_at'),'universe_size':len(tradable),'scope':'All eligible CAD/USD spot assets; one quote per asset, no top-20 cap','recent':self.scan.get('recent',[])}
            if not candidates and not held:
                self.scan['status']='NO_ELIGIBLE_PRODUCTS';self.last_cycle_completed_at=time.time();return self.scan
            batch=sorted(held,key=lambda p:self.latest.get(p,{}).get('at',0))[:2]
            if self.intelligence:
                ticks=self.intelligence.stream.snapshot();priority=[]
                for pid in candidates:
                    row=self.latest.get(pid,{});plan=row.get('plan');tick=ticks.get(pid)
                    if not plan or not tick or time.time()-tick['timestamp']>15 or time.time()-row['at']<8:continue
                    # Stream prices are native; plan prices are CAD.
                    rate=float(self.adapter.fx()['rate']) if pid.endswith('-USD') else 1
                    current=float(tick['price'])*rate
                    near=plan.get('entry_low') is not None and plan.get('entry_high') is not None and plan['entry_low']<=current<=plan['entry_high']
                    exit_due=pid in held and ((plan.get('invalidation') is not None and current<=plan['invalidation']) or (plan.get('target') is not None and current>=plan['target']))
                    if near or exit_due:
                        priority.append(pid)
                batch+=sorted(priority,key=lambda p:(p not in held,-float((self.latest.get(p,{}).get('plan') or {}).get('sizing',{}).get('score',0)),self.latest.get(p,{}).get('at',0)))[:2]
            ready=[p for p in candidates if time.time()-self._unavailable.get(p,0)>60 and (not self.intelligence or self.intelligence.get(p))]
            for _ in range(min(4,len(ready))):
                pid=ready[self.cursor%len(ready)];self.cursor+=1
                if pid not in batch:batch.append(pid)
            preparation_errors={}
            if self.rapid:
                batch=self.rapid_batch(candidates,held)
                # Independent metadata/book reads overlap, while ledger writes
                # and final budget checks remain strictly serial.
                futures={pid:self._quote_pool.submit(self.market,pid) for pid in batch}
                for pid,future in futures.items():
                    try:self._prepared[pid]=future.result()
                    except ExchangeError as error:preparation_errors[pid]=error
            for pid in dict.fromkeys(batch):
                if self._paper_stop.is_set():break
                if self.intelligence and not self.intelligence.get(pid,allow_stale=pid in held):continue
                if self.intelligence and (pid not in held or self.rapid):
                    try:
                        if pid in preparation_errors:raise preparation_errors[pid]
                        if pid not in self._prepared:self._prepared[pid]=self.market(pid)
                    except ExchangeError as e:
                        self._unavailable[pid]=time.time()
                        row={'at':time.time(),'product':pid,'reason':str(e)[:80],'status':'MARKET_DATA_UNAVAILABLE','plan':None,'retry_at':time.time()+60}
                        self.latest[pid]=row
                        with self.repo.db() as c:self.repo.put(c,'system_events',str(time.time_ns()),{'category':'market_data','code':'PAPER_MARKET_DATA_RETRY',**row})
                        self.last_progress_at=time.time();self.market_wait={'product':pid,'reason':str(e),'at':time.time()}
                        continue
                with self.repo.db() as c:
                    if not self.repo.get(c,'multi_running') or self.repo.get(c,'halted'):break
                    self.repo.set(c,'armed',{'product':pid,'version':self.active_version,'after':after})
                result=self.decide(pid,self.active_version,automatic=True)
                self.last_progress_at=time.time()
                if result.get('decision_id'):self.market_wait=None
                reason=result.get('risk',{}).get('reason',result.get('status'))
                if reason=='NO_EXECUTABLE_SIGNAL':reason=', '.join(result.get('analysis',{}).get('reason_codes',[]))
                row={'at':result.get('timestamp',time.time()),'price_checked_at':time.time(),'product':pid,'reason':reason,'status':result.get('status'),'decision_id':result.get('decision_id'),'net_edge':result.get('analysis',{}).get('net_edge'),'plan':result.get('analysis',{}).get('plan'),'strategies':result.get('analysis',{}).get('strategy_candidates',[]),'duplicate':result.get('duplicate',False),'requested_cash':result.get('requested_order',{}).get('notional'),'approved_cash':result.get('risk',{}).get('approved_notional')}
                self.latest[pid]=row
                if result.get('status')=='MARKET_DATA_UNAVAILABLE':
                    self._unavailable[pid]=time.time();row['retry_at']=time.time()+60
                elif self._unavailable.pop(pid,None) is not None:
                    with self.repo.db() as c:self.repo.put(c,'system_events',str(time.time_ns()),{'category':'market_data','code':'PAPER_MARKET_DATA_RECOVERED','product':pid})
                self.scan['recent']=([row]+[r for r in self.scan['recent'] if r['product']!=pid])[:20]
                self.scan.update(last_product=pid,last_result=reason)
            self.scan['checked_at']=time.time()
            self.last_cycle_completed_at=time.time()
            self._stable_cycles+=1
            if self._stable_cycles>=10:self.recovery['attempts']=0
            return self.scan
    def rapid_batch(self,candidates,held,now=None):
        now=time.time() if now is None else now
        ranked=self.rapid.ranked(now)
        allowed=(set(candidates)|set(held))-set(self.suspended_products)
        def ready(pid):
            row=self.latest.get(pid,{})
            return pid in allowed and now-self._unavailable.get(pid,0)>2 and now-row.get('at',0)>=3
        exits=[r['product'] for r in ranked if r['decision'] in ('SELL','REDUCE') and ready(r['product'])]
        buys=[r['product'] for r in ranked if r['decision']=='BUY' and ready(r['product'])]
        # Age precedes score within each priority class, so a repeatedly blocked
        # candidate cannot monopolize the queue or starve other eligible coins.
        exits.sort(key=lambda pid:self.latest.get(pid,{}).get('at',0))
        buys.sort(key=lambda pid:self.latest.get(pid,{}).get('at',0)//15)
        quiet=sorted((pid for pid in held if ready(pid)),key=lambda pid:self.latest.get(pid,{}).get('at',0))
        batch=list(dict.fromkeys(exits[:4]+quiet[:2]+buys[:4]))[:8]
        if len(batch)<8:
            rest=sorted((r['product'] for r in ranked if ready(r['product']) and r['product'] not in batch),key=lambda pid:self.latest.get(pid,{}).get('at',0))
            batch+=rest[:min(1,8-len(batch))]
        # Startup: histories/tickers can warm asynchronously. Held assets are
        # still assessed and protected before the first full screen completes.
        return batch
    def research_state(self):
        research=self.intelligence.state() if self.intelligence else {}
        screened=self.rapid.snapshot() if self.rapid else {}
        coverage=[]
        if self.intelligence:
            for pid in list(self.intelligence.products):
                r=screened.get(pid) or self.latest.get(pid,{});error=research.get('history_errors',{}).get(pid)
                daily=self.intelligence.daily.get(pid,{})
                coverage.append({'product':pid,'assessed_at':r.get('at'),'reason':r.get('reason'),'screening_only':r.get('screening_only',False),'price_at':r.get('price_at'),'execution_checked_at':self.latest.get(pid,{}).get('at'),
                                 'history_ready':self.intelligence.get(pid) is not None,'daily_bars':len(daily.get('bars',[])),'error':error})
        catalog=[]
        if self.active_version==execution_strategies.VERSION:
            with self.repo.db() as c:fills=self.repo.rows(c,'fills',10000)
            for rule in execution_strategies.CATALOG:
                assessments=[s for r in list((screened or self.latest).values()) for s in r.get('strategies',[]) if s['strategy_id']==rule['id'] and s['reason']!='BTC_ONLY']
                trades=[f for f in fills if f.get('strategy_id')==rule['id']]
                catalog.append({**rule,'assessed_assets':len(assessments),'entry_matches':sum(s['decision']=='BUY' for s in assessments),'fills':len(trades),'last_fill_at':max((f['created'] for f in trades),default=None)})
        return {'research':research,'coverage':coverage,'plans':sorted(list(self.latest.values()),key=lambda r:r['at'],reverse=True)[:8],
                'assessed_assets':sum(r.get('status')=='SCREENED' for r in screened.values()) if screened else len(self.latest),'rapid_scan':self.rapid.state() if self.rapid else None,'strategy_version':self.active_version,
                'execution_strategies':catalog,'sizing_policy':sizing.describe() if self.active_version==execution_strategies.VERSION else None}
    def inspect_plan(self,pid):
        row=self.latest.get(pid)
        history=self.intelligence.get(pid) if self.intelligence else None
        if row and (row.get('plan') or {}).get('strategy_id'):
            return {'assessment':row,'strategy_bundle':True,'hourly_chart':history['bars'][-72:] if history else [],'daily_chart':(self.intelligence.get_daily(pid,allow_stale=True) or [])[-220:] if self.intelligence else []}
        forecast=history['forecast'] if history else ((row or {}).get('plan') or {}).get('forecast')
        if not forecast:raise ValueError('PLAN_NOT_READY')
        return {'assessment':row,'forecast':forecast}
    def plan_state(self):
        self.contribute()
        with self.repo.db() as c:
            plan=self.repo.get(c,'cad_plan');paid=self.repo.get(c,'cad_paid_period',0)
            account=dict(c.execute("SELECT * FROM paper_accounts WHERE currency='CAD'").fetchone())
            bonus=performance_bonus.state(self.repo,c,'paper',time.time())
            plan={**plan,'weekly':bonus['weekly_cad'],'next_weekly':bonus['next_weekly_cad'],'performance_bonus':bonus}
            running=self.repo.get(c,'multi_running') and not self.repo.get(c,'halted') and not self._fatal
            trade_count=c.execute('SELECT COUNT(*) FROM paper_orders').fetchone()[0]
            last_trade=c.execute('SELECT MAX(created) FROM paper_orders').fetchone()[0]
            events=[r for r in self.repo.rows(c,'system_events',1000) if r.get('category')=='contribution']
            last=self.repo.rows(c,'trade_decisions',1)
            sales=[r for r in self.repo.rows(c,'paper_orders',100000) if r['side']=='SELL' and r['currency']=='CAD']
            losses=0
            for sale in sales:
                if number(sale['realized'])>=0:break
                losses+=1
            last_risk=last[0].get('risk_context',{}) if last else {}
            observation={'mode':'CONTINUOUS_PAPER_OBSERVATION','consecutive_losses':losses,
                         'streak_warning':losses>=self.execution_limits()['consecutive_losses'],
                         'loss_warning':number(last_risk.get('daily_loss',0))>=number(self.execution_limits()['daily_loss']) or number(last_risk.get('drawdown',0))>=number(self.execution_limits()['drawdown']),
                         'daily_loss':last_risk.get('daily_loss'),'drawdown':last_risk.get('drawdown'),
                         'daily_loss_warning':self.execution_limits()['daily_loss'],'drawdown_warning':self.execution_limits()['drawdown'],
                         'note':'Losses are recorded without stopping paper simulation. Cash, exposure, market-data and ledger checks remain enforced. LIVE limits are unchanged.'}
            activity={'started_at':self.repo.get(c,'paper_started_at'),'last_assessment_at':last[0]['timestamp'] if last else None,
                      'loss_observation':observation,
                      'last_product':last[0]['product'] if last else None,'assessment_count':c.execute('SELECT COUNT(*) FROM trade_decisions').fetchone()[0],
                      'halt_reason':self.repo.get(c,'halt_reason'),'halt_at':self.repo.get(c,'halt_at'),
                      'last_cycle_completed_at':self.last_cycle_completed_at,'recovery':{**self.recovery,'requested':self._resume_requested},
                      'last_progress_at':self.last_progress_at,'market_wait':self.market_wait,'suspended_products':list(self.suspended_products.values()),
                      'book_stream':self.adapter.book_stream_status() if callable(getattr(self.adapter,'book_stream_status',None)) else None,
                      'events':[r for r in self.repo.rows(c,'system_events',1000) if r.get('category') in ('system','execution','risk')][:8]}
        return {**self.research_state(),'activity':activity,'shared_budget_policy':description(),'plan':plan,'account':account,'next_contribution':None,'running':bool(running),'last_cycle_at':self.last_cycle_at,'trade_count':trade_count,'last_trade_at':last_trade,'scan':self.scan,'contributions':events,'live_enabled':False,'entry_enabled':not bool(self.suspended_products),'block_reason':'SUSPENDED_ASSET_ENTRY_HOLD' if self.suspended_products else None,'minimum_net_return':str(net_edge.MIN_NET),'cost_model':{'fee_bps_each_way':risk.DEFAULTS['fee_bps'],'slippage_bps_each_way':risk.DEFAULTS['slippage_bps'],'fx_markup_bps_each_way':'25','fx_source':'Bank of Canada daily USD/CAD reference; paper approximation only','return_model':'Sixteen execution rules with conditional exit targets; not a return forecast. Weekly DCA uses a cost cap instead of the short-term target gate.' if self.active_version==execution_strategies.VERSION else 'Rolling-origin 6h forecast; not a profit guarantee'},'weekly_note':'One-time total principal of 1000 CAD. No recurring contributions. One 20 CAD reward at 200 CAD cumulative net realized profit.'}
    def funding_adjusted_metrics(self,c,portfolio):
        import datetime as dt
        today=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
        snapshots=self.repo.equity_series(c,'CAD')
        initial=number(portfolio['initial']);equity=number(portfolio['equity'])
        def adjusted(row):return number(row['equity'])+initial-number(row['initial'])
        prior=next((r for r in snapshots if dt.datetime.fromtimestamp(r['created'],dt.timezone.utc).strftime('%Y-%m-%d')<today),None)
        start=adjusted(prior) if prior else initial
        peak=max([initial,equity]+[adjusted(r) for r in snapshots])
        return equity-start,max(Decimal(0),(start-equity)/start),max(Decimal(0),(peak-equity)/peak)
    def context(self,c,portfolio,book,pos):
        result=super().context(c,portfolio,book,pos)
        _,daily,drawdown=self.funding_adjusted_metrics(c,portfolio)
        result.update(daily_loss=str(daily),drawdown=str(drawdown));return result
    def portfolio(self):
        try:result=super().portfolio()
        except ValueError as error:
            if str(error)!='PORTFOLIO_MARK_UNAVAILABLE':raise
            # Display-only fallback. Risk, execution and bonus qualification
            # continue requiring fresh books and never consume this response.
            with self.repo.db() as c:
                row=c.execute("SELECT created,payload FROM portfolio_snapshots WHERE json_extract(payload,'$.currency')='CAD' ORDER BY created DESC LIMIT 1").fetchone()
                if not row:raise
                p=self.repo.decode_payload(row['payload']);stamp=p.get('mark_as_of',row['created'])
                orders=[o for o in self.repo.rows(c,'paper_orders',100000) if o['currency']=='CAD' and o['created']<=row['created']]
                p.update(mark_status='STALE',mark_as_of=stamp,valuation_note='Last saved valuation; not a current executable price.',
                         realized=str(sum((number(o['realized']) for o in orders),Decimal(0))),fees=str(sum((number(o['fee']) for o in orders),Decimal(0))))
                result={'portfolios':[p],'fx_conversion':False}
        with self.repo.db() as c:
            for p in result['portfolios']:
                pnl,_,drawdown=self.funding_adjusted_metrics(c,p)
                p.update(today_pnl=str(pnl),drawdown=str(drawdown),valuation_note='CAD contributions excluded from returns and loss metrics.')
        return result
