"""Share fresh PAPER REST books between valuation and execution readers.

Exchange timestamps are never advanced. Expired or incomplete snapshots cannot
authorize an order. A bounded pool coalesces concurrent reads of the same asset.
"""
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from .exchange import ExchangeError


def unavailable(code,products):
    error=ExchangeError(code);error.products=list(products);return error


class PaperBooks:
    def __init__(self, loader, clock=time.time, timeout=10, workers=8):
        self.loader=loader;self.clock=clock;self.timeout=timeout
        self.lock=threading.RLock();self.cache={};self.pending={};self.errors={}
        self.pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='stock101-paper-book')

    def _fresh(self, row):
        return row and -5<=self.clock()-row[0]['timestamp']<=12 and 0<=self.clock()-row[1]<=10

    def _completed(self,pid,future):
        with self.lock:
            if self.pending.get(pid) is future:self.pending.pop(pid,None)
            try:
                book=future.result()
                if not -5<=self.clock()-book['timestamp']<=12:raise ExchangeError('STALE_MARKET_DATA')
                self.cache[pid]=(copy.deepcopy(book),self.clock());self.errors.pop(pid,None)
            except ExchangeError as e:self.errors[pid]=(str(e),self.clock()+2)
            except Exception:pass  # The caller receives the original programming error.

    def get_many(self,products):
        products=list(dict.fromkeys(products));futures={}
        with self.lock:
            for pid in products:
                if self._fresh(self.cache.get(pid)):continue
                error=self.errors.get(pid)
                if error and self.clock()<error[1]:raise unavailable(error[0],[pid])
                future=self.pending.get(pid)
                if future is None:
                    future=self.pool.submit(self.loader,pid);self.pending[pid]=future
                    future.add_done_callback(lambda f,p=pid:self._completed(p,f))
                futures[pid]=future
        if futures:
            _,incomplete=wait(futures.values(),timeout=self.timeout)
            if incomplete:raise unavailable('MARKET_DATA_REFRESH_PENDING',[p for p,f in futures.items() if f in incomplete])
            for pid,future in futures.items():
                # Also perform completion here: Future.wait can return before its
                # callbacks finish. Re-reading a result never repeats the request.
                try:future.result()
                except ExchangeError as e:raise unavailable(str(e),[pid]) from None
                self._completed(pid,future)
                with self.lock:
                    error=self.errors.get(pid)
                    if error:raise unavailable(error[0],[pid])
        with self.lock:
            expired=[pid for pid in products if not self.cache.get(pid) or not -5<=self.clock()-self.cache[pid][0]['timestamp']<=12]
            if expired:raise unavailable('MARKET_DATA_REFRESH_PENDING',expired)
            return {pid:copy.deepcopy(self.cache[pid][0]) for pid in products}

    def get(self,pid):return self.get_many([pid])[pid]

    def peek(self,pid):
        """Read an already fresh book without scheduling any network work."""
        with self.lock:
            row=self.cache.get(pid)
            return copy.deepcopy(row[0]) if self._fresh(row) else None

    def close(self):self.pool.shutdown(wait=True,cancel_futures=True)
