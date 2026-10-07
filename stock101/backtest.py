"""Long-only daily simulation: close signal, next-open fill, explicit costs."""
import datetime as dt
import math
import statistics as stats
from . import indicators, strategies


def bounded(value, lo, hi, name):
    if isinstance(value,bool): raise ValueError(name+' must be numeric.')
    v=float(value)
    if not math.isfinite(v) or not lo<=v<=hi: raise ValueError(f'{name} must be {lo}–{hi}.')
    return v


def run(bars, strategy, capital=100000, commission=1, slippage_bps=5, allocation=.02, benchmark=None, start=None, end=None):
    strategies.validate(strategy)
    capital=bounded(capital,100,1e12,'Capital'); commission=bounded(commission,0,10000,'Commission')
    slip=bounded(slippage_bps,0,1000,'Slippage')/10000; allocation=bounded(allocation,.001,1,'Allocation')
    if len(bars)<2: raise ValueError('At least two daily bars are needed.')
    ind=indicators.calculate(bars)
    eligible=[i for i,b in enumerate(bars) if (not start or b['date']>=start) and (not end or b['date']<=end)]
    if len(eligible)<2: raise ValueError('No usable bars in the requested date range.')
    cash=capital; qty=0; basis=0; trades=[]; equity=[]; outcomes=[]; peak=capital; max_dd=0
    pending=None
    for i in eligible:
        b=bars[i]
        if pending:
            side,why,signal_date=pending; pending=None
            price=b['open']*(1+slip if side=='BUY' else 1-slip)
            if side=='BUY' and qty==0:
                size=max(0,math.floor((min(cash,cash*allocation)-commission)/price))
                if size:
                    qty=size; basis=qty*price+commission; cash-=basis
                    trades.append({'date':b['date'],'signal_date':signal_date,'action':'BUY','price':price,'quantity':qty,'fees':commission,'reason':why,'pnl':None})
            elif side=='SELL' and qty:
                proceeds=qty*price-commission; pnl=proceeds-basis; cash+=proceeds; outcomes.append(pnl)
                trades.append({'date':b['date'],'signal_date':signal_date,'action':'SELL','price':price,'quantity':qty,'fees':commission,'reason':why,'pnl':pnl}); qty=0; basis=0
        value=cash+qty*b['close']; peak=max(peak,value); max_dd=max(max_dd,1-value/peak)
        equity.append({'date':b['date'],'value':value,'strategy':100*value/capital})
        node=strategy['exit'] if qty else strategy['entry']
        if strategies.evaluate(node,ind,i): pending=('SELL' if qty else 'BUY',strategies.reason(node,ind,i),b['date'])
    returns=[equity[i]['value']/equity[i-1]['value']-1 for i in range(1,len(equity))]
    vol=stats.stdev(returns)*math.sqrt(252) if len(returns)>1 else None
    mean=stats.mean(returns) if returns else 0
    years=(dt.date.fromisoformat(equity[-1]['date'])-dt.date.fromisoformat(equity[0]['date'])).days/365.25
    final=equity[-1]['value']; gains=[x for x in outcomes if x>0]; losses=[x for x in outcomes if x<0]
    bench={b['date']:b['close'] for b in benchmark or []}; first_bench=None; last_bench=None
    for point in equity:
        if point['date'] in bench:
            last_bench=bench[point['date']]
            if first_bench is None: first_bench=last_bench
        point['benchmark']=last_bench/first_bench*100 if first_bench else None
    return {'initial_capital':capital,'final_value':final,'total_return':(final/capital-1)*100,
            'annualized_return':((final/capital)**(1/years)-1)*100 if years>=1/365.25 else None,
            'max_drawdown':max_dd*100,'volatility':vol*100 if vol is not None else None,
            'sharpe':mean*252/vol if vol else None,'win_rate':len(gains)/len(outcomes)*100 if outcomes else None,
            'number_of_trades':len(trades),'closed_trades':len(outcomes),'average_gain':stats.mean(gains) if gains else None,
            'average_loss':stats.mean(losses) if losses else None,'profit_factor':sum(gains)/abs(sum(losses)) if losses else None,
            'equity':equity,'trades':trades,'open_quantity':qty,'unrealized_pnl':qty*bars[eligible[-1]]['close']-basis if qty else 0,
            'assumptions':['Signals evaluated at close; fills at next available open. No final-bar signal is filled.',
                           'Whole shares, long only, fixed fee per fill, adverse slippage on both sides.',
                           'Daily split/dividend-adjusted OHLC from the provider; no separate dividend cash ledger.',
                           'Sharpe assumes zero risk-free rate and 252 sessions. Benchmark rebased on common dates; missing sessions carry the last known value.',
                           'Survivorship, delistings, tax, FX conversion and market impact are not modeled. Not evidence of future profitability.']}


def walk_forward(bars,strategy,settings,train_end,validation_end):
    if not bars[0]['date']<train_end<validation_end<bars[-1]['date']: raise ValueError('Split dates must create three chronological periods inside the dataset.')
    periods=[('Development',bars[0]['date'],train_end),('Validation',(dt.date.fromisoformat(train_end)+dt.timedelta(days=1)).isoformat(),validation_end),('Out of sample',(dt.date.fromisoformat(validation_end)+dt.timedelta(days=1)).isoformat(),bars[-1]['date'])]
    return [{'period':label,**run(bars,strategy,**settings,start=start,end=end)} for label,start,end in periods]
