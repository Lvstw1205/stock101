"""Explainable profit management, not a guaranteed forecast or an LLM order.

A positive liquidation estimate only makes profit taking eligible. Completed
hourly trend, resistance and drawdown determine HOLD / partial / full exit.
The execution layer independently checks the actual proposed fill's net cost.
"""
import statistics
from decimal import Decimal
from .exchange import number
from .performance_bonus import liquidation_value, realized_net

VERSION = 'discretionary_net_profit_v1'
EXIT_REASONS = {'BREAKOUT_NET_SESSION_EXIT', 'AGENTS_NET_EXIT', 'AGENTS_PARTIAL_NET_EXIT', 'PROFIT_TREND_WEAKENED', 'PROFIT_PULLBACK_EXIT', 'PROFIT_RESISTANCE_PARTIAL'}


def live_cost_basis(repo, connection, position):
    costs = realized_net(repo, connection, 'live', with_reserves=True)
    if costs is None or 'cost_cad' not in position or position['product'] not in costs[1]:
        return None
    return number(position['cost_cad']) + costs[1].get(position['product'], Decimal(0))


def apply(signal, hour, position, book, *, fee_bps, slippage_bps, fx_bps=0,
          cad_rate=1, cost_basis=None):
    if not position or number(position.get('quantity', 0)) <= 0:
        return signal
    qty = number(position['quantity'])
    # A LIVE caller supplies the historical CAD basis including entry FX reserve.
    basis = number(cost_basis) if cost_basis is not None else (
        None if 'cost_cad' in position else number(position['cost']))
    proceeds = liquidation_value(qty, book, number(fee_bps)/10000, number(slippage_bps)/10000)
    if proceeds is not None:
        proceeds *= (1-number(fx_bps)/10000)*number(cad_rate)
    net = proceeds-basis if proceeds is not None and basis is not None and basis > 0 else None
    detail = dict(version=VERSION, status='COSTS_UNVERIFIED', action=signal['decision'],
                  net_profit_cad=str(net) if net is not None else None,
                  net_return=str(net/basis) if net is not None else None,
                  cost_basis_cad=str(basis) if basis is not None else None,
                  liquidation_cad=str(proceeds) if proceeds is not None else None,
                  evaluated_at=book['timestamp'], automatic_on_positive=False,
                  basis='bid depth, sell fee/slippage, entry costs and both FX sides')
    signal.setdefault('plan', {})['profit_management'] = detail
    if net is not None:signal['plan']['position_net_exit'] = str(net/basis)
    # Preserve original targets, stops, session exits and deliberate risk reduction.
    if signal['decision'] in ('SELL', 'REDUCE'):
        detail['status'] = 'STRATEGY_EXIT'
        return signal
    if net is None:
        return signal
    if net <= 0:
        detail['status'] = 'NET_NOT_POSITIVE'
        return signal
    if not hour or len(hour) < 50:
        detail['status'] = 'HISTORY_REQUIRED'
        return signal
    close = [float(b['close']) for b in hour]
    mid = float(book['mid']);ma20 = statistics.mean(close[-20:])
    ma50 = statistics.mean(close[-50:]);prior20 = statistics.mean(close[-23:-3])
    atr = max(mid*.001, statistics.mean(max(b['high']-b['low'], abs(b['high']-a['close']),
              abs(b['low']-a['close'])) for a,b in zip(hour[-15:-1],hour[-14:])))
    resistance = max(b['high'] for b in hour[-24:])
    peak = max(mid, float(position.get('peak') or mid))
    strong = mid >= ma20 and close[-1] > ma20 > ma50 and ma20 > prior20 and close[-1] > close[-4]
    weak = mid < ma20 and ma20 < prior20 and close[-1] < close[-4]
    pullback = peak-mid >= max(atr, mid*.003)
    near_resistance = abs(mid-resistance) <= .35*atr
    detail.update(ma20=ma20, ma50=ma50, ma20_prior=prior20, atr=atr,
                  resistance=resistance, peak=peak, strong_trend=strong,
                  weakening_trend=weak, pullback=peak-mid)
    if strong:
        detail['status'] = 'HOLD_STRONG_TREND'
        return signal
    if weak:
        reason, fraction = 'PROFIT_TREND_WEAKENED', 1
    elif pullback:
        reason, fraction = 'PROFIT_PULLBACK_EXIT', 1
    elif near_resistance and not position.get('profit_taken'):
        reason, fraction = 'PROFIT_RESISTANCE_PARTIAL', .5
    else:
        detail['status'] = 'HOLD_FOR_MORE_EVIDENCE'
        return signal
    reasons = [reason] + (['PARTIAL_PROFIT_TAKING'] if fraction < 1 else [])
    signal.update(decision='SELL', sell_fraction=fraction, reason_codes=reasons)
    detail.update(status=reason, action='PARTIAL_SELL' if fraction < 1 else 'SELL')
    for candidate in signal.get('strategy_candidates', []):
        if candidate['strategy_id'] == signal.get('strategy_id'):
            candidate.update(decision='SELL', reason=reason, sell_fraction=fraction)
    return signal


def discretionary(signal):
    return bool(set(signal.get('reason_codes', [])) & EXIT_REASONS)


def exit_allowed(proceeds, cost, signal, minimum):
    cost = number(cost);proceeds = number(proceeds)
    return cost > 0 and proceeds > cost and (discretionary(signal) or proceeds/cost-1 >= number(minimum))
