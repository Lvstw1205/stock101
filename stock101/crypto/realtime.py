"""Public multi-asset ticker stream. Ticks schedule REST checks, never fills."""
import datetime as dt
import json
import threading
import time
from .exchange import number, product_id

class MarketStream:
    def __init__(self):
        self.lock=threading.RLock(); self.products=set(); self.quotes={}; self.thread=None
        self.changed=threading.Event(); self.connected=False; self.last=0; self.reason='NOT_STARTED'; self.messages=0
    def set_products(self, products):
        products={product_id(p) for p in products}
        with self.lock:
            if products != self.products:
                self.products=products; self.changed.set()
        if not self.thread and products:
            self.thread=threading.Thread(target=self.run,name='stock101-all-market-stream',daemon=True); self.thread.start()
    def observe(self, msg, now=None):
        now=time.time() if now is None else now
        if msg.get('type')=='error':raise ValueError('STREAM_SUBSCRIPTION_REJECTED')
        if msg.get('channel') not in ('ticker_batch','heartbeats'):return
        stamp=dt.datetime.fromisoformat(msg['timestamp'].replace('Z','+00:00')).timestamp()
        if not -5<=now-stamp<=15:raise ValueError('STREAM_TIMESTAMP_STALE')
        with self.lock:
            self.last=now; self.connected=True; self.reason=None; self.messages+=1
            for event in msg.get('events',[]):
                for row in event.get('tickers',[]):
                    pid=row.get('product_id')
                    if pid not in self.products:continue
                    price=number(row.get('price'))
                    if price<=0:continue
                    prior=self.quotes.get(pid)
                    if prior and stamp<=prior['timestamp']:continue
                    self.quotes[pid]={'price':str(price),'timestamp':stamp,'received_at':now,
                                      'previous':prior['price'] if prior else None}
    def run(self):
        from websockets.sync.client import connect
        retry=2
        while True:
            try:
                with self.lock:products=sorted(self.products); self.quotes={}; self.changed.clear()
                if not products:return
                with connect('wss://advanced-trade-ws.coinbase.com',open_timeout=8,close_timeout=2,max_size=4_000_000) as ws:
                    ws.send(json.dumps({'type':'subscribe','channel':'heartbeats'}))
                    ws.send(json.dumps({'type':'subscribe','channel':'ticker_batch','product_ids':products}))
                    while not self.changed.is_set():
                        self.observe(json.loads(ws.recv(timeout=10))); retry=2
            except Exception:
                with self.lock:self.connected=False; self.reason='RECONNECTING_REST_CHECKS_CONTINUE'
                self.changed.wait(retry); retry=min(60,retry*2)
    def snapshot(self):
        with self.lock:return dict(self.quotes)
    def status(self):
        with self.lock:return {'connected':self.connected and time.time()-self.last<15,'subscribed':len(self.products),
                              'received_products':len(self.quotes),'last_message_at':self.last or None,'messages':self.messages,
                              'reason':self.reason,'channel':'ticker_batch','update_seconds':5,'execution_source':'fresh REST order book'}
