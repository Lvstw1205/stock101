"""Explicit PAPER-only CAD valuation model. Never executable FX quotes."""
import datetime as dt
import json,time,urllib.request
from .exchange import CoinbaseAdapter,ExchangeError,number
from decimal import Decimal
class CadPaperAdapter:
    def __init__(self,source=None,fx_loader=None):
        self.source=source or CoinbaseAdapter();self.fx_loader=fx_loader or self._load_fx
        self._fx=None;self._fx_at=0;self.fx_bps=Decimal('25');self._books=None;self._level2=None
    def enable_book_cache(self):
        from .paper_books import PaperBooks
        from .level2_books import HeldLevel2Books
        if self._books is None:self._books=PaperBooks(self.source.get_orderbook)
        if self._level2 is None:self._level2=HeldLevel2Books()
    def set_held_products(self,products):
        if self._level2:self._level2.set_products(products)
    def book_stream_status(self):return self._level2.status() if self._level2 else {'connected':False,'ready':0,'subscribed':0}
    def validate_book(self,book):
        if book.get('source')=='COINBASE_LEVEL2' and (not self._level2 or not self._level2.is_current(book)):
            raise ExchangeError('STALE_MARKET_DATA')
    def __getattr__(self,name):return getattr(self.source,name)
    def _load_fx(self):
        request=urllib.request.Request('https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1',headers={'User-Agent':'Stock101-Paper/1.0'})
        with urllib.request.urlopen(request,timeout=12) as response:data=json.load(response)
        row=data['observations'][-1]
        return {'rate':row['FXUSDCAD']['v'],'date':row['d'],'source':'Bank of Canada daily reference; not an executable rate'}
    def fx(self):
        if self._fx is None or time.time()-self._fx_at>3600:
            try:
                result=self.fx_loader();rate=number(result['rate']);date=dt.date.fromisoformat(result['date'])
                age=(dt.datetime.now(dt.timezone.utc).date()-date).days
                if not Decimal('.5')<rate<Decimal('3') or not 0<=age<=5:raise ValueError()
            except Exception:raise ExchangeError('PAPER_FX_REFERENCE_UNAVAILABLE_OR_STALE') from None
            self._fx={**result,'rate':str(rate)};self._fx_at=time.time()
        return self._fx
    def convert_product(self,p):
        rate=number(self.fx()['rate']) if p['quote_currency_id']=='USD' else Decimal(1)
        result={**p,'native_quote_currency_id':p['quote_currency_id'],'quote_currency_id':'CAD','valuation_currency':'CAD','paper_fx_model':True}
        for key in ('quote_min_size','quote_max_size','quote_increment'):result[key]=str(number(p[key])*rate)
        return result
    def get_products(self):
        rows=self.source.get_products()
        # USD and direct CAD only. USDC is not treated as USD at par.
        return [self.convert_product(p) for p in rows if p['quote_currency_id'] in ('USD','CAD')]
    def get_product(self,pid):
        p=self.source.get_product(pid)
        if p['quote_currency_id'] not in ('USD','CAD'):raise ExchangeError('PAPER_FX_PAIR_UNSUPPORTED')
        return self.convert_product(p)
    def get_candles(self,pid,*args,**kwargs):
        rows=self.source.get_candles(pid,*args,**kwargs)
        rate=float(self.fx()['rate']) if pid.endswith('-USD') else 1
        return [{**r,**{k:r[k]*rate for k in ('open','high','low','close')}} for r in rows]
    def get_orderbook(self,pid):
        streamed=self._level2.get_many([pid]) if self._level2 else {}
        book=streamed.get(pid)
        if book is None:book=self._books.get(pid) if self._books else self.source.get_orderbook(pid)
        return self.convert_book(pid,book)
    def get_orderbooks(self,products):
        if not self._books:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=4) as pool:return dict(zip(products,pool.map(self.get_orderbook,products)))
        products=list(products)
        books=self._level2.get_many(products) if self._level2 else {}
        missing=[pid for pid in products if pid not in books]
        if missing:books.update(self._books.get_many(missing))
        # REST may take seconds. Read the latest contiguous stream state after
        # that wait, retaining its genuine exchange watermark and update times.
        if self._level2:books.update(self._level2.get_many(products))
        for book in books.values():self.validate_book(book)
        return {pid:self.convert_book(pid,b) for pid,b in books.items()}
    def convert_book(self,pid,b):
        foreign=pid.endswith('-USD')
        rate=number(self.fx()['rate']) if foreign else Decimal(1);markup=self.fx_bps/10000 if foreign else Decimal(0)
        result={**b,'native_mid':b['mid'],'mid':str(number(b['mid'])*rate),'valuation_currency':'CAD','fx_rate':str(rate),'fx_markup_bps':str(self.fx_bps if foreign else 0),'fx_reference':self.fx() if foreign else None}
        for side,mult in [('asks',1+markup),('bids',1-markup)]:
            result[side]=[{**x,'price':str(number(x['price'])*rate*mult)} for x in b[side]]
        # spread_bps remains exchange spread; FX markup is separately charged in levels.
        result['native_spread_bps']=b['spread_bps']
        return result
    def get_daily_candles(self,pid):
        rows=self.source.get_daily_candles(pid)
        rate=float(self.fx()['rate']) if pid.endswith('-USD') else 1
        return [{**r,**{k:r[k]*rate for k in ('open','high','low','close')}} for r in rows]
