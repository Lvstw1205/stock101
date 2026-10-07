"""Broker boundary. No adapter ever reports a fabricated live fill."""
import datetime as dt
import json
import math
import os
import urllib.request
import uuid
from decimal import Decimal, ROUND_HALF_EVEN
from .store import now
from .data import MARKETS, market_of, symbol
from .backtest import bounded


def error(code,message=''): return {'ok':False,'code':code,'message':message or code}


class BrokerAdapter:
    name='Future broker'
    def getAccount(self): return error('UNSUPPORTED_BY_BROKER')
    def getPositions(self): return error('UNSUPPORTED_BY_BROKER')
    def getBalance(self): return error('UNSUPPORTED_BY_BROKER')
    def getQuote(self,*args): return error('UNSUPPORTED_BY_BROKER')
    def getHistoricalData(self,*args): return error('UNSUPPORTED_BY_BROKER')
    def getOrders(self): return error('UNSUPPORTED_BY_BROKER')
    def placeOrder(self,*args,**kwargs): return error('UNSUPPORTED_BY_BROKER')
    def cancelOrder(self,*args): return error('UNSUPPORTED_BY_BROKER')
    def getOrderStatus(self,*args): return error('UNSUPPORTED_BY_BROKER')


class MiraeAssetAdapter(BrokerAdapter):
    name='Mirae Asset'
    mode='ANALYSIS_AND_PAPER'
    message='Live trading through Mirae Asset is currently unavailable or not configured. Stock101 is operating in Paper Trading Mode.'
    # Paper execution is routed to an explicitly named PaperTradingAdapter, never a disguised Mirae fill.


class IBKRAdapter(BrokerAdapter):
    name='IBKR'
    def _get(self,path):
        base=os.environ.get('STOCK101_IBKR_GATEWAY','')
        # Only official local Client Portal Gateway; credentials remain in IBKR's login UI.
        if base not in ('https://localhost:5000/v1/api','https://127.0.0.1:5000/v1/api'): return error('BROKER_NOT_CONFIGURED','Configure a trusted-TLS local Client Portal Gateway and authenticate there.')
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(base+path,timeout=10) as r: return {'ok':True,'data':json.load(r)}
        except Exception as e: return error('BROKER_UNAVAILABLE',type(e).__name__+'; check gateway login, certificate and permissions.')
    def getAccount(self): return self._get('/portfolio/accounts')
    def getOrders(self): return self._get('/iserver/account/orders')
    def getPositions(self):
        account=os.environ.get('STOCK101_IBKR_ACCOUNT','')
        if not account.isalnum(): return error('BROKER_NOT_CONFIGURED')
        return self._get('/portfolio/'+account+'/positions/0')
    def getBalance(self):
        account=os.environ.get('STOCK101_IBKR_ACCOUNT','')
        if not account.isalnum(): return error('BROKER_NOT_CONFIGURED')
        return self._get('/portfolio/'+account+'/summary')
    def getQuote(self,conid):
        if not str(conid).isdigit(): return error('INVALID_CONTRACT_ID')
        return self._get('/iserver/marketdata/snapshot?conids='+str(conid)+'&fields=31,84,86')
    def getHistoricalData(self,conid):
        if not str(conid).isdigit(): return error('INVALID_CONTRACT_ID')
        return self._get('/iserver/marketdata/history?conid='+str(conid)+'&period=1y&bar=1d&outsideRth=false')
    def placeOrder(self,*args,**kwargs): return error('LIVE_TRADING_DISABLED','Live execution is not commissioned. No request was sent to IBKR.')
    def cancelOrder(self,*args): return error('LIVE_TRADING_DISABLED','Cancellation has not been commissioned; use IBKR directly.')


