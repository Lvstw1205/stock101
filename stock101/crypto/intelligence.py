"""Independent history/news workers keep scanning from blocking execution checks."""
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from . import planner,fx_reserve
from .news import NewsResearch
from .realtime import MarketStream

class Intelligence:
    def __init__(self, adapter, repo):
        self.adapter=adapter; self.repo=repo; self.lock=threading.RLock(); self.products={}; self.history={}; self.errors={}; self.attempted={}; self.thread=None
        self.stream=MarketStream(); self.news=NewsResearch(repo)
        self.fx_history=None;self.fx_error=None;self.fx_thread=None;self.usdc_quote=None;self.fx_attempted=0
        self.daily={};self.daily_errors={};self.daily_attempted={};self.daily_thread=None
        with repo.db() as c:
            self.fx_history=repo.get(c,fx_reserve.KEY)
            for row in repo.rows(c,'daily_history',10000):self.daily[row['product']]=row
            for row in repo.rows(c,'market_history',10000):
                if row.get('forecast',{}).get('version')==planner.VERSION:self.history[row['product']]=row
    def set_products(self, products):
        with self.lock:self.products={p['product_id']:p for p in products}
        self.stream.set_products(self.products)
        self.news.set_products(products)
        if not self.fx_thread:
            self.fx_thread=threading.Thread(target=self.run_fx,name='stock101-fx-history',daemon=True);self.fx_thread.start()
        if not self.thread:
            self.thread=threading.Thread(target=self.run,name='stock101-history-research',daemon=True); self.thread.start()
        if not self.daily_thread:
            self.daily_thread=threading.Thread(target=self.run_daily,name='stock101-daily-history',daemon=True);self.daily_thread.start()
    def fx_context(self,now=None):
        with self.lock:
            context=fx_reserve.context(self.fx_history,time.time() if now is None else now)
            return {**context,'usdc_quote':self.usdc_quote} if context else None
    def run_fx(self):
        while True:
            try:
                if (not self.fx_history or not fx_reserve.context(self.fx_history,time.time()) or time.time()-self.fx_history.get('updated_at',0)>21600) and time.time()-self.fx_attempted>300:
                    self.fx_attempted=time.time()
                    data=fx_reserve.load()
                    with self.repo.db() as c:self.repo.set(c,fx_reserve.KEY,data)
                    with self.lock:self.fx_history=data;self.fx_error=None
            except Exception:self.fx_error='FX_HISTORY_UNAVAILABLE'
            try:
                quote=fx_reserve.load_peg()
                with self.lock:self.usdc_quote=quote
            except Exception:pass  # The previous quote expires; no assumed USD parity.
            threading.Event().wait(60)
    def get_daily(self,pid,allow_stale=False):
        with self.lock:row=self.daily.get(pid)
        if not row or not row.get('bars'):return None
        if not allow_stale and row['bars'][-1]['start']!=int(time.time())//86400*86400-86400:return None
        rate=float(self.adapter.fx()['rate']) if pid.endswith('-USD') and getattr(self.adapter,'fx_loader',None) else 1.
        old=row.get('fx_rate',rate)
        return [{**b,**{k:b[k]*rate/old for k in ('open','high','low','close')}} for b in row['bars']]
    def run_daily(self):
        while True:
            with self.lock:
                due=[p for p in self.products if (p not in self.daily or self.daily[p]['bars'][-1]['start']!=int(time.time())//86400*86400-86400) and time.time()-self.daily_attempted.get(p,0)>900]
                due.sort(key=lambda p:(not p.startswith('BTC-'),self.daily_attempted.get(p,0)))
            if not due:threading.Event().wait(2);continue
            pid=due[0];self.daily_attempted[pid]=time.time()
            try:
                bars=self.adapter.get_daily_candles(pid)
                if not bars or bars[-1]['start']!=int(time.time())//86400*86400-86400:raise ValueError('STALE_DAILY_CANDLES')
                if any(b['start']-a['start']!=86400 for a,b in zip(bars,bars[1:])):raise ValueError('DAILY_CANDLE_GAP')
                rate=float(self.adapter.fx()['rate']) if pid.endswith('-USD') and getattr(self.adapter,'fx_loader',None) else 1.
                row={'product':pid,'bars':bars,'fx_rate':rate,'updated_at':time.time()}
                with self.repo.db() as c:self.repo.put(c,'daily_history',pid,row,replace=True)
                with self.lock:self.daily[pid]=row;self.daily_errors.pop(pid,None)
            except Exception as e:
                code=str(e)
                with self.lock:self.daily_errors[pid]={'reason':code if len(code)<80 and code.replace('_','').isalnum() else 'DAILY_HISTORY_UNAVAILABLE','at':time.time()}
            threading.Event().wait(.35)
    def get(self, pid, allow_stale=False):
        with self.lock:row=self.history.get(pid)
        # Stale historical plans cannot authorize entries. UI still shows diagnostics.
        return row if row and (allow_stale or row['bars'][-1]['start']==int(time.time())//3600*3600-3600) else None
    def bitcoin_context(self,now=None):
        """Read one fresh BTC benchmark in its original quote currency.

        PAPER histories are CAD-valued, while WebSocket prices are exchange
        quotes. Undo each history's own FX rate before comparing percentages.
        No network or order operations run on this path.
        """
        now=time.time() if now is None else now;quotes=self.stream.snapshot()
        with self.lock:
            pid=next((p for p in ('BTC-USD','BTC-CAD','BTC-USDC') if p in self.products),None)
            tick=quotes.get(pid);daily=self.daily.get(pid);hour=self.history.get(pid)
            if not tick or not daily or not hour or not -5<=now-tick['timestamp']<=15:return None
            def unscale(row):
                rate=float(row.get('fx_rate',1))
                return [{**b,**{k:b[k]/rate for k in ('open','high','low','close')}} for b in row['bars']]
            days=unscale(daily);hours=unscale(hour);day=int(now)//86400*86400
            opening=next((b['open'] for b in hours if b['start']==day and b['start']+3600<=now),None)
            return dict(product_id=pid,price=tick['price'],timestamp=tick['timestamp'],daily=days,session_open=opening)
    def refresh_hourly(self,pid,hour=None):
        hour=int(time.time())//3600*3600 if hour is None else hour
        try:
            with self.lock:old_row=self.history.get(pid,{})
            old=old_row.get('bars',[])
            # Refresh only the changed tail. Preserve actual stored bars, never
            # fabricate missing intervals or download 350 unchanged bars hourly.
            start=max(hour-350*3600,old[-1]['start']-2*3600) if old else hour-350*3600
            bars=self.adapter.get_candles(pid,start=start,end=hour,contiguous_tail=True)
            if not bars or bars[-1]['start']!=hour-3600:raise ValueError('STALE_CANDLES')
            fx_rate=float(self.adapter.fx()['rate']) if pid.endswith('-USD') and getattr(self.adapter,'fx_loader',None) else 1.
            old_rate=old_row.get('fx_rate',fx_rate)
            if old_rate!=fx_rate:
                old=[{**b,**{k:b[k]*fx_rate/old_rate for k in ('open','high','low','close')}} for b in old]
            combined={b['start']:b for b in old};combined.update({b['start']:b for b in bars})
            merged=sorted(combined.values(),key=lambda b:b['start'])[-2160:]
            if any(b['start']-a['start']!=3600 for a,b in zip(merged,merged[1:])):merged=bars
            model=planner.forecast(merged) if len(merged)>=90 else {'version':planner.VERSION,'status':'HISTORY_LESS_THAN_90_HOURS','history_hours':len(merged)}
            row={'product':pid,'bars':merged,'forecast':model,'updated_at':time.time(),'fx_rate':fx_rate}
            with self.repo.db() as c:self.repo.put(c,'market_history',pid,row,replace=True)
            with self.lock:self.history[pid]=row;self.errors.pop(pid,None)
        except Exception as e:
            code=str(e)
            with self.lock:self.errors[pid]={'reason':code if len(code)<80 and code.replace('_','').isalnum() else 'HISTORY_FETCH_FAILED','at':time.time()}
    def refresh_history_batch(self,pool):
        now=time.time();hour=int(now)//3600*3600
        with self.lock:
            due=[p for p in self.products if not self.get(p) and now-self.attempted.get(p,0)>120]
            due.sort(key=lambda p:self.attempted.get(p,0))
            due=due[:4]
            for pid in due:self.attempted[pid]=now
        futures=[pool.submit(self.refresh_hourly,pid,hour) for pid in due]
        for future in futures:future.result()
        return len(due)
    def run(self):
        # Bounded parallel reads share CoinbaseAdapter's rate-limit/backoff gate.
        # Slow history requests no longer serialize all market refreshes.
        with ThreadPoolExecutor(max_workers=4,thread_name_prefix='stock101-history-fetch') as pool:
            while True:
                count=self.refresh_history_batch(pool)
                threading.Event().wait(.15 if count else 2)
    def state(self):
        with self.lock:
            ready=sum(self.get(p) is not None for p in self.products)
            result={'universe':len(self.products),'history_ready':ready,'history_pending':len(self.products)-ready,
                    'history_errors':dict(self.errors),'history_initial_hours':350,'history_retention_hours':2160,
                    'daily_ready':sum(p in self.daily and self.daily[p]['bars'][-1]['start']==int(time.time())//86400*86400-86400 for p in self.products),
                    'daily_ma200_ready':sum(p in self.daily and len(self.daily[p]['bars'])>=201 and self.daily[p]['bars'][-1]['start']==int(time.time())//86400*86400-86400 for p in self.products),'daily_errors':dict(self.daily_errors)}
        return {**result,'fx_reference':self.fx_context(),'fx_error':self.fx_error,'stream':self.stream.status(),'news':self.news.state()}
