"""Timestamped public market data; no model-generated prices."""
import datetime as dt,json,os,re,time,urllib.request,hashlib,math
from pathlib import Path
from decimal import Decimal

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def get_json(url):
    req=urllib.request.Request(url,headers={'User-Agent':'AIRI personal research/1.0'})
    with urllib.request.urlopen(req,timeout=12) as r:return json.load(r)
def cached(key,ttl,fetch):
    root=Path(os.environ.get('AIRI_DATA',str(Path(__file__).parent/'data')))/'market-cache';root.mkdir(exist_ok=True,parents=True,mode=0o700)
    path=root/(hashlib.sha256(key.encode()).hexdigest()+'.json')
    try:
        saved=json.loads(path.read_text())
        if time.time()-saved['time']<ttl:return {**saved['value'],'cached':True}
    except (OSError,ValueError,KeyError):pass
    value=fetch();temp=path.with_suffix('.'+str(os.getpid())+'.tmp');temp.write_text(json.dumps({'time':time.time(),'value':value},allow_nan=False));temp.chmod(0o600);temp.replace(path)
    return {**value,'cached':False}

def exchange_rate(base='USD',quote='CAD',amount=1):
    base=base.upper();quote=quote.upper()
    if not re.fullmatch('[A-Z]{3}',base) or not re.fullmatch('[A-Z]{3}',quote):raise ValueError('Use ISO currency codes such as USD and CAD.')
    if not math.isfinite(float(amount)) or not 0<=float(amount)<=1e12:raise ValueError('Amount must be between 0 and 1 trillion.')
    def fetch():
        if base==quote:return {'base':base,'quote':quote,'rate':1,'as_of':dt.date.today().isoformat(),'source':'Identity conversion','source_url':'','rate_type':'identity','fetched_at':now()}
        if {base,quote}=={'CAD','USD'}:
            url='https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=10'
            obs=get_json(url)['observations'];row=next(r for r in reversed(obs) if r.get('FXUSDCAD',{}).get('v'))
            rate=Decimal(row['FXUSDCAD']['v']);rate=rate if base=='USD' else Decimal(1)/rate
            date=row['d'];source='Bank of Canada';source_url='https://www.bankofcanada.ca/rates/exchange/daily-exchange-rates/'
        else:
            data=get_json('https://api.frankfurter.dev/v2/rate/'+base+'/'+quote);rate=Decimal(str(data['rate']));date=data['date'];source='Frankfurter central-bank reference';source_url='https://frankfurter.dev/'
        age=(dt.datetime.now(dt.timezone.utc).date()-dt.date.fromisoformat(date)).days
        if rate<=0:raise ValueError('Provider returned an invalid rate.')
        return {'base':base,'quote':quote,'rate':float(rate),'as_of':date,'age_days':age,'stale':age>5,'source':source,'source_url':source_url,'rate_type':'daily indicative reference, not a bank execution quote','fetched_at':now()}
    result=cached('fx:'+base+quote,300,fetch)
    result['amount']=float(amount);result['converted']=str((Decimal(str(amount))*Decimal(str(result['rate']))).quantize(Decimal('.01')))
    return result

def quote(symbol):
    symbol=symbol.upper().strip()
    if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=\-]{0,19}',symbol):raise ValueError('Use a valid Yahoo ticker, such as AAPL or RY.TO.')
    def fetch():
        import yfinance as yf
        ticker=yf.Ticker(symbol);info=ticker.get_info()
        price=info.get('regularMarketPrice');stamp=info.get('regularMarketTime');currency=info.get('currency')
        if price is None or not math.isfinite(float(price)) or float(price)<0 or not stamp or not currency:raise ValueError('No timestamped price and currency returned for '+symbol+'. Check the exchange suffix, e.g. .TO for Toronto.')
        age=time.time()-float(stamp)
        fields=('shortName','exchange','fullExchangeName','marketState','regularMarketChangePercent','regularMarketChange','regularMarketPreviousClose','marketCap','trailingPE','forwardPE','fiftyTwoWeekLow','fiftyTwoWeekHigh')
        return {'symbol':symbol,'price':price,'currency':currency,'as_of':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat(),'fetched_at':now(),'age_seconds':round(age),'stale':age>86400,'source':'Yahoo Finance via yfinance','source_url':'https://finance.yahoo.com/quote/'+symbol,'price_type':'regular-session quote; may be delayed, not guaranteed real-time',**{k:info.get(k) for k in fields}}
    return cached('quote:'+symbol,20,fetch)

