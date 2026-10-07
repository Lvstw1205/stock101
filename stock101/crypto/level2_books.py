"""Public, sequenced Level2 books for PAPER holdings; never submits an order.

An unchanged book stays current only while the same connection delivers a
contiguous sequence of Level2 messages and heartbeats. BTC is subscribed as an
active anchor. Heartbeats alone cannot refresh a book. Gaps, reconnects and
subscription changes discard every book and require new snapshots.
"""
import datetime as dt
import heapq
import json
import threading
import time
import uuid

from .exchange import ExchangeError, number, product_id


class Level2Books:
    URL = 'wss://advanced-trade-ws.coinbase.com'
    MAX_AGE = 5

    def __init__(self, clock=time.time, connector=None, autostart=True):
        self.clock = clock
        self.connector = connector
        self.autostart = autostart
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.changed = threading.Event()
        self.thread = None
        self.desired = frozenset()
        self.reconnects = 0
        self.last_error = None
        self.disconnect()

    def set_products(self, products):
        products = frozenset(product_id(p) for p in products)
        if products:
            products |= {'BTC-USD'}
        with self.lock:
            if products != self.desired:
                self.desired = products
                self.disconnect()
                self.changed.set()
            if self.autostart and products and self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True, name='stock101-level2')
                self.thread.start()

    def disconnect(self, reason=None):
        with self.lock:
            self.connected = False
            self.books = {}
            self.sequence = None
            self.watermark = None
            self.received = None
            self.heartbeat = None
            self.heartbeat_counter = None
            if reason:
                self.last_error = reason

    def begin(self, products):
        with self.lock:
            self.disconnect()
            self.subscribed = frozenset(products)
            self.connection_id = uuid.uuid4().hex
            self.connected = True

    def _fresh(self):
        now = self.clock()
        return (self.connected and self.watermark is not None and self.received is not None and self.heartbeat is not None
                and -2 <= now - self.watermark <= self.MAX_AGE
                and 0 <= now - self.received <= self.MAX_AGE
                and 0 <= now - self.heartbeat <= self.MAX_AGE)

    def is_current(self, book):
        with self.lock:
            return self._fresh() and book.get('connection_id') == self.connection_id and book['product_id'] in self.books

    def ingest(self, message):
        """Apply the entire message atomically, including both sides of a book."""
        with self.lock:
            try:
                if not self.connected:
                    raise ExchangeError('LEVEL2_DISCONNECTED')
                if message.get('type') == 'error':
                    raise ExchangeError('LEVEL2_SUBSCRIPTION_REJECTED')
                seq = message['sequence_num']
                if type(seq) is not int or seq != (0 if self.sequence is None else self.sequence + 1):
                    raise ExchangeError('LEVEL2_SEQUENCE_GAP')
                stamp = dt.datetime.fromisoformat(message['timestamp'].replace('Z', '+00:00')).timestamp()
                now = self.clock()
                if not -2 <= now - stamp <= self.MAX_AGE:
                    raise ExchangeError('LEVEL2_STALE_MESSAGE')
                channel = message['channel']
                if channel == 'heartbeats':
                    for event in message['events']:
                        counter = int(event['heartbeat_counter'])
                        if self.heartbeat_counter is not None and counter != self.heartbeat_counter + 1:
                            raise ExchangeError('LEVEL2_HEARTBEAT_GAP')
                        self.heartbeat_counter = counter
                    self.heartbeat = now
                elif channel == 'l2_data':
                    for event in message['events']:
                        pid = event['product_id']
                        if pid not in self.subscribed:
                            raise ExchangeError('LEVEL2_UNEXPECTED_PRODUCT')
                        if event['type'] == 'snapshot':
                            self.books[pid] = {'bids': {}, 'asks': {}}
                        elif event['type'] != 'update' or pid not in self.books:
                            raise ExchangeError('LEVEL2_SNAPSHOT_REQUIRED')
                        book = self.books[pid]
                        for update in event['updates']:
                            side = {'bid': 'bids', 'offer': 'asks'}.get(update['side'])
                            price, size = number(update['price_level']), number(update['new_quantity'])
                            if side is None or price <= 0 or size < 0:
                                raise ExchangeError('LEVEL2_INVALID_LEVEL')
                            if size:
                                book[side][price] = size
                            else:
                                book[side].pop(price, None)
                        book['updated_at'] = stamp
                    self.watermark = stamp
                    self.received = now
                elif channel != 'subscriptions':
                    raise ExchangeError('LEVEL2_UNEXPECTED_MESSAGE')
                self.sequence = seq
            except Exception as error:
                self.disconnect(str(error) if isinstance(error, ExchangeError) else 'LEVEL2_INVALID_MESSAGE')
                raise ExchangeError(self.last_error) from None

    def get_many(self, products):
        """Return only verified books; callers may fetch missing products by REST."""
        with self.lock:
            if not self._fresh():
                return {}
            result = {}
            for pid in products:
                book = self.books.get(pid)
                if not book or not book['bids'] or not book['asks']:
                    continue
                bids = heapq.nlargest(20, book['bids'])
                asks = heapq.nsmallest(20, book['asks'])
                if bids[0] >= asks[0]:
                    continue
                mid = (bids[0] + asks[0]) / 2
                result[pid] = {
                    'product_id': pid, 'timestamp': self.watermark, 'received_at': self.received,
                    'book_updated_at': book['updated_at'], 'sequence_num': self.sequence, 'connection_id': self.connection_id,
                    'source': 'COINBASE_LEVEL2', 'mid': str(mid),
                    'spread_bps': str((asks[0] - bids[0]) / mid * 10000),
                    'bids': [{'price': str(p), 'size': str(book['bids'][p])} for p in bids],
                    'asks': [{'price': str(p), 'size': str(book['asks'][p])} for p in asks],
                }
            return result

    def status(self):
        with self.lock:
            products = list(self.desired)
            return {'connected': self.connected, 'subscribed': len(products),
                    'ready': len(self.get_many(products)), 'sequence_num': self.sequence,
                    'last_level2_at': self.watermark, 'last_heartbeat_at': self.heartbeat,
                    'reconnects': self.reconnects, 'last_error': self.last_error}

    def _run(self):
        connector = self.connector
        if connector is None:
            from websockets.sync.client import connect
            connector = connect
        while not self.stop.is_set():
            with self.lock:
                products = self.desired
                self.changed.clear()
            if not products:
                self.changed.wait(1)
                continue
            try:
                with connector(self.URL, open_timeout=8, close_timeout=1, max_size=32_000_000) as ws:
                    with self.lock:
                        if products != self.desired:
                            continue
                        self.begin(products)
                    ws.send(json.dumps({'type': 'subscribe', 'channel': 'heartbeats'}))
                    ws.send(json.dumps({'type': 'subscribe', 'channel': 'level2', 'product_ids': sorted(products)}))
                    started = self.clock()
                    while not self.stop.is_set() and not self.changed.is_set():
                        try:
                            message = ws.recv(timeout=1)
                        except TimeoutError:
                            if self.clock() - (self.received or started) > self.MAX_AGE:
                                raise ExchangeError('LEVEL2_STREAM_STALE')
                            continue
                        with self.lock:
                            if self.changed.is_set():
                                break
                            self.ingest(json.loads(message))
                        if self.clock() - (self.received or started) > self.MAX_AGE:
                            raise ExchangeError('LEVEL2_STREAM_STALE')
            except Exception as error:
                self.disconnect(str(error) if isinstance(error, ExchangeError) else 'LEVEL2_CONNECTION_LOST')
            finally:
                self.disconnect()
                self.reconnects += 1
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        self.changed.set()
        if self.thread:
            self.thread.join(timeout=10)
        self.disconnect()


