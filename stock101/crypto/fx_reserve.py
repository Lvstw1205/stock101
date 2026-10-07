"""USD/CAD discount accumulation; public references are not executable FX quotes."""
import datetime as dt
import json,math,statistics,time,urllib.request,urllib.parse

KEY='fx_reference_history_v1'
CATALOG=[dict(id='fx_usdc',name='환율 하락 시 USDC 적립',interval='캐나다 중앙은행 일일 환율 + 현재 호가',scope='USDC · CAD 자금',
 source_url='https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/',
 rule='USD/CAD가 최근 60영업일 평균보다 2% 이상 저렴 + 비용 차감 여유 확인 · 하루 1회 분할',
 adaptation='하락은 1 USD를 사는 CAD 비용이 줄어드는 뜻입니다. 목표는 진입 시 60일 평균 환율, 방어는 진입가 −3%. USDC가 USD 대비 0.5% 이상 이탈하면 신규 매수하지 않습니다. 실제 주문은 CAD 결제 USDC 현물 경로가 있을 때만 가능합니다.')]

def load_peg():
    req=urllib.request.Request('https://api.coinbase.com/v2/prices/USDC-USD/spot',headers={'User-Agent':'Stock101-Research/1.0'})
    with urllib.request.urlopen(req,timeout=5) as r:data=json.load(r)['data']
    value=float(data['amount'])
    if data.get('base')!='USDC' or data.get('currency')!='USD' or not math.isfinite(value) or value<=0:raise ValueError('USDC_PRICE_REFERENCE_REQUIRED')
    return dict(price=value,checked_at=time.time(),source='Coinbase indicative USDC/USD spot; not an executable order book')

def load():
    now=time.time();end=dt.datetime.fromtimestamp(now,dt.timezone.utc).date();start=end-dt.timedelta(days=650)
    query=urllib.parse.urlencode(dict(start_date=start.isoformat(),end_date=end.isoformat()))
    req=urllib.request.Request('https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?'+query,headers={'User-Agent':'Stock101-Research/1.0','Cache-Control':'no-cache'})
    with urllib.request.urlopen(req,timeout=12) as r:data=json.load(r)
    rows=[]
    for r in data['observations']:
        if not r.get('FXUSDCAD',{}).get('v'):continue
        value=float(r['FXUSDCAD']['v']);date=dt.date.fromisoformat(r['d'])
        if not start<=date<=end:raise ValueError('FX_HISTORY_OUTSIDE_REQUESTED_DATES')
        if not math.isfinite(value) or not .5<value<3:raise ValueError('INVALID_FX_HISTORY')
        # Conservative availability: the next UTC day, after the publication.
        at=dt.datetime.combine(date+dt.timedelta(days=1),dt.time(),dt.timezone.utc).timestamp()
        rows.append(dict(date=date.isoformat(),rate=value,available_at=at))
    if len(rows)<60:raise ValueError('FX_HISTORY_REQUIRED')
    result=dict(rows=rows,updated_at=now,source='Bank of Canada FXUSDCAD daily reference')
    if not context(result,now):raise ValueError('FX_HISTORY_STALE_OR_INCOMPLETE')
    return result

def context(data,now):
    rows=[r for r in (data or {}).get('rows',[]) if r['available_at']<=now]
    if len(rows)<60 or not 0<=now-rows[-1]['available_at']<=5*86400:return None
    if any(b['available_at']<=a['available_at'] for a,b in zip(rows,rows[1:])):return None
    if any(b['available_at']-a['available_at']>7*86400 for a,b in zip(rows[-60:],rows[-59:])):return None
    if any(not math.isfinite(float(r['rate'])) or not .5<float(r['rate'])<3 for r in rows):return None
    average=statistics.mean(r['rate'] for r in rows[-60:])
    return dict(rate=rows[-1]['rate'],mean60=average,date=rows[-1]['date'],available_at=rows[-1]['available_at'],
                discount=rows[-1]['rate']/average-1,source='Bank of Canada daily reference')

def evaluate(book,fx):
    pid=book['product_id'];now=book['timestamp'];mid=float(book['mid'])
    row=dict(strategy_id='fx_usdc',reason='USDC_ONLY',entry=False,facts={})
    if not pid.startswith('USDC-'):return row
    row['reason']='CAD_FUNDING_ROUTE_REQUIRED'
    if not (pid.endswith('-CAD') or book.get('valuation_currency')=='CAD'):return row
    row['reason']='FX_HISTORY_REQUIRED'
    if not fx or not 0<=now-fx.get('available_at',0)<=5*86400:return row
    if pid.endswith('-USD'):
        peg=float(book.get('native_mid',mid/float(book.get('fx_rate',fx['rate']))))
    else:
        quote=fx.get('usdc_quote') or {}
        if not -5<=now-quote.get('checked_at',0)<=120:
            row['reason']='USDC_PRICE_REFERENCE_REQUIRED';return row
        peg=float(quote.get('price',0))
    row['facts']={**fx,'usdc_usd':peg,'reference_not_executable':True}
    row['reason']='USDC_PEG_DEVIATION'
    if not .995<=peg<=1.005:return row
    row['reason']='FX_DISCOUNT_REQUIRED'
    if fx['discount']>-.02 or mid>fx['mean60']*.98:return row
    row.update(entry=True,reason='FX_USDC_BUY',entry_token='fx_usdc:'+fx['date'],target=fx['mean60'],
               invalidation=mid*.97,entry_low=mid*.97,entry_high=fx['mean60']*.98)
    return row
