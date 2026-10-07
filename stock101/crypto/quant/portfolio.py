"""Aligned return correlations and constrained research allocations."""
import math,statistics,time

def correlation(a,b):
    x={int(r['at']):float(r['value']) for r in a};y={int(r['at']):float(r['value']) for r in b}
    times=sorted(x.keys()&y.keys())
    if len(times)<72:return dict(value=None,observations=len(times),status='INSUFFICIENT_OVERLAP')
    xs=[x[t] for t in times];ys=[y[t] for t in times];mx=statistics.mean(xs);my=statistics.mean(ys)
    vx=sum((v-mx)**2 for v in xs);vy=sum((v-my)**2 for v in ys)
    if vx<=1e-20 or vy<=1e-20:return dict(value=None,observations=len(times),status='FLAT_SERIES')
    return dict(value=max(-1,min(1,sum((u-mx)*(v-my) for u,v in zip(xs,ys))/math.sqrt(vx*vy))),observations=len(times),status='MEASURED')

def health(experiment,report,observed,now=None):
    now=time.time() if now is None else now;flags=[]
    if not report:flags.append('VALIDATION_PENDING')
    elif report.get('status')!='RESEARCH_EVIDENCE_READY':flags.append('VALIDATION_SAMPLE_OR_EDGE_WEAK')
    if observed:
        if now-observed.get('as_of',0)>max(45,observed.get('cadence_seconds',60)*2):flags.append('OBSERVER_DELAYED')
        if not observed.get('fresh'):flags.append('MARKET_MARK_STALE')
        if observed.get('drawdown') is not None and float(observed['drawdown'])>=.10:flags.append('DRAWDOWN_WARNING')
        if observed.get('closed_exits',0)<20 or observed.get('days',0)<7:flags.append('INCUBATION_SAMPLE_SHORT')
        if float(observed.get('realized_cad') or 0)<0:flags.append('REALIZED_EXPECTANCY_NEGATIVE')
        if report and observed.get('return_ratio') is not None and float(observed['return_ratio'])<min(-.03,report['summary']['return_ratio']-.05):flags.append('PERFORMANCE_DEGRADATION')
    else:flags.append('OBSERVATION_NOT_STARTED')
    serious={'OBSERVER_DELAYED','MARKET_MARK_STALE','DRAWDOWN_WARNING','REALIZED_EXPECTANCY_NEGATIVE','PERFORMANCE_DEGRADATION'}
    return dict(experiment_id=experiment['id'],status='ATTENTION' if serious&set(flags) else 'COLLECTING_EVIDENCE' if flags else 'REVIEW_READY',
                flags=flags,advice='기록·비용·데이터 상태를 재검토하세요.' if serious&set(flags) else '표본을 더 모으고 검증 결과와 비교합니다.' if flags else '수동 검토에 필요한 연구 표본이 준비되었습니다.',
                auto_live_promotion=False,base_engine_paused=False)

def analyze(experiments,reports,observations):
    pairs=[];eligible=[];series={}
    for e in experiments:
        report=reports.get(e['id'])
        if report:series[e['id']]=report.get('return_series',[])
    ids=list(series)
    for i,left in enumerate(ids):
        for right in ids[i+1:]:pairs.append(dict(left=left,right=right,**correlation(series[left],series[right])))
    health_rows=[health(e,reports.get(e['id']),observations.get(e['id'])) for e in experiments]
    for e,h in zip(experiments,health_rows):
        report=reports.get(e['id'])
        if not report or report.get('status')!='RESEARCH_EVIDENCE_READY' or h['status']!='REVIEW_READY':continue
        if report['summary']['return_ratio']<=0:continue
        eligible.append(e)
    eligible.sort(key=lambda e:reports[e['id']]['summary']['return_ratio']-reports[e['id']]['summary']['max_drawdown'],reverse=True)
    selected=[];reasons={};weights={e['id']:0. for e in experiments}
    for e in eligible:
        related=[p for p in pairs if e['id'] in (p['left'],p['right']) and any(s['id'] in (p['left'],p['right']) for s in selected)]
        if any(p['value'] is None for p in related):reasons[e['id']]='CORRELATION_UNVERIFIED';continue
        if any(abs(p['value'])>=.8 for p in related):reasons[e['id']]='CORRELATED_STRATEGY';continue
        selected.append(e)
    # Recommendations only. Never write execution weights or spend core funds.
    # Limit total strategy allocation and per-coin implied allocation to the
    # same existing 60% investment / 40% reserve / 10% asset caps.
    used=0.;asset_weights={}
    for e in selected:
        assets=e['spec']['products'];capacity=min(.10,(.60-used),min(max(0,.10-asset_weights.get(p.split('-')[0],0))*len(assets) for p in assets))
        weight=max(0,capacity);weights[e['id']]=weight;used+=weight
        for pid in assets:asset_weights[pid.split('-')[0]]=asset_weights.get(pid.split('-')[0],0)+weight/len(assets)
    return dict(at=time.time(),correlations=pairs,health=health_rows,
                allocations=[dict(experiment_id=e['id'],weight=weights[e['id']],reason=reasons.get(e['id'],'RESEARCH_ALLOCATION' if weights[e['id']]>0 else 'EVIDENCE_NOT_READY')) for e in experiments],
                cash_weight=1-used,asset_weights=asset_weights,applied_to_trading=False,
                risk_intelligence=dict(concentration_cap=.10,total_investment_cap=.60,cash_reserve_floor=.40,
                    high_correlation_pairs=sum(p['value'] is not None and abs(p['value'])>=.8 for p in pairs),
                    unverified_pairs=sum(p['value'] is None for p in pairs),sample_basis='overlapping_out_of_sample_hourly_returns',
                    regime_note='각 전략의 기존 추세·횡보·변동성 판단을 유지하며, 표본 구간과 비용 민감도를 함께 검토합니다.'))
