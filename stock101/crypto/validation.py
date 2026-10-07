"""Fixed-rule chronological holdouts, cost stress and block-bootstrap research.

No broker, account or database writes. The exact production evaluator and risk
limits are used, but hourly historical quotes cannot reproduce live depth,
latency, intrabar stops or eligibility. Results never promote a strategy to LIVE.
"""
import bisect,copy,hashlib,json,math,random,statistics
from decimal import Decimal,ROUND_DOWN
from . import execution_strategies as rules,fx_reserve,risk,sizing

SCENARIOS=[dict(id='base',fee_bps=60,slippage_bps=5,spread_bps=10,fx_bps=25,skip=0),
           dict(id='cost_stress',fee_bps=90,slippage_bps=20,spread_bps=30,fx_bps=50,skip=0),
           dict(id='execution_stress',fee_bps=120,slippage_bps=40,spread_bps=50,fx_bps=75,skip=.1)]

def validate_rows(rows,step):
    if not rows:raise ValueError('VALIDATION_HISTORY_MISSING')
    for r in rows:
        if int(r['start'])!=r['start'] or r['start']%step:raise ValueError('INVALID_BAR_TIME')
        if not all(math.isfinite(float(r[k])) and float(r[k])>0 for k in ('open','high','low','close')):raise ValueError('INVALID_BAR_PRICE')
        if not r['low']<=min(r['open'],r['close'])<=max(r['open'],r['close'])<=r['high']:raise ValueError('INVALID_BAR_RANGE')
        if not math.isfinite(float(r.get('volume',0))) or float(r.get('volume',0))<0:raise ValueError('INVALID_BAR_VOLUME')
    if any(b['start']-a['start']!=step for a,b in zip(rows,rows[1:])):raise ValueError('VALIDATION_HISTORY_GAP')

