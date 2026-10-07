"""Full-universe PAPER screening, independent of the serialized order ledger.

Ticker-derived levels are indicative screening inputs, never executable books.
Every queued candidate is recomputed against a fresh exchange book by Engine.
"""
import copy
import threading
import time
from . import execution_strategies
from .exchange import number

INTERVAL = 10
MAX_SCREEN_AGE = 300
MAX_FRAME_AGE = 30


def screening_book(pid, tick, rate, now):
    age = now - float(tick['timestamp'])
    price = number(tick['price']) * number(rate)
    if not -5 <= age <= MAX_SCREEN_AGE or price <= 0:
        raise ValueError('WAITING_FRESH_PRICE')
    # Conservative assumed spread plus the existing PAPER FX markup. Only
    # real L2/REST depth may determine fill prices or accounting values.
    half_spread = number('.0025')
    fx = number('.0025') if pid.endswith('-USD') else number(0)
    return dict(product_id=pid, mid=str(price), timestamp=float(tick['timestamp']),
                source='INDICATIVE_TICKER_SCREEN', executable=False,
                spread_bps='50', fx_rate=str(rate), fx_markup_bps=str(fx * 10000),
                bids=[dict(price=str(price * (1-half_spread) * (1-fx)), size='0')],
                asks=[dict(price=str(price * (1+half_spread) * (1+fx)), size='0')])


