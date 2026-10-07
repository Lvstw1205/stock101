"""Explicit spot adaptations of the Koothan strategy categories, not its return claims."""
import math
import statistics
from .reference_strategies import ema, rsi, rma, true_ranges

SOURCE='https://koothaninfotech.com/blog/crypto-trading-bot-strategies'
CATALOG=[
 dict(id='volume_breakout',name='거래량 확인 돌파',interval='완료 1시간봉',scope='모든 후보',source_url=SOURCE,
      rule='직전 20봉 고점 돌파 + 거래량 1.5배 + EMA 20 > 50 + ADX 25 이상 · ATR 목표·방어',
      adaptation='글의 모멘텀·거래량 확인 개념을 적용했습니다. 20봉·1.5배·EMA 20/50·ADX 14/25·목표 3 ATR·방어 2 ATR은 Stock101의 명시적 설정이며 원문에 공개된 수치가 아닙니다.'),
 dict(id='bollinger_reentry',name='볼린저밴드 복귀 매수',interval='완료 1시간봉',scope='모든 후보',source_url=SOURCE,
      rule='볼린저(20,2) 하단 밖 종가 → 밴드 안 복귀 + RSI 30 상향 + ADX 25 미만 · 중심선 목표',
      adaptation='기존 RSI 눌림목과 달리 횡보 구간의 밴드 재진입을 사용합니다. 기간·기준·최근 5봉 저점 아래 ATR 방어는 Stock101 설정입니다. 밴드 하단에 닿기만 하면 매수하지 않습니다.')]
IDS={r['id'] for r in CATALOG}

def adx(rows,length=14):
    if len(rows)<2*length:return None
    plus=[0.];minus=[0.]
    for a,b in zip(rows,rows[1:]):
        up=b['high']-a['high'];down=a['low']-b['low']
        plus.append(up if up>down and up>0 else 0.)
        minus.append(down if down>up and down>0 else 0.)
    tr=rma(true_ranges(rows),length);p=rma(plus,length);m=rma(minus,length)
    dx=[]
    for t,u,d in zip(tr,p,m):
        if t is None:continue
        a=100*u/t if t else 0.;b=100*d/t if t else 0.
        dx.append(100*abs(a-b)/(a+b) if a+b else 0.)
    return rma(dx,length)[-1]

def bands(values):
    mean=statistics.mean(values[-20:]);sd=statistics.pstdev(values[-20:])
    return mean-2*sd,mean,mean+2*sd

def evaluate(hour,now,mid):
    result=[dict(strategy_id=s['id'],reason='HOURLY_HISTORY_REQUIRED',entry=False,exit_signal=False,facts={}) for s in CATALOG]
    if not hour or len(hour)<51:return result
    close=[b['close'] for b in hour];last=hour[-1];a=rma(true_ranges(hour),14)[-1]
    strength=adx(hour);fast=ema(close,20)[-1];slow=ema(close,50)[-1]
    common=dict(adx=strength,atr=a,ema20=fast,ema50=slow,signal_close=close[-1],signal_at=last['start'])
    r=result[0];high=max(b['high'] for b in hour[-21:-1])
    r.update(reason='VOLUME_BREAKOUT_REQUIRED',exit_signal=fast<slow,facts={**common,'breakout_high':high})
    volume=[b.get('volume') for b in hour[-21:]]
    if any(v is None or not math.isfinite(float(v)) or float(v)<0 for v in volume):r['reason']='VOLUME_HISTORY_REQUIRED'
    else:
        average=statistics.mean(volume[:-1]);ratio=volume[-1]/average if average>0 else 0
        r['facts']['volume_ratio']=ratio
        if close[-1]>high and ratio>=1.5 and fast>slow and strength>=25:
            r.update(entry=True,reason='VOLUME_BREAKOUT_BUY',target=close[-1]+3*a,invalidation=max(close[-1]*.01,close[-1]-2*a),entry_low=high,entry_high=close[-1]+.5*a)
    r=result[1];old_lower,_,_=bands(close[:-1]);lower,center,upper=bands(close);strengths=rsi(close,14)
    r.update(reason='BOLLINGER_REENTRY_REQUIRED',facts={**common,'lower':lower,'center':center,'upper':upper,'rsi':strengths[-1],'previous_rsi':strengths[-2]})
    if strength<25 and close[-2]<old_lower and lower<close[-1]<center and strengths[-2]<=30<strengths[-1]:
        r.update(entry=True,reason='BOLLINGER_REENTRY_BUY',target=center,invalidation=max(close[-1]*.01,min(b['low'] for b in hour[-5:])-a),entry_low=lower,entry_high=min(center,close[-1]+.5*a))
    for r in result:
        if r['entry']:
            r['entry_token']=f'{r["strategy_id"]}:{last["start"]}'
            if a<=0 or not r['entry_low']<mid<=r['entry_high']:r.update(entry=False,reason='REFERENCE_PRICE_EXTENDED')
    return result
