"""Causal, long-only adaptations of the three user-supplied videos.

See STRATEGY-REFERENCES.md for timestamps, published rules and adaptations.
These functions produce candidates, never orders or performance predictions.
"""
import math
import statistics

VIDEO_1='https://www.youtube.com/watch?v=Np5zmePC_5Y'
VIDEO_2='https://www.youtube.com/watch?v=UAC-tD1-Sa8'
VIDEO_3='https://www.youtube.com/watch?v=OtImPEfcpvc'
CATALOG=[
 dict(id='supertrend',name='슈퍼트렌드 전환',interval='완료 일봉',scope='모든 후보',
      rule='ATR 10 × 3 추세가 상승으로 전환하면 매수 · 하락 전환 시 청산',source_url=VIDEO_1+'&t=115s',
      adaptation='일봉·ATR 10·배수 3 적용. 현물 매수만 사용하며 공통 목표·방어선·비용 검사를 추가했습니다.'),
 dict(id='hull',name='Hull 추세 추종',interval='완료 일봉',scope='모든 후보',
      rule='55기간 HMA가 2봉 전보다 높으면 매수 · 낮아지면 청산',source_url=VIDEO_1+'&t=342s',
      adaptation='DashTrader의 Hull Suite 기본 HMA 55 규칙. 공통 목표·방어선 추가, 같은 완료 일봉에서 재매수하지 않습니다.'),
 dict(id='ichimoku',name='일목균형표 TK 추세',interval='완료 일봉',scope='모든 후보',
      rule='전환선 20 > 기준선 60 + 종가 > EMA 200 · 전환선이 기준선 아래면 청산',source_url=VIDEO_1+'&t=674s',
      adaptation='dukeofdolma의 공개 규칙과 기간을 사용합니다. 미래로 이동해 그린 구름은 판단에 쓰지 않습니다.'),
 dict(id='macd_trend',name='MACD + 이동평균',interval='완료 1시간봉',scope='모든 후보',
      rule='EMA 35 > 195 + MACD(20,46,2) 0선 상향 돌파 · 추세 반전 시 청산',source_url=VIDEO_2+'&t=80s',
      adaptation='영상의 0선 돌파 설명을 적용합니다. 실행 봉은 1시간으로 정했고 공통 목표·방어선을 추가했습니다.'),
 dict(id='rsi_cross',name='RSI 확인 후 교차 매수',interval='1시간봉 + 완료 8시간봉',scope='모든 후보',
      rule='EMA 5가 160 상향 돌파 + 8시간 RSI(14) > 50 · 하향 교차 시 청산',source_url=VIDEO_2+'&t=220s',
      adaptation='8시간봉은 UTC 기준 시간봉 8개를 합칩니다. 미확정 RSI 기간은 14로 명시해 적용했습니다.'),
 dict(id='rsi_reversion',name='상승 추세 눌림목',interval='완료 1시간봉',scope='모든 후보',
      rule='종가 > EMA 200 + RSI(9) < 25 · RSI 70에서 순익 확인 또는 추세 이탈 방어',source_url=VIDEO_2+'&t=328s',
      adaptation='영상 설명의 20/25·70/75 불일치 중 설정 설명의 25/70을 채택했습니다. EMA 200은 1시간봉 기준입니다.'),
 dict(id='red_day',name='BTC 하락 신호 분할매수',interval='BTC 실시간 + 완료 일봉',scope='BTC·ETH 제외 후보',
      rule='BTC 당일 −4.7/−7/−9.5/−12/−15/−18% · 일봉 −3.3% · 3일 연속 하락',source_url=VIDEO_3+'&t=387s',
      adaptation='BTC 신호 1개당 포트폴리오 전체 1회, 주차당 최대 15회. 개별 코인 목표·방어선과 기존 예산 한도를 적용합니다. 과거 고정 달러 가격은 사용하지 않습니다.')]
IDS={r['id'] for r in CATALOG}
TREND_IDS=IDS-{'red_day'}

