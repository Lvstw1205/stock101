"""Live spot execution with isolated intents, preview checks and reconciliation.
No automatic deposits/converts. Only already-funded quote currency is spent.
Every restart disarms. Unknown submissions are reconciled, never retried blindly.
"""
import datetime as dt,hashlib,time,threading,uuid
from decimal import Decimal,ROUND_DOWN,ROUND_UP
from .repository import Repository
from .exchange import CoinbaseAdapter,ExchangeError,number
from .live_exchange import LiveAdapter
from .paper_fx import CadPaperAdapter
from . import strategy,net_edge,risk,credentials,planner,execution_strategies,performance_bonus,sizing
from .budget_policy import INITIAL_CAD,WEEKLY_CAD,WEEK_SECONDS
TERMINAL={'FILLED','CANCELLED','EXPIRED','FAILED'}
class LiveEngine:
    def __init__(self,path,adapter=None,market=None,fx=None,clock=time.time,strategy_version=planner.VERSION):
        self.repo=Repository(path);self.clock=clock;self.lock=threading.RLock();self.stop_event=threading.Event();self.stop_event.set()
        self.strategy_version=strategy_version
        if strategy_version not in (planner.VERSION,execution_strategies.VERSION):raise ValueError('UNKNOWN_STRATEGY_VERSION')
        self.adapter=adapter or LiveAdapter(credential_provider=lambda:credentials.read('LIVE') or credentials.read())
        self.market=market or CoinbaseAdapter();self.fx=fx or CadPaperAdapter();self._permit=False;self.adapter.permit=lambda kind:self._permit and (kind=='cancel' or self.enabled())
        self.intelligence=None;self.latest={}
        self.products=[];self._products_at=0;self.cursor=0;self.status_cache={};self.last_cycle=None;self.last_completed=None
        with self.repo.db() as c:
            self.repo.set(c,'live_running',False);self.repo.set(c,'live_reason','STARTUP_CONFIRMATION_REQUIRED')
            cfg=self.repo.get(c,'live_config')
            if cfg:self.repo.set(c,'live_config',{**cfg,'strategy_version':strategy_version,'initial_cad':str(INITIAL_CAD),'weekly_cad':'0','recurring':False})
    def enable_background(self):
        from .intelligence import Intelligence
        self.intelligence=Intelligence(self.market,self.repo)
    def default_limits(self):return sizing.LIMITS if self.strategy_version==execution_strategies.VERSION else risk.DEFAULTS
    def config(self):
        with self.repo.db() as c:return self.repo.get(c,'live_config')
    def enabled(self):
        with self.repo.db() as c:return self.repo.get(c,'live_running',False) and not self.stop_event.is_set()
    def pending(self):
        with self.repo.db() as c:return [r for r in self.repo.rows(c,'orders',100000) if r['state'] not in ('SETTLED','REJECTED')]
    def event(self,code,extra=None):
        with self.repo.db() as c:self.repo.put(c,'system_events',str(time.time_ns()),{'category':'live','code':code,**(extra or {})})
    def fail(self,reason):
        self.stop_event.set();self._permit=False
        with self.repo.db() as c:self.repo.set(c,'live_running',False);self.repo.set(c,'live_reason',reason)
        self.event(reason)
    def quote_rate(self,quote):
        if quote=='CAD':return Decimal(1)
        rate=number(self.fx.fx()['rate'])
        if quote=='USD':return rate
        if quote=='USDC':
            b=self.market.get_orderbook('USDC-USD')
            # Budget and cash valuation use adverse upper USD value, never 1:1.
            if not -5<=self.clock()-b['timestamp']<=15:raise ValueError('STALE_FX_BOOK')
            return rate*number(b['asks'][0]['price'])
        raise ValueError('UNSUPPORTED_FUNDING_CURRENCY')
    def configure(self,body):
        with self.lock:return self._configure(body)
    def _configure(self,body):
        if self.enabled() or self.pending():raise ValueError('STOP_AND_RECONCILE_FIRST')
        quote=body.get('quote')
        if quote not in ('CAD','USD','USDC'):raise ValueError('SELECT_FUNDING_CURRENCY')
        minimum=number(body.get('minimum_net','.005'));fx_cost=number(body.get('fx_cost_bps','25'))
        if not Decimal('.005')<=minimum<=Decimal('.20') or not 0<=fx_cost<=500:raise ValueError('INVALID_COST_POLICY')
        if quote=='CAD':fx_cost=Decimal(0)  # Direct CAD spot orders have no CAD conversion leg.
        if body.get('costs_confirmed') is not True or body.get('enable_live') is not True:raise ValueError('EXPLICIT_LIVE_CONFIGURATION_REQUIRED')
        limits=dict(self.default_limits());custom=body.get('risk_limits',{})
        configurable={'single_order','asset_exposure','total_exposure','daily_deployment','daily_loss','drawdown','cash_reserve'}
        if not isinstance(custom,dict) or set(custom)-configurable:raise ValueError('INVALID_RISK_CONFIGURATION')
        for k,v in custom.items():
            n=number(v);default=number(self.default_limits()[k])
            if not (default<=n<=Decimal('.99') if k=='cash_reserve' else Decimal('.001')<=n<=default):raise ValueError('RISK_LIMIT_CANNOT_BE_WEAKENED')
            limits[k]=str(n)
        with self.repo.db() as c:
            held=[p for p in self.repo.rows(c,'positions') if number(p['quantity'])>0]
            old=self.repo.get(c,'live_config')
            if held and old and old['quote']!=quote:raise ValueError('OPEN_POSITION_CURRENCY_LOCK')
            cfg={'quote':quote,'strategy_version':self.strategy_version,'minimum_net':str(minimum),'fx_cost_bps':str(fx_cost),'initial_cad':str(INITIAL_CAD),'weekly_cad':'0','recurring':False,'risk_limits':limits,'configured_at':self.clock(),'allow_live':True}
            self.repo.set(c,'live_config',cfg)
        self.event('USER_CONFIGURED_LIVE');return {'ok':True,'configuration':cfg,'running':False}
    def readiness(self):
        cfg=self.config();result={'checked_at':self.clock(),'configured':bool(cfg),'config':cfg,'ready':False,'checks':[]}
        try:
            permissions=self.adapter.permissions();result['permissions']=permissions
            balances=self.adapter.balances();fees=self.adapter.fees();result['fees']=fees
            result['funded_currencies']=[{'currency':k,'available':v['available']} for k,v in balances.items() if k in ('CAD','USD','USDC') and number(v['available'])>0]
            markets=[p for p in self.adapter.trading_products() if p['tradable']]
            result['eligible_currencies']={q:sum(p['quote_currency_id']==q for p in markets) for q in ('CAD','USD','USDC')}
            result['cad_markets']=[p['product_id'] for p in markets if p['quote_currency_id']=='CAD']
            result['major_asset_markets']={base:[p['product_id'] for p in markets if p['base_currency_id']==base and p['quote_currency_id'] in ('CAD','USD','USDC')] for base in ('BTC','ETH','SOL')}
            result['execution_cost_note']='Advanced Trade limit IOC uses taker fees. No Simple Buy, Convert, transfer, or automated FX.'
            result['checks'].append({'name':'계정 조회','ok':True})
            result['checks'].append({'name':'매매 권한 · 출금 권한 없음','ok':permissions['trade']})
            if cfg:
                rate=self.quote_rate(cfg['quote']);cash=number(balances.get(cfg['quote'],{}).get('available','0'))
                products=[p for p in markets if p['quote_currency_id']==cfg['quote']]
                result.update(quote=cfg['quote'],available=str(cash),cad_reference_rate=str(rate),eligible_products=len(products),budget_cad=str(self.cap()))
                result['checks'].extend([{'name':'입금된 거래 통화','ok':cash>0},{'name':'계정에서 거래 가능한 코인','ok':bool(products)},{'name':'미확정 주문 없음','ok':not self.pending()}])
                result['ready']=all(x['ok'] for x in result['checks'])
            else:result['checks'].append({'name':'실거래 예산·비용 설정','ok':False})
        except Exception as e:
            result['error']=self.safe_error(e);result['checks'].append({'name':'연결·시세 확인','ok':False})
        if self.status_cache.get('summary'):result['summary']=self.status_cache['summary']
        self.status_cache=result;return result
    def cap(self):
        with self.repo.db() as c:
            anchor=self.repo.get(c,'live_anchor',self.clock());award=self.repo.get(c,performance_bonus.KEY)
        return performance_bonus.cap(anchor,self.clock(),award)
    @staticmethod
    def safe_error(e):
        text=str(e)
        return text if text and len(text)<100 and all(x.isupper() or x.isdigit() or x=='_' for x in text) else 'LIVE_CHECK_FAILED'
    def activate(self,body,before_start=None):
        if body.get('confirmation')!='실제 돈으로 자동매매 시작' or body.get('confirmed') is not True:raise ValueError('LOCAL_LIVE_CONFIRMATION_REQUIRED')
        with self.lock:
            if self.pending():self.reconcile()
            ready=self.readiness()
            if not ready['ready']:raise ValueError('LIVE_REQUIREMENTS_NOT_MET')
            self.audit_ledger();self.reconcile_positions(self.adapter.balances())
            if before_start:before_start()
            with self.repo.db() as c:
                if self.repo.get(c,'live_anchor') is None:self.repo.set(c,'live_anchor',self.clock())
                self.repo.set(c,'live_running',True);self.repo.set(c,'live_reason',None)
            self.stop_event.clear();self.event('USER_ARMED_LIVE')
        return {'ok':True,'mode':'LIVE'}
    def stop(self):
        # Set before acquiring lock: a slow preview cannot submit after stop request.
        self.stop_event.set()
        with self.repo.db() as c:self.repo.set(c,'live_running',False);self.repo.set(c,'live_reason','USER_STOP')
        with self.lock:
            for row in self.pending():
                if row.get('order_id'):
                    try:
                        self._permit=True;self.adapter.cancel_order(row['order_id'])
                    except Exception:self.event('CANCEL_STATUS_UNCERTAIN',{'client_order_id':row['client_order_id']})
                    finally:self._permit=False
        return {'ok':True,'pending_reconciliation':len(self.pending())}
    def reconcile_positions(self,balances):
        with self.repo.db() as c:positions=self.repo.rows(c,'positions',10000)
        total={}
        for p in positions:total[p['base']]=total.get(p['base'],Decimal(0))+number(p['quantity'])
        for base,qty in total.items():
            b=balances.get(base,{});held=number(b.get('available','0'))+number(b.get('held','0'))
            if held<qty:raise ValueError('LIVE_POSITION_BALANCE_MISMATCH')
    def audit_ledger(self):
        """Replay only this bot's confirmed fills before live activation/recovery."""
        with self.repo.db() as c:
            if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('LIVE_LEDGER_INTEGRITY_FAILED')
            fills=self.repo.rows(c,'fills',1000000);positions=self.repo.rows(c,'positions',100000)
            orders={o['client_order_id']:o for o in self.repo.rows(c,'orders',1000000)}
        replay={}
        for f in reversed(fills):
            o=orders.get(f['client_order_id'])
            if not o or o['state']!='SETTLED' or any(f[k]!=o[k] for k in ('product','side','order_id','decision_id')):raise ValueError('LIVE_LEDGER_ORDER_MISMATCH')
            if any(number(f[a])!=number(o[b]) for a,b in (('quantity','filled_size'),('notional','filled_value'),('fee','fees'))):raise ValueError('LIVE_LEDGER_FILL_MISMATCH')
            p=replay.setdefault(f['product'],{'quantity':Decimal(0),'cost':Decimal(0),'cost_cad':Decimal(0),'entry_cost_reserve':Decimal(0)})
            qty,value,fee,rate=[number(f[k]) for k in ('quantity','notional','fee','cad_rate')]
            if qty<=0 or value<=0 or fee<0 or rate<=0:raise ValueError('LIVE_LEDGER_INVALID_FILL')
            if f['side']=='BUY':
                p['quantity']+=qty;p['cost']+=value+fee;p['cost_cad']+=(value+fee)*rate
                p['entry_cost_reserve']+=(value+fee)*(1+number(o.get('fx_cost_bps','0'))/10000)
            elif f['side']=='SELL':
                if qty>p['quantity']:raise ValueError('LIVE_LEDGER_OVERSELL')
                for field in ('cost','cost_cad','entry_cost_reserve'):p[field]-=p[field]*qty/p['quantity']
                p['quantity']-=qty
            else:raise ValueError('LIVE_LEDGER_INVALID_SIDE')
        actual={p['product']:p for p in positions}
        if set(actual)!=set(replay):raise ValueError('LIVE_LEDGER_POSITION_MISMATCH')
        for pid,p in replay.items():
            if any(abs(number(actual[pid].get(k,'0'))-v)>Decimal('.000000000000000001') for k,v in p.items()):raise ValueError('LIVE_LEDGER_POSITION_MISMATCH')
        fill_ids={f['client_order_id'] for f in fills}
        for o in orders.values():
            if o['state']=='SETTLED' and number(o.get('filled_size','0'))>0 and o['client_order_id'] not in fill_ids:raise ValueError('LIVE_LEDGER_MISSING_FILL')
        return True
    def reconcile(self):
        with self.lock:
            for row in self.pending():
                try:
                    if row.get('order_id'):order=self.adapter.get_order(row['order_id'])
                    else:
                        start=dt.datetime.fromtimestamp(row['submitted_at']-60,dt.timezone.utc).isoformat()
                        order=self.adapter.find_order(row['client_order_id'],start)
                    if not order:raise ValueError('ORDER_STATUS_UNVERIFIED')
                    self.settle(row,order)
                except Exception as e:self.fail(self.safe_error(e));return False
            if self.pending():return False
            try:self.audit_ledger();self.reconcile_positions(self.adapter.balances());return True
            except Exception as e:self.fail(self.safe_error(e));return False
    def settle(self,row,order):
        if not isinstance(order,dict) or order.get('client_order_id')!=row['client_order_id'] or order.get('product_id')!=row['product'] or order.get('side')!=row['side']:raise ValueError('ORDER_IDENTITY_MISMATCH')
        oid=order.get('order_id');status=order.get('status')
        if not isinstance(oid,str) or not oid or (row.get('order_id') and oid!=row['order_id']):raise ValueError('INVALID_ORDER_ID')
        if status not in TERMINAL or (number(order.get('filled_size','0'))>0 and order.get('settled') is not True):
            with self.repo.db() as c:self.repo.put(c,'orders',row['client_order_id'],{**row,'state':'PENDING','order_id':oid,'exchange_status':status},replace=True)
            if self.clock()-row['submitted_at']>60:self.fail('ORDER_SETTLEMENT_DELAYED')
            return
        qty=number(order.get('filled_size','0'));value=number(order.get('filled_value','0'));fee=number(order.get('total_fees','0'))
        if min(qty,value,fee)<0 or qty>number(row['size']):raise ValueError('INVALID_FILL_TOTALS')
        if qty==0 and (value or fee):raise ValueError('INVALID_EMPTY_FILL')
        if qty>0:
            if value<=0:raise ValueError('INVALID_FILL_TOTALS')
            limit=number(row['limit_price'])
            if (row['side']=='BUY' and value>qty*limit+Decimal('.00000001')) or (row['side']=='SELL' and value<qty*limit-Decimal('.00000001')):raise ValueError('LIMIT_PRICE_VIOLATED')
            if row['side']=='BUY' and (value+fee)*number(row['cad_rate'])>number(row['reserved_cad'])+Decimal('.000001'):raise ValueError('RESERVED_BUDGET_EXCEEDED')
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE');current=self.repo.one(c,'orders',row['client_order_id'])
            if current and current['state']=='SETTLED':return
            pos=self.repo.one(c,'positions',row['product']) or {'product':row['product'],'base':row['base'],'quote':row['quote'],'quantity':'0','cost':'0','peak':'0','profit_taken':False}
            held=number(pos['quantity']);cost=number(pos['cost']);reserve=number(pos.get('entry_cost_reserve','0'));realized=Decimal(0);cost_cad=number(pos.get('cost_cad','0'));realized_cad=Decimal(0)
            if qty:
                if row['side']=='BUY':
                    if held==0:pos.update(peak='0',profit_taken=False,entry_plan=row.get('entry_plan'))
                    held+=qty;cost+=value+fee;cost_cad+=(value+fee)*number(row['cad_rate']);reserve+=(value+fee)*(1+number(row.get('fx_cost_bps','0'))/10000)
                else:
                    if qty>held:raise ValueError('LIVE_OVERSELL')
                    removed=cost*qty/held;removed_cad=cost_cad*qty/held;reserve-=reserve*qty/held;cost-=removed;cost_cad-=removed_cad;held-=qty
                    realized=value-fee-removed;realized_cad=(value-fee)*number(row['cad_rate'])-removed_cad
                pos.update(quantity=str(held),cost=str(cost),cost_cad=str(cost_cad),entry_cost_reserve=str(reserve),peak=str(max(number(pos['peak']),value/qty)),profit_taken=pos.get('profit_taken',False) or 'PARTIAL_PROFIT_TAKING' in row.get('reasons',[]))
                self.repo.put(c,'positions',row['product'],pos,replace=True)
                fill={'mode':'LIVE','product':row['product'],'side':row['side'],'quantity':str(qty),'notional':str(value),'fee':str(fee),'quote':row['quote'],'cad_rate':row['cad_rate'],'realized':str(realized),'realized_cad':str(realized_cad),'order_id':oid,'client_order_id':row['client_order_id'],'decision_id':row['decision_id'],'status':status}
                fill.update(strategy_id=row.get('strategy_id'),strategy_token=row.get('strategy_token'),reason_codes=row.get('reasons',[]))
                self.repo.put(c,'fills',row['client_order_id'],fill)
            self.repo.put(c,'orders',row['client_order_id'],{**row,'state':'SETTLED','exchange_status':status,'order_id':oid,'filled_size':str(qty),'filled_value':str(value),'fees':str(fee),'settled_at':self.clock()},replace=True)
    def snapshot(self,balances,rate,cfg,fees=None):
        with self.repo.db() as c:positions=self.repo.rows(c,'positions',10000);fills=self.repo.rows(c,'fills',100000);snapshots=self.repo.rows(c,'portfolio_snapshots',10000)
        invested=Decimal(0);asset_values={};owned=[];cost_total=Decimal(0);exit_total=Decimal(0);stamp=self.clock()
        verified=fees is not None
        for p in positions:
            qty=number(p['quantity'])
            if qty<=0:continue
            product=self.adapter.trading_product(p['product']);book=self.market.get_orderbook(product['alias'])
            mark=qty*number(book['bids'][0]['price'])*rate;invested+=mark;cost_total+=number(p.get('cost_cad','0'));asset_values[p['base']]=asset_values.get(p['base'],Decimal(0))+mark
            owned.append({**p,'value_cad':str(mark)})
            stamp=min(stamp,book['timestamp'])
            exit_value=performance_bonus.liquidation_value(qty,book,number(fees['taker']),Decimal('.0005')) if fees else None
            if exit_value is None:verified=False
            else:exit_total+=exit_value*(1-number(cfg['fx_cost_bps'])/10000)*rate
        realized=sum((number(f['realized_cad']) for f in fills),Decimal(0));pnl=realized+invested-cost_total
        # Budget is independent of the rest of the Coinbase wallet; no synthetic deposits.
        cap=self.cap();equity=max(Decimal(0),cap+pnl)
        cash=min(number(balances.get(cfg['quote'],{}).get('available','0'))*rate,max(Decimal(0),equity-invested))
        now=self.clock();today=dt.datetime.fromtimestamp(now,dt.timezone.utc).date()
        prior=next((x for x in snapshots if dt.datetime.fromtimestamp(x['created'],dt.timezone.utc).date()<today),None)
        baseline=number(prior['pnl_cad']) if prior else Decimal(0)
        peak=max([Decimal(0),pnl]+[number(x['pnl_cad']) for x in snapshots]);daily=max(Decimal(0),(baseline-pnl)/cap);drawdown=max(Decimal(0),(peak-pnl)/cap)
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE');costs=performance_bonus.realized_net(self.repo,c,'live',with_reserves=True)
            # Concurrent reconciliation cannot authorize a reward on old marks.
            if c.execute('SELECT COUNT(*) FROM fills').fetchone()[0]!=len(fills):verified=False
            net_assets=cap+costs[0]+exit_total-cost_total-sum(costs[1].values(),Decimal(0)) if verified and costs is not None else None
            performance_bonus.observe(self.repo,c,'live',self.clock(),net_assets,stamp)
        return {'cap_cad':str(cap),'equity_cad':str(equity),'available_cad':str(cash),'invested_cad':str(invested),'pnl_cad':str(pnl),'realized_cad':str(realized),'positions':owned,'asset_values':{k:str(v) for k,v in asset_values.items()},'daily_loss':str(daily),'drawdown':str(drawdown),'fills':fills}
    def cycle(self):
        self.last_cycle=self.clock()
        with self.lock:
            if self.pending():
                if not self.reconcile():return
            if not self.enabled():return
            try:
                cfg=self.config();self.adapter.permissions(True);fees=self.adapter.fees();balances=self.adapter.balances();rate=self.quote_rate(cfg['quote']);self.reconcile_positions(balances)
                initial=self.snapshot(balances,rate,cfg,fees)
                limits=cfg.get('risk_limits',self.default_limits())
                if number(initial['daily_loss'])>=number(limits['daily_loss']) or number(initial['drawdown'])>=number(limits['drawdown']):
                    self.fail('LOSS_LIMIT');return
                if self.clock()-self._products_at>300 or not self.products:
                    self.products=[p for p in self.adapter.trading_products() if p['tradable'] and p['quote_currency_id']==cfg['quote']];self._products_at=self.clock()
                with self.repo.db() as c:held=[p['product'] for p in self.repo.rows(c,'positions',10000) if number(p['quantity'])>0]
                unique={}
                for p in sorted(self.products,key=lambda x:number(x.get('quote_volume_24h','0')),reverse=True):unique.setdefault(p['base_currency_id'],p['product_id'])
                candidates=list(unique.values());batch=list(held)
                if self.intelligence:
                    self.intelligence.set_products([{**p,'product_id':p['alias']} for p in self.products])
                    aliases={p['product_id']:p['alias'] for p in self.products}
                    candidates=[pid for pid in candidates if self.intelligence.get(aliases[pid])]
                for _ in range(min(3,len(candidates))):
                    p=candidates[self.cursor%len(candidates)];self.cursor+=1
                    if p not in batch:batch.append(p)
                for pid in batch:
                    if not self.enabled() or self.pending():break
                    self.evaluate(pid,cfg,fees,balances,rate)
                snap=self.snapshot(balances,rate,cfg,fees);public={k:v for k,v in snap.items() if k!='fills'}
                self.last_completed=self.clock();self.status_cache.update(summary=public,checked_at=self.clock(),eligible_products=len(candidates))
                with self.repo.db() as c:self.repo.put(c,'portfolio_snapshots',str(time.time_ns()),public)
            except Exception as e:self.fail(self.safe_error(e))
    def evaluate(self,pid,cfg,fees,balances,rate):
        p=self.adapter.trading_product(pid)
        if not p['tradable'] or p['quote_currency_id']!=cfg['quote']:return
        cached=self.intelligence.get(p['alias'],allow_stale=True) if self.intelligence else None
        if self.intelligence and not cached:return
        b=self.market.get_orderbook(p['alias']);bars=cached['bars'] if cached else self.market.get_candles(p['alias']);b={**b,'product_id':pid}
        if not -5<=self.clock()-b['timestamp']<=15:raise ValueError('STALE_MARKET_DATA')
        with self.repo.db() as c:
            pos=self.repo.one(c,'positions',pid)
            if pos and number(pos['quantity'])>0:
                pos['peak']=str(max(number(pos.get('peak','0')),number(b['mid'])))
                self.repo.put(c,'positions',pid,pos,replace=True)
        snap=self.snapshot(balances,rate,cfg,fees)
        limits=cfg.get('risk_limits',self.default_limits())
        if pos:pos={**pos,'overweight':number(snap['asset_values'].get(p['base_currency_id'],'0'))>number(snap['equity_cad'])*number(limits['asset_exposure'])}
        fee=number(fees['taker']);minimum=number(cfg['minimum_net']);net_cost=None
        if pos and number(pos['quantity'])>0:
            with self.repo.db() as c:net_cost=execution_strategies.profit_policy.live_cost_basis(self.repo,c,pos)
        news=self.intelligence.news.context(p) if self.intelligence else None
        bundle=cfg.get('strategy_version')==execution_strategies.VERSION
        if bundle:
            from .validation_job import evidence_for
            daily=self.intelligence.get_daily(p['alias']) if self.intelligence else self.market.get_daily_candles(p['alias'])
            with self.repo.db() as c:anchor=self.repo.get(c,'live_anchor',self.clock())
            benchmark=self.intelligence.bitcoin_context(b['timestamp']) if self.intelligence else None
            signal=execution_strategies.evaluate(bars,pos,b,daily=daily,news=news,previous_price=self.latest.get(pid),fills=snap['fills'],anchor=anchor,fee_bps=fee*10000,fx_bps=number(cfg['fx_cost_bps']),minimum=minimum,benchmark=benchmark,fx_context=self.intelligence.fx_context(b['timestamp']) if self.intelligence and hasattr(self.intelligence,'fx_context') else None,evidence=evidence_for(self.repo.path.parent,pid),cad_rate=rate,cost_basis=net_cost)
        else:
            signal=planner.evaluate(bars,pos,b,model=cached['forecast'] if cached else None,news=news,
                                    previous_price=self.latest.get(pid),fee_bps=fee*10000,fx_bps=number(cfg['fx_cost_bps']),minimum=minimum)
        from . import agent_bridge
        signal=agent_bridge.apply(signal,pos,b,self.repo.path.parent,'live')
        if self.intelligence and not self.intelligence.get(p['alias']) and not (pos and number(pos['quantity'])>0):
            signal.update(decision='WAIT',reason_codes=['HISTORY_REFRESH_PENDING'])
        self.latest[pid]=float(b['mid']);edge=signal['net_edge']
        if signal['decision']=='BUY' and not bundle:
            last=next((f for f in snap['fills'] if f['product']==pid and f['side']=='BUY'),None)
            if last and self.clock()-last['created']<3600:signal.update(decision='WAIT',reason_codes=['ENTRY_COOLDOWN'])
        side='BUY' if signal['decision']=='BUY' else 'SELL' if signal['decision'] in ('SELL','REDUCE') else None
        key=str(uuid.uuid5(uuid.NAMESPACE_URL,'stock101-live:'+cfg.get('strategy_version',planner.VERSION)+':'+pid+':'+str(bars[-1]['start'])+':'+str(int(b['timestamp'])//5)+':'+b['bids'][0]['price']+':'+b['asks'][0]['price']))
        with self.repo.db() as c:
            if self.repo.one(c,'trade_decisions',key):return
            decision_id=self.repo.decision_id(c).replace('ST101-','ST101-LIVE-')
        fills=snap['fills'];now=self.clock();today=dt.datetime.fromtimestamp(now,dt.timezone.utc).date();losses=0
        for f in [x for x in fills if x['side']=='SELL']:
            if number(f['realized_cad'])<0:losses+=1
            else:break
        context={'equity':snap['equity_cad'],'cash':snap['available_cad'],'invested':snap['invested_cad'],'asset_value':snap['asset_values'].get(p['base_currency_id'],'0'),'halted':not self.enabled(),'age':now-b['timestamp'],'spread_bps':b['spread_bps'],'daily_loss':snap['daily_loss'],'drawdown':snap['drawdown'],'consecutive_losses':losses,'since_loss':now-next((f['created'] for f in fills if number(f['realized_cad'])<0),0),'trades_hour':sum(f['created']>now-3600 for f in fills),'deployed_today':str(sum((number(f['notional'])*number(f['cad_rate'])+number(f['fee'])*number(f['cad_rate']) for f in fills if f['side']=='BUY' and dt.datetime.fromtimestamp(f['created'],dt.timezone.utc).date()==today),Decimal(0))),'signal_deviation':str(abs(number(b['mid'])/number(signal['plan']['reference_price'])-1))}
        requested=number(snap['equity_cad'])*number(signal.get('allocation','.02')) if side=='BUY' else number(pos['quantity'])*number(b['mid'])*rate*number(signal['sell_fraction']) if side=='SELL' and pos else Decimal(0)
        if side=='BUY' and signal.get('requested_cash_cad') is not None:requested=min(requested,number(signal['requested_cash_cad']))
        check=risk.assess(requested,side,context,{**limits,'fee_bps':str(fee*10000)}) if side else {'status':'BLOCK','reason':'NO_EXECUTABLE_SIGNAL','approved_notional':'0'}
        record={'decision_id':decision_id,'timestamp':now,'mode':'LIVE','product':pid,'signal':signal,'net_edge':edge,'risk':check,'status':'NO_ORDER','cost_policy':cfg,'fee_tier':fees,'market_timestamp':b['timestamp'],'market_mid':b['mid'],'risk_context':context}
        # Persist the causal decision before any external side effect, including a crash.
        with self.repo.db() as c:self.repo.put(c,'trade_decisions',key,record)
        if check['status']=='HALT':self.fail(check['reason'])
        if side and check['status'] in ('PASS','REDUCE'):
            try:self.execute(key,record,p,b,side,number(check['approved_notional']),fee,rate,cfg,pos,edge)
            except ValueError as e:record.update(status='BLOCKED',reason=self.safe_error(e))
        with self.repo.db() as c:self.repo.put(c,'trade_decisions',key,record,replace=True)
    def execute(self,key,record,p,b,side,approved_cad,fee,rate,cfg,pos,edge):
        if not self.enabled():raise ValueError('LIVE_STOPPED')
        tick=number(p['price_increment']);slip=Decimal('.0005');best=number(b['asks' if side=='BUY' else 'bids'][0]['price'])
        # Price cap plus IOC bounds slippage and removes unfilled balance immediately.
        price=(best*(1+slip if side=='BUY' else 1-slip)/tick).to_integral_value(rounding=ROUND_UP if side=='BUY' else ROUND_DOWN)*tick
        if side=='BUY' and record['signal'].get('plan',{}).get('entry_limit') is not None:
            price=min(price,(number(record['signal']['plan']['entry_limit'])/tick).to_integral_value(rounding=ROUND_DOWN)*tick)
        step=number(p['base_increment']);qty=(approved_cad/rate/(Decimal('1.05') if cfg['quote']!='CAD' else Decimal(1))/(1+fee)/price/step).to_integral_value(rounding=ROUND_DOWN)*step
        if side=='SELL':
            # Sale fees come from proceeds; buy-side FX/cash reserves must not
            # reduce a closing sell and strand a position below exchange minimum.
            qty=min(number(pos['quantity']),(approved_cad/rate/price/step).to_integral_value(rounding=ROUND_DOWN)*step)
        if qty<number(p['base_min_size']) or qty>number(p['base_max_size']) or qty*price<number(p['quote_min_size']) or qty*price>number(p['quote_max_size']):raise ValueError('ORDER_BELOW_EXCHANGE_MINIMUM_OR_TOO_LARGE')
        payload={'client_order_id':key,'product_id':p['product_id'],'side':side,'order_configuration':{'sor_limit_ioc':{'base_size':str(qty),'limit_price':str(price)}}}
        preview=self.adapter.preview(payload)
        if preview.get('errs')!=[] or preview.get('warning'):raise ValueError('ORDER_PREVIEW_REJECTED')
        commission=number(preview.get('commission_total'));preview_id=preview.get('preview_id')
        if commission<0 or not isinstance(preview_id,str) or not preview_id:raise ValueError('INVALID_ORDER_PREVIEW')
        if commission>qty*price*Decimal('.05'):raise ValueError('PREVIEW_FEE_TOO_HIGH')
        if number(preview.get('base_size'))!=qty:raise ValueError('PREVIEW_SIZE_MISMATCH')
        maximum=qty*price+commission
        if side=='BUY':
            if maximum*rate>approved_cad:raise ValueError('PREVIEW_EXCEEDS_APPROVED_BUDGET')
            if execution_strategies.scheduled_dca(record['signal']):
                if qty*number(edge['immediate_exit_per_unit_after_cost'])/(maximum*(1+number(cfg['fx_cost_bps'])/10000))-1<Decimal('-.03'):raise ValueError('DCA_COST_LIMIT')
            elif qty*number(edge['scenario_exit_per_unit_after_cost'])/(maximum*(1+number(cfg['fx_cost_bps'])/10000))-1<number(cfg['minimum_net']):raise ValueError('NET_EDGE_LOST_AFTER_PREVIEW')
        elif not (set(record['signal']['reason_codes']) & (planner.PROTECTIVE|execution_strategies.PROTECTIVE)):
            with self.repo.db() as c:basis=execution_strategies.profit_policy.live_cost_basis(self.repo,c,pos)
            if basis is None:raise ValueError('NET_EXIT_COST_UNVERIFIED')
            cost=basis*qty/number(pos['quantity'])
            proceeds=(qty*price-commission)*(1-number(cfg['fx_cost_bps'])/10000)*rate
            if not execution_strategies.profit_policy.exit_allowed(proceeds,cost,record['signal'],cfg['minimum_net']):raise ValueError('NET_EXIT_PROFIT_TOO_SMALL')
        # Re-read funds and data after preview; no write if stop arrived meanwhile.
        balances=self.adapter.balances();fresh=self.market.get_orderbook(p['alias']);self.reconcile_positions(balances)
        if not self.enabled() or not -5<=self.clock()-fresh['timestamp']<=15:raise ValueError('LIVE_STOPPED_OR_STALE')
        if number(fresh['spread_bps'])>number(risk.DEFAULTS['max_spread_bps']):raise ValueError('WIDE_SPREAD')
        if abs(number(fresh['mid'])/number(b['mid'])-1)>Decimal('.002'):raise ValueError('PRICE_CHANGED_AFTER_PREVIEW')
        if side=='BUY' and number(balances.get(p['quote_currency_id'],{}).get('available','0'))<maximum:raise ValueError('INSUFFICIENT_FUNDS')
        if side=='SELL' and number(balances.get(p['base_currency_id'],{}).get('available','0'))<qty:raise ValueError('INSUFFICIENT_POSITION')
        row={'client_order_id':key,'decision_id':record['decision_id'],'product':p['product_id'],'base':p['base_currency_id'],'quote':p['quote_currency_id'],'side':side,'size':str(qty),'limit_price':str(price),'reserved_cad':str(approved_cad),'cad_rate':str(rate),'fx_cost_bps':cfg['fx_cost_bps'],'state':'SUBMITTING','submitted_at':self.clock(),'reasons':record['signal']['reason_codes']}
        row.update(strategy_id=record['signal'].get('strategy_id'),strategy_token=record['signal'].get('plan',{}).get('entry_token') if side=='BUY' else None)
        if signal_plan:=record['signal'].get('plan'):
            row['entry_plan']=execution_strategies.entry_plan(record['signal'])
        payload['preview_id']=preview_id
        with self.repo.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if self.repo.one(c,'orders',key):raise ValueError('DUPLICATE_ORDER_INTENT')
            self.repo.put(c,'orders',key,row)
        if not self.enabled():
            with self.repo.db() as c:self.repo.put(c,'orders',key,{**row,'state':'REJECTED','reason':'STOPPED_BEFORE_SEND'},replace=True)
            raise ValueError('LIVE_STOPPED')
        try:
            self._permit=True;response=self.adapter.place_order(payload)
            if response.get('success') is True:
                oid=response.get('success_response',{}).get('order_id')
                if not isinstance(oid,str) or not oid:raise ValueError('ORDER_STATUS_UNVERIFIED')
                with self.repo.db() as c:self.repo.put(c,'orders',key,{**row,'order_id':oid,'state':'PENDING'},replace=True)
                record.update(status='SUBMITTED',client_order_id=key,order_id=oid)
            elif response.get('success') is False and isinstance(response.get('error_response'),dict):
                with self.repo.db() as c:self.repo.put(c,'orders',key,{**row,'state':'REJECTED','reason':'EXCHANGE_REJECTED'},replace=True)
                record.update(status='REJECTED')
            else:raise ValueError('ORDER_STATUS_UNVERIFIED')
        except Exception:
            record.update(status='UNKNOWN_RECONCILIATION_REQUIRED',client_order_id=key);self.fail('ORDER_STATUS_UNVERIFIED')
        finally:self._permit=False
    def state(self):
        with self.repo.db() as c:
            snapshots=self.repo.rows(c,'portfolio_snapshots',1)
            bonus=performance_bonus.state(self.repo,c,'live',self.clock())
            return {'strategy_version':self.strategy_version,'execution_strategies':execution_strategies.CATALOG if self.strategy_version==execution_strategies.VERSION else [],'research':self.intelligence.state() if self.intelligence else {},'mode':'LIVE','running':self.repo.get(c,'live_running',False) and not self.stop_event.is_set(),'reason':self.repo.get(c,'live_reason'),'config':self.repo.get(c,'live_config'),'readiness':self.status_cache,'summary':self.status_cache.get('summary') or (snapshots[0] if snapshots else None),'last_cycle':self.last_cycle,'last_completed':self.last_completed,'budget_cap_cad':str(self.cap()),'fill_count':c.execute('SELECT COUNT(*) FROM fills').fetchone()[0],'orders':self.repo.rows(c,'orders',50),'fills':self.repo.rows(c,'fills',50),'decisions':self.repo.rows(c,'trade_decisions',50),'positions':self.repo.rows(c,'positions',100),'events':self.repo.rows(c,'system_events',30),'initial_cad':str(INITIAL_CAD),'weekly_cad':bonus['weekly_cad'],'next_weekly_cad':bonus['next_weekly_cad'],'performance_bonus':bonus,'actual_deposits_automated':False,'risk_limits':(self.repo.get(c,'live_config') or {}).get('risk_limits',self.default_limits()),'sizing_policy':sizing.describe() if self.strategy_version==execution_strategies.VERSION else None}