class QuestradeAdapter(BrokerAdapter):
    name='Questrade'
    def _get(self,path):
        import re
        base=os.environ.get('STOCK101_QUESTRADE_SERVER','');token=os.environ.get('STOCK101_QUESTRADE_TOKEN','')
        if not re.fullmatch(r'https://api\d+\.iq\.questrade\.com/',base) or not token: return error('BROKER_NOT_CONFIGURED','Use the official OAuth-returned server and short-lived access token in environment variables.')
        req=urllib.request.Request(base+'v1/'+path,headers={'Authorization':'Bearer '+token})
        try:
            with urllib.request.urlopen(req,timeout=10) as r: return {'ok':True,'data':json.load(r)}
        except Exception as e: return error('BROKER_UNAVAILABLE',type(e).__name__)
    def getAccount(self): return self._get('accounts')
    def _account(self,part):
        account=os.environ.get('STOCK101_QUESTRADE_ACCOUNT','')
        return self._get('accounts/'+account+'/'+part) if account.isdigit() else error('BROKER_NOT_CONFIGURED')
    def getPositions(self): return self._account('positions')
    def getBalance(self): return self._account('balances')
    def getOrders(self): return self._account('orders')
    def getQuote(self,symbol_id): return self._get('markets/quotes/'+str(symbol_id)) if str(symbol_id).isdigit() else error('INVALID_SYMBOL_ID')
    def placeOrder(self,*args,**kwargs): return error('BROKER_PARTNER_PERMISSION_REQUIRED')
    def cancelOrder(self,*args): return error('BROKER_PARTNER_PERMISSION_REQUIRED')


class FutureBrokerAdapter(BrokerAdapter): pass


