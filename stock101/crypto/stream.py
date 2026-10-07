"""Public heartbeat/ticker observer; disconnect latches halt, never resumes trading."""
import json
import threading
import time
from .exchange import StreamHealth
class Monitor:
    def __init__(self,adapter,on_failure):
        self.adapter=adapter;self.on_failure=on_failure;self.health=StreamHealth();self.product=None;self.thread=None;self.stop=threading.Event();self.latest=None
    def start(self,product):
        if self.thread and self.thread.is_alive():
            if self.product==product:return
            raise ValueError('MONITOR_ALREADY_RUNNING_FOR_OTHER_PRODUCT')
        self.product=product;self.stop.clear();self.health=StreamHealth()
        def run():
            try:
                from websockets.sync.client import connect
                with connect(self.adapter.websocket_market,open_timeout=8,close_timeout=2,max_size=2_000_000) as ws:
                    for channel in ('heartbeats','ticker'):ws.send(json.dumps(self.adapter.subscription([product],channel)))
                    while not self.stop.is_set():
                        msg=json.loads(ws.recv(timeout=10))
                        if not self.health.observe(msg):raise ValueError(self.health.reason)
                        if msg.get('channel')=='ticker':self.latest={'received_at':time.time(),'channel':'ticker'}
            except Exception:
                self.health.disconnect();self.on_failure('WS_DISCONNECTED_RECONCILIATION_REQUIRED')
        self.thread=threading.Thread(target=run,name='stock101-coinbase-market-ws',daemon=True);self.thread.start()
    def status(self):return {'connected':self.health.connected,'product':self.product,'last_message_at':self.health.last,'reason':self.health.reason}
