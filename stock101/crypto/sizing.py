"""User-authorized flexible allocation. Evidence scores are NOT win probabilities.

Cash reserve 40%, total exposure 60%, per-asset 10%. A stop-loss risk budget
and the hard execution limits still cap every proposed amount after sizing.
"""
import math
from . import risk
from .reference_strategies import rma,true_ranges

POLICY_VERSION='flexible_40_v1'
LIMITS={**risk.DEFAULTS,'single_order':'.10','asset_exposure':'.10','total_exposure':'.60','cash_reserve':'.40','daily_deployment':'.60'}
FAMILIES={**dict.fromkeys(('ma200_swing','supertrend','hull','ichimoku','macd_trend','rsi_cross'),'trend'),
          **dict.fromkeys(('breakout','volume_breakout'),'breakout'),
          **dict.fromkeys(('range','rsi_reversion','bollinger_reentry'),'reversion')}

def clamp(x):return min(1.,max(0.,float(x)))

def describe():
    return dict(version=POLICY_VERSION,base_fraction=.02,max_fraction=.10,cash_reserve=.40,total_exposure=.60,
                asset_exposure=.10,daily_deployment=.60,stop_risk_budget=.005,
                note='Continuous evidence-weighted sizing, not a profit probability. DCA remains 2 CAD/week. No forced spending.')

def apply(signal,hour,book,evidence=None):
    """Called only after deterministic strategy selection; never changes BUY/SELL."""
    plan=signal.get('plan') or {};sid=signal.get('strategy_id');base=.02
    detail=dict(policy=POLICY_VERSION,score=0.,fraction=0.,validation='NOT_AVAILABLE',factors={},reason='NO_BUY_SIGNAL')
    signal['sizing']=detail;plan['sizing']=detail
    if signal['decision']!='BUY':return signal
    detail.update(fraction=base,reason='BASE_ALLOCATION')
    news=clamp((plan.get('news') or {}).get('allocation_multiplier',1))
    if sid not in FAMILIES:
        signal['allocation']=base*news;detail['fraction']=signal['allocation']
        detail['reason']='SCHEDULED_OR_ACCUMULATION';return signal
    try:
        mid=float(book['mid']);stop=float(plan['invalidation']);net=float(plan['net_center'])
        if not hour or len(hour)<50 or not all(math.isfinite(x) for x in (mid,stop,net)) or not 0<stop<mid or net<.005:raise ValueError()
        volatility=rma(true_ranges(hour),14)[-1]/mid
        costs=max(0.,float(signal['net_edge']['estimated_round_trip_cost']))
        downside=1-stop/mid+costs
        families={FAMILIES[r['strategy_id']] for r in signal.get('strategy_candidates',[]) if r['strategy_id'] in FAMILIES and (r.get('would_decide') or r['decision'])=='BUY'}
        factors=dict(agreement=clamp((len(families)-1)/2),cost_buffer=clamp((net-.005)/.045),
                     reward_risk=clamp((net/max(.005,downside)-1)/2),volatility=clamp(.02/max(.002,volatility)))
        status=(evidence or {}).get('status','NOT_AVAILABLE')
        # Results may support only their exact tested product/version and past cutoff.
        if evidence and (evidence.get('product')!=book['product_id'] or evidence.get('strategy_id')!=sid or evidence.get('strategy_version')!=signal['strategy_version'] or not 0<=book['timestamp']-evidence.get('data_end',0)<=14*86400):status='NOT_AVAILABLE'
        empirical=1. if status=='ROBUST_SAMPLE' else 0.
        score=.20*factors['agreement']+.25*factors['cost_buffer']+.25*factors['reward_risk']+.20*factors['volatility']+.10*empirical
        fraction=min(.02+.08*score,.005/max(.005,downside))
        if status=='WEAK_SAMPLE':fraction*=.5
        fraction=min(.10,max(0.,fraction))*news
        signal['allocation']=fraction
        detail.update(score=round(score,4),fraction=round(fraction,6),validation=status,factors={k:round(v,4) for k,v in factors.items()},
                      estimated_stop_loss=downside,reason='FLEXIBLE_EVIDENCE_SIZE',agreement_families=sorted(families))
    except (ValueError,TypeError,KeyError,ZeroDivisionError):
        signal['allocation']=base*news;detail.update(fraction=signal['allocation'],reason='SIZING_DATA_INCOMPLETE')
    return signal
