"""Fixed principal shared by paper/live. Does not transfer real money."""
from decimal import Decimal
from .exchange import number
INITIAL_CAD=Decimal('1000')
WEEKLY_CAD=Decimal('0')
WEEK_SECONDS=604800  # Still used by weekly strategies, not deposits.
POLICY_VERSION='fixed_1000_realized_bonus_v2'
BONUS_CAD=Decimal('20')
BONUS_TARGET_CAD=Decimal('200')

def live_cap(available_cad,verified_contributions_cad,anchor,now):
    cash=number(available_cad);funded=number(verified_contributions_cad)
    if cash<0 or funded<0:raise ValueError('INVALID_VERIFIED_BALANCE')
    return min(cash,funded,INITIAL_CAD)

def migrate_paper(repo,c,now):
    """Caller owns transaction. Top up the initial allocation once; keep every fill/holding."""
    plan=repo.get(c,'cad_plan')
    if plan and plan.get('policy_version')==POLICY_VERSION:return
    account=c.execute("SELECT cash,initial FROM paper_accounts WHERE currency='CAD'").fetchone()
    credited=number(account['initial']) if account else Decimal(0)
    added=max(Decimal(0),INITIAL_CAD-credited)
    if account:
        c.execute("UPDATE paper_accounts SET cash=?,initial=? WHERE currency='CAD'",(str(number(account['cash'])+added),str(credited+added)))
    else:c.execute("INSERT INTO paper_accounts VALUES('CAD',?,?)",(str(INITIAL_CAD),str(INITIAL_CAD)))
    repo.set(c,'cad_plan',{**(plan or {}),'currency':'CAD','initial':str(INITIAL_CAD),'weekly':'0','anchor':(plan or {}).get('anchor',now),'enabled':False,'recurring':False,'policy_version':POLICY_VERSION,'changed_at':now})
    repo.put(c,'system_events',POLICY_VERSION,{'category':'contribution','code':'FIXED_PRINCIPAL_SET','amount':str(added),'currency':'CAD','principal_cad':str(INITIAL_CAD),'previous_credited_cad':str(credited),'recurring':False,'holdings_preserved':True})

def description():
    return {'currency':'CAD','policy_version':POLICY_VERSION,'initial_limit':str(INITIAL_CAD),'weekly_additional_limit':'0','recurring':False,
      'performance_bonus':{'trigger_net_return':'.20','target_realized_cad':str(BONUS_TARGET_CAD),'bonus_cad':str(BONUS_CAD),'basis':'net_realized_profit','effective':'once_on_threshold','separate_paper_live':True},
      'paper_contribution':'virtual','live_contribution':'verified_actual_funds_only','real_auto_deposit_enabled':False,'live_execution_enabled':False,'net_profit_and_fx_verification_required':True,'status':'POLICY_ONLY_NOT_LIVE_EXECUTION'}
