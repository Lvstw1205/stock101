"""Prospective PAPER scorecard and bounded, evidence-only allocation feedback.

No orders, network calls, generated Python, or changes to live/core risk limits.
Forecast probabilities are experimental estimates, not target-price confidence.
"""
import contextlib
import copy
import hashlib
import json
import math
import sqlite3
import statistics
import threading
import time
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime
from . import planner, risk, storage

VERSION = 'paper_learning_v1'
MODEL = 'fixed_trend24_residual_v1'
FORECAST_INTERVAL = 600
MAX_ENTRY = .06
MIN_TRIPS = 20
SOURCES = [
    dict(url='https://lazyowen.com/guides/grok-polymarket-trading-agent',
         applied='시장 순환 탐색, 청산 호가 확인, 제한된 Half-Kelly 배분, 진입 근거와 48시간 회고'),
    dict(url='https://lazyowen.com/guides/phil-ai-trader-paper-1000',
         applied='가상 원금 보존, 사전 예측 성적표, 100회 완결 거래 관찰, 매매 회고와 변경 이력'),
    dict(url='https://github.com/bennyjo/phil',
         applied='예측 정확도와 손익 분리, 핵심 한도 보호, 검증할 전략 후보를 별도로 기록'),
]


def packed(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(packed(value).encode()).hexdigest()


def number(value):
    out = Decimal(str(value))
    if not out.is_finite():
        raise ValueError('LEARNING_NONFINITE_VALUE')
    return out


def round_trips(fills):
    """One flat-to-flat episode; partial exits are never separate Kelly trials."""
    opened, closed = {}, []
    for fill in sorted(fills, key=lambda r: (r['created'], r['id'])):
        pid = fill['product']; qty = number(fill['quantity'])
        if fill.get('currency') != 'CAD' or qty <= 0:
            raise ValueError('LEARNING_INVALID_FILL')
        if fill['side'] == 'BUY':
            if pid not in opened:
                opened[pid] = dict(id=fill['id'], product=pid, opened_at=fill['created'], quantity=Decimal(0),
                                  cost=Decimal(0), fees=Decimal(0), realized=Decimal(0), strategies=set(),
                                  versions=set(), reasons=set(), fill_count=0)
            row = opened[pid]
            row['quantity'] += qty
            row['cost'] += number(fill['notional']) + number(fill['fee'])
            row['strategies'].add(fill.get('strategy_id') or 'UNKNOWN')
            row['versions'].add(fill.get('execution_version') or 'LEGACY_UNRECORDED')
        elif fill['side'] == 'SELL':
            row = opened.get(pid)
            if row is None or qty > row['quantity']:
                raise ValueError('LEARNING_UNMATCHED_SALE')
            row['quantity'] -= qty
            row['realized'] += number(fill['realized'])
            row['reasons'].update(fill.get('reason_codes') or [])
        else:
            raise ValueError('LEARNING_INVALID_SIDE')
        row['fees'] += number(fill['fee']); row['fill_count'] += 1
        if row['quantity'] == 0:
            if row['cost'] <= 0:
                raise ValueError('LEARNING_MISSING_COST')
            closed.append(dict(id=row['id'], product=pid, opened_at=row['opened_at'], closed_at=fill['created'],
                               close_fill_id=fill['id'], strategy_id=next(iter(row['strategies'])) if len(row['strategies']) == 1 else 'MIXED',
                               execution_version=next(iter(row['versions'])) if len(row['versions']) == 1 else 'MIXED',
                               cost_cad=str(row['cost']), net_cad=str(row['realized']), fees_cad=str(row['fees']),
                               net_return=float(row['realized']/row['cost']), fill_count=row['fill_count'],
                               exit_reasons=sorted(row['reasons'])))
            del opened[pid]
    return closed, len(opened)


def performance(rows):
    wins = [r for r in rows if number(r['net_cad']) > 0]
    losses = [r for r in rows if number(r['net_cad']) < 0]
    positive = sum((number(r['net_cad']) for r in wins), Decimal(0))
    negative = -sum((number(r['net_cad']) for r in losses), Decimal(0))
    cost = sum((number(r['cost_cad']) for r in rows), Decimal(0))
    net = sum((number(r['net_cad']) for r in rows), Decimal(0))
    return dict(closed_trips=len(rows), wins=len(wins), losses=len(losses),
                win_rate=len(wins)/len(rows) if rows else None, net_cad=str(net),
                closed_cost_cad=str(cost), net_on_deployed_cost=float(net/cost) if cost else None,
                profit_factor=float(positive/negative) if negative else None,
                fees_cad=str(sum((number(r['fees_cad']) for r in rows), Decimal(0))))


def kelly(rows):
    """A conservative sizing cap, not an optimal spot-portfolio allocation."""
    rows = rows[-100:]
    result = dict(samples=len(rows), required=MIN_TRIPS, status='WARMING', cap_fraction=MAX_ENTRY,
                  policy=VERSION, independent_trials_assumed=False)
    wins = [r['net_return'] for r in rows if r['net_return'] > 0]
    losses = [-r['net_return'] for r in rows if r['net_return'] < 0]
    if len(rows) < MIN_TRIPS or len(wins) < 5 or len(losses) < 5:
        return result
    n = len(rows); p = len(wins)/n; z = 1.645
    lower = (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)
    payoff = statistics.mean(wins)/statistics.mean(losses)
    half = max(0., .5*(lower-(1-lower)/payoff))
    return {**result, 'status':'MEASURED', 'win_rate':p, 'conservative_win_rate':lower,
            'payoff_ratio':payoff, 'half_kelly':half, 'cap_fraction':min(MAX_ENTRY, half)}


def forecast_record(pid, history, book, now):
    """Freeze before the outcome. All calibration errors end before creation."""
    bars = history['bars']; fx = float(history.get('fx_rate', 1))
    if len(bars) < 120 or not math.isfinite(fx) or fx <= 0:
        raise ValueError('FORECAST_HISTORY_WARMING')
    end = int(now)//3600*3600
    if bars[-1]['start'] != end-3600 or any(b['start']-a['start'] != 3600 for a,b in zip(bars,bars[1:])):
        raise ValueError('FORECAST_HISTORY_STALE')
    if not -5 <= now-book['timestamp'] <= 15 or book.get('executable') is False:
        raise ValueError('FORECAST_QUOTE_STALE')
    closes = [float(b['close'])/fx for b in bars]
    if not all(math.isfinite(c) and c > 0 for c in closes):
        raise ValueError('FORECAST_INVALID_HISTORY')
    rate = float(book.get('fx_rate', 1)); mid = float(book['mid'])
    ask = float(book['asks'][0]['price']); bid = float(book['bids'][0]['price'])
    if not all(math.isfinite(v) and v > 0 for v in (rate, mid, ask, bid)) or bid > ask:
        raise ValueError('FORECAST_INVALID_BOOK')
    fee = float(risk.DEFAULTS['fee_bps'])/10000; slip = float(risk.DEFAULTS['slippage_bps'])/10000
    # Bid/ask ratio already contains adverse FX markups. Don't subtract FX twice.
    cost_factor = bid*(1-slip)*(1-fee)/(ask*(1+slip)*(1+fee))
    threshold = -math.log(cost_factor)
    # Resolve at the first completed hourly close at least 6h after creation.
    due = (int(now)//3600+7)*3600
    horizon = (due-now)/3600
    center = planner.prediction(closes, 'trend24', horizon)+math.log(closes[-1]/(mid/rate))
    errors, actuals = [], []
    for stop in range(max(60, len(closes)-350), len(closes)-7+1, 7):
        actual = math.log(closes[stop+6]/closes[stop-1])
        errors.append(actual-planner.prediction(closes[:stop], 'trend24', 7)); actuals.append(actual)
    # Beta(2,2) smoothing prevents 0/1 certainty from a short historical window.
    probability = (sum(center+error > threshold for error in errors)+2)/(len(errors)+4)
    baseline = (sum(actual > threshold for actual in actuals)+2)/(len(actuals)+4)
    return dict(id=f'{MODEL}:{pid}:{end}', product=pid, model=MODEL, created_at=now,
                reference_native=mid/rate, fx_rate_fixed=rate, due_at=due, horizon_hours=horizon,
                probability=probability, baseline_probability=baseline, neutral_probability=.5,
                cost_factor=cost_factor, calibration_samples=len(errors), expected_log_return=center,
                target_event='net_return_after_assumed_costs_gt_0',
                method='fixed trend24 + past 7h residuals; smoothed empirical frequency; prospective scoring only',
                fx_policy='fixed entry FX; future FX movement excluded', status='PENDING')


def resolve_forecast(row, history, now):
    if now < row['due_at']:
        return None
    bar = next((b for b in history.get('bars', []) if b['start']+3600 == row['due_at']), None)
    if not bar:
        return None
    fx = float(history.get('fx_rate', 1)); close = float(bar['close'])
    if not all(math.isfinite(v) and v > 0 for v in (fx, close)):
        raise ValueError('FORECAST_INVALID_OUTCOME')
    net = close/fx/row['reference_native']*row['cost_factor']-1
    outcome = int(net > 0)
    return dict(resolved_at=now, outcome_at=row['due_at'], outcome=outcome, scenario_net_return=net,
                brier=(row['probability']-outcome)**2,
                baseline_brier=(row['baseline_probability']-outcome)**2, neutral_brier=(.5-outcome)**2)


def exit_liquidity(book, held, approved_notional):
    """Require visible bids for the whole proposed holding within 0.5% impact.

    This is a current-book check, not a guarantee of liquidity at a later exit.
    The existing IOC fill still determines the actual acquired quantity.
    """
    slip = number(risk.DEFAULTS['slippage_bps'])/10000
    fee = number(risk.DEFAULTS['fee_bps'])/10000
    top_ask = number(book['asks'][0]['price']); top_bid = number(book['bids'][0]['price'])
    needed = number(held) + number(approved_notional)/(1+fee)/(top_ask*(1+slip))
    remaining = needed; proceeds = Decimal(0); visible = Decimal(0)
    for level in book['bids']:
        price = number(level['price']); size = number(level['size'])
        if price <= 0 or size < 0:
            raise ValueError('EXIT_LIQUIDITY_INVALID_BOOK')
        if price < top_bid*Decimal('.995'):
            break
        take = min(size, remaining); proceeds += take*price*(1-slip)*(1-fee)
        remaining -= take; visible += take
        if remaining <= 0:
            break
    return dict(passed=remaining <= 0, reason='EXIT_DEPTH_CONFIRMED' if remaining <= 0 else 'EXIT_DEPTH_INSUFFICIENT',
                checked_at=book['timestamp'], required_quantity=str(needed), visible_quantity=str(visible),
                estimated_exit_cad=str(proceeds) if remaining <= 0 else None, max_depth_impact=.005,
                note='Current visible bids only; future liquidation is not guaranteed. Fees/slippage included; FX already in book.')


class Learning:
    def __init__(self, engine):
        self.engine = engine; self.path = engine.repo.path.parent/'learning.sqlite3'
        self.lock = threading.RLock(); self.cache = {}; self.last_error = None
        self.proposal_sink = None; self.forecast_cursor = 0
        storage.guard(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY,product TEXT NOT NULL,created REAL NOT NULL,due REAL NOT NULL,payload TEXT NOT NULL,outcome TEXT);
                CREATE INDEX IF NOT EXISTS forecasts_product_created ON forecasts(product,created DESC);
                CREATE INDEX IF NOT EXISTS forecasts_due ON forecasts(due) WHERE outcome IS NULL;
                CREATE TABLE IF NOT EXISTS reviews(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL,previous_hash TEXT,hash TEXT NOT NULL);
            ''')
            c.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', ('started_at', packed(time.time())))
            self.started_at = self.meta(c, 'started_at')
            self.cache = self.meta(c, 'summary', {})
        self.path.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        storage.guard(self.path)
        with contextlib.closing(sqlite3.connect(self.path, timeout=3)) as c:
            c.row_factory = sqlite3.Row
            with c:
                yield c

    @staticmethod
    def meta(c, key, default=None):
        row = c.execute('SELECT payload FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_meta(c, key, value):
        c.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, packed(value)))

    @staticmethod
    def review(c, key, payload, now):
        if c.execute('SELECT 1 FROM reviews WHERE id=?', (key,)).fetchone():
            return
        previous = c.execute('SELECT hash FROM reviews ORDER BY seq DESC LIMIT 1').fetchone()
        previous = previous[0] if previous else None
        hashed = digest(dict(id=key, created=now, payload=payload, previous_hash=previous))
        c.execute('INSERT INTO reviews(id,created,payload,previous_hash,hash) VALUES(?,?,?,?,?)',
                  (key, now, packed(payload), previous, hashed))

    def ingest(self, c):
        cursor = self.meta(c, 'cursor', [0, ''])
        # Read-only source: no Repository/Engine constructor, no ledger writes.
        with contextlib.closing(sqlite3.connect(self.engine.repo.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as source:
            rows = source.execute('SELECT id,created,payload FROM fills WHERE (created,id)>(?,?) ORDER BY created,id LIMIT 5000', tuple(cursor)).fetchall()
        for key, created, raw in rows:
            if (created, key) <= tuple(cursor):
                continue
            value = json.loads(raw); value.update(id=key, created=created)
            c.execute('INSERT OR IGNORE INTO fills VALUES(?,?,?)', (key, created, packed(value)))
            self.set_meta(c, 'cursor', [created, key])

    def forecast_cycle(self, c, now):
        intelligence = self.engine.intelligence
        if intelligence is None:
            return
        with intelligence.lock:
            histories = dict(intelligence.history)
        # Resolution never depends on a browser, a new trade, or current live price.
        pending = c.execute('SELECT id,product,payload FROM forecasts WHERE outcome IS NULL AND due<=? ORDER BY due LIMIT 2000', (now,)).fetchall()
        for row in pending:
            prediction = json.loads(row['payload'])
            outcome = resolve_forecast(prediction, histories.get(row['product'], {}), now)
            if outcome:
                c.execute('UPDATE forecasts SET outcome=? WHERE id=? AND outcome IS NULL', (packed(outcome), row['id']))
            elif now-prediction['due_at'] > 7*86400:
                c.execute('UPDATE forecasts SET outcome=? WHERE id=? AND outcome IS NULL',
                          (packed(dict(status='UNRESOLVED_HISTORY_GAP', recorded_at=now)), row['id']))
        if now-self.meta(c, 'last_forecast_at', 0) < FORECAST_INTERVAL or self.engine.execution_stopped():
            return
        self.set_meta(c, 'last_forecast_at', now)
        products = sorted(histories)
        # Only existing, genuinely executable books. No extra Coinbase traffic.
        quote_source = getattr(self.engine.adapter, '_books', None)
        levels = getattr(self.engine.adapter, '_level2', None)
        if not quote_source and not levels:
            return
        attempted = added = 0
        for _ in range(len(products)):
            pid = products[self.forecast_cursor % len(products)]; self.forecast_cursor += 1
            prior = c.execute('SELECT created FROM forecasts WHERE product=? ORDER BY created DESC LIMIT 1', (pid,)).fetchone()
            if prior and now-prior[0] < 7*3600:
                continue
            attempted += 1
            try:
                books = levels.get_many([pid]) if levels else {}
                raw = books.get(pid)
                if raw is None and quote_source:
                    # PaperBooks.peek is intentionally read-only and does not fetch.
                    raw = quote_source.peek(pid)
                if raw is None:
                    continue
                # Reuse the known FX reference without calling its network loader.
                rate = number(1); markup = number(0)
                if pid.endswith('-USD'):
                    fx = getattr(self.engine.adapter, '_fx', None)
                    if not fx or not 0 <= now-getattr(self.engine.adapter,'_fx_at',0) <= 3600:
                        continue
                    rate = number(fx['rate']); markup = number(self.engine.adapter.fx_bps)/10000
                book = {**raw, 'mid':str(number(raw['mid'])*rate), 'fx_rate':str(rate)}
                for side,multiplier in [('bids',1-markup),('asks',1+markup)]:
                    book[side]=[{**level,'price':str(number(level['price'])*rate*multiplier)} for level in raw[side]]
                prediction = forecast_record(pid, histories[pid], book, now)
                c.execute('INSERT OR IGNORE INTO forecasts(id,product,created,due,payload) VALUES(?,?,?,?,?)',
                          (prediction['id'], pid, now, prediction['due_at'], packed(prediction)))
                added += 1
            except (KeyError, ValueError, TypeError, ArithmeticError):
                continue
            if added >= 40:
                break
        self.set_meta(c, 'forecast_cycle', dict(at=now, examined=attempted, saved=added))

    def cycle(self, now=None):
        now = time.time() if now is None else now
        try:
            self._cycle(now)
            self.last_error = None
        except Exception as error:
            self.last_error = dict(code=type(error).__name__, at=now)
            raise

    def _cycle(self, now):
        from . import execution_strategies
        with self.lock, self.db() as c:
            self.ingest(c)
            fills = [json.loads(row[0]) for row in c.execute('SELECT payload FROM fills ORDER BY created,id')]
            trips, open_count = round_trips(fills)
            forward = [r for r in trips if r['opened_at'] >= self.started_at and r['execution_version'] == execution_strategies.VERSION]
            groups = defaultdict(list)
            for row in forward:
                if row['strategy_id'] != 'MIXED':
                    groups[row['strategy_id']].append(row)
            allocations = {sid:kelly([r for r in rows if now-r['closed_at'] <= 30*86400]) for sid,rows in groups.items()}
            self.review(c, VERSION, dict(kind='POLICY', version=VERSION, max_entry=MAX_ENTRY,
                        min_trips=MIN_TRIPS, existing_risk_limits_retained=True, mode='PAPER',
                        core_code_editing=False, sources=SOURCES), self.started_at)
            for trip in trips:
                self.review(c, 'closed:'+trip['id'], dict(kind='TRADE', trade=trip,
                            prospective=trip['opened_at'] >= self.started_at,
                            finding='NET_GAIN' if number(trip['net_cad'])>0 else 'NET_LOSS_OR_FLAT',
                            causal_claim=False), now)
            for cadence, bucket in [('DAILY',datetime.fromtimestamp(now,ZoneInfo('UTC')).date().isoformat()),
                                    ('48H',str(int((now-self.started_at)//(48*3600))))]:
                key=f'{cadence}:{bucket}'
                self.review(c, key, dict(kind=cadence, closed=performance(trips), forward=performance(forward),
                            allocations=allocations, action='COLLECT_MORE_EVIDENCE' if len(forward)<MIN_TRIPS else 'APPLY_BOUNDED_SIZE_CAP',
                            first20=performance(forward[:20]), last20=performance(forward[-20:]),
                            samples_overlap=len(forward)<40), now)
            self.forecast_cycle(c, now)
            predictions = [dict(prediction=json.loads(r['payload']), outcome=json.loads(r['outcome']) if r['outcome'] else None)
                           for r in c.execute('SELECT payload,outcome FROM forecasts ORDER BY created')]
            resolved = [r for r in predictions if r['outcome'] and 'brier' in r['outcome']]
            means = {k:statistics.mean(r['outcome'][k] for r in resolved) if resolved else None for k in ('brier','baseline_brier','neutral_brier')}
            forecast_summary = dict(total=len(predictions), resolved=len(resolved), pending=sum(r['outcome'] is None for r in predictions),
                                    unresolved=sum(bool(r['outcome']) and 'brier' not in r['outcome'] for r in predictions), **means,
                                    delta=means['brier']-means['baseline_brier'] if resolved else None,
                                    first_due_at=min((r['prediction']['due_at'] for r in predictions if r['outcome'] is None),default=None),
                                    last_cycle=self.meta(c,'forecast_cycle'), recent=predictions[-12:][::-1])
            forecast_summary['calibration'] = []
            for low,high in [(0,.2),(.2,.4),(.4,.6),(.6,.8),(.8,1.01)]:
                bucket=[r for r in resolved if low<=r['prediction']['probability']<high]
                forecast_summary['calibration'].append(dict(low=low,high=min(1,high),samples=len(bucket),
                    mean_prediction=statistics.mean(r['prediction']['probability'] for r in bucket) if bucket else None,
                    observed_frequency=statistics.mean(r['outcome']['outcome'] for r in bucket) if bucket else None))
            latest = c.execute('SELECT created,hash FROM reviews ORDER BY seq DESC LIMIT 1').fetchone()
            summary = dict(version=VERSION, mode='PAPER', started_at=self.started_at, updated_at=now,
                           cycle_seconds=60, forecast_interval_seconds=FORECAST_INTERVAL,
                           imported_fills=len(fills), open_episodes=open_count, historical=performance(trips),
                           forward=performance(forward), first20=performance(forward[:20]), last20=performance(forward[-20:]),
                           samples_overlap=len(forward)<40, target_trips=100, allocations=allocations,
                           forecasts=forecast_summary, latest_review=dict(at=latest['created'], hash=latest['hash']) if latest else None,
                           reviews=[{**json.loads(r['payload']), 'id':r['id'], 'at':r['created'], 'hash':r['hash']}
                                    for r in c.execute('SELECT * FROM reviews ORDER BY seq DESC LIMIT 12')],
                           sources=SOURCES, extra_model_calls=0, model_cost_status='NO_ADDITIONAL_CALLS_EXISTING_MODEL_COSTS_NOT_MEASURED',
                           max_entry_fraction=MAX_ENTRY, kelly_min_trips=MIN_TRIPS)
            summary['strategies'] = [dict(strategy_id=sid,name=name,
                historical=performance([r for r in trips if r['strategy_id']==sid]),
                forward=performance(groups.get(sid,[])), sizing=allocations.get(sid,kelly([])))
                for sid,name in execution_strategies.NAMES.items()]
            self.set_meta(c, 'summary', summary)
        self.cache = summary
        self.propose(trips, now)

    def propose(self, trips, now):
        """Loss reviews propose bounded data-only experiments, never deploy them."""
        if not self.proposal_sink:
            return
        from . import execution_strategies
        from .quant import specs
        groups = defaultdict(list)
        for row in trips:
            if row['strategy_id'] in execution_strategies.NAMES:
                groups[row['strategy_id']].append(row)
        for sid, rows in groups.items():
            recent = rows[-40:]
            if len(recent) < MIN_TRIPS or number(performance(recent)['net_cad']) >= 0:
                continue
            products = list(dict.fromkeys(r['product'] for r in recent[::-1]))[:2]
            try:
                spec = specs.validate(dict(name=f'회고 · {execution_strategies.NAMES[sid]} 축소 검증', strategy_id=sid,
                    products=products, parameters=dict(allocation_scale=.5, minimum_net_return=.01),
                    hypothesis='최근 완결 거래의 비용 차감 손익이 음수입니다. 배분을 절반으로 줄이고 순수익 진입 기준을 1%로 높인 후보를 별도 검증합니다. 실운용에 자동 적용하지 않습니다.'))
            except ValueError:
                continue
            key = 'proposal:'+specs.digest(spec)
            with self.db() as c:
                if self.meta(c, key):
                    continue
            experiment = self.proposal_sink(spec, key)
            with self.db() as c:
                self.set_meta(c,key,experiment['id'])
                self.review(c,key,dict(kind='PROPOSAL',experiment_id=experiment['id'],spec=spec,
                            evidence=performance(recent),status='PROPOSED_NOT_DEPLOYED'),now)

    def apply(self, signal, now=None):
        if signal.get('decision') != 'BUY':
            return signal
        now = time.time() if now is None else now
        data = self.cache
        detail = dict((data.get('allocations') or {}).get(signal.get('strategy_id'), kelly([])))
        if not data or now-data.get('updated_at',0)>1800 or self.last_error:
            detail.update(status='STALE_EVIDENCE',cap_fraction=min(MAX_ENTRY,detail['cap_fraction']))
        original = float(signal.get('allocation',.02))
        if not math.isfinite(original) or original < 0:
            raise ValueError('LEARNING_INVALID_ALLOCATION')
        fraction = min(original, detail['cap_fraction'], MAX_ENTRY)
        detail.update(original_fraction=original, applied_fraction=fraction, evidence_at=data.get('updated_at'))
        signal['allocation'] = fraction
        signal.setdefault('plan',{})['learning'] = detail
        signal['plan']['allocation'] = fraction
        if signal.get('sizing'):
            signal['sizing'] = {**signal['sizing'], 'fraction':fraction, 'before_learning_fraction':original}
            signal['plan']['sizing'] = signal['sizing']
        if fraction == 0:
            signal.update(decision='WAIT',reason_codes=['MEASURED_EDGE_NOT_POSITIVE'])
        return signal

    def state(self):
        with self.lock:
            data = copy.deepcopy(self.cache)
            data.update(status='RETRYING' if self.last_error else 'RUNNING' if data else 'WARMING',last_error=self.last_error)
            return data
