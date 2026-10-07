"""Read-only charts of the primary CAD paper ledger; no order/engine side effects."""
import datetime as dt
import sqlite3
import time
from contextlib import closing
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from .profit_calendar import TIMEZONE, aggregate, events

MAX_RECORDS = 200000
BUCKETS = 100
GAP_SECONDS = 900


def downsample(points, since, until):
    """Keep endpoints and equity/P&L extrema in each time bucket, plus gaps."""
    buckets = {}
    width = max(1, (until - since) / BUCKETS)
    for i, point in enumerate(points):
        buckets.setdefault(int((point['timestamp'] - since) / width), []).append(i)
    keep = set()
    for indices in buckets.values():
        keep.update((indices[0], indices[-1]))
        for key in ('equity', 'net_profit_cad'):
            keep.add(min(indices, key=lambda i: Decimal(points[i][key])))
            keep.add(max(indices, key=lambda i: Decimal(points[i][key])))
    for i in range(1, len(points)):
        if points[i]['initial'] != points[i-1]['initial']:
            keep.update((i-1, i))
        if points[i]['timestamp'] - points[i-1]['timestamp'] > GAP_SECONDS:
            points[i]['gap_before'] = True
            keep.update((i-1, i))
    return [points[i] for i in sorted(keep)]


def view(repo, days='1', now=None):
    if str(days) not in ('1', '7', '30'):
        raise ValueError('INVALID_CHART_RANGE')
    days = int(days)
    now = time.time() if now is None else now
    since = now - days * 86400
    zone = ZoneInfo(TIMEZONE)
    today = dt.datetime.fromtimestamp(now, zone).date()
    daily_days = max(7, days)
    first_day = today - dt.timedelta(days=daily_days-1)
    points, dropped, by_day, first_fill = [], 0, {}, None
    # Read-only URI prevents writes even during future refactors. BEGIN gives
    # history and fills one consistent WAL read snapshot without a write lock.
    with closing(sqlite3.connect(repo.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)) as c:
        c.row_factory = sqlite3.Row
        c.execute('BEGIN')
        rows = c.execute("SELECT created,json_extract(payload,'$.equity') AS equity,"
                         "json_extract(payload,'$.initial') AS initial FROM portfolio_snapshots "
                         "WHERE json_extract(payload,'$.currency')=? AND created>=? AND created<=? "
                         "ORDER BY created DESC LIMIT ?", ('CAD', since, now, MAX_RECORDS+1)).fetchall()
        truncated = len(rows) > MAX_RECORDS
        for row in reversed(rows[:MAX_RECORDS]):
            try:
                equity, initial = Decimal(str(row['equity'])), Decimal(str(row['initial']))
                if not equity.is_finite() or not initial.is_finite() or initial <= 0:
                    raise ValueError('INVALID_MARK')
            except (InvalidOperation, ValueError):
                dropped += 1
                continue
            points.append(dict(timestamp=row['created'], equity=str(equity), initial=str(initial),
                               net_profit_cad=str(equity-initial),
                               return_ratio=str((equity-initial)/initial)))
        for event in events(c, 'paper'):
            if event['created'] > now:
                continue
            day = dt.datetime.fromtimestamp(event['created'], zone).date()
            first_fill = min(first_fill, day) if first_fill else day
            if first_day <= day <= today:
                by_day.setdefault(day.isoformat(), []).append(event)
    daily = []
    for offset in range(daily_days):
        day = first_day + dt.timedelta(days=offset)
        summary = aggregate(by_day.get(day.isoformat(), []))
        status = 'before_records' if first_fill is None or day < first_fill else 'today' if day == today else 'closed'
        if status == 'before_records':
            summary.update(net_profit_cad=None, return_ratio=None)
        daily.append(dict(date=day.isoformat(), status=status, **summary))
    return dict(mode='paper', currency='CAD', timezone=TIMEZONE, days=days,
                requested_from=since, observed_at=now, first_at=points[0]['timestamp'] if points else None,
                last_at=points[-1]['timestamp'] if points else None, record_count=len(points),
                truncated=truncated, invalid_marks=dropped, gap_seconds=GAP_SECONDS,
                history=downsample(points, since, now), daily=daily, daily_days=daily_days,
                source='primary_paper_ledger', timestamp_basis='recorded_at',
                equity_basis='cash_plus_bid_mark_before_future_exit_costs',
                pnl_basis='equity_minus_cumulative_funding',
                daily_return_basis='realized_net_profit_divided_by_matched_sold_cost',
                includes_research_accounts=False)
