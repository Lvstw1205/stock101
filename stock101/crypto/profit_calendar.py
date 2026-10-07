"""Read-only daily CAD realization history, rebuilt from durable confirmed fills.

Percent is net realized P&L / matched entry cost of sold units, not account
return. Deposits and unsold marks never enter P&L. LIVE FX is a documented
cost reserve and CAD translation, not a claim that conversion was executed.
"""
import calendar
import datetime as dt
import json
import re
import time
from decimal import Decimal
from zoneinfo import ZoneInfo
from .exchange import number

TIMEZONE = 'UTC'
ZERO = Decimal(0)


def events(connection, mode):
    reserves = {}
    rows = connection.execute('SELECT f.id,f.created,f.payload,o.payload AS intent FROM fills f '
                              'LEFT JOIN orders o ON o.id=f.id ORDER BY f.created,f.rowid')
    for row in rows:
        f = json.loads(row['payload']);side = f['side'];pid = f['product']
        event = dict(id=row['id'], created=row['created'], product=pid, side=side,
                     quantity=f['quantity'], strategy_id=f.get('strategy_id'),
                     net_profit_cad=None, cost_basis_cad=None, sell_proceeds_cad=None,
                     sell_fee_cad=None, entry_fx_reserve_cad=None, exit_fx_reserve_cad=None,
                     verified=True)
        if mode == 'paper':
            if side == 'SELL':
                # CAD adapter embeds both FX sides in fill prices; no second charge.
                if f.get('valuation_currency', f.get('currency')) != 'CAD':
                    event['verified'] = False
                else:
                    proceeds = number(f['notional'])-number(f['fee']);net = number(f['realized'])
                    event.update(net_profit_cad=net, cost_basis_cad=proceeds-net,
                                 sell_proceeds_cad=proceeds, sell_fee_cad=number(f['fee']))
        else:
            intent = json.loads(row['intent']) if row['intent'] else {}
            qty = number(f['quantity']);held, reserve = reserves.get(pid, (ZERO, ZERO))
            rate = number(f['cad_rate']) if f.get('cad_rate') else None
            fx = number(intent['fx_cost_bps'])/10000 if 'fx_cost_bps' in intent else None
            valid = rate is not None and rate > 0 and fx is not None and ZERO <= fx <= Decimal('.05') and qty > 0
            if side == 'BUY':
                reserve = reserve+(number(f['notional'])+number(f['fee']))*rate*fx if valid and reserve is not None else None
                held += qty
            elif side == 'SELL':
                valid = valid and reserve is not None and held >= qty and held > 0 and 'realized_cad' in f
                if valid:
                    entry_fx = reserve*qty/held
                    proceeds = (number(f['notional'])-number(f['fee']))*rate
                    exit_fx = proceeds*fx;net = number(f['realized_cad'])-entry_fx-exit_fx
                    basis = proceeds-number(f['realized_cad'])+entry_fx
                    event.update(net_profit_cad=net, cost_basis_cad=basis,
                                 sell_proceeds_cad=proceeds-exit_fx, sell_fee_cad=number(f['fee'])*rate,
                                 entry_fx_reserve_cad=entry_fx, exit_fx_reserve_cad=exit_fx)
                    reserve -= entry_fx
                else:
                    event['verified'] = False;reserve = None
                held -= qty
                if held == 0:reserve = ZERO
            reserves[pid] = (held, reserve)
        if side == 'SELL' and (event['cost_basis_cad'] is None or event['cost_basis_cad'] <= 0):
            event['verified'] = False
        yield event


def aggregate(rows):
    exits = [r for r in rows if r['side'] == 'SELL']
    verified = all(r['verified'] for r in exits)
    cost = sum((r['cost_basis_cad'] for r in exits), ZERO) if verified else None
    net = sum((r['net_profit_cad'] for r in exits), ZERO) if verified else None
    return dict(net_profit_cad=str(net) if net is not None else None,
                cost_basis_cad=str(cost) if cost is not None else None,
                return_ratio=str(net/cost) if cost is not None and cost > 0 else None,
                buy_count=sum(r['side']=='BUY' for r in rows), sell_count=len(exits),
                verified=verified)


def view(repo, mode='paper', month=None, now=None):
    if mode not in ('paper', 'live'):raise ValueError('INVALID_CALENDAR_MODE')
    now = time.time() if now is None else now
    local_today = dt.datetime.fromtimestamp(now, ZoneInfo(TIMEZONE)).date()
    if month is None:month = local_today.strftime('%Y-%m')
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('INVALID_CALENDAR_MONTH')
    year, mon = map(int, month.split('-'))
    if not 2000 <= year <= 2100:raise ValueError('INVALID_CALENDAR_MONTH')
    by_day = {};first = None;latest = None;selected = []
    # No limits: an old day's history must not disappear after the latest 100 fills.
    with repo.db() as c:
        for event in events(c, mode):
            date = dt.datetime.fromtimestamp(event['created'], ZoneInfo(TIMEZONE)).date()
            first = min(first, date) if first else date;latest = event['created']
            if date.strftime('%Y-%m') == month:
                by_day.setdefault(date.isoformat(), []).append(event);selected.append(event)
    days = []
    for n in range(1, calendar.monthrange(year, mon)[1]+1):
        day = dt.date(year, mon, n);rows = by_day.get(day.isoformat(), [])
        summary = aggregate(rows)
        status = 'future' if day > local_today else 'before_records' if first is None or day < first else 'today' if day == local_today else 'closed'
        if status in ('future', 'before_records'):
            summary.update(net_profit_cad=None, return_ratio=None)
        rendered = []
        for row in rows:
            value = {k:str(v) if isinstance(v,Decimal) else v for k,v in row.items()}
            cost = row['cost_basis_cad']
            value['return_ratio'] = str(row['net_profit_cad']/cost) if row['verified'] and cost and cost > 0 else None
            rendered.append(value)
        days.append(dict(date=day.isoformat(), day=n, status=status, **summary, trades=rendered))
    return dict(mode=mode, month=month, currency='CAD', timezone=TIMEZONE,
                today=local_today.isoformat(), first_record_date=first.isoformat() if first else None,
                last_fill_at=latest, refreshed_at=now, weekday_offset=(dt.date(year,mon,1).weekday()+1)%7,
                summary=aggregate(selected), days=days, source='confirmed_fill_ledger',
                return_basis='realized_net_profit / matched_sold_cost_including_entry_costs',
                cost_basis_label='모의 체결 가격에 거래·환전 비용 반영' if mode=='paper' else '확정 체결 + 체결 당시 CAD 환산 · 양쪽 환전 비용은 설정한 준비금',
                includes_unrealized=False, includes_deposits=False)
