"""Coinbase live adapter. Only spot limit IOC + preview/cancel writes exist.
The default adapter remains read-only. This instance needs an in-process permit.
"""
import json,re,time,urllib.request,urllib.error,urllib.parse,uuid
from .exchange import CoinbaseAdapter,ExchangeError,number,product_id
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ExchangeError('API_REDIRECT_REFUSED')
class LiveAdapter(CoinbaseAdapter):
    def __init__(self,transport=None,clock=time.time,credential_provider=None):
        super().__init__(None,clock);self.live_transport=transport;self.credentials=credential_provider;self.permit=lambda kind:False
    def call(self,method,path,params=None,body=None):
        if method not in ('GET','POST') or not path.startswith('/'):raise ExchangeError('INVALID_API_METHOD')
        full=self.prefix+path;headers={'Accept':'application/json','Content-Type':'application/json','User-Agent':'Stock101/3','Authorization':'Bearer '+self._jwt(full,method)}
        try:
            if self.live_transport:return self.live_transport(method,full,params or {},body,headers)
            with self._lock:
                if self.clock()<self._blocked_until:raise ExchangeError('RATE_LIMIT_COOLDOWN')
                delay=.25-(time.monotonic()-self._last)
                if delay>0:time.sleep(delay)
                self._last=time.monotonic()
            url=self.base+full+('?' + urllib.parse.urlencode(params,doseq=True) if params else '')
            data=json.dumps(body,allow_nan=False).encode() if body is not None else None
            with urllib.request.build_opener(NoRedirect()).open(urllib.request.Request(url,data=data,headers=headers,method=method),timeout=8) as response:
                raw=response.read(8_000_001)
                if len(raw)>8_000_000:raise ExchangeError('RESPONSE_TOO_LARGE')
                result=json.loads(raw,parse_float=str)
            if not isinstance(result,dict) or result.get('error'):raise ExchangeError('INVALID_API_RESPONSE')
            return result
        except urllib.error.HTTPError as e:
            if e.code==429:self._blocked_until=self.clock()+60
            raise ExchangeError({401:'AUTHENTICATION_FAILED',403:'ACCESS_DENIED',429:'RATE_LIMITED'}.get(e.code,'EXCHANGE_REQUEST_FAILED')) from None
        except ExchangeError:raise
        except Exception:raise ExchangeError('EXCHANGE_REQUEST_UNCERTAIN') from None
    def permissions(self,require_trade=False):
        p=self.call('GET','/key_permissions')
        if p.get('can_view') is not True or p.get('can_transfer') is not False:raise ExchangeError('VIEW_WITHOUT_TRANSFER_REQUIRED')
        if require_trade and p.get('can_trade') is not True:raise ExchangeError('TRADE_PERMISSION_REQUIRED')
        return {'view':True,'trade':p.get('can_trade') is True,'transfer':False}
    def balances(self):
        output={};seen=set();cursor=None
        for _ in range(100):
            d=self.call('GET','/accounts',{'limit':250,**({'cursor':cursor} if cursor else {})})
            if not isinstance(d.get('accounts'),list):raise ExchangeError('INVALID_ACCOUNTS_RESPONSE')
            for a in d['accounts']:
                currency=a.get('currency');free=a.get('available_balance',{});hold=a.get('hold',{})
                if not isinstance(currency,str) or not re.fullmatch('[A-Z0-9]{1,24}',currency) or free.get('currency')!=currency or hold.get('currency')!=currency:raise ExchangeError('INVALID_ACCOUNT_BALANCE')
                f=number(free.get('value'));h=number(hold.get('value'))
                if min(f,h)<0:raise ExchangeError('INVALID_ACCOUNT_BALANCE')
                old=output.get(currency,{'available':'0','held':'0'})
                output[currency]={'available':str(number(old['available'])+f),'held':str(number(old['held'])+h)}
            if not d.get('has_next'):return output
            cursor=d.get('cursor')
            if not isinstance(cursor,str) or not cursor or cursor in seen:raise ExchangeError('INVALID_ACCOUNT_PAGINATION')
            seen.add(cursor)
        raise ExchangeError('ACCOUNT_PAGINATION_LIMIT')
    def fees(self):
        data=self.call('GET','/transaction_summary',{'product_type':'SPOT'})
        tier=data.get('fee_tier',{});maker=number(tier.get('maker_fee_rate'));taker=number(tier.get('taker_fee_rate'))
        if min(maker,taker)<0 or max(maker,taker)>.05:raise ExchangeError('INVALID_FEE_TIER')
        return {'maker':str(maker),'taker':str(taker),'checked_at':self.clock()}
    def trading_products(self):
        rows=[]
        for offset in range(0,10000,500):
            d=self.call('GET','/products',{'product_type':'SPOT','get_tradability_status':'true','limit':500,'offset':offset})
            page=d.get('products')
            if not isinstance(page,list):raise ExchangeError('INVALID_PRODUCTS')
            for x in page:
                if x.get('product_type')!='SPOT':continue
                rows.append(self.live_product(x))
            if len(page)<500:return rows
        raise ExchangeError('PRODUCT_PAGINATION_LIMIT')
    def trading_product(self,pid):
        raw=self.call('GET','/products/'+product_id(pid),{'get_tradability_status':'true'})
        p=self.live_product(raw)
        if p['product_id']!=pid:raise ExchangeError('PRODUCT_MISMATCH')
        return p
    def live_product(self,raw):
        p=self._product(raw);tick=number(raw.get('price_increment',p['quote_increment']))
        if tick<=0:raise ExchangeError('INVALID_PRICE_INCREMENT')
        p['price_increment']=str(tick);p['alias']=product_id(raw.get('alias') or p['product_id'])
        return p
    @staticmethod
    def validate_payload(order):
        if set(order)-{'client_order_id','product_id','side','order_configuration','preview_id'}:raise ExchangeError('UNSUPPORTED_ORDER_FIELD')
        product_id(order.get('product_id'))
        if order.get('side') not in ('BUY','SELL'):raise ExchangeError('INVALID_SIDE')
        cfg=order.get('order_configuration',{})
        if set(cfg)!= {'sor_limit_ioc'}:raise ExchangeError('LIMIT_IOC_REQUIRED')
        spec=cfg['sor_limit_ioc']
        if set(spec)!={'base_size','limit_price'} or any(number(spec[x])<=0 for x in spec):raise ExchangeError('INVALID_LIMIT_ORDER')
    def preview(self,order):
        self.validate_payload(order)
        return self.call('POST','/orders/preview',body={k:v for k,v in order.items() if k not in ('client_order_id','preview_id')})
    def place_order(self,order):
        if not self.permit('submit'):raise ExchangeError('LOCAL_LIVE_ACTIVATION_REQUIRED')
        self.validate_payload(order)
        try:
            if str(uuid.UUID(order.get('client_order_id','')))!=order['client_order_id']:raise ValueError()
        except (ValueError,KeyError,TypeError,AttributeError):raise ExchangeError('INVALID_CLIENT_ORDER_ID') from None
        return self.call('POST','/orders',body=order)
    def cancel_order(self,order_id):
        if not self.permit('cancel'):raise ExchangeError('LOCAL_LIVE_ACTIVATION_REQUIRED')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',order_id):raise ExchangeError('INVALID_ORDER_ID')
        return self.call('POST','/orders/batch_cancel',body={'order_ids':[order_id]})
    def get_order(self,order_id):
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',order_id):raise ExchangeError('INVALID_ORDER_ID')
        return self.call('GET','/orders/historical/'+order_id).get('order')
    def find_order(self,client_id,start):
        cursor=None;seen=set()
        for _ in range(100):
            d=self.call('GET','/orders/historical/batch',{'start_date':start,'limit':100,'product_type':'SPOT',**({'cursor':cursor} if cursor else {})})
            if not isinstance(d.get('orders'),list):raise ExchangeError('INVALID_ORDERS')
            for order in d['orders']:
                if order.get('client_order_id')==client_id:return order
            if not d.get('has_next'):return None
            cursor=d.get('cursor')
            if not cursor or cursor in seen:raise ExchangeError('INVALID_ORDER_PAGINATION')
            seen.add(cursor)
        raise ExchangeError('ORDER_PAGINATION_LIMIT')
