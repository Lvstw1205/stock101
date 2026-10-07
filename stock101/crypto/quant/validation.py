"""Training-only parameter selection composed with Stock101's simulator."""
import copy,math,statistics
from .. import validation as base
from . import specs

def windows(snapshot):
    times=[t for fold in base.folds_for(snapshot['assets'],limit=None) for t in fold]
    if len(times)<120:raise ValueError('QUANT_NEEDS_120_TEST_HOURS')
    boundary=len(times)*4//5;initial=boundary//2
    development=times[:boundary];holdout=times[boundary:]
    folds=[]
    for i in range(3):
        a=initial+(boundary-initial)*i//3;b=initial+(boundary-initial)*(i+1)//3
        folds.append((development[:a],development[a:b]))
    assert all(train[-1]<test[0] for train,test in folds)
    assert development[-1]<holdout[0]
    return development,holdout,folds

def score(run):return run['return_ratio']-run['max_drawdown']

def run(snapshot,spec,progress=lambda value:None,checkpoint=None):
    spec=specs.validate(spec);critique=specs.critique(spec,snapshot)
    if not critique['valid']:raise ValueError('QUANT_REQUIRED_DATA_MISSING')
    if set(snapshot['assets'])!=set(spec['products']):raise ValueError('QUANT_SNAPSHOT_SCOPE_MISMATCH')
    development,holdout,folds=windows(snapshot);variants=specs.neighbors(spec)
    checkpoint=checkpoint if checkpoint is not None else {}
    cache=checkpoint.setdefault('runs',{});done=0;total=len(variants)*(len(folds)+1)+len(folds)+len(base.SCENARIOS)
    def simulate(candidate,times,costs,trace=False):
        nonlocal done
        key=specs.digest(candidate)+':'+str(times[0])+':'+str(times[-1])+':'+costs['id']+':'+str(trace)
        if key not in cache:
            cache[key]=base.simulate(snapshot['assets'],times,costs,strategy_id=spec['strategy_id'],fx_data=snapshot.get('fx_history'),capital=1000,trace=trace,evaluator=specs.evaluator(candidate))
        done+=1
        progress(dict(completed=done,total=total,stage='과거 구간 검증',candidate=done,checkpoint=checkpoint))
        return copy.deepcopy(cache[key])
    oos=[];walk=[];train_returns=[]
    for index,(train,test) in enumerate(folds):
        trials=[simulate(candidate,train,base.SCENARIOS[0]) for candidate in variants]
        best=max(range(len(variants)),key=lambda i:score(trials[i]))
        result=simulate(variants[best],test,base.SCENARIOS[0],trace=True);oos.append(result)
        train_returns.append(trials[best]['return_ratio'])
        walk.append(dict(fold=index+1,train_start=train[0],train_end=train[-1]+3600,
                         test_start=test[0],test_end=test[-1]+3600,selected=variants[best]['parameters'],
                         train_return=trials[best]['return_ratio'],test_return=result['return_ratio'],
                         closed_exits=result['closed_exits'],max_drawdown=result['max_drawdown']))
    # This final selection cannot read the last 20% of timestamps. Every neighbor
    # is selected on development data only; the holdout is evaluated once for the
    # locked candidate, with separate cost/latency stresses of that same choice.
    training=[simulate(candidate,development,base.SCENARIOS[0]) for candidate in variants]
    winner=max(range(len(variants)),key=lambda i:score(training[i]));locked=variants[winner]
    stress={cost['id']:simulate(locked,holdout,cost,trace=True) for cost in base.SCENARIOS}
    final=stress['base'];combined=oos+[final];summary=base.summarize(combined)
    returns=[r for result in combined for r in result['returns']]
    timestamps=[p['time'] for result in combined for p in result['equity']]
    enough=sum((result['end']-result['start'])/86400 for result in combined)>=30 and summary['closed_exits']>=20
    flags=[]
    if training[winner]['return_ratio']>0 and final['return_ratio']<=0:flags.append('TRAIN_HOLDOUT_SIGN_REVERSAL')
    positive=sum(r['return_ratio']>0 for r in training)/len(training)
    if positive<.5:flags.append('NARROW_PARAMETER_PLATEAU')
    if any(r['return_ratio']<0 for r in stress.values()):flags.append('COST_OR_EXECUTION_SENSITIVITY')
    if sum(w['test_return']>0 for w in walk)<2:flags.append('WALK_FORWARD_INCONSISTENT')
    if not enough:flags.append('INSUFFICIENT_SAMPLE')
    if len(variants)>1:flags.append('MULTIPLE_TRIAL_SELECTION_BIAS')
    deviation=statistics.pstdev(returns) if len(returns)>1 else 0
    status='INSUFFICIENT_SAMPLE' if not enough else 'WEAK_EVIDENCE' if any(f!='MULTIPLE_TRIAL_SELECTION_BIAS' for f in flags) else 'RESEARCH_EVIDENCE_READY'
    return dict(status=status,strategy_id=spec['strategy_id'],spec_hash=specs.digest(spec),locked_spec=locked,
                products=list(snapshot['assets']),starting_capital_cad=1000,critique=critique,
                walk_forward=walk,holdout=dict(start=holdout[0],end=holdout[-1]+3600,result=final),
                summary=summary,stress={k:{x:v[x] for x in ('return_ratio','max_drawdown','closed_exits','fees','friction','halted')} for k,v in stress.items()},
                robustness=dict(variants=len(variants),positive_fraction=positive,
                    grid=[dict(parameters=v['parameters'],training_return=r['return_ratio'],training_drawdown=r['max_drawdown']) for v,r in zip(variants,training)]),
                overfitting=dict(flags=flags,diagnostic_type='heuristic_not_probability',selection_trials=len(variants)*(len(folds)+1),
                                training_mean=statistics.mean(train_returns),oos_mean=statistics.mean(r['return_ratio'] for r in oos)),
                out_of_sample_days=len(returns)/24,sharpe_annualized=statistics.mean(returns)/deviation*math.sqrt(365*24) if deviation else None,
                monte_carlo=base.monte_carlo(returns),return_series=[dict(at=t,value=r) for t,r in zip(timestamps,returns)],
                core_execution_changed=False,live_promotion=False,cost_assumptions=base.SCENARIOS,
                risk_model='Existing historical simulator loss stops; forward PAPER observations use current continuous-loss observation policy.',
                limitations=['시작 시점에 고정한 공개 시세로 계산한 연구 결과입니다. 미래 수익 보장이 아닙니다.',
                 '각 훈련·검증 구간은 독립 가상 1,000 CAD에서 시작합니다. 연결 수익률은 단일 연속 계좌와 다릅니다.',
                 '마지막 20% 구간은 파라미터 선택에 사용하지 않습니다. 반복 연구를 통해 사람이 이 구간을 다시 보면 독립성이 약해집니다.',
                 '현재 상장 목록, 시간봉 체결 가정, 과거 호가 깊이·장중 손절·계정별 거래 가능 여부의 한계가 있습니다.',
                 '과최적화 경고와 몬테카를로는 진단이며 미래 손실 확률이 아닙니다.',
                 '실험 간 사용 시세가 겹칠 수 있습니다. 좋은 결과만 골라 실거래 전략으로 자동 승격하지 않습니다.'])