def folds_for(assets,limit=720):
    times=None
    for asset in assets.values():
        validate_rows(asset['hourly'],3600);validate_rows(asset['daily'],86400)
        usable={r['start'] for r in asset['hourly'][201:]}
        times=usable if times is None else times&usable
    times=sorted(times or [])
    if limit is not None:times=times[-limit:]
    if len(times)<72:raise ValueError('VALIDATION_NEEDS_72_HOLDOUT_HOURS_AFTER_201_WARMUP')
    if any(b-a!=3600 for a,b in zip(times,times[1:])):raise ValueError('VALIDATION_HOLDOUT_GAP')
    n=len(times)
    return [times[i*n//3:(i+1)*n//3] for i in range(3)]

def quantile(values,p):
    values=sorted(values);at=(len(values)-1)*p;i=int(at);j=min(i+1,len(values)-1)
    return values[i]+(values[j]-values[i])*(at-i)

def monte_carlo(returns,iterations=200,seed=101):
    if len(returns)<72:return dict(status='INSUFFICIENT_SAMPLE',iterations=0)
    rng=random.Random(seed);ends=[];drawdowns=[];block=min(24,len(returns));n=len(returns)
    for _ in range(iterations):
        sampled=[]
        while len(sampled)<n:
            start=rng.randrange(n);sampled.extend(returns[(start+i)%n] for i in range(block))
        value=peak=1.;dd=0.
        for r in sampled[:n]:value*=1+r;peak=max(peak,value);dd=max(dd,1-value/peak)
        ends.append(value-1);drawdowns.append(dd)
    return dict(status='HISTORICAL_RESAMPLING',iterations=iterations,seed=seed,block_hours=block,
                return_p05=quantile(ends,.05),return_p50=quantile(ends,.5),return_p95=quantile(ends,.95),
                drawdown_p95=quantile(drawdowns,.95),loss_fraction=sum(v<0 for v in ends)/iterations,
                note='Resampled historical hourly returns, not a future loss probability or confidence interval.')

def make_book(pid,mid,now,costs,rate,foreign):
    spread=costs['spread_bps']/20000;fx=costs['fx_bps']/10000 if foreign else 0
    return dict(product_id=pid,mid=str(mid),timestamp=now,valuation_currency='CAD',fx_rate=str(rate),
                native_mid=str(mid/rate),spread_bps=str(costs['spread_bps']),
                bids=[dict(price=str(mid*(1-spread)*(1-fx)),size='1000000000')],
                asks=[dict(price=str(mid*(1+spread)*(1+fx)),size='1000000000')])

def simulate(assets,times,costs,*,strategy_id=None,fx_data=None,capital=100,trace=False,evaluator=None):
    """A fold starts flat. Warmup is read-only; no training/fitting or future bars."""
    evaluate=evaluator or rules.evaluate
    cash=float(capital);positions={};fills=[];equity=[];pending_returns=[];peak=last_value=float(capital)
    daily_baseline=capital;day=None;halted=False;last_loss=-1e30;losses=0;fees=friction=0.;matches=0
    blocked={};signals={};last_prices={};previous={};buy_times=[];per_product={p:dict(closed_exits=0,realized=0.,fees=0.) for p in assets}
    hour_maps={p:{r['start']:i for i,r in enumerate(a['hourly'])} for p,a in assets.items()}
    daily_ends={p:[r['start']+86400 for r in a['daily']] for p,a in assets.items()}
    def block(reason):blocked[reason]=blocked.get(reason,0)+1
    def rate_at(asset,t):
        if not asset['foreign']:return 1.
        rates=[r for r in (fx_data or {}).get('rows',[]) if r['available_at']<=t]
        return rates[-1]['rate'] if rates and t-rates[-1]['available_at']<=5*86400 else asset['reference_fx']
    def scale(rows,rate):return [{**b,**{k:float(b[k])*rate for k in ('open','high','low','close')}} for b in rows]
    def mark(prices,net=False):
        total=cash
        for pid,pos in positions.items():
            value=pos['quantity']*prices[pid]
            if net:value*= (1-costs['spread_bps']/20000)*(1-costs['slippage_bps']/10000)*(1-costs['fee_bps']/10000)*(1-(costs['fx_bps']/10000 if assets[pid]['foreign'] else 0))
            total+=value
        return total
    for t in times:
        prices={p:a['hourly'][hour_maps[p][t]-1]['close']*rate_at(a,t) for p,a in assets.items()}
        current_equity=mark(prices)
        if t//86400!=day:day=t//86400;daily_baseline=current_equity
        benchmark=None
        btc=next((p for p in assets if p.startswith('BTC-')),None)
        if btc:
            a=assets[btc];i=hour_maps[btc][t];history=a['hourly'][:i];days=a['daily'][:bisect.bisect_right(daily_ends[btc],t)]
            benchmark=dict(product_id=btc,price=history[-1]['close'],timestamp=t,daily=days,session_open=next((b['open'] for b in history if b['start']==t//86400*86400),None))
        for pid,asset in assets.items():
            i=hour_maps[pid][t];rate=rate_at(asset,t);hour=scale(asset['hourly'][:i],rate)
            daily=scale(asset['daily'][:bisect.bisect_right(daily_ends[pid],t)],rate);mid=prices[pid]
            pos=positions.get(pid)
            if pos:
                pos['overweight']=pos['quantity']*mid>mark(prices)*.10
                pos['peak']=max(pos.get('peak',mid),mid)
            book=make_book(pid,mid,t,costs,rate,asset['foreign'])
            s=evaluate(hour,pos,book,daily=daily,previous_price=previous.get(pid,mid),fills=fills,anchor=times[0],
                             benchmark=benchmark,only_strategy=strategy_id,fx_context=fx_reserve.context(fx_data,t),
                             fee_bps=costs['fee_bps'],slippage_bps=costs['slippage_bps'])
            previous[pid]=mid;side='BUY' if s['decision']=='BUY' else 'SELL' if s['decision'] in ('SELL','REDUCE') else None
            for row in s['strategy_candidates']:
                if row['decision']=='BUY':signals[row['strategy_id']]=signals.get(row['strategy_id'],0)+1
            if not side:continue
            if side=='BUY':
                matches+=1
                if costs['skip'] and int(hashlib.sha256(f'{pid}:{s["strategy_id"]}:{t}'.encode()).hexdigest()[:8],16)/0xffffffff<costs['skip']:block('MISSED_ENTRY_STRESS');continue
            # Only the next open is now revealed; the next candle's high/low/close
            # never enters the decision or this execution calculation.
            opening=asset['hourly'][i]['open']*rate
            if side=='BUY' and abs(opening/mid-1)>.03:block('OPEN_GAP_LIMIT');continue
            trade_book=make_book(pid,opening,t,costs,rate,asset['foreign'])
            price=float(trade_book['asks' if side=='BUY' else 'bids'][0]['price'])*(1+(1 if side=='BUY' else -1)*costs['slippage_bps']/10000)
            valuation={**prices,pid:opening};value=mark(valuation);invested=value-cash
            context=dict(equity=str(value),cash=str(cash),invested=str(invested),asset_value=str(pos['quantity']*opening if pos else 0),
                         halted=halted,age=0,spread_bps=costs['spread_bps'],daily_loss=max(0,(daily_baseline-value)/daily_baseline),drawdown=max(0,1-value/peak),
                         consecutive_losses=losses,since_loss=t-last_loss,trades_hour=sum(f['created']>t-3600 for f in fills),
                         deployed_today=sum(amount for at,amount in buy_times if at//86400==t//86400))
            requested=value*s['allocation'] if side=='BUY' else pos['quantity']*opening*s['sell_fraction']
            if side=='BUY' and s.get('requested_cash_cad') is not None:requested=min(requested,float(s['requested_cash_cad']))
            check=risk.assess(requested,side,context,{**sizing.LIMITS,'fee_bps':str(costs['fee_bps'])})
            if check['status']=='HALT':halted=True
            if check['status'] not in ('PASS','REDUCE'):block(check['reason']);continue
            fee_rate=costs['fee_bps']/10000;approved=float(check['approved_notional'])
            amount=approved/(1+fee_rate)/price if side=='BUY' else min(pos['quantity']*s['sell_fraction'],approved/price)
            step=Decimal(asset['base_increment']);qty=float((Decimal(str(amount))/step).to_integral_value(rounding=ROUND_DOWN)*step)
            notional=qty*price;fee=notional*fee_rate
            if qty<float(asset['base_min_size']) or notional<float(asset['quote_min_size'])*rate:block('EXCHANGE_MINIMUM');continue
            if qty>float(asset['base_max_size']) or notional>float(asset['quote_max_size'])*rate:block('EXCHANGE_MAXIMUM');continue
            realized=0.
            if side=='BUY':
                if s['plan'].get('entry_limit') and price>s['plan']['entry_limit']:block('ENTRY_LIMIT');continue
                target=s['plan'].get('target');stop=s['plan'].get('invalidation')
                if stop and opening<=stop:block('ENTRY_STOP_CROSSED');continue
                if target is not None:
                    target_book=make_book(pid,target,t,costs,rate,asset['foreign'])
                    proceeds=qty*float(target_book['bids'][0]['price'])*(1-costs['slippage_bps']/10000)*(1-fee_rate)
                    if proceeds/(notional+fee)-1<s['plan'].get('research_minimum_net_return',.005):block('TARGET_NET_TOO_SMALL_AT_OPEN');continue
                elif qty*float(trade_book['bids'][0]['price'])*(1-costs['slippage_bps']/10000)*(1-fee_rate)/(notional+fee)-1<-.03:block('DCA_COST_LIMIT');continue
                if notional+fee>cash+1e-8:raise AssertionError('NEGATIVE_RESEARCH_CASH')
                cash-=notional+fee;buy_times.append((t,notional+fee))
                if not pos:
                    pos=dict(quantity=0.,cost=0.,entry_plan=rules.entry_plan(s),profit_taken=False,peak=opening);positions[pid]=pos
                pos['quantity']+=qty;pos['cost']+=notional+fee
            else:
                removed=pos['cost']*qty/pos['quantity'];realized=notional-fee-removed
                if not set(s['reason_codes'])&rules.PROTECTIVE and not rules.profit_policy.exit_allowed(notional-fee,removed,s,'.005'):block('NET_EXIT_PROFIT_TOO_SMALL');continue
                cash+=notional-fee;pos['quantity']-=qty;pos['cost']-=removed;pos['profit_taken']=True
                per_product[pid]['closed_exits']+=1;per_product[pid]['realized']+=realized
                if realized<0:losses+=1;last_loss=t
                else:losses=0
                if pos['quantity']<float(asset['base_min_size'])*.1:positions.pop(pid,None)
            fees+=fee;per_product[pid]['fees']+=fee;friction+=qty*abs(price-opening)
            fills.append(dict(product=pid,side=side,quantity=qty,notional=notional,fee=fee,realized=realized if side=='SELL' else None,
                              strategy_id=s['strategy_id'],strategy_token=s['plan'].get('entry_token'),created=t,reason=s['reason_codes']))
        last_prices={p:a['hourly'][hour_maps[p][t]]['close']*rate_at(a,t+3600) for p,a in assets.items()}
        net=mark(last_prices,True);equity.append(dict(time=t+3600,equity=net));pending_returns.append(net/last_value-1);last_value=net;peak=max(peak,net)
    exits=[f['realized'] for f in fills if f['side']=='SELL'];wins=sum(x for x in exits if x>0);loss=-sum(x for x in exits if x<0)
    high=capital;dd=0.
    for point in equity:high=max(high,point['equity']);dd=max(dd,1-point['equity']/high)
    for pid,p in per_product.items():
        pos=positions.get(pid)
        liquidation=0.
        if pos:
            b=make_book(pid,last_prices[pid],times[-1]+3600,costs,rate_at(assets[pid],times[-1]+3600),assets[pid]['foreign'])
            liquidation=pos['quantity']*float(b['bids'][0]['price'])*(1-costs['slippage_bps']/10000)*(1-costs['fee_bps']/10000)-pos['cost']
        p['net_pnl']=p['realized']+liquidation
    return dict(start=times[0],end=times[-1]+3600,return_ratio=equity[-1]['equity']/capital-1,max_drawdown=dd,
                closed_exits=len(exits),fills=len(fills),win_rate=sum(x>0 for x in exits)/len(exits) if exits else None,
                profit_factor=wins/loss if loss else None,fees=fees,friction=friction,entry_matches=matches,
                signals=signals,blocked=blocked,halted=halted,by_product=per_product,returns=pending_returns,
                equity=equity if trace else [],trades=fills if trace else [])

def summarize(runs):
    ret=math.prod(1+r['return_ratio'] for r in runs)-1
    by={}
    for run in runs:
        for pid,stats in run['by_product'].items():
            row=by.setdefault(pid,dict(closed_exits=0,net_pnl=0.,fees=0.))
            for k in row:row[k]+=stats[k]
    return dict(return_ratio=ret,max_drawdown=max(r['max_drawdown'] for r in runs),closed_exits=sum(r['closed_exits'] for r in runs),
                fills=sum(r['fills'] for r in runs),fees=sum(r['fees'] for r in runs),friction=sum(r['friction'] for r in runs),
                entry_matches=sum(r['entry_matches'] for r in runs),fold_returns=[r['return_ratio'] for r in runs],
                by_product=by,halted_folds=sum(r['halted'] for r in runs),
                blocked={k:sum(r['blocked'].get(k,0) for r in runs) for k in {k for r in runs for k in r['blocked']}})

def run(snapshot,progress=lambda x:None):
    assets=snapshot['assets'];folds=folds_for(assets);entries=[];ids=[None]+list(rules.NAMES)
    duration=sum(len(f) for f in folds)/24;total=len(ids)*len(SCENARIOS)*3;done=0
    for sid in ids:
        scenarios={};base_returns=[]
        for costs in SCENARIOS:
            runs=[]
            for i,times in enumerate(folds):
                out=simulate(assets,times,costs,strategy_id=sid,fx_data=snapshot.get('fx_history'))
                runs.append(out);done+=1
                progress(dict(completed=done,total=total,strategy_id=sid or 'combined',scenario=costs['id'],fold=i+1))
            scenarios[costs['id']]=summarize(runs)
            if costs['id']=='base':base_returns=[x for r in runs for x in r['returns']]
        evidence={}
        for pid,base in scenarios['base']['by_product'].items():
            enough=base['closed_exits']>=20 and duration>=30
            good=base['net_pnl']>0 and all(s['by_product'][pid]['net_pnl']>=0 for s in scenarios.values())
            evidence[pid]=dict(product=pid,strategy_id=sid or 'combined',strategy_version=rules.VERSION,
                               data_end=folds[-1][-1]+3600,closed_exits=base['closed_exits'],days=duration,
                               status=('ROBUST_SAMPLE' if good else 'WEAK_SAMPLE') if enough else 'INSUFFICIENT_SAMPLE')
        entries.append(dict(strategy_id=sid or 'combined',name=rules.NAMES.get(sid,'전체 전략 · 공유 예산'),
                            scenarios=scenarios,monte_carlo=monte_carlo(base_returns),evidence=evidence,
                            status='INSUFFICIENT_SAMPLE' if scenarios['base']['closed_exits']<20 or duration<30 else ('ROBUST_SAMPLE' if all(x['return_ratio']>0 for x in scenarios.values()) else 'WEAK_SAMPLE')))
    return dict(strategy_version=rules.VERSION,products=list(assets),starting_capital_cad=100,test_days=duration,
                folds=[dict(start=f[0],end=f[-1]+3600,hours=len(f)) for f in folds],warmup_hours=201,entries=entries,
                scenarios=SCENARIOS,policy=sizing.describe(),input_hash=hashlib.sha256(json.dumps(snapshot,sort_keys=True).encode()).hexdigest(),
                data_end=folds[-1][-1]+3600,parameter_fitting=False,live_promotion=False,
                limitations=['Fixed rules; chronological historical holdouts were not used for parameter fitting, but are not independently unseen future data.',
                 'Each fold starts flat with 100 CAD. No weekly contributions or bonus. Stitched fold return; positions are not carried between folds.',
                 'Completed-candle decision, next open fills; historical depth, partial fills and intrabar stops are unavailable.',
                 'Current eligible cached products only; survivorship and selection bias remain. Missing assets are not implied to be tested.',
                 'FX uses published Bank of Canada daily history when available, otherwise a labeled fixed snapshot rate; not executable exchange rates.',
                 'Open holdings marked after estimated exit costs. No fictional final sale added to completed-trade counts.',
                 'Stress reruns the strategy with higher costs and deterministic 10% missed entries. Monte Carlo resamples 24-hour return blocks, not future outcomes.',
                 'Evidence requires 30 days and 20 actual simulated exits per product/strategy. Functional tests and targets do not prove profitability.',
                 'Historical evidence is disabled inside its own backtest to prevent circular sizing. Research floats; production ledgers use Decimal.'])
