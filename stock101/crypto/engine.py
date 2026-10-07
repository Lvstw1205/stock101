"""Strategy → hard risk → validation → atomic PAPER execution → immutable journal."""
import datetime as dt
import hashlib
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from decimal import Decimal, ROUND_DOWN
from .exchange import CoinbaseAdapter, ExchangeError, number, product_id
from .repository import Repository
from . import risk, strategy, execution_strategies

class Engine:
    def __init__(self,path,adapter=None):
        self.repo=Repository(path);self.adapter=adapter or CoinbaseAdapter();self.lock=threading.RLock()
        from .stream import Monitor
        self.monitor=Monitor(self.adapter,self.fail)
        self.health={'state':'OFFLINE','code':'NOT_CONNECTED'};self.products=[];self._products_at=0;self._fatal=False;self.monitor_product=None
        with self.repo.db() as c:
            if not self.repo.one(c,'strategy_versions','balanced_v1'):self.repo.put(c,'strategy_versions','balanced_v1',strategy.DEFAULT)
    def fail(self,code,error=None):
        # Keep the original failure; the daemon must not replace it with a wrapper.
        if self._fatal:return
        diagnostic=None
        if error is not None:
            diagnostic={'type':type(error).__name__,'frames':[
                {'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
                for f in traceback.extract_tb(error.__traceback__)[-5:]]}
        self.health={'state':'HALTED','code':code,'checked_at':time.time(),'diagnostic':diagnostic}
        self._fatal=True  # fail-closed even when SQLite cannot persist the halt
        try:self.repo.halt(code,diagnostic)
        except Exception:pass
    def discover(self):
        if time.time()-self._products_at<300 and self.products:return self.products
        try:
            self.products=self.adapter.get_products();self._products_at=time.time()
            self.health={'state':'HEALTHY','code':'PUBLIC_REST_CONNECTED','checked_at':time.time()}
            return self.products
        except Exception as error:
            self.fail('PRODUCT_DISCOVERY_FAILED',error);raise ValueError('COINBASE_UNAVAILABLE') from None
    def market(self,pid):
        product_id(pid)
        if pid not in {p['product_id'] for p in self.discover()}:raise ValueError('UNSUPPORTED_PRODUCT')
        try:
            p=self.adapter.get_product(pid);bars=self.adapter.get_candles(pid);book=self.adapter.get_orderbook(pid)
            self.health={'state':'HEALTHY','code':'PUBLIC_REST_CONNECTED','checked_at':time.time()}
            self.monitor_product=pid
            return p,bars,book
        except Exception as e:
            code=str(e) if isinstance(e,ExchangeError) else 'INVALID_MARKET_DATA'
            self.fail(code,e);raise ValueError(code) from None
    def initialize_currency(self,c,currency):
        c.execute('INSERT OR IGNORE INTO paper_accounts VALUES(?,?,?)',(currency,'10000','10000'))
    def positions(self,c):return self.repo.rows(c,'positions',10000)
    def value(self,c,currency,books):
        account=c.execute('SELECT * FROM paper_accounts WHERE currency=?',(currency,)).fetchone()
        if not account:raise ValueError('PAPER_ACCOUNT_MISSING')
        cash=number(account['cash']);invested=Decimal(0);cost=Decimal(0);rows=[]
        for pos in self.positions(c):
            if pos['currency']!=currency or number(pos['quantity'])==0:continue
            book=books.get(pos['product'])
            if not book or not -5<=time.time()-book['timestamp']<=15:raise ValueError('PORTFOLIO_MARK_STALE')
            if callable(getattr(self.adapter,'validate_book',None)):self.adapter.validate_book(book)
            # Bid liquidation value, not a fabricated mid-market fill.
            price=number(book['bids'][0]['price']);value=number(pos['quantity'])*price
            invested+=value;cost+=number(pos['cost'])
            rows.append({**pos,'price':str(price),'market_value':str(value),'unrealized':str(value-number(pos['cost']))})
        return {'currency':currency,'cash':str(cash),'initial':account['initial'],'equity':str(cash+invested),'invested':str(invested),'unrealized':str(invested-cost),'positions':rows}
    def books_for(self,currency,initial=None):
        books=dict(initial or {})
        with self.repo.db() as c:positions=self.positions(c)
        held=[pos['product'] for pos in positions if pos['currency']==currency and number(pos['quantity'])>0]
        if callable(getattr(self.adapter,'set_held_products',None)):self.adapter.set_held_products(held)
        missing=[pid for pid in held if pid not in books]
        # Marks are independent reads. Bound concurrency to avoid serial quote
        # age accumulating across holdings; the adapter still paces requests.
        if missing:
            if callable(getattr(self.adapter,'get_orderbooks',None)):
                books.update(self.adapter.get_orderbooks(missing))
            else:
                with ThreadPoolExecutor(max_workers=min(4,len(missing))) as pool:
                    books.update(zip(missing,pool.map(self.adapter.get_orderbook,missing)))
        if self.retry_stale_books():
            # Refresh only marks that aged during the other reads; never change
            # their exchange timestamps or loosen the 15-second execution gate.
            for pid,b in list(books.items()):
                if not -5<=time.time()-b['timestamp']<=12:books[pid]=self.adapter.get_orderbook(pid)
        return books
    def reconcile_ledger(self,c):
        expected_cash={row['currency']:number(row['initial']) for row in c.execute('SELECT * FROM paper_accounts')}
        quantities={};costs={}
        for row in reversed(self.repo.rows(c,'paper_orders',1000000)):
            symbol=row['product'];currency=row['currency'];qty=number(row['quantity']);notional=number(row['notional']);fee=number(row['fee'])
            if qty<=0 or notional<=0 or fee<0:raise ValueError('LEDGER_INVALID_FILL')
            held=quantities.get(symbol,Decimal(0));cost=costs.get(symbol,Decimal(0))
            if row['side']=='BUY':
                expected_cash[currency]-=notional+fee;quantities[symbol]=held+qty;costs[symbol]=cost+notional+fee
            elif row['side']=='SELL' and held>=qty and held>0:
                expected_cash[currency]+=notional-fee;quantities[symbol]=held-qty;costs[symbol]=Decimal(0) if qty==held else cost-cost*qty/held
            else:raise ValueError('LEDGER_INCONSISTENT')
        for row in c.execute('SELECT * FROM paper_accounts'):
            if number(row['cash'])!=expected_cash[row['currency']] or number(row['cash'])<0:raise ValueError('CASH_RECONCILIATION_FAILED')
        positions={p['product']:p for p in self.positions(c)}
        if set(quantities)!=set(positions):raise ValueError('POSITION_RECONCILIATION_FAILED')
        for symbol,pos in positions.items():
            if number(pos['quantity'])!=quantities[symbol] or number(pos['cost'])!=costs[symbol]:raise ValueError('POSITION_RECONCILIATION_FAILED')
    def release(self,pid):
        with self.lock:
            try:
                p,bars,book=self.market(pid);books=self.books_for(p['quote_currency_id'],{pid:book})
                with self.repo.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('DATABASE_INTEGRITY_FAILED')
                    self.initialize_currency(c,p['quote_currency_id'])
                    self.reconcile_ledger(c)
                    self.value(c,p['quote_currency_id'],books)
                    self.repo.set(c,'halted',False);self.repo.set(c,'halt_reason',None);self.repo.set(c,'armed',None)
                    self.repo.put(c,'system_events',str(time.time_ns()),{'category':'system','code':'PAPER_LEDGER_RECONCILED','product':pid})
                self._fatal=False
                return {'ok':True,'message':'Paper ledger and current quotes reconciled. Automation remains disarmed.'}
            except Exception:self.fail('RECOVERY_FAILED');raise
    def halt(self):
        with self.lock:self.fail('USER_KILL_SWITCH')
        return {'ok':True}
    def analyze(self,pid,version='balanced_v1'):
        p,bars,book=self.market(pid)
        with self.repo.db() as c:
            config=self.repo.one(c,'strategy_versions',version)
            pos=self.repo.one(c,'positions',pid)
        if not config:raise ValueError('UNKNOWN_STRATEGY_VERSION')
        result=self.evaluate_signal(bars,pos,config,book)
        return {'product':p,'book':book,'bars':bars,'analysis':result,'recommendation_only':True}
    def arm(self,pid,version,confirmed):
        if confirmed is not True:raise ValueError('PAPER_CONFIRMATION_REQUIRED')
        with self.lock:
            analysis=self.analyze(pid,version)
            with self.repo.db() as c:
                c.execute('BEGIN IMMEDIATE')
                if self._fatal or self.repo.get(c,'halted') or self.repo.get(c,'mode')!='PAPER':raise ValueError('PAPER_RECOVERY_REQUIRED')
                self.repo.set(c,'armed',{'product':pid,'version':version,'after':analysis['bars'][-1]['start']})
                self.repo.put(c,'system_events',str(time.time_ns()),{'category':'execution','code':'PAPER_ARMED','product':pid,'version':version})
        return {'ok':True}
    def mode(self,mode):
        if mode not in ('PAPER','BACKTEST'):raise ValueError('LIVE_EXECUTION_DISABLED')
        with self.lock,self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE');self.repo.set(c,'mode',mode);self.repo.set(c,'armed',None)
        return {'ok':True}
    def cycle(self):
        with self.repo.db() as c:cfg=self.repo.get(c,'armed')
        if not cfg:return {'status':'DISARMED'}
        return self.decide(cfg['product'],cfg['version'],automatic=True)
    def decide(self,pid,version='balanced_v1',automatic=False):
        with self.lock:
            if self._fatal:raise ValueError('PAPER_RECOVERY_REQUIRED')
            market_ready=False
            try:
                p,bars,book=self.market(pid);books=self.books_for(p['quote_currency_id'],{pid:book})
                book=books.get(pid,book);market_ready=True
                with self.repo.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if self.repo.get(c,'mode')!='PAPER':raise ValueError('PAPER_MODE_REQUIRED')
                    config=self.repo.one(c,'strategy_versions',version)
                    if not config:raise ValueError('UNKNOWN_STRATEGY_VERSION')
                    if automatic:
                        cfg=self.repo.get(c,'armed')
                        if self.execution_stopped() or not cfg or cfg['product']!=pid or cfg['version']!=version:return {'status':'DISARMED'}
                        if self.require_bar_boundary(version) and bars[-1]['start']<=cfg['after']:return {'status':'WAITING_FOR_NEW_HOURLY_BAR'}
                    key=self.decision_key(pid,version,bars,book)
                    existing=self.repo.one(c,'trade_decisions',key)
                    if existing:return {**existing,'duplicate':True}
                    self.initialize_currency(c,p['quote_currency_id']);before=self.value(c,p['quote_currency_id'],books)
                    pos=self.repo.one(c,'positions',pid)
                    if pos and number(pos['quantity'])>0:
                        pos['overweight']=number(pos['quantity'])*number(book['mid'])>number(before['equity'])*number(risk.DEFAULTS['asset_exposure'])
                    signal=self.evaluate_signal(bars,pos,config,book);decision_id=self.repo.decision_id(c)
                    context=self.context(c,before,book,pos)
                    context['signal_deviation']=str(abs(number(book['mid'])/number(signal.get('plan',{}).get('reference_price',bars[-1]['close']))-1))
                    side='BUY' if signal['decision']=='BUY' else 'SELL' if signal['decision'] in ('SELL','REDUCE') else None
                    requested=number(before['equity'])*number(signal.get('allocation',.02)) if side=='BUY' else number(pos['quantity'])*number(book['mid'])*number(signal['sell_fraction']) if side=='SELL' and pos else Decimal(0)
                    if side=='BUY' and signal.get('requested_cash_cad') is not None:requested=min(requested,number(signal['requested_cash_cad']))
                    check=risk.assess(requested,side,context,self.execution_limits(),paper_observation=self.observe_paper_losses()) if side else {'status':'BLOCK','reason':'NO_EXECUTABLE_SIGNAL','approved_notional':'0','requested_notional':'0'}
                    # A quote can expire while other holdings/history are read.
                    # Multi-asset PAPER rolls back this attempt and retries with
                    # fresh data; the age limit and final fill check stay intact.
                    if self.retry_stale_books() and check.get('reason')=='STALE_MARKET_DATA':raise ExchangeError('STALE_MARKET_DATA')
                    if context['halted']:check={'status':'HALT','reason':'TRADING_HALTED','approved_notional':'0','requested_notional':str(requested)}
                    record={'decision_id':decision_id,'timestamp':time.time(),'exchange':'Coinbase','mode':'PAPER','status':'SIMULATED_NO_TRADE','portfolio':p['quote_currency_id'],'product':pid,'strategy_version':version,'signal_bar':bars[-1]['start'],'market_price':book['mid'],'analysis':signal,'airi_recommendation':{'source':'deterministic evidence summary','decision':signal['decision'],'executable':False},'risk':check,'risk_context':context,'requested_order':{'side':side,'notional':str(requested)},'portfolio_before':before,'execution':None}
                    if check['status']=='HALT':
                        self.repo.set(c,'halted',True);self.repo.set(c,'halt_reason',check['reason']);self.repo.set(c,'armed',None)
                        self.repo.set(c,'halt_at',time.time())
                        self.repo.put(c,'system_events',str(time.time_ns()),{'category':'risk','code':check['reason'],'product':pid})
                    if side and check['status'] in ('PASS','REDUCE'):
                        try:record['execution']=self.fill(c,key,p,book,pos,side,check,before,signal)
                        except ValueError as e:
                            record['risk']={**check,'status':'BLOCK','reason':str(e)}
                        else:record['status']=record['execution']['status']
                    elif pos and number(pos['quantity'])>0:
                        pos['peak']=str(max(number(pos.get('peak','0')),number(book['mid'])))
                        self.repo.put(c,'positions',pid,pos,replace=True)
                    record['portfolio_after']=self.value(c,p['quote_currency_id'],books)
                    self.after_valuation(c,record['portfolio_after'],books)
                    self.repo.put(c,'trade_decisions',key,record)
                    self.repo.put(c,'strategy_signals',decision_id,signal)
                    self.repo.put(c,'risk_decisions',decision_id,record['risk'])
                    self.repo.put(c,'market_snapshots',decision_id,{'product':p,'book':book,'bar':bars[-1]})
                    self.repo.put(c,'portfolio_snapshots',decision_id,record['portfolio_after'])
                    return record
            except ExchangeError as e:
                if self.retry_stale_books() and str(e) in ('STALE_MARKET_DATA','PORTFOLIO_MARK_STALE'):return self.stale_book_wait(pid,str(e),getattr(e,'products',None))
                if self.retry_stale_books() and not market_ready and str(e) in ('EXCHANGE_UNAVAILABLE','RATE_LIMITED','RATE_LIMIT_COOLDOWN','MARKET_DATA_REFRESH_PENDING','PAPER_FX_REFERENCE_UNAVAILABLE_OR_STALE'):
                    return self.stale_book_wait(pid,str(e),getattr(e,'products',None))
                self.fail(str(e));raise
            except Exception as e:
                if self.retry_stale_books() and isinstance(e,ValueError) and str(e)=='PORTFOLIO_MARK_STALE':return self.stale_book_wait(pid,str(e))
                # Includes rollback after a write or ambiguous commit. No automatic retry.
                self.fail('EXECUTION_OR_DATABASE_FAILURE',e);raise ValueError('EXECUTION_HALTED_CHECK_JOURNAL') from None
    def execution_limits(self):return risk.DEFAULTS
    def observe_paper_losses(self):return False
    def retry_stale_books(self):return False
    def after_valuation(self,c,portfolio,books):pass
    def stale_book_wait(self,pid,reason,unavailable_products=None):
        with self.repo.db() as c:self.repo.put(c,'system_events',str(time.time_ns()),{'category':'market_data','code':'PAPER_MARKET_DATA_RETRY','product':pid,'reason':reason,'unavailable_products':unavailable_products or []})
        return {'status':'MARKET_DATA_UNAVAILABLE','product':pid,'timestamp':time.time(),'risk':{'status':'BLOCK','reason':reason},'execution':None}
    def execution_stopped(self):return False
    def require_bar_boundary(self,version):return True
    def decision_key(self,pid,version,bars,book):
        return hashlib.sha256(f'PAPER:{pid}:{bars[-1]["start"]}'.encode()).hexdigest()
    def evaluate_signal(self,bars,pos,config,book):
        return strategy.evaluate(bars,pos,config)
    def validate_execution(self,side,quantity,notional,fee,cost,held,signal):
        pass
    def context(self,c,portfolio,book,pos):
        now=time.time();day=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
        orders=[x for x in self.repo.rows(c,'paper_orders',100000) if x['currency']==portfolio['currency']]
        today=[x for x in orders if dt.datetime.fromtimestamp(x['created'],dt.timezone.utc).strftime('%Y-%m-%d')==day]
        snaps=self.repo.equity_series(c,portfolio['currency'])
        equity=number(portfolio['equity']);peak=max([number(portfolio['initial']),equity]+[number(x['equity']) for x in snaps])
        prior=[x for x in snaps if dt.datetime.fromtimestamp(x['created'],dt.timezone.utc).strftime('%Y-%m-%d')<day]
        start=number(prior[0]['equity']) if prior else number(portfolio['initial'])
        losses=0
        for x in [x for x in orders if x['side']=='SELL']:
            if number(x['realized'])<0:losses+=1
            else:break
        last_loss=next((x['created'] for x in orders if number(x['realized'])<0),0)
        return {'equity':str(equity),'cash':portfolio['cash'],'invested':portfolio['invested'],'asset_value':str(number(pos['quantity'])*number(book['mid']) if pos else 0),'halted':self.repo.get(c,'halted') or self._fatal,'age':now-book['timestamp'],'spread_bps':book['spread_bps'],'daily_loss':str(max(Decimal(0),(start-equity)/start)),'drawdown':str((peak-equity)/peak),'consecutive_losses':losses,'since_loss':now-last_loss,'trades_hour':sum(x['created']>now-3600 for x in orders),'buys_hour':sum(x['created']>now-3600 and x['side']=='BUY' for x in orders),'deployed_today':str(sum((number(x['notional'])+number(x['fee']) for x in today if x['side']=='BUY'),Decimal(0)))}
    def fill(self,c,key,p,book,pos,side,check,before,signal):
        slip=number(risk.DEFAULTS['slippage_bps'])/10000
        levels=book['asks' if side=='BUY' else 'bids']
        # IOC model consumes visible depth; unfilled remainder is cancelled, never retried.
        budget=number(check['approved_notional']);held=number(pos['quantity']) if pos else Decimal(0)
        if side=='BUY':budget/=1+number(risk.DEFAULTS['fee_bps'])/10000
        increment=number(p['base_increment']);quantity=Decimal(0);notional=Decimal(0);unslipped=Decimal(0)
        for level in levels:
            raw=number(level['price']);price=raw*(1+slip if side=='BUY' else 1-slip)
            if side=='BUY' and signal.get('plan',{}).get('entry_limit') is not None and price>number(signal['plan']['entry_limit']):break
            remaining=(budget-notional)/price
            take=min(number(level['size']),remaining,held-quantity if side=='SELL' else remaining)
            take=(take/increment).to_integral_value(rounding=ROUND_DOWN)*increment
            if take<=0:break
            quantity+=take;notional+=take*price;unslipped+=take*raw
        if self.execution_stopped() or self._fatal or self.repo.get(c,'halted'):raise ValueError('TRADING_HALTED')
        if callable(getattr(self.adapter,'validate_book',None)):self.adapter.validate_book(book)
        risk.validate_order(p,book,quantity,notional,side,number(before['cash']),held,check,time.time())
        fee=notional*number(risk.DEFAULTS['fee_bps'])/10000;cash=number(before['cash']);cost=number(pos['cost']) if pos else Decimal(0)
        if side=='BUY' and notional+fee>number(check['approved_notional']):raise ValueError('APPROVED_AMOUNT_EXCEEDED')
        realized=Decimal(0)
        if side=='BUY':cash-=notional+fee;newqty=held+quantity;newcost=cost+notional+fee
        else:
            removed=cost if quantity==held else cost*quantity/held;realized=notional-fee-removed;cash+=notional-fee;newqty=held-quantity;newcost=cost-removed
        self.validate_execution(side,quantity,notional,fee,cost,held,signal)
        if cash<0 or newqty<0:raise ValueError('LEDGER_NEGATIVE_BALANCE')
        c.execute('UPDATE paper_accounts SET cash=? WHERE currency=?',(str(cash),p['quote_currency_id']))
        newpos={'product':p['product_id'],'currency':p['quote_currency_id'],'quantity':str(newqty),'cost':str(newcost),'peak':str(max(number(pos.get('peak','0')) if pos else Decimal(0),number(book['mid']))),'profit_taken':bool(pos and pos.get('profit_taken')) or 'PARTIAL_PROFIT_TAKING' in signal['reason_codes']}
        if pos and pos.get('entry_plan'):newpos['entry_plan']=pos['entry_plan']
        if side=='BUY' and held==0 and signal.get('plan'):
            newpos['entry_plan']=execution_strategies.entry_plan(signal)
        if newqty==0:newpos.update(cost='0',peak='0',profit_taken=False,entry_plan=None)
        self.repo.put(c,'positions',p['product_id'],newpos,replace=True)
        residual=budget-notional
        row={'mode':'PAPER','status':'SIMULATED_PARTIAL_IOC' if residual>max(number(p['quote_increment']),increment*(notional/quantity)) else 'SIMULATED_FILLED','product':p['product_id'],'currency':p['quote_currency_id'],'side':side,'quantity':str(quantity),'notional':str(notional),'average_fill':str(notional/quantity),'fee':str(fee),'slippage':str(abs(notional-unslipped)),'realized':str(realized),'unfilled_notional_cancelled':str(max(Decimal(0),residual))}
        row.update(strategy_id=signal.get('strategy_id'),strategy_token=signal.get('plan',{}).get('entry_token') if side=='BUY' else None,reason_codes=signal['reason_codes'])
        self.repo.put(c,'paper_orders',key,row);self.repo.put(c,'fills',key,row)
        return row
    def state(self,compact=False):
        with self.repo.db() as c:
            accounts=[dict(x) for x in c.execute('SELECT * FROM paper_accounts')]
            return {'mode':self.repo.get(c,'mode'),'halted':self.repo.get(c,'halted') or self._fatal,'halt_reason':self.repo.get(c,'halt_reason'),'armed':self.repo.get(c,'armed'),'health':self.health,'accounts':accounts,'positions':self.positions(c),'versions':self.repo.rows(c,'strategy_versions'),'journal':self.repo.decision_summaries(c,100) if compact else self.repo.rows(c,'trade_decisions',100),'events':self.repo.rows(c,'system_events',50),'limits':self.execution_limits(),'live_enabled':False,'transport':'Fresh REST books for fills; optional public WebSocket monitoring','websocket':self.monitor.status(),'account_eligibility':'Public product discovery does not verify Canadian account eligibility.'}
    def portfolio(self):
        with self.repo.db() as c:currencies=[x[0] for x in c.execute('SELECT currency FROM paper_accounts')]
        results=[]
        try:
            for currency in currencies:
                books=self.books_for(currency)
                with self.repo.db() as c:
                    c.execute('BEGIN IMMEDIATE')
                    portfolio=self.value(c,currency,books)
                    portfolio.setdefault('mark_status','FRESH')
                    portfolio.setdefault('mark_as_of',min((b['timestamp'] for b in books.values()),default=time.time()))
                    self.after_valuation(c,portfolio,books)
                    orders=[x for x in self.repo.rows(c,'paper_orders',100000) if x['currency']==currency]
                    snapshots=self.repo.equity_series(c,currency)
                    today=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
                    prior=[x for x in snapshots if dt.datetime.fromtimestamp(x['created'],dt.timezone.utc).strftime('%Y-%m-%d')<today]
                    start=number(prior[0]['equity']) if prior else number(portfolio['initial'])
                    peak=max([number(portfolio['equity']),number(portfolio['initial'])]+[number(x['equity']) for x in snapshots])
                    portfolio.update(realized=str(sum((number(x['realized']) for x in orders),Decimal(0))),fees=str(sum((number(x['fee']) for x in orders),Decimal(0))),today_pnl=str(number(portfolio['equity'])-start),drawdown=str(1-number(portfolio['equity'])/peak),valuation_note='Sampled equity; daily baseline is previous observed UTC-day closing value, or initial capital.')
                    results.append(portfolio)
                    key='mark:'+currency+':'+str(int(time.time())//60)
                    if not self.repo.one(c,'portfolio_snapshots',key):self.repo.put(c,'portfolio_snapshots',key,portfolio)
        except ExchangeError:raise ValueError('PORTFOLIO_MARK_UNAVAILABLE') from None
        except ValueError as error:
            # An unavailable UI mark is not an execution failure. Execution
            # independently revalidates every quote, age, cash and exposure.
            if str(error)=='PORTFOLIO_MARK_STALE':raise ValueError('PORTFOLIO_MARK_UNAVAILABLE') from None
            self.fail('PORTFOLIO_MARK_FAILED',error);raise ValueError('PORTFOLIO_MARK_FAILED') from None
        except Exception as error:self.fail('PORTFOLIO_MARK_FAILED',error);raise ValueError('PORTFOLIO_MARK_FAILED') from None
        return {'portfolios':results,'fx_conversion':False}
