"""Use a completed AI opinion without waiting for the model on the order path."""
import copy,datetime as dt,json,time
from pathlib import Path
from .exchange import number
from . import profit_policy

MAX_AGE=3600

def opinion(book,data_dir,mode,now):
    root=Path(data_dir)/'tradingagents'
    settings=json.loads((root/'settings.json').read_text())
    if not settings.get('apply_to_execution',True):return None,None
    pid=book['product_id']
    if not pid or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in pid):return None,None
    paths=[]
    if mode=='paper' and settings.get('rapid_enabled',True) and settings.get('auto_enabled'):
        paths.append((root/'fast_opinions'/mode/f'{pid}.json',180))
    paths.append((root/'opinions'/mode/f'{pid}.json',MAX_AGE))
    fallback=(None,None)
    for path,age in paths:
        try:
            row=json.loads(path.read_text())
            if not isinstance(row,dict):continue
            rating=row.get('rating');status='APPLIED'
            if row.get('ticker')!=pid or row.get('asset_type')!='crypto' or row.get('mode')!=mode:status='OTHER_CONTEXT'
            elif row.get('date')!=dt.datetime.fromtimestamp(now).date().isoformat() or not 0<=now-row.get('created_at',0)<=age:status='STALE_OPINION'
            elif row.get('reference_price') is None or not 0<=now-(row.get('reference_at') or 0)<=age:status='STALE_PRICE_CONTEXT'
            elif rating not in ('Buy','Overweight','Hold','Underweight','Sell'):status='REVIEW_REQUIRED'
            elif abs(number(book['mid'])/number(row['reference_price'])-1)>number('.03'):status='PRICE_CONTEXT_CHANGED'
            if status=='APPLIED':return row,status
            fallback=(row,status)
        except (OSError,ValueError,TypeError,ArithmeticError):continue
    return fallback

def selected_entry(signal,position,book,data_dir,now=None):
    try:
        if position and number(position.get('quantity',0))>0:return None
        row,status=opinion(book,data_dir,'paper',time.time() if now is None else now)
        if status!='APPLIED' or row.get('rating')!='Buy' or row.get('source')!='rapid_candidate_review':return None
        return next((c['strategy_id'] for c in signal.get('strategy_candidates',[]) if c['decision']=='BUY' and c['strategy_id']==row.get('strategy_id') and c.get('entry_token') and c['entry_token']==row.get('entry_token')),None)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,ArithmeticError):return None

def apply(signal,position,book,data_dir,mode='paper',now=None):
    try:return _apply(copy.deepcopy(signal),position,book,data_dir,mode,now)
    except (OSError,ValueError,TypeError,KeyError,AttributeError,ArithmeticError):return signal

def _apply(signal,position,book,data_dir,mode='paper',now=None):
    now=time.time() if now is None else now
    row,status=opinion(book,data_dir,mode,now)
    if row is None:return signal
    rating=row.get('rating')
    signal.setdefault('plan',{})['ai_team']={'status':status,'rating':rating,'job_id':row.get('job_id'),'at':row.get('created_at'),'source':row.get('source','TradingAgents'),'applied':False}
    proof=signal['plan']['ai_team']
    if status!='APPLIED':return signal
    # Every explicit stop or existing strategy exit retains precedence.
    if signal['decision'] in ('SELL','REDUCE'):return signal
    if rating=='Buy' and signal['decision']=='BUY' and row.get('source')=='rapid_candidate_review' and signal.get('strategy_id')==row.get('strategy_id') and signal['plan'].get('entry_token')==row.get('entry_token'):
        proof.update(applied=True,action='ENTRY_SELECTED')
        signal['reason_codes']=list(signal['reason_codes'])+['AGENTS_SELECTED_ENTRY']
    if rating in ('Underweight','Sell'):
        if signal['decision']=='BUY':
            signal.update(decision='HOLD' if position and number(position.get('quantity',0))>0 else 'WAIT',reason_codes=['AGENTS_AVOID_ENTRY']);proof['applied']=True
        p=signal['plan'].get('profit_management') or {}
        if position and number(position.get('quantity',0))>0 and p.get('net_profit_cad') is not None and number(p['net_profit_cad'])>0:
            if rating=='Underweight' and position.get('profit_taken'):return signal
            fraction=1 if rating=='Sell' else .5
            signal.update(decision='SELL',sell_fraction=fraction,reason_codes=['AGENTS_NET_EXIT'] if fraction==1 else ['AGENTS_PARTIAL_NET_EXIT','PARTIAL_PROFIT_TAKING']);proof['applied']=True
    return signal