class HeldLevel2Books:
    """Small stable subscriptions respecting the exchange's per-session limit.

    Each connection holds at most eight assets plus BTC. Changing one holding
    resnapshots only its group, not every coin in the portfolio.
    """
    def __init__(self, factory=Level2Books):
        self.factory = factory
        self.lock = threading.RLock()
        self.groups = []
        self.desired = frozenset()

    def set_products(self, products):
        wanted = frozenset(product_id(p) for p in products)
        with self.lock:
            if wanted == self.desired:
                return
            self.desired = wanted
            assigned = set()
            for group in self.groups:
                group['products'] &= wanted
                assigned |= group['products']
            for pid in sorted(wanted - assigned):
                group = next((g for g in self.groups if len(g['products']) < 8), None)
                if group is None:
                    group = {'products': set(), 'stream': self.factory()}
                    self.groups.append(group)
                group['products'].add(pid)
            for group in self.groups:
                group['stream'].set_products(group['products'])

    def get_many(self, products):
        with self.lock:
            result = {}
            for group in self.groups:
                wanted = set(products) & group['products']
                if wanted:
                    result.update(group['stream'].get_many(wanted))
            return result

    def is_current(self, book):
        with self.lock:
            return any(book['product_id'] in g['products'] and g['stream'].is_current(book) for g in self.groups)

    def status(self):
        with self.lock:
            states = [g['stream'].status() for g in self.groups if g['products']]
            stamps = [s['last_level2_at'] for s in states if s['last_level2_at'] is not None]
            return {'connected': bool(states) and all(s['connected'] for s in states),
                    'subscribed': len(self.desired), 'ready': len(self.get_many(self.desired)),
                    'connections': len(states), 'last_level2_at': min(stamps) if len(stamps)==len(states) and stamps else None,
                    'reconnects': sum(s['reconnects'] for s in states),
                    'last_error': next((s['last_error'] for s in states if not s['connected'] and s['last_error']), None)}

    def close(self):
        for group in self.groups:
            group['stream'].close()
