"""One-time CAD 20 reward on CAD 200 net realized profit, isolated per ledger.
Unrealized marks and contributions never qualify. Live reward is a budget
allowance only: no deposit, FX conversion, or order activation is performed.
"""
import json
from decimal import Decimal
from .budget_policy import INITIAL_CAD, WEEKLY_CAD, WEEK_SECONDS, BONUS_CAD, BONUS_TARGET_CAD
from .exchange import number

KEY='performance_bonus_realized_v2'
VALUATION_KEY='performance_bonus_valuation_v2'
THRESHOLD=Decimal('.20')

def period(anchor,now):return max(0,int((now-anchor)//WEEK_SECONDS))
def weekly_at(index,award=None):return Decimal(0)
def additions(paid,due,award=None):return Decimal(0)
def cap(anchor,now,award=None):return INITIAL_CAD+(BONUS_CAD if award else Decimal(0))

def realized_net(repo,c,mode,with_reserves=False):
    """Use confirmed fills only; subtract both sides' allocated FX reserves.

    Replay weighted entry reserves for partial sells. Missing live cost records
    make the reward unverified, without affecting orders or the original ledger.
    """
    total=Decimal(0);reserves={}
    sql='SELECT f.payload AS fill,o.payload AS intent FROM fills f LEFT JOIN orders o ON o.id=f.id ORDER BY f.created,f.rowid'
    for row in c.execute(sql):
        f=json.loads(row['fill'])
        if mode=='paper':
            if 'realized' not in f:return None
            total+=number(f['realized']);continue
        intent=json.loads(row['intent']) if row['intent'] else None
        if not intent or 'fx_cost_bps' not in intent:return None
        qty=number(f['quantity']);rate=number(f['cad_rate']);fx=number(intent['fx_cost_bps'])/10000
        held,reserve=reserves.get(f['product'],(Decimal(0),Decimal(0)))
        if qty<=0:return None
        if f['side']=='BUY':
            reserve+=(number(f['notional'])+number(f['fee']))*rate*fx;held+=qty
        else:
            if qty>held:return None
            removed=reserve*qty/held
            exit_reserve=(number(f['notional'])-number(f['fee']))*rate*fx
            total+=number(f['realized_cad'])-removed-exit_reserve
            held-=qty;reserve-=removed
        reserves[f['product']]=(held,reserve)
    return (total,{pid:reserve for pid,(_,reserve) in reserves.items()}) if with_reserves else total

def liquidation_value(quantity,book,fee,slippage=Decimal(0)):
    """Estimate full disposal using available bid depth, fees and slippage."""
    remaining=number(quantity);value=Decimal(0)
    for level in book['bids']:
        take=min(remaining,number(level['size']));value+=take*number(level['price'])*(1-slippage)*(1-fee);remaining-=take
        if remaining==0:return value
    return None

def observe(repo,c,mode,now,net_assets_cad,valuation_at=None):
    from . import evaluation_policy,daily_target
    # The supplied mark precedes evaluate() and therefore excludes any reward
    # first awarded below. Remove only rewards already credited in this mark.
    credited=BONUS_CAD if repo.get(c,KEY) else Decimal(0)
    evaluation_policy.observe(repo,c,mode,now,net_assets_cad,
                              valuation_at if valuation_at is not None else now,credited)
    daily_target.observe(repo,c,mode,now,net_assets_cad,valuation_at if valuation_at is not None else now,credited)
    repo.set(c,VALUATION_KEY,{'net_assets_cad':str(net_assets_cad) if net_assets_cad is not None else None,
             'valuation_at':valuation_at if valuation_at is not None else now})
    evaluate(repo,c,mode,now)

def state(repo,c,mode,now):
    from . import evaluation_policy,daily_target
    award=repo.get(c,KEY);net=realized_net(repo,c,mode);verified=net is not None
    mark=repo.get(c,VALUATION_KEY,{})
    return {'mode':mode,'evaluation':evaluation_policy.state(repo,c,mode,now),'daily_target':daily_target.state(repo,c,mode,now),'basis':'net_realized_profit','basis_label':'고정 원금',
            'principal_cad':str(INITIAL_CAD),'net_profit_cad':str(net) if verified else None,
            'net_assets_cad':mark.get('net_assets_cad'),'valuation_at':mark.get('valuation_at'),
            'return_ratio':str(net/INITIAL_CAD) if verified else None,'threshold_ratio':str(THRESHOLD),
            'target_profit_cad':str(BONUS_TARGET_CAD),'verified':verified,
            'base_weekly_cad':'0','weekly_cad':'0','next_weekly_cad':'0','next_contribution_at':None,
            'bonus_cad':str(BONUS_CAD),'paid_bonus_cad':str(BONUS_CAD if award else 0),'one_time':True,
            'earned':bool(award),'active':bool(award),'award':award,'automatic_real_deposit':False}

def evaluate(repo,c,mode,now):
    """Reward, paper cash credit and triggering fill commit/roll back together."""
    if repo.get(c,KEY):return
    net=realized_net(repo,c,mode)
    if net is None or net<BONUS_TARGET_CAD:return
    award={'earned_at':now,'effective_at':now,'net_profit_cad':str(net),
           'principal_cad':str(INITIAL_CAD),'return_ratio':str(net/INITIAL_CAD),
           'basis':'net_realized_profit','bonus_cad':str(BONUS_CAD),'mode':mode,'one_time':True}
    if mode=='paper':
        account=c.execute("SELECT cash,initial FROM paper_accounts WHERE currency='CAD'").fetchone()
        # Account initial is total credited cash for ledger reconciliation. The
        # performance hurdle remains the fixed 1,000 principal above.
        c.execute("UPDATE paper_accounts SET cash=?,initial=? WHERE currency='CAD'",(str(number(account['cash'])+BONUS_CAD),str(number(account['initial'])+BONUS_CAD)))
    repo.set(c,KEY,award)
    repo.put(c,'system_events',KEY,{'category':'funding_reward','code':'ONE_TIME_REALIZED_BONUS_EARNED',**award})
