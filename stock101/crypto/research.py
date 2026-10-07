"""Reproducible hourly research. Fixed versions; next-open simulation; no optimization."""
import hashlib
import json
import math
import statistics
import time
from . import strategy
from .exchange import number


def backtest(bars,config,capital=10000,fee_bps=60,slippage_bps=5,spread_bps=10):
    config=strategy.validate(config)
    if len(bars)<80:raise ValueError('BACKTEST_NEEDS_80_HOURLY_BARS')
    if not 100<=capital<=10000000:raise ValueError('INVALID_CAPITAL')
    cash=float(capital);qty=0.;cost=0.;peak_price=0.;profit_taken=False
    equity=[];trades=[];pending=None;fees=0.;slippage=0.;turnover=0.
    for i,b in enumerate(bars):
        if pending:
            side,fraction,allocation,reason=pending
            price=b['open']*(1+(slippage_bps+spread_bps/2)/10000*(1 if side=='BUY' else -1))
            if side=='BUY' and qty==0:
                notional=min(cash/(1+fee_bps/10000),cash*allocation);amount=notional/price
                fee=notional*fee_bps/10000;cash-=notional+fee;qty=amount;cost=notional+fee;peak_price=price;profit_taken=False
            elif side=='SELL' and qty>0:
                amount=qty*fraction;notional=amount*price;fee=notional*fee_bps/10000;removed=cost*fraction;realized=notional-fee-removed;cash+=notional-fee;qty-=amount;cost-=removed
                trades.append({'time':b['start'],'side':side,'quantity':amount,'price':price,'fee':fee,'realized':realized,'reason':reason});profit_taken=True
                if qty<1e-12:qty=0.;cost=0.;peak_price=0.;profit_taken=False
            else:amount=0.;notional=0.;fee=0.
            if amount>0:
                fees+=fee;slippage+=amount*abs(price-b['open']);turnover+=notional
                if side=='BUY':trades.append({'time':b['start'],'side':side,'quantity':amount,'price':price,'fee':fee,'realized':None,'reason':reason})
            pending=None
        mark=cash+qty*b['close'];equity.append({'time':b['start'],'equity':mark})
        if i>=59:
            peak_price=max(peak_price,b['close']) if qty else 0
            position={'quantity':qty,'cost':cost,'peak':peak_price,'profit_taken':profit_taken} if qty else None
            s=strategy.evaluate(bars[:i+1],position,config)
            if s['decision']=='BUY' and not qty:pending=('BUY',1,s['allocation'],s['reason_codes'])
            elif s['decision'] in ('SELL','REDUCE') and qty:pending=('SELL',s['sell_fraction'],0,s['reason_codes'])
    peak=capital;dd=0
    for point in equity:peak=max(peak,point['equity']);dd=max(dd,1-point['equity']/peak)
    pnls=[x['realized'] for x in trades if x['realized'] is not None];wins=[x for x in pnls if x>0];losses=[x for x in pnls if x<0]
    returns=[b['equity']/a['equity']-1 for a,b in zip(equity,equity[1:])];std=statistics.pstdev(returns) if returns else 0
    return {'mode':'BACKTEST','strategy_version':config['name'],'starting_capital':capital,'ending_capital':equity[-1]['equity'],'total_return':equity[-1]['equity']/capital-1,'max_drawdown':dd,'trade_count':len(trades),'closed_exits':len(pnls),'win_rate':len(wins)/len(pnls) if pnls else None,'profit_factor':sum(wins)/abs(sum(losses)) if losses else None,'average_win':statistics.mean(wins) if wins else None,'average_loss':statistics.mean(losses) if losses else None,'fees':fees,'estimated_spread_and_slippage':slippage,'turnover':turnover/capital,'sharpe':statistics.mean(returns)/std*math.sqrt(365*24) if std else None,'benchmark_return':bars[-1]['close']/bars[0]['close']-1,'equity':equity,'trades':trades,'limitations':['Hourly next-open simulation; no historical depth or partial fills','Current discovered products only; survivorship bias is not eliminated','Configured spread/fee assumptions, not actual account tier','Research floats; execution ledger uses Decimal','No forced final liquidation; open holdings marked at last close','Backtest research does not emulate every intraday operational risk gate']}


def experiment(engine,config):
    config=strategy.validate(config)
    digest=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest()[:12]
    version=config['name']+'_'+digest;config['name']=version
    with engine.repo.db() as c:
        if not engine.repo.one(c,'strategy_versions',version):engine.repo.put(c,'strategy_versions',version,config)
        if not engine.repo.one(c,'strategy_experiments',version):engine.repo.put(c,'strategy_experiments',version,{'version':version,'status':'CANDIDATE_PAPER_ONLY','live_promotion':False})
    return {'version':version,'config':config}


def analytics(engine):
    with engine.repo.db() as c:
        journal=engine.repo.rows(c,'trade_decisions',100000);fills=engine.repo.rows(c,'paper_orders',100000)
        snapshots=engine.repo.rows(c,'portfolio_snapshots',1000)
    risk_counts={x:sum(y['risk']['status']==x for y in journal) for x in ('PASS','REDUCE','BLOCK','HALT')}
    groups={}
    for row in journal:
        fill=row.get('execution')
        if not fill:continue
        key=row['strategy_version']+' / '+row['analysis']['regime']['name']+' / '+row['portfolio']
        item=groups.setdefault(key,{'fills':0,'fees':0.,'realized':0.,'wins':0,'losses':0})
        item['fills']+=1;item['fees']+=float(fill['fee']);item['realized']+=float(fill['realized']);item['wins']+=float(fill['realized'])>0;item['losses']+=float(fill['realized'])<0
    return {'risk_interventions':risk_counts,'strategy_regime_performance':groups,'portfolio_snapshots':snapshots,'sample_size':len(fills),'note':'Currencies are separate. Observational samples are not proof of strategy quality.'}


def post_trade(engine):
    """Observed hourly excursions; incomplete windows stay pending, never extrapolated."""
    with engine.repo.db() as c:records=engine.repo.rows(c,'trade_decisions',100)
    saved=[]
    for row in records:
        fill=row.get('execution')
        if not fill:continue
        for hours in (1,24,168,720):
            key=row['decision_id']+':'+str(hours)
            with engine.repo.db() as c:
                if engine.repo.one(c,'post_trade_analysis',key):continue
            start=int(row['timestamp'])//3600*3600+3600;end=start+hours*3600
            if end>time.time():continue
            bars=[]
            for chunk in range(start,end,300*3600):bars.extend(engine.adapter.get_candles(row['product'],chunk,min(end,chunk+300*3600)))
            if len(bars)!=hours:continue
            price=float(fill['average_fill']);direction=1 if fill['side']=='BUY' else -1
            outcomes=[direction*(x['close']/price-1) for x in bars]
            favorable=max((x['high']/price-1 if direction==1 else 1-x['low']/price) for x in bars)
            adverse=min((x['low']/price-1 if direction==1 else 1-x['high']/price) for x in bars)
            result={'decision_id':row['decision_id'],'hours':hours,'return':outcomes[-1],'max_favorable_excursion':favorable,'max_adverse_excursion':adverse,'fees':fill['fee'],'slippage':fill['slippage'],'regime_at_decision':row['analysis']['regime'],'note':'Starts at next complete hourly boundary. Directional opportunity study, not realized P&L or validated regime accuracy.'}
            with engine.repo.db() as c:engine.repo.put(c,'post_trade_analysis',key,result)
            saved.append(result)
    return {'new_observations':saved,'live_parameters_modified':False}
