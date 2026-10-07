"""Market/news provenance stays attached through every calculation."""
import csv
import datetime as dt
import io
import math
import re
import threading
import time
from urllib.parse import urlsplit

MARKETS={'KR':{'currency':'KRW','benchmark':'^KS11','name':'Korea'},'CA':{'currency':'CAD','benchmark':'^GSPTSE','name':'Canada'},'US':{'currency':'USD','benchmark':'^GSPC','name':'United States'}}
CATALOG=[('005930.KS','Samsung Electronics','KR','Technology'),('000660.KS','SK Hynix','KR','Technology'),('035420.KS','NAVER','KR','Communication'),('069500.KS','KODEX 200 ETF','KR','ETF'),('247540.KQ','Ecopro BM','KR','Materials'),('SHOP.TO','Shopify','CA','Technology'),('RY.TO','Royal Bank of Canada','CA','Financials'),('ENB.TO','Enbridge','CA','Energy'),('XIU.TO','iShares S&P/TSX 60 ETF','CA','ETF'),('AAPL','Apple','US','Technology'),('MSFT','Microsoft','US','Technology'),('NVDA','NVIDIA','US','Technology'),('SPY','SPDR S&P 500 ETF','US','ETF')]
_cache={}; _lock=threading.Lock()


def symbol(value):
    if not isinstance(value,str): raise ValueError('Ticker must be text.')
    s=value.strip().upper()
    if re.fullmatch(r'\d{6}',s): s+='.KS'
    if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=-]{0,19}',s): raise ValueError('Invalid ticker. Use exchange suffixes such as .TO, .V, .KS or .KQ.')
    if '.' in s and not s.endswith(('.TO','.V','.KS','.KQ')): raise ValueError('This listing is outside the supported markets. Use US share-class notation such as BRK-B.')
    return s


def market_of(ticker):
    return 'KR' if ticker.endswith(('.KS','.KQ')) else 'CA' if ticker.endswith(('.TO','.V')) else 'US'


def validate_bars(bars):
    if not isinstance(bars,list) or not 2<=len(bars)<=10000: raise ValueError('Supply 2–10,000 daily bars.')
    result=[]; previous=''
    for b in bars:
        date=dt.date.fromisoformat(str(b['date'])).isoformat()
        if date<=previous or date>dt.date.today().isoformat(): raise ValueError('Dates must be unique, chronological and not in the future.')
        v={k:float(b[k]) for k in ('open','high','low','close','volume')}
        if not all(math.isfinite(x) for x in v.values()) or min(v[k] for k in ('open','high','low','close'))<=0 or v['volume']<0: raise ValueError('Invalid OHLCV number.')
        tolerance=max(v['high'],v['close'])*1e-12
        if v['high']+tolerance<max(v['open'],v['close'],v['low']) or v['low']-tolerance>min(v['open'],v['close']): raise ValueError('High/low do not contain the open and close on '+date+'.')
        result.append({'date':date,**v}); previous=date
    return result


def import_csv(text):
    if not isinstance(text,str) or len(text)>150000: raise ValueError('CSV must be smaller than 150 KB.')
    return validate_bars(list(csv.DictReader(io.StringIO(text))))


