"""Deterministic evidence, regime and conflict-aware synthesis. No exchange logic."""
import math
import statistics
from .. import indicators

DEFAULT={'name':'balanced_v1','rsi_entry':32,'rsi_exit':75,'profit_take':.15,'trailing_stop':.08,'allocation':.02}

def validate(config):
    if not isinstance(config,dict) or set(config)-set(DEFAULT):raise ValueError('INVALID_STRATEGY_CONFIG')
    result={**DEFAULT,**config}
    if not isinstance(result['name'],str) or not 1<=len(result['name'])<=80:raise ValueError('INVALID_NAME')
    for k,lo,hi in [('rsi_entry',10,45),('rsi_exit',55,95),('profit_take',.02,1),('trailing_stop',.01,.3),('allocation',.001,.02)]:
        v=result[k]
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('INVALID_STRATEGY_PARAMETER')
    return result

def evaluate(bars,position=None,config=None):
    config=validate(config or DEFAULT)
    if len(bars)<60:return {'decision':'NO_TRADE','strength':0,'regime':{'name':'UNKNOWN','confidence':0},'signals':[],'reason_codes':['INSUFFICIENT_HISTORY'],'sell_fraction':0}
    closes=[x['close'] for x in bars];ind=indicators.calculate(bars)
    last=closes[-1];sma20=sum(closes[-20:])/20;sma50=sum(closes[-50:])/50
    ret=last/closes[-25]-1;vol=statistics.pstdev([closes[i]/closes[i-1]-1 for i in range(len(closes)-24,len(closes))])
    atr=sum(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars[-15:-1],bars[-14:]))/14
    rsi=ind['rsi'][-1];momentum=last/closes[-7]-1;prev_mom=closes[-2]/closes[-8]-1
    if ret<-.12 and vol>.02:regime='PANIC'
    elif vol>.025:regime='HIGH_VOLATILITY'
    elif last>sma20>sma50:regime='BULL_TREND'
    elif last<sma20<sma50:regime='BEAR_TREND'
    elif last>sma20 and sma20<sma50 and momentum>0:regime='RECOVERY'
    elif vol<.002:regime='LOW_VOLATILITY'
    else:regime='SIDEWAYS'
    signals=[]
    def signal(name,decision,strength,reason,flags=None):signals.append({'strategy':name,'decision':decision,'strength':strength,'reason':reason,'risk_flags':flags or []})
    signal('Trend Following','BUY' if regime=='BULL_TREND' else 'SELL' if regime=='BEAR_TREND' else 'WAIT',.7,'20/50-hour trend alignment')
    signal('Momentum','BUY' if momentum>.01 else 'SELL' if momentum<-.01 else 'WAIT',.65,'Six-hour return and direction')
    signal('Mean Reversion','BUY' if rsi<config['rsi_entry'] and regime=='SIDEWAYS' else 'WAIT',.6,'Oversold entry only in sideways regime')
    declining_volume=bars[-1]['volume']<sum(b['volume'] for b in bars[-7:-1])/6
    signal('Oversold Reversal','BUY' if rsi<config['rsi_entry'] and momentum>prev_mom and declining_volume and regime in ('RECOVERY','SIDEWAYS') else 'WAIT',.6,'Requires oversold, improving momentum, easing volume and suitable regime')
    signal('Breakout','BUY' if last>max(b['high'] for b in bars[-21:-1]) and bars[-1]['volume']>sum(b['volume'] for b in bars[-21:-1])/20 else 'WAIT',.7,'Confirmed close above prior 20-hour high with volume')
    signal('Volatility Filter','NO_TRADE' if regime in ('PANIC','HIGH_VOLATILITY') else 'HOLD',.85,'Volatility veto', ['HIGH_VOLATILITY'] if regime in ('PANIC','HIGH_VOLATILITY') else [])
    signal('Portfolio Rebalancing','REDUCE' if position and position.get('overweight') else 'HOLD',.8,'Reduce only when allocation exceeds configured asset limit')
    buys=sum(x['decision']=='BUY' for x in signals);sells=sum(x['decision']=='SELL' for x in signals)
    conflict=bool(buys and sells);decision='WAIT';reasons=[];fraction=0
    if position and float(position.get('quantity',0))>0:
        entry=float(position['cost'])/float(position['quantity']);peak=max(float(position.get('peak',last)),last)
        if last<=peak*(1-config['trailing_stop']):decision='SELL';fraction=1;reasons=['TRAILING_PROTECTION']
        elif position.get('overweight'):decision='REDUCE';fraction=.25;reasons=['PORTFOLIO_REBALANCE']
        elif sells>=2:decision='SELL';fraction=1;reasons=['TREND_AND_MOMENTUM_EXIT']
        elif last/entry-1>=config['profit_take'] and not position.get('profit_taken'):
            decision='REDUCE';fraction=.25 if regime=='BULL_TREND' and rsi<config['rsi_exit'] else .5;reasons=['PARTIAL_PROFIT_TAKING']
        else:decision='HOLD';reasons=['RETAIN_POSITION']
    elif regime in ('PANIC','HIGH_VOLATILITY'):decision='NO_TRADE';reasons=['VOLATILITY_VETO']
    elif conflict:reasons=['STRATEGY_DISAGREEMENT']
    elif buys>=2:decision='BUY';reasons=['MULTI_STRATEGY_CONFIRMATION']
    else:reasons=['INSUFFICIENT_CONFIRMATION']
    return {'decision':decision,'strength':.75 if decision in ('BUY','SELL','REDUCE') else .4,'regime':{'name':regime,'confidence':.7},'conflict':conflict,'reason_codes':reasons,'signals':signals,'sell_fraction':fraction,'allocation':config['allocation'],'indicators':{'rsi':rsi,'sma20':sma20,'sma50':sma50,'momentum':momentum,'volatility':vol,'atr':atr,'return_24h':ret,'macd':ind['macd'][-1]},'strategy_version':config['name']}

def advisory(value,known_products):
    """Optional LLM proposals cannot be passed to execution; malformed input becomes NO_TRADE."""
    if not isinstance(value,dict) or value.get('product') not in known_products or value.get('decision') not in ('BUY','SELL','REDUCE','HOLD','WAIT','NO_TRADE'):
        return {'decision':'NO_TRADE','reason_codes':['UNTRUSTED_OR_INVALID_ADVISORY'],'executable':False}
    return {'product':value['product'],'decision':value['decision'],'executable':False}
