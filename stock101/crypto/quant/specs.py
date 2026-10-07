"""Data-only hypotheses using existing executable rules, never generated code."""
import copy,hashlib,json,math,re
from .. import execution_strategies as rules

PARAMETERS={'minimum_net_return':(.005,.03,.005),'allocation_scale':(.25,1.,1.)}

def validate(raw):
    if not isinstance(raw,dict) or set(raw)-{'name','hypothesis','strategy_id','parameters','products'}:raise ValueError('QUANT_INVALID_SPEC')
    sid=raw.get('strategy_id')
    if not isinstance(sid,str) or sid not in rules.NAMES:raise ValueError('QUANT_UNKNOWN_STRATEGY')
    params=raw.get('parameters',{})
    if not isinstance(params,dict) or set(params)-set(PARAMETERS):raise ValueError('QUANT_INVALID_PARAMETERS')
    cleaned={}
    for key,(low,high,default) in PARAMETERS.items():
        value=params.get(key,default)
        if type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high:raise ValueError('QUANT_PARAMETER_OUT_OF_RANGE')
        cleaned[key]=float(value)
    products=raw.get('products',['BTC-USD','ETH-USD'])
    if not isinstance(products,list) or not 1<=len(products)<=4 or any(not isinstance(p,str) for p in products) or len(products)!=len(set(products)):raise ValueError('QUANT_INVALID_PRODUCTS')
    if any(not isinstance(p,str) or not re.fullmatch('[A-Z0-9]{1,24}-(USD|CAD)',p) for p in products):raise ValueError('QUANT_INVALID_PRODUCTS')
    if len({p.split('-')[0] for p in products})!=len(products):raise ValueError('QUANT_DUPLICATE_ASSET')
    if sid in ('dca_weekly','mayer'):products=[p for p in products if p.startswith('BTC-')]
    if sid=='fx_usdc':products=[p for p in products if p.startswith('USDC-')]
    if not products:raise ValueError('QUANT_STRATEGY_PRODUCT_MISMATCH')
    name=raw.get('name',rules.NAMES[sid]);hypothesis=raw.get('hypothesis','기존 규칙의 비용 차감 성과와 재현성을 검증합니다.')
    if not isinstance(name,str) or not 1<=len(name)<=120 or not isinstance(hypothesis,str) or len(hypothesis)>1600:raise ValueError('QUANT_INVALID_TEXT')
    return dict(name=name,hypothesis=hypothesis,strategy_id=sid,parameters=cleaned,products=products)

def digest(spec):return hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def evaluator(spec):
    spec=validate(spec)
    def evaluate(bars,position,book,**kwargs):
        kwargs.update(only_strategy=spec['strategy_id'],minimum=spec['parameters']['minimum_net_return'])
        out=rules.evaluate(bars,position,book,**kwargs)
        out['allocation']*=spec['parameters']['allocation_scale']
        out['plan']['research_spec_hash']=digest(spec)
        out['plan']['research_minimum_net_return']=spec['parameters']['minimum_net_return']
        return out
    return evaluate

def neighbors(spec):
    spec=validate(spec);params=spec['parameters'];out={}
    values={k:sorted({max(low,min(high,round(params[k]*factor,8))) for factor in (.8,1,1.2)}) for k,(low,high,_) in PARAMETERS.items()}
    for edge in values['minimum_net_return']:
        for scale in values['allocation_scale']:
            row=copy.deepcopy(spec);row['parameters']=dict(minimum_net_return=edge,allocation_scale=scale)
            out[digest(row)]=row
    return list(out.values())

def critique(spec,snapshot=None):
    spec=validate(spec);issues=[]
    if not snapshot:issues.append(dict(code='DATA_NOT_FROZEN',severity='info',message='검증 시작 시 입력 시세를 고정합니다.'))
    else:
        for pid in spec['products']:
            asset=snapshot.get('assets',{}).get(pid)
            if not asset:issues.append(dict(code='MISSING_PRODUCT',severity='error',product=pid,message='필요한 시간봉·일봉이 없습니다.'))
            elif len(asset['hourly'])<345:issues.append(dict(code='SHORT_HISTORY',severity='warning',product=pid,message='30일 이상 성과 검증에는 표본이 부족할 수 있습니다.'))
        if any(a['foreign'] for a in snapshot.get('assets',{}).values()) and not snapshot.get('fx_history'):
            issues.append(dict(code='FIXED_FX_ASSUMPTION',severity='warning',message='해당 과거 환율이 없으면 명시된 고정 환율을 사용합니다.'))
    issues.extend([dict(code='TARGET_IS_NOT_FORECAST',severity='info',message='목표 가격은 예상 수익의 보장이 아닙니다.'),
                   dict(code='CURRENT_LISTING_BIAS',severity='warning',message='현재 수집 가능한 상장 코인만 포함합니다. 상장 폐지 편향이 남습니다.')])
    return dict(valid=not any(i['severity']=='error' for i in issues),issues=issues,generated_code=False,
                core_risk_override=False,required_evidence=dict(out_of_sample_days=30,closed_exits=20,shadow_days=7))