def ema(values,length):
    """SMA seed, then exponential smoothing; unavailable prefix stays None."""
    out=[None]*len(values)
    if len(values)<length:return out
    value=statistics.mean(values[:length]);out[length-1]=value;alpha=2/(length+1)
    for i in range(length,len(values)):
        value+=alpha*(values[i]-value);out[i]=value
    return out

def rma(values,length):
    out=[None]*len(values)
    if len(values)<length:return out
    value=statistics.mean(values[:length]);out[length-1]=value
    for i in range(length,len(values)):
        value+=(values[i]-value)/length;out[i]=value
    return out

def rsi(values,length):
    changes=[b-a for a,b in zip(values,values[1:])]
    up=rma([max(x,0) for x in changes],length);down=rma([max(-x,0) for x in changes],length)
    return [None]+[None if u is None else (50. if u==d==0 else 100. if d==0 else 100-100/(1+u/d)) for u,d in zip(up,down)]

def wma(values,length):
    out=[None]*len(values);denom=length*(length+1)/2
    for i in range(length-1,len(values)):
        window=values[i-length+1:i+1]
        if all(x is not None for x in window):out[i]=sum((j+1)*x for j,x in enumerate(window))/denom
    return out

def hma(values,length=55):
    fast=wma(values,length//2);slow=wma(values,length)
    raw=[None if a is None or b is None else 2*a-b for a,b in zip(fast,slow)]
    return wma(raw,int(math.sqrt(length)+.5))

def true_ranges(rows):
    return [max(b['high']-b['low'],abs(b['high']-rows[max(0,i-1)]['close']),abs(b['low']-rows[max(0,i-1)]['close'])) if i else b['high']-b['low'] for i,b in enumerate(rows)]

def supertrend(rows,length=10,factor=3):
    volatility=rma(true_ranges(rows),length);out=[];upper=lower=None;up=False
    for i,(b,a) in enumerate(zip(rows,volatility)):
        if a is None:out.append(None);continue
        basic_upper=(b['high']+b['low'])/2+factor*a;basic_lower=(b['high']+b['low'])/2-factor*a
        prior=rows[max(0,i-1)]['close'];first=upper is None
        upper=basic_upper if first or basic_upper<upper or prior>upper else upper
        lower=basic_lower if first or basic_lower>lower or prior<lower else lower
        up=False if first else (b['close']>=lower if up else b['close']>upper)
        out.append(dict(up=up,line=lower if up else upper,atr=a))
    return out

def aggregate_8h(rows,now):
    """Discard partial UTC blocks; never invent candles across missing hours."""
    blocks={}
    for row in rows:
        start=int(row['start'])//28800*28800
        if start+28800<=now:blocks.setdefault(start,[]).append(row)
    result=[]
    for start,group in sorted(blocks.items()):
        if [b['start'] for b in group]!=list(range(start,start+28800,3600)):continue
        result.append(dict(start=start,open=group[0]['open'],close=group[-1]['close'],high=max(b['high'] for b in group),low=min(b['low'] for b in group)))
    if not result or result[-1]['start']!=int(now)//28800*28800-28800:return []
    if any(b['start']-a['start']!=28800 for a,b in zip(result,result[1:])):return []
    return result

def daily_valid(rows,now,count=4):
    rows=[b for b in (rows or []) if b['start']+86400<=now]
    if len(rows)<count or rows[-1]['start']!=int(now)//86400*86400-86400:return None
    if any(b['start']-a['start']!=86400 for a,b in zip(rows,rows[1:])):return None
    if any(not math.isfinite(float(b[k])) or float(b[k])<=0 for b in rows for k in ('open','high','low','close')):return None
    return rows

def red_day_trigger(benchmark,now,fills,anchor):
    """One durable token per BTC event across all destination assets."""
    facts={};week=max(0,int((now-anchor)//604800));prefix=f'red_day:{week}:'
    if not benchmark or benchmark.get('product_id') not in ('BTC-USD','BTC-CAD','BTC-USDC') or not -5<=now-benchmark.get('timestamp',0)<=15:return 'BTC_REFERENCE_REQUIRED',None,facts
    rows=daily_valid(benchmark.get('daily'),now)
    if not rows:return 'BTC_REFERENCE_REQUIRED',None,facts
    price=float(benchmark.get('price',0))
    if not math.isfinite(price) or price<=0:return 'BTC_REFERENCE_REQUIRED',None,facts
    filled={f.get('strategy_token') for f in fills if f.get('side')=='BUY' and float(f.get('quantity',0))>0}
    count=sum(isinstance(t,str) and t.startswith(prefix) for t in filled)
    facts.update(btc_product=benchmark.get('product_id'),btc_price=price,btc_at=benchmark['timestamp'],weekly_events=count,weekly_limit=15)
    if count>=15:return 'RED_DAY_WEEKLY_LIMIT',None,facts
    day=int(now)//86400*86400;today_open=benchmark.get('session_open')
    tokens=[]
    if today_open and math.isfinite(float(today_open)) and float(today_open)>0:
        drop=price/float(today_open)-1;facts['btc_intraday_return']=drop
        for level in (.18,.15,.12,.095,.07,.047):
            if drop<=-level+1e-12:tokens.append((f'{prefix}intraday:{day}:{level:g}','BTC_INTRADAY_DIP_BUY'))
    last=rows[-1];daily_return=last['close']/last['open']-1;facts['btc_daily_return']=daily_return
    if daily_return<=-.033+1e-12:tokens.append((f'{prefix}close:{last["start"]}','BTC_RED_DAY_BUY'))
    returns=[b['close']/a['close']-1 for a,b in zip(rows[-4:-1],rows[-3:])]
    # Exclude daily-close triggers from this separate three-day event.
    red=all(-.033<x<0 for x in returns) and all(b['close']/b['open']-1>-.033 for b in rows[-3:])
    facts['btc_three_red_days']=red
    if red:tokens.append((f'{prefix}red3:{last["start"]}','BTC_THREE_RED_BUY'))
    for token,reason in tokens:
        if token not in filled:return reason,token,facts
    return ('INSTALLMENT_ALREADY_FILLED' if tokens else 'BTC_DIP_TRIGGER_REQUIRED'),None,facts

def evaluate(hour,days,now,mid,pid,*,benchmark=None,fills=(),anchor=0):
    result=[]
    def row(sid,reason,**kwargs):
        r=dict(strategy_id=sid,reason=reason,entry=False,exit_signal=False,profit_exit=False,facts={},**kwargs);result.append(r);return r
    def enter(r,rows):
        a=rma(true_ranges(rows),14)[-1];close=rows[-1]['close']
        r['facts'].update(atr=a,signal_close=close,signal_at=rows[-1]['start'])
        r.update(target=close+3*a,invalidation=max(close*.01,close-2*a),entry_low=max(close*.01,close-2*a),entry_high=close+.5*a)
        if a<=0 or not r['entry_low']<mid<=r['entry_high']:
            r['reason']='REFERENCE_PRICE_EXTENDED';return
        r.update(entry=True,entry_token=f'{r["strategy_id"]}:{rows[-1]["start"]}')
    r=row('supertrend','DAILY_REFERENCE_HISTORY_REQUIRED')
    if days and len(days)>=50:
        st=supertrend(days);current,previous=st[-1],st[-2]
        r.update(reason='SUPERTREND_TURN_REQUIRED',exit_signal=not current['up'],facts=dict(supertrend=current['line'],up=current['up'],atr_length=10,factor=3))
        if current['up'] and not previous['up']:r['reason']='SUPERTREND_BUY';enter(r,days)
    r=row('hull','DAILY_REFERENCE_HISTORY_REQUIRED')
    if days and len(days)>=65:
        hull=hma([b['close'] for b in days]);delta=hull[-1]-hull[-3];eps=days[-1]['close']*1e-12
        r.update(reason='HULL_UPTREND_REQUIRED',exit_signal=delta < -eps,facts=dict(hma=hull[-1],hma_two_bars_ago=hull[-3],length=55))
        if delta>eps:r['reason']='HULL_BUY';enter(r,days)
    r=row('ichimoku','DAILY_200_HISTORY_REQUIRED')
    if days and len(days)>=201:
        closes=[b['close'] for b in days];average=ema(closes,200)[-1]
        tenkan=(max(b['high'] for b in days[-20:])+min(b['low'] for b in days[-20:]))/2
        kijun=(max(b['high'] for b in days[-60:])+min(b['low'] for b in days[-60:]))/2
        r.update(reason='TK_TREND_REQUIRED',exit_signal=tenkan<kijun,facts=dict(tenkan=tenkan,kijun=kijun,ema200=average))
        if tenkan>kijun and closes[-1]>average:r['reason']='ICHIMOKU_BUY';enter(r,days)
    r=row('macd_trend','HOURLY_200_HISTORY_REQUIRED')
    if hour and len(hour)>=201:
        close=[b['close'] for b in hour];fast=ema(close,35);slow=ema(close,195)
        macd=[a-b for a,b in zip(ema(close,20)[45:],ema(close,46)[45:])];signal=ema(macd,2)
        r.update(reason='MACD_ZERO_CROSS_REQUIRED',exit_signal=fast[-1]<slow[-1] or macd[-1]<0,
                 facts=dict(ema35=fast[-1],ema195=slow[-1],macd=macd[-1],macd_previous=macd[-2],macd_signal=signal[-1]))
        if fast[-1]>slow[-1] and macd[-2]<=0<macd[-1]:r['reason']='MACD_TREND_BUY';enter(r,hour)
    r=row('rsi_cross','HOURLY_200_HISTORY_REQUIRED')
    if hour and len(hour)>=201:
        close=[b['close'] for b in hour];fast=ema(close,5);slow=ema(close,160);eight=aggregate_8h(hour,now)
        r.update(reason='RSI_8H_HISTORY_REQUIRED',exit_signal=fast[-1]<slow[-1],facts=dict(ema5=fast[-1],ema160=slow[-1],eight_hour_bars=len(eight)))
        if len(eight)>=15:
            strength=rsi([b['close'] for b in eight],14)[-1];r['facts']['rsi8h']=strength;r['reason']='RSI_EMA_CROSS_REQUIRED'
            if fast[-2]<=slow[-2] and fast[-1]>slow[-1] and strength>50:r['reason']='RSI_CROSS_BUY';enter(r,hour)
    r=row('rsi_reversion','HOURLY_200_HISTORY_REQUIRED')
    if hour and len(hour)>=201:
        close=[b['close'] for b in hour];average=ema(close,200)[-1];strength=rsi(close,9)[-1]
        r.update(reason='RSI_PULLBACK_REQUIRED',exit_signal=close[-1]<average,profit_exit=strength>=70,facts=dict(ema200=average,rsi9=strength,entry_rsi=25,exit_rsi=70))
        if close[-1]>average and strength<25:r['reason']='RSI_REVERSION_BUY';enter(r,hour)
    r=row('red_day','ALT_ONLY' if pid.split('-')[0] in ('BTC','ETH') else 'DAILY_REFERENCE_HISTORY_REQUIRED')
    if pid.split('-')[0] not in ('BTC','ETH') and days and len(days)>=20:
        reason,token,facts=red_day_trigger(benchmark,now,fills,anchor);r.update(reason=reason,facts=facts)
        if token:
            a=rma(true_ranges(days),14)[-1];target=max(b['high'] for b in days[-20:]);stop=max(mid*.01,min(b['low'] for b in days[-20:])-2*a)
            r.update(entry=True,entry_token=token,target=target,invalidation=stop,entry_low=stop,entry_high=target)
            r['facts'].update(target_basis='20 completed daily highs',atr=a)
            if not stop<mid<target:r.update(entry=False,reason='REFERENCE_PRICE_EXTENDED')
    return result
