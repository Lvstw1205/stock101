"""Read-only Coinbase spot adapter; live writes use a separate guarded adapter."""
import datetime as dt
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Protocol

class ExchangeError(ValueError):
    pass

def number(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite(): raise ValueError()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise ExchangeError('INVALID_NUMBER') from None

def product_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Z0-9]{1,24}-[A-Z0-9]{1,24}',value):
        raise ExchangeError('INVALID_PRODUCT')
    return value

class ExchangeAdapter(Protocol):
    def get_accounts(self): ...
    def get_portfolios(self): ...
    def get_products(self): ...
    def get_product(self, product): ...
    def get_candles(self, product): ...
    def get_best_bid_ask(self, product): ...
    def get_orderbook(self, product): ...
    def get_orders(self): ...
    def get_order(self, order_id): ...
    def place_order(self, order): ...
    def cancel_order(self, order_id): ...

class CoinbaseAdapter:
    base='https://api.coinbase.com'
    prefix='/api/v3/brokerage'
    websocket_market='wss://advanced-trade-ws.coinbase.com'
    websocket_user='wss://advanced-trade-ws-user.coinbase.com'
    def __init__(self, transport=None, clock=time.time):
        self.transport=transport; self.clock=clock; self.credentials=None
        self._lock=threading.Lock(); self._last=0.; self._blocked_until=0.
    def _jwt(self,path=None,method="GET"):
        # These values never leave this method except in an Authorization header.
        key=os.environ.get('ST101_COINBASE_KEY_NAME')
        secret=os.environ.get('ST101_COINBASE_PRIVATE_KEY')
        if self.credentials:
            pair=self.credentials()
            key,secret=pair if pair else (None,None)
        elif not key or not secret:
            from . import credentials
            pair=credentials.read()
            if pair:key,secret=pair
        if not key or not secret: raise ExchangeError('READ_ONLY_CREDENTIALS_NOT_CONFIGURED')
        try:
            import jwt
            stamp=int(self.clock())
            claims={'sub':key,'iss':'cdp','nbf':stamp,'exp':stamp+120}
            if path: claims['uri']=method+' api.coinbase.com'+path
            return jwt.encode(claims,secret.replace('\\n','\n'),algorithm='ES256',headers={'kid':key,'nonce':secrets.token_hex(16)})
        except Exception: raise ExchangeError('AUTH_CONFIGURATION_INVALID') from None
    def request(self,path,params=None,private=False):
        headers={'Accept':'application/json','Cache-Control':'no-cache','User-Agent':'Stock101-Paper/2'}
        full=self.prefix+path
        if private: headers['Authorization']='Bearer '+self._jwt(full)
        with self._lock:
            if self.clock()<self._blocked_until: raise ExchangeError('RATE_LIMIT_COOLDOWN')
            # Conservative application cap, not a claim about the exchange quota.
            delay=.25-(time.monotonic()-self._last)
            if delay>0 and not self.transport: time.sleep(delay)
            self._last=time.monotonic()
        try:
            if self.transport: result=self.transport(full,params or {},headers)
            else:
                url=self.base+full+('?' + urllib.parse.urlencode(params,doseq=True) if params else '')
                req=urllib.request.Request(url,headers=headers,method='GET')
                with urllib.request.urlopen(req,timeout=8) as response:
                    raw=response.read(8_000_001)
                    if len(raw)>8_000_000: raise ExchangeError('RESPONSE_TOO_LARGE')
                    result=json.loads(raw)
            if not isinstance(result,dict) or result.get('error'):raise ExchangeError('INVALID_API_RESPONSE')
            return result
        except urllib.error.HTTPError as e:
            if e.code==429:
                self._blocked_until=self.clock()+60
            if e.code==404 and (path.startswith('/market/products/') or path=='/market/product_book'):
                raise ExchangeError('PRODUCT_NOT_FOUND') from None
            raise ExchangeError({401:'AUTHENTICATION_FAILED',403:'ACCESS_DENIED',429:'RATE_LIMITED'}.get(e.code,'EXCHANGE_UNAVAILABLE')) from None
        except ExchangeError:raise
        except Exception:raise ExchangeError('EXCHANGE_UNAVAILABLE') from None
    def get_products(self):
        rows=[]
        for offset in range(0,10000,1000):
            result=self.request('/market/products',{'product_type':'SPOT','limit':1000,'offset':offset})
            page=result.get('products')
            if not isinstance(page,list):raise ExchangeError('INVALID_PRODUCTS')
            rows.extend(self._product(p) for p in page if p.get('product_type')=='SPOT')
            if len(page)<1000:break
        else: raise ExchangeError('INCOMPLETE_PRODUCT_DISCOVERY')
        return rows
    def _product(self,p):
        pid=product_id(p.get('product_id')); base=p.get('base_currency_id'); quote=p.get('quote_currency_id')
        if not base or not quote or pid!=base+'-'+quote:raise ExchangeError('INVALID_PRODUCT_METADATA')
        result={k:p.get(k) for k in ('product_id','base_currency_id','base_name','quote_currency_id','status','product_type','trading_disabled','is_disabled','cancel_only','limit_only','post_only','view_only')}
        for key in ('base_increment','quote_increment','base_min_size','base_max_size','quote_min_size','quote_max_size'):
            n=number(p.get(key))
            if n<=0:raise ExchangeError('INVALID_PRODUCT_LIMITS')
            result[key]=str(n)
        try:result['quote_volume_24h']=str(max(number(p.get('volume_24h','0'))*number(p.get('price','0')),0))
        except Exception:result['quote_volume_24h']='0'
        result['tradable']=p.get('status')=='online' and p.get('product_type')=='SPOT' and not any(p.get(k,False) for k in ('trading_disabled','is_disabled','cancel_only','limit_only','post_only','view_only'))
        return result
    def get_product(self,product):
        p=self._product(self.request('/market/products/'+product_id(product)))
        if p['product_id']!=product:raise ExchangeError('PRODUCT_MISMATCH')
        return p
    def get_orderbook(self,product):
        product_id(product)
        p=self.request('/market/product_book',{'product_id':product,'limit':20}).get('pricebook',{})
        if p.get('product_id')!=product:raise ExchangeError('BOOK_PRODUCT_MISMATCH')
        try:stamp=dt.datetime.fromisoformat(p['time'].replace('Z','+00:00')).timestamp()
        except Exception:raise ExchangeError('MISSING_EXCHANGE_TIMESTAMP') from None
        sides={}
        for side in ('bids','asks'):
            rows=p.get(side)
            if not isinstance(rows,list) or not rows:raise ExchangeError('EMPTY_ORDERBOOK')
            sides[side]=[]
            for row in rows:
                price,size=number(row.get('price')),number(row.get('size'))
                if price<=0 or size<=0:raise ExchangeError('INVALID_ORDERBOOK')
                sides[side].append({'price':str(price),'size':str(size)})
            sides[side].sort(key=lambda x:number(x['price']),reverse=side=='bids')
        bid=number(sides['bids'][0]['price']); ask=number(sides['asks'][0]['price'])
        if bid>=ask:raise ExchangeError('CROSSED_ORDERBOOK')
        age=self.clock()-stamp
        if not -5<=age<=15:raise ExchangeError('STALE_MARKET_DATA')
        return {'product_id':product,**sides,'timestamp':stamp,'received_at':self.clock(),'mid':str((bid+ask)/2),'spread_bps':str((ask-bid)/((ask+bid)/2)*10000)}
    def get_best_bid_ask(self,product):
        book=self.get_orderbook(product)
        return {'bid':book['bids'][0],'ask':book['asks'][0],'timestamp':book['timestamp']}
    def get_candles(self,product,start=None,end=None,contiguous_tail=False,granularity='ONE_HOUR'):
        step={'ONE_HOUR':3600,'ONE_DAY':86400,'THIRTY_MINUTE':1800}.get(granularity)
        if not step:raise ExchangeError('UNSUPPORTED_CANDLE_INTERVAL')
        end=int(end or self.clock())//step*step
        start=int(start if start is not None else end-350*step)
        if end<=start or end-start>350*step:raise ExchangeError('CANDLE_WINDOW_MAX_350_HOURS' if step==3600 else 'CANDLE_WINDOW_MAX_350_BARS')
        rows=self.request('/market/products/'+product_id(product)+'/candles',{'start':start,'end':end,'granularity':granularity,'limit':350}).get('candles')
        if not isinstance(rows,list):raise ExchangeError('INVALID_CANDLES')
        result=[]
        for x in rows:
            t=int(x['start'])
            if t>=end:continue  # incomplete candle excluded
            if t<start or t%step:raise ExchangeError('INVALID_CANDLE_TIME')
            values={k:number(x[k]) for k in ('open','high','low','close','volume')}
            if values['low']<=0 or values['volume']<0 or not values['low']<=min(values['open'],values['close'])<=max(values['open'],values['close'])<=values['high']:raise ExchangeError('INVALID_CANDLE')
            result.append({'start':t,**{k:float(v) for k,v in values.items()}})
        result.sort(key=lambda x:x['start'])
        if len({b['start'] for b in result})!=len(result):raise ExchangeError('CANDLE_GAP_OR_DUPLICATE')
        gaps=[i for i,(a,b) in enumerate(zip(result,result[1:]),1) if b['start']-a['start']!=step]
        if gaps:
            if contiguous_tail:result=result[gaps[-1]:]
            else:raise ExchangeError('CANDLE_GAP_OR_DUPLICATE')
        if not result or result[-1]['start']!=end-step:raise ExchangeError('STALE_CANDLES')
        return result
    def get_daily_candles(self,product):return self.get_candles(product,granularity='ONE_DAY',contiguous_tail=True)
    def check_readonly_connection(self):
        permissions=self.request('/key_permissions',private=True)
        if permissions.get('can_view') is not True or permissions.get('can_trade') is not False or permissions.get('can_transfer') is not False:
            raise ExchangeError('VIEW_ONLY_KEY_REQUIRED')
        accounts=self.get_accounts()
        if not isinstance(accounts.get('accounts'),list):raise ExchangeError('INVALID_ACCOUNTS_RESPONSE')
        return {'connected':True,'access':'READ_ONLY','account_count':len(accounts['accounts']),'more_accounts':bool(accounts.get('has_next')),'checked_at':self.clock(),'live_enabled':False,'storage':'macOS Keychain or backend environment','balances_imported_into_paper':False}
    def readonly_balances(self):
        self.check_readonly_connection()
        balances=[];cursor=None;seen=set()
        for _ in range(100):
            response=self.request('/accounts',{'limit':250,**({'cursor':cursor} if cursor else {})},private=True)
            accounts=response.get('accounts')
            if not isinstance(accounts,list):raise ExchangeError('INVALID_ACCOUNTS_RESPONSE')
            for account in accounts:
                available=account.get('available_balance',{});hold=account.get('hold',{})
                currency=account.get('currency')
                if not isinstance(currency,str) or not re.fullmatch(r'[A-Z0-9]{1,24}',currency):raise ExchangeError('INVALID_ACCOUNT_CURRENCY')
                if available.get('currency')!=currency or hold.get('currency')!=currency:raise ExchangeError('INVALID_ACCOUNT_BALANCE')
                free=number(available.get('value'));held=number(hold.get('value'))
                if free<0 or held<0:raise ExchangeError('INVALID_ACCOUNT_BALANCE')
                if free or held:balances.append({'currency':currency,'available':str(free),'held':str(held)})
            if not response.get('has_next'):
                return {'source':'COINBASE_REAL_ACCOUNT','access':'READ_ONLY','live_enabled':False,'balances':balances,'as_of':self.clock(),'paper_funds_included':False}
            cursor=response.get('cursor')
            if not isinstance(cursor,str) or not cursor or cursor in seen:raise ExchangeError('INVALID_ACCOUNT_PAGINATION')
            seen.add(cursor)
        raise ExchangeError('ACCOUNT_PAGINATION_LIMIT')
    def get_accounts(self):return self.request('/accounts',private=True)
    def get_portfolios(self):return self.request('/portfolios',private=True)
    def get_orders(self):return self.request('/orders/historical/batch',private=True)
    def get_order(self,order_id):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',order_id):raise ExchangeError('INVALID_ORDER_ID')
        return self.request('/orders/historical/'+order_id,private=True)
    def place_order(self,order):raise ExchangeError('LIVE_EXECUTION_DISABLED')
    def cancel_order(self,order_id):raise ExchangeError('LIVE_EXECUTION_DISABLED')
    def subscription(self,products,channel='ticker'):
        if channel not in ('ticker','heartbeats','level2','market_trades','candles','user'):raise ExchangeError('UNSUPPORTED_CHANNEL')
        msg={'type':'subscribe','product_ids':[product_id(p) for p in products],'channel':channel}
        if channel=='user':msg['jwt']=self._jwt()
        return msg

class StreamHealth:
    """Transport-independent continuity guard; reconnect always requires reconciliation."""
    def __init__(self):self.connected=False;self.last=0;self.sequences={};self.reason='OFFLINE'
    def observe(self,message,now=None):
        now=time.time() if now is None else now
        channel=message.get('channel');seq=message.get('sequence_num')
        if not channel or not isinstance(seq,int):return self.disconnect('INVALID_WS_MESSAGE')
        previous=self.sequences.get('connection')
        if previous is not None and seq!=previous+1:return self.disconnect('WS_SEQUENCE_GAP')
        self.sequences['connection']=seq;self.last=now;self.connected=True;self.reason='CONNECTED'
        return True
    def disconnect(self,reason='WS_DISCONNECTED'):
        self.connected=False;self.sequences={};self.reason=reason;return False