class PaperTradingAdapter(BrokerAdapter):
    name='Stock101 Paper'
    def __init__(self,store,market):
        if market not in MARKETS: raise ValueError('Unknown market.')
        self.store=store;self.market=market
    def getAccount(self):
        with self.store.db() as c: account=dict(c.execute('SELECT * FROM accounts WHERE market=?',(self.market,)).fetchone())
        return {'ok':True,'mode':'PAPER','account':account,'positions':self.getPositions()['positions'],'orders':self.getOrders()['orders']}
    def getBalance(self): return {'ok':True,'cash':self.getAccount()['account']['cash']}
    def getPositions(self):
        with self.store.db() as c: positions=[dict(r) for r in c.execute('SELECT * FROM positions WHERE market=? AND quantity>0',(self.market,))]
        return {'ok':True,'positions':positions}
    def getOrders(self):
        with self.store.db() as c: rows=c.execute('SELECT payload FROM orders WHERE market=? ORDER BY created DESC LIMIT 300',(self.market,)).fetchall()
        return {'ok':True,'orders':[json.loads(r[0]) for r in rows]}
    def getOrderStatus(self,order_id):
        with self.store.db() as c: r=c.execute('SELECT payload FROM orders WHERE id=? AND market=?',(order_id,self.market)).fetchone()
        return {'ok':True,'order':json.loads(r[0])} if r else error('ORDER_NOT_FOUND')
    def cancelOrder(self,order_id): return error('ORDER_NOT_CANCELLABLE','Paper orders fill immediately against the reviewed reference price; no pending order exists.')
    def placeOrder(self,order,reference):
        if self.store.setting('stage',0)<2: return error('PAPER_STAGE_REQUIRED')
        if order.get('automated') and not order.get('automation_id'): return error('AUTOMATION_DISABLED')
        ticker=symbol(order['symbol']);side=order['side'];key=order['id']
        if market_of(ticker)!=self.market or side not in ('BUY','SELL'): raise ValueError('Invalid market or side.')
        uuid.UUID(key)
        qty=bounded(order['quantity'],1,100000000,'Quantity')
        if qty!=int(qty): raise ValueError('Use whole shares.')
        qty=int(qty);bounded(reference['price'],.000001,1e12,'Reference price');price=Decimal(str(reference['price']))
        bounded(order.get('fee',1),0,10000,'Fee');bounded(order.get('slippage_bps',5),0,1000,'Slippage')
        fee=Decimal(str(order.get('fee',1)));slip=Decimal(str(order.get('slippage_bps',5)))/Decimal('10000')
        unit=Decimal('1') if self.market=='KR' else Decimal('0.01')
        money=lambda v:v.quantize(unit,rounding=ROUND_HALF_EVEN)
        fee=money(fee)
        fill=price*(1+slip if side=='BUY' else 1-slip)
        with self.store.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT payload FROM orders WHERE id=?',(key,)).fetchone()
            if old: return {'ok':True,'order':json.loads(old[0]),'duplicate':True}
            account=c.execute('SELECT * FROM accounts WHERE market=?',(self.market,)).fetchone()
            pos=c.execute('SELECT * FROM positions WHERE market=? AND symbol=?',(self.market,ticker)).fetchone()
            held=pos['quantity'] if pos else 0;cost=Decimal(pos['cost']) if pos else Decimal('0')
            available=Decimal(account['cash'])
            if order.get('automated'):
                latch=c.execute("SELECT value FROM settings WHERE key='stopped'").fetchone()
                cfgrow=c.execute("SELECT payload FROM documents WHERE id=? AND kind='automation'",(order['automation_id'],)).fetchone()
                cfg=json.loads(cfgrow[0]) if cfgrow else {}
                if latch and json.loads(latch[0]):return error('KILL_SWITCH_ACTIVE')
                if not cfg.get('enabled') or cfg.get('symbol')!=ticker:return error('AUTOMATION_DISABLED')
                limitsrow=c.execute("SELECT value FROM settings WHERE key='limits'").fetchone()
                from .risk import DEFAULTS
                limits=json.loads(limitsrow[0]) if limitsrow else DEFAULTS
                initial=Decimal(account['initial']);notional=money(qty*fill)
                # Conservative paper exposure gate based on committed cost and initial virtual capital.
                if side=='BUY' and notional>initial*Decimal(str(limits['max_trade'])):return error('PAPER_TRADE_LIMIT')
                if side=='BUY' and cost+notional+fee>initial*Decimal(str(limits['max_position'])):return error('PAPER_POSITION_LIMIT')
                committed=sum((Decimal(r[0]) for r in c.execute('SELECT cost FROM positions WHERE market=?',(self.market,))),Decimal('0'))
                if side=='BUY' and committed+notional+fee>initial*Decimal(str(limits['max_exposure'])):return error('PAPER_EXPOSURE_LIMIT')
            if side=='BUY':
                required=money(qty*fill)+fee
                if required>available: return error('INSUFFICIENT_PAPER_CASH')
                cash=available-required;newqty=held+qty;newcost=cost+required;pnl=None
            else:
                if qty>held: return error('INSUFFICIENT_PAPER_SHARES')
                proceeds=money(qty*fill)-fee;cash=available+proceeds
                if cash<0:return error('INSUFFICIENT_PAPER_CASH_FOR_FEES')
                newqty=held-qty;removed=cost if newqty==0 else money(cost*Decimal(qty)/Decimal(held));newcost=cost-removed;pnl=proceeds-removed
            result={'id':key,'symbol':ticker,'market':self.market,'side':side,'quantity':qty,'price':str(fill),'fee':str(fee),'pnl':str(pnl) if pnl is not None else None,'created':now(),'status':'PAPER_FILLED','reference':reference,'broker':self.name}
            c.execute('UPDATE accounts SET cash=? WHERE market=?',(str(cash),self.market))
            c.execute('INSERT OR REPLACE INTO positions VALUES(?,?,?,?)',(self.market,ticker,newqty,str(newcost)))
            c.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(key,self.market,ticker,json.dumps(result),now()))
            c.execute('INSERT INTO audit(kind,payload,created) VALUES(?,?,?)',('paper_fill',json.dumps(result),now()))
        return {'ok':True,'order':result}