class RapidScanner:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.thread = None
        self.stopping = threading.Event()
        self.rows = {}
        self.summary = dict(status='WARMING', interval_seconds=INTERVAL, passes=0,
                            evaluated=0, universe=0, candidates=0, exits=0)
        self.news_cache = {}
        self.evidence_cache = {}
        self.reviewer = None

    def ensure_thread(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self.run, name='stock101-all-coins', daemon=True)
            self.thread.start()
            from .rapid_review import RapidReviewer
            self.reviewer = RapidReviewer(self)
            self.reviewer.ensure_thread()

    def run(self):
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                if not self.engine.execution_stopped():
                    self.scan_once()
            except Exception as error:
                with self.lock:
                    self.summary.update(status='RETRYING', error=type(error).__name__, error_at=time.time())
            self.stopping.wait(max(1, INTERVAL - (time.monotonic()-started)))

    def scan_once(self, now=None):
        now = time.time() if now is None else now
        started = time.monotonic()
        engine = self.engine
        intel = engine.intelligence
        with intel.lock:
            products = dict(intel.products)
            histories = dict(intel.history)
            daily = dict(intel.daily)
        ticks = intel.stream.snapshot()
        if not products:
            return
        with engine.repo.db() as c:
            c.execute('BEGIN')
            positions = {p['product']: p for p in engine.positions(c) if number(p['quantity']) > 0}
            fills = engine.repo.rows(c, 'fills', 10000)
            anchor = engine.repo.get(c, 'cad_plan')['anchor']
        # One FX and benchmark read per pass, not one request per coin.
        rate = float(engine.adapter.fx()['rate']) if any(p.endswith('-USD') for p in products) else 1.
        benchmark = intel.bitcoin_context(now)
        fx_context = intel.fx_context(now)
        previous = self.snapshot()
        rows = {}
        counts = {}
        for pid, product in products.items():
            row = dict(product=pid, at=now, price_at=None, decision='WAIT', status='WAITING',
                       reason='HISTORY_REFRESH_PENDING', plan=None, strategies=[], screening_only=True)
            try:
                h = histories.get(pid)
                if not h or not h.get('bars') or h['bars'][-1]['start'] != int(now)//3600*3600-3600:
                    raise ValueError('HISTORY_REFRESH_PENDING')
                tick = ticks.get(pid)
                if not tick:
                    raise ValueError('WAITING_FRESH_PRICE')
                row['price_at'] = tick['timestamp']
                book = screening_book(pid, tick, rate if pid.endswith('-USD') else 1, now)
                current_rate = rate if pid.endswith('-USD') else 1
                def rescale(history):
                    if not history:
                        return None
                    factor = current_rate / float(history.get('fx_rate', current_rate))
                    return [{**b, **{k: b[k]*factor for k in ('open', 'high', 'low', 'close')}} for b in history['bars']]
                cached_news = self.news_cache.get(pid)
                if not cached_news or now-cached_news[0] >= 60:
                    self.news_cache[pid] = (now, intel.news.context(product))
                cached_evidence = self.evidence_cache.get(pid)
                if not cached_evidence or now-cached_evidence[0] >= 60:
                    self.evidence_cache[pid] = (now, engine.validation_evidence(pid))
                signal = execution_strategies.evaluate(
                    rescale(h), positions.get(pid), book, daily=rescale(daily.get(pid)),
                    news=self.news_cache[pid][1], fills=fills, anchor=anchor,
                    previous_price=(previous.get(pid, {}).get('plan') or {}).get('current'),
                    benchmark=benchmark, fx_context=fx_context, evidence=self.evidence_cache[pid][1])
                row.update(status='SCREENED', decision=signal['decision'], reason=', '.join(signal['reason_codes']),
                           plan=signal['plan'], strategies=signal['strategy_candidates'],
                           held=pid in positions, quote_age_seconds=round(now-book['timestamp'], 1),
                           score=(signal['plan'].get('sizing') or {}).get('score', 0))
                row['plan']['screening_only'] = True
            except ValueError as error:
                code = str(error)
                row['reason'] = code if code in ('HISTORY_REFRESH_PENDING', 'WAITING_FRESH_PRICE') else 'SCREEN_DATA_INVALID'
            except Exception:
                row['reason'] = 'SCREEN_ANALYSIS_RETRY'
            rows[pid] = row
            counts[row['reason'] if row['status'] != 'SCREENED' else 'SCREENED'] = counts.get(row['reason'] if row['status'] != 'SCREENED' else 'SCREENED', 0) + 1
        with self.lock:
            self.rows = rows
            self.summary = dict(status='RUNNING', universe=len(products), evaluated=counts.get('SCREENED', 0),
                                fresh_prices=sum(r.get('quote_age_seconds', float('inf')) <= 15 for r in rows.values()),
                                candidates=sum(r['decision'] == 'BUY' for r in rows.values()),
                                exits=sum(r['decision'] in ('SELL', 'REDUCE') for r in rows.values()),
                                skipped={k: v for k, v in counts.items() if k != 'SCREENED'},
                                started_at=now, completed_at=time.time(), duration_seconds=round(time.monotonic()-started, 3),
                                interval_seconds=INTERVAL, passes=self.summary['passes']+1,
                                source='cached_completed_charts_and_coinbase_ticker', executable=False)

    def snapshot(self):
        with self.lock:
            return dict(self.rows)

    def state(self):
        with self.lock:
            state = dict(self.summary)
        if self.engine.execution_stopped():
            state['status'] = 'STOPPED'
        elif state.get('completed_at') and time.time()-state['completed_at'] > MAX_FRAME_AGE:
            state['status'] = 'DELAYED'
        state['ai'] = self.reviewer.state() if self.reviewer else {'status': 'WARMING'}
        return state

    def ranked(self, now=None):
        now = time.time() if now is None else now
        rows = self.snapshot()
        priorities = self.reviewer.priorities(now) if self.reviewer else {}
        usable = [r for r in rows.values() if r['status'] == 'SCREENED' and 0 <= now-r['at'] <= MAX_FRAME_AGE]
        # Stops/exit candidates always precede entries. AI only ranks candidates
        # that still satisfy the current deterministic strategy calculations.
        return sorted(usable, key=lambda r: (0 if r['decision'] in ('SELL', 'REDUCE') else 1 if r['decision'] == 'BUY' else 2,
                                             priorities.get(r['product'], 9999), -float(r.get('score', 0)), r['product']))
