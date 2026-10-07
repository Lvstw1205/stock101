"""Isolate confirmed removed PAPER markets without inventing an executable quote.

Historical holdings/fills remain untouched. Last observed marks are indicative
only; the multi engine blocks all entries until every holding is priceable.
Freshly quoted assets can still be assessed and sold under existing exit rules.
"""
import time
from decimal import Decimal
from types import SimpleNamespace
from .exchange import ExchangeError, number
from .engine import Engine


def update(engine, positions):
    if not engine.products:
        return
    now = time.time()
    listed = {p['product_id'] for p in engine.products}
    held = {p['product']: p for p in positions if number(p['quantity']) > 0}
    with engine._suspension_lock:
        for pid in list(engine.suspended_products):
            if pid not in held or pid in listed:
                engine.suspended_products.pop(pid, None)
        for pid, pos in held.items():
            if pid in listed:
                continue
            cached = engine._missing_product_checks.get(pid)
            if cached and now - cached['checked_at'] < 60:
                missing = cached['missing']
            else:
                try:
                    engine.adapter.get_product(pid)
                except ExchangeError as error:
                    if str(error) != 'PRODUCT_NOT_FOUND':
                        raise
                    missing = True
                else:
                    missing = False
                engine._missing_product_checks[pid] = dict(checked_at=now, missing=missing)
            if not missing:
                engine.suspended_products.pop(pid, None)
                continue
            if pid in engine.suspended_products:
                continue
            with engine.repo.db() as c:
                row = c.execute("SELECT created,payload FROM portfolio_snapshots WHERE json_extract(payload,'$.currency')=? ORDER BY created DESC LIMIT 1", (pos['currency'],)).fetchone()
            snapshot = engine.repo.decode_payload(row['payload']) if row else {}
            prior = next((p for p in snapshot.get('positions', []) if p['product'] == pid and number(p['quantity']) == number(pos['quantity'])), None)
            if not prior or number(prior.get('price', 0)) <= 0:
                raise ExchangeError('UNAVAILABLE_HOLDING_MARK_MISSING')
            stamp = prior.get('mark_as_of') or snapshot.get('mark_as_of') or row['created']
            engine.suspended_products[pid] = dict(product=pid, reason='PRODUCT_NOT_FOUND',
                last_price=prior['price'], last_mark_at=stamp, detected_at=now,
                quantity=pos['quantity'], executable=False)


def books_for(engine, currency, initial=None):
    with engine.repo.db() as c:
        positions = [p for p in engine.positions(c) if p['currency'] == currency and number(p['quantity']) > 0]
    update(engine, positions)
    suspended = dict(engine.suspended_products)
    held = [p['product'] for p in positions if p['product'] not in suspended]
    books = {pid: book for pid, book in (initial or {}).items() if pid not in suspended}
    if callable(getattr(engine.adapter, 'set_held_products', None)):
        engine.adapter.set_held_products(held)
    missing = [pid for pid in held if pid not in books]
    if missing:
        if callable(getattr(engine.adapter, 'get_orderbooks', None)):
            books.update(engine.adapter.get_orderbooks(missing))
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
                books.update(zip(missing, pool.map(engine.adapter.get_orderbook, missing)))
    for pid, book in list(books.items()):
        if not -5 <= time.time() - book['timestamp'] <= 12:
            books[pid] = engine.adapter.get_orderbook(pid)
    return books


def value(engine, c, currency, books):
    positions = engine.positions(c)
    suspended = dict(engine.suspended_products)
    pending = [p for p in positions if p['currency'] == currency and number(p['quantity']) > 0 and p['product'] in suspended]
    if not pending:
        return Engine.value(engine, c, currency, books)
    # Keep the base engine's fresh-quote validation for every unaffected asset.
    reader = SimpleNamespace(adapter=engine.adapter, positions=lambda _: [p for p in positions if p['product'] not in suspended])
    portfolio = Engine.value(reader, c, currency, books)
    indicative = Decimal(0)
    unrealized = Decimal(0)
    for pos in pending:
        mark = suspended[pos['product']]
        amount = number(pos['quantity']) * number(mark['last_price'])
        profit = amount - number(pos['cost'])
        portfolio['positions'].append({**pos, 'price': mark['last_price'], 'market_value': str(amount),
            'unrealized': str(profit), 'mark_status': 'STALE', 'mark_as_of': mark['last_mark_at'],
            'mark_reason': 'PRODUCT_NOT_FOUND', 'executable': False})
        indicative += amount
        unrealized += profit
    for key in ('equity', 'invested'):
        portfolio[key] = str(number(portfolio[key]) + indicative)
    portfolio['unrealized'] = str(number(portfolio['unrealized']) + unrealized)
    portfolio.update(mark_status='PARTIAL', mark_as_of=min(suspended[p['product']]['last_mark_at'] for p in pending),
        fresh_mark_as_of=min((b['timestamp'] for b in books.values()), default=time.time()),
        indicative_value_cad=str(indicative), unavailable_products=[p['product'] for p in pending],
        entry_enabled=False, valuation_note='Includes last observed prices for removed markets; not current liquidation value. New buys paused; fresh-market exit management continues.')
    return portfolio