def history(ticker):
    ticker=symbol(ticker)
    with _lock:
        cached=_cache.get(ticker)
        if cached and time.monotonic()-cached[0]<300: return cached[1]
    try:
        import yfinance as yf
        instrument=yf.Ticker(ticker)
        frame=instrument.history(period='10y',interval='1d',auto_adjust=True,timeout=15)
        meta=instrument.history_metadata
        expected=MARKETS[market_of(ticker)]['currency']
        if meta.get('currency')!=expected:raise ValueError('Provider currency does not match the selected market.')
        bars=[];quality=[]
        for stamp,row in frame.iterrows():
            # Exclude today's potentially unfinished daily bar for reproducible analysis.
            if stamp.date()>=dt.datetime.now(stamp.tzinfo).date(): continue
            bars.append({'date':stamp.date().isoformat(),**{k.lower():float(row[k]) for k in ('Open','High','Low','Close','Volume')}})
        # Never repair substantive bad OHLC or silently bridge a missing session.
        # Retain only the contiguous suffix after the last corrupt provider bar.
        last_bad=-1
        for i,b in enumerate(bars):
            try:validate_bars([dict(b,date='1900-01-01'),b])
            except ValueError as e:last_bad=i;quality.append(str(e).replace('1900-01-01',b['date']))
        if last_bad>=0:bars=bars[last_bad+1:]
        bars=validate_bars(bars)
    except Exception as e:
        raise ValueError('Historical data is unavailable for '+ticker+'. Retry later or import a sourced OHLCV CSV. Provider: '+type(e).__name__) from None
    result={'symbol':ticker,'bars':bars,'market':market_of(ticker),'currency':MARKETS[market_of(ticker)]['currency'],
            'source':'Yahoo Finance via existing AIRI yfinance dependency','source_url':'https://finance.yahoo.com/quote/'+ticker+'/history/',
            'as_of':bars[-1]['date'],'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),'adjusted':True,
            'price_type':'Completed daily adjusted bars, not an executable quote',
            'quality_warnings':quality,'history_truncated':last_bad>=0}
    with _lock: _cache[ticker]=(time.monotonic(),result)
    return result


def news(ticker):
    ticker=symbol(ticker)
    import yfinance as yf
    try: raw=yf.Ticker(ticker).news
    except Exception: raise ValueError('News provider unavailable; no sentiment was inferred.') from None
    now=dt.datetime.now(dt.timezone.utc); items=[]
    for item in raw or []:
        c=item.get('content') or item
        headline=c.get('title'); date=c.get('pubDate'); url=(c.get('canonicalUrl') or {}).get('url'); source=(c.get('provider') or {}).get('displayName')
        try: published=dt.datetime.fromisoformat(date.replace('Z','+00:00'))
        except (ValueError,TypeError,AttributeError): continue
        if not all((headline,url,source)) or urlsplit(url).scheme!='https' or published.tzinfo is None or not 0<=(now-published).total_seconds()<=7*86400: continue
        # Transparent lexical baseline. Neutral means no matched cue, not a human judgment.
        positive=len(re.findall(r'\b(beat|beats|growth|upgrade|profit|record|raises)\b',headline,re.I))
        negative=len(re.findall(r'\b(miss|misses|loss|downgrade|probe|falls|cuts)\b',headline,re.I))
        sentiment='Positive' if positive>negative else 'Negative' if negative>positive else 'Neutral'
        items.append({'headline':headline,'published_at':published.isoformat(),'url':url,'source':source,'ticker':ticker,'market':market_of(ticker),'sentiment':sentiment})
    unique={x['url']:x for x in items}; items=list(unique.values())[:15]
    return {'items':items,'counts':{k:sum(x['sentiment']==k for x in items) for k in ('Positive','Neutral','Negative')},'method':'Headline keyword baseline, not an LLM assessment. Provider ticker association may include related companies. Read each source.','fetched_at':now.isoformat()}


def search(query,market):
    if market not in MARKETS: raise ValueError('Unknown market.')
    query=str(query)[:80]
    local=[{'symbol':s,'name':n,'market':m,'sector':sector} for s,n,m,sector in CATALOG if m==market and (query.lower() in (s+' '+n).lower() or query=='')]
    if not query or local: return local
    try:
        import yfinance as yf
        rows=yf.Search(query,max_results=12,news_count=0).quotes
        allowed={'KR':{'KSC','KOE'},'CA':{'TOR','VAN'},'US':{'NMS','NYQ','NGM','NCM','ASE','PCX','BTS'}}
        return [{'symbol':r['symbol'],'name':r.get('shortname') or r.get('longname') or r['symbol'],'market':market} for r in rows if r.get('exchange') in allowed[market] and market_of(r['symbol'])==market and r.get('quoteType') in ('EQUITY','ETF')]
    except Exception: return []