def search(query):
    if not isinstance(query,str) or not 1<=len(query)<=80:raise ValueError('Search text must be 1–80 characters.')
    def fetch():
        import yfinance as yf
        matches=yf.Search(query,max_results=8,news_count=0).quotes
        allowed={'NMS','NYQ','NGM','NCM','ASE','PCX','BTS','TOR','VAN','CNQ'}
        rows=[{'symbol':r['symbol'],'name':r.get('shortname') or r.get('longname'),'exchange':r.get('exchange')} for r in matches if r.get('exchange') in allowed and r.get('quoteType') in ('EQUITY','ETF','INDEX')]
        return {'matches':rows,'source':'Yahoo Finance via yfinance','region':'Canada and US','fetched_at':now()}
    return cached('search:'+query,3600,fetch)

def snapshot(symbol):
    result=quote(symbol)
    def fetch_chart():
        import yfinance as yf
        frame=yf.Ticker(result['symbol']).history(period='5d',interval='5m',auto_adjust=False,prepost=False,timeout=12)
        values=[]
        if not frame.empty:
            day=frame.index[-1].date()
            for stamp,row in frame.iterrows():
                price=float(row['Close'])
                if stamp.date()==day and math.isfinite(price):values.append({'time':stamp.isoformat(),'close':price})
        return {'points':values,'session_date':frame.index[-1].date().isoformat() if not frame.empty else None}
    try:chart=cached('spark:'+result['symbol'],120,fetch_chart)
    except Exception:chart={'points':[],'session_date':None,'error':'Chart unavailable'}
    return {**result,'chart':chart}


def fundamentals(symbol):
    """Dated statement rows, without guessing unavailable AFFO or future prices."""
    symbol=symbol.upper().strip()
    if not re.fullmatch(r'[A-Z0-9^][A-Z0-9.^=\-]{0,19}',symbol):raise ValueError('Invalid ticker')
    def fetch():
        import yfinance as yf
        ticker=yf.Ticker(symbol);info=ticker.get_info()
        fields=('shortName','currency','financialCurrency','sector','industry','marketCap','enterpriseValue','trailingPE','forwardPE','priceToBook','enterpriseToEbitda','totalCash','totalDebt','operatingCashflow','freeCashflow','revenueGrowth','earningsGrowth','profitMargins','operatingMargins','returnOnEquity','dividendRate','payoutRatio','lastFiscalYearEnd','mostRecentQuarter')
        metrics={k:v for k in fields if (v:=info.get(k)) is not None and (not isinstance(v,float) or math.isfinite(v))}
        financials={};failures=[]
        for name,attribute,rows in [('income','quarterly_income_stmt',['Total Revenue','Operating Income','Net Income','Diluted EPS']),('cash_flow','quarterly_cashflow',['Operating Cash Flow','Capital Expenditure','Free Cash Flow','Cash Dividends Paid']),('balance_sheet','quarterly_balance_sheet',['Total Debt','Cash And Cash Equivalents','Stockholders Equity'])]:
            try:
                frame=getattr(ticker,attribute);periods=[]
                for period in frame.columns[:4]:
                    values={}
                    for row in rows:
                        if row in frame.index:
                            value=float(frame.loc[row,period])
                            if math.isfinite(value):values[row]=value
                    if values:periods.append({'period_end':period.date().isoformat(),'values':values})
                financials[name]=periods
            except Exception:failures.append(name)
        return {'symbol':symbol,'metrics':metrics,'financials':financials,'fetched_at':now(),'source':'Yahoo Finance via yfinance','source_url':'https://finance.yahoo.com/quote/'+symbol+'/financials/','limitations':['Secondary provider; report dates are period ends, not filing timestamps.','No AFFO or five-year targets inferred.','Ratios are provider snapshots; definitions and fiscal bases can differ.']+failures}
    return cached('fundamentals:'+symbol,900,fetch)
