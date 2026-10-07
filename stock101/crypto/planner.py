"""Versioned historical forecasts and fresh-price execution plans.

Walk-forward errors describe this short history, not a probability of profit.
No future candle is used to fit any validation prediction. News never creates
a price target or an executable signal on its own.
"""
import math
import statistics
from . import risk, net_edge
from .exchange import number

VERSION = 'historical_plan_v2'
PROTECTIVE = {'PLAN_INVALIDATED', 'TRAILING_PROTECTION', 'PORTFOLIO_REBALANCE', 'TREND_AND_MOMENTUM_EXIT'}

def quantile(values, q):
    rows = sorted(values)
    index = (len(rows)-1)*q
    lo = int(index)
    return rows[lo] + (rows[min(lo+1, len(rows)-1)]-rows[lo])*(index-lo)

def prediction(closes, model, horizon=6):
    if model == 'unchanged':
        return 0.
    if model == 'range24':
        return math.log(statistics.mean(closes[-24:])/closes[-1])*.5
    width = int(model[5:])
    values = [math.log(x) for x in closes[-width:]]
    center = (len(values)-1)/2
    slope = sum((i-center)*v for i,v in enumerate(values))/sum((i-center)**2 for i in range(len(values)))
    return max(-.25, min(.25, slope*horizon))

def forecast(bars):
    if len(bars) < 90:
        raise ValueError('HISTORY_LESS_THAN_90_HOURS')
    closes = [float(b['close']) for b in bars]
    if any(not math.isfinite(x) or x <= 0 for x in closes):
        raise ValueError('INVALID_HISTORY')
    if any(b['start']-a['start'] != 3600 for a,b in zip(bars, bars[1:])):
        raise ValueError('CANDLE_GAP_OR_DUPLICATE')
    horizon = 6
    errors = {k: [] for k in ('unchanged', 'trend24', 'trend48', 'range24')}
    # Non-overlapping six-hour outcomes; restrict diagnostics to recent 14 days.
    for end in range(max(60, len(closes)-288), len(closes)-horizon+1, horizon):
        actual = math.log(closes[end+horizon-1]/closes[end-1])
        for model in errors:
            errors[model].append(actual-prediction(closes[:end], model, horizon))
    mae = {k: statistics.mean(abs(x) for x in v) for k,v in errors.items()}
    model = min(mae, key=mae.get)
    center = prediction(closes, model, horizon)
    residuals = errors[model]
    # A minimum band prevents a perfectly smooth short sample implying certainty.
    lo = min(-.001, quantile(residuals, .2))
    hi = max(.001, quantile(residuals, .8))
    last = closes[-1]
    atr = statistics.mean(max(b['high']-b['low'], abs(b['high']-a['close']), abs(b['low']-a['close'])) for a,b in zip(bars[-15:-1], bars[-14:]))
    atr = max(last*.001, atr)
    sma20 = statistics.mean(closes[-20:]); sma50 = statistics.mean(closes[-50:])
    regime = 'BULL_TREND' if last>sma20>sma50 else 'BEAR_TREND' if last<sma20<sma50 else 'SIDEWAYS'
    return {'version': VERSION, 'model': model, 'bar': bars[-1]['start'], 'as_of': bars[-1]['start']+3600,
            'horizon_hours': horizon, 'history_hours': len(bars), 'reference': last,
            'center': last*math.exp(center), 'lower': last*math.exp(center+lo), 'upper': last*math.exp(center+hi),
            'support': min(b['low'] for b in bars[-20:]), 'resistance': max(b['high'] for b in bars[-20:]),
            'atr': atr, 'sma20': sma20, 'sma50': sma50, 'regime': regime,
            'validation': {'samples': len(residuals), 'mae_log_return': mae[model], 'baseline_mae': mae['unchanged'],
                           'models': mae, 'method': 'rolling origin; disjoint 6h outcomes; prefix-only fits',
                           'limitation': 'Model selection diagnostics, not an independent strategy backtest or guaranteed coverage.'},
            'chart': [{'at': b['start']+3600, 'close': b['close']} for b in bars[-72:]]}

def evaluate(bars, position, book, *, model=None, news=None, previous_price=None, fee_bps=None, fx_bps=0, minimum=net_edge.MIN_NET):
    f = model or forecast(bars)
    news = news or {'status': 'NOT_CHECKED', 'allocation_multiplier': 1, 'articles': []}
    mid = float(book['mid']); atr = f['atr']; last = f['reference']
    fee = number(risk.DEFAULTS['fee_bps'] if fee_bps is None else fee_bps)/10000
    slip = number(risk.DEFAULTS['slippage_bps'])/10000
    fx = number(fx_bps)/10000
    ask = number(book['asks'][0]['price'])*(1+fx)
    bid = number(book['bids'][0]['price'])*(1-fx)
    # Preserve the current bid/mid discount (spread/FX) in future sale proceeds.
    exit_unit = number(f['center'])*(bid/number(mid))*(1-slip)*(1-fee)
    entry_unit = ask*(1+slip)*(1+fee)
    net = exit_unit/entry_unit-1
    lower_net = number(f['lower'])*(bid/number(mid))*(1-slip)*(1-fee)/entry_unit-1
    upper_net = number(f['upper'])*(bid/number(mid))*(1-slip)*(1-fee)/entry_unit-1
    # Largest executable mid that still clears the complete round-trip cost gate.
    cost_limit = float(exit_unit/(1+minimum)/(ask/number(mid))/(1+slip)/(1+fee))
    continuation = f['regime']=='BULL_TREND' and f['center'] > last
    kind = 'TREND_PULLBACK' if continuation else 'RANGE_RECOVERY'
    lower = max(f['support'], last-1.5*atr) if continuation else f['support']
    upper = min(cost_limit, last+.35*atr if continuation else min(f['sma20'], last)-.25*atr)
    invalidation = max(last*.05, lower-atr)
    reward_risk = (f['center']-mid)/max(atr*.1, mid-invalidation)
    improving = mid >= (previous_price if previous_price is not None else last)*(1-1e-12)
    volatility_scale = min(1., .015/(atr/last))
    allocation = .02*volatility_scale*float(news.get('allocation_multiplier', 1))
    plan = {'kind': kind, 'currency': book.get('valuation_currency') or book['product_id'].split('-')[-1],
            'current': mid, 'entry_low': lower, 'entry_high': upper, 'cost_entry_ceiling': cost_limit,
            'target': f['center'], 'invalidation': invalidation, 'reward_risk': reward_risk,
            'allocation': allocation, 'volatility_scale': volatility_scale, 'price_improving': improving,
            'reference_price': mid, 'evaluated_at': book['timestamp'], 'forecast': {k:v for k,v in f.items() if k!='chart'}, 'news': news,
            'net_low': str(lower_net), 'net_high': str(upper_net), 'net_center': str(net)}
    decision = 'WAIT'; reason = 'ENTRY_PRICE_NOT_REACHED'; fraction = 0
    qty = number(position.get('quantity', '0')) if position else 0
    if qty > 0:
        entry_plan = position.get('entry_plan') or {}
        stop = float(entry_plan.get('invalidation', invalidation))
        target = float(entry_plan.get('target', f['center']))
        peak = max(mid, float(position.get('peak', mid)))
        stop = max(stop, peak*(1-.08))
        plan.update(invalidation=stop, target=target)
        cost = max(number(position['cost']), number(position.get('entry_cost_reserve', position['cost'])))
        current_net = qty*bid*(1-slip)*(1-fee)/cost-1
        plan['position_net_exit'] = str(current_net)
        if mid <= stop:
            decision='SELL'; fraction=1; reason='PLAN_INVALIDATED'
        elif position.get('overweight'):
            decision='REDUCE'; fraction=.25; reason='PORTFOLIO_REBALANCE'
        elif mid>=target and current_net>=minimum:
            decision='SELL'; fraction=1; reason='PLAN_TARGET_REACHED'
        else:
            decision='HOLD'; reason='MANAGING_ENTRY_PLAN'
    elif mid <= invalidation:
        reason='REBUILD_AFTER_SUPPORT_BREAK'
    elif upper < lower or net < minimum:
        reason='NET_EDGE_TOO_SMALL'
    elif lower <= mid <= upper:
        if not improving: reason='WAITING_FOR_PRICE_RECOVERY'
        elif reward_risk < 1.2: reason='REWARD_RISK_BELOW_1_2'
        else: decision='BUY'; reason='PLAN_ENTRY_REACHED'
    edge = {'passed': net>=minimum, 'reason': 'NET_EDGE_PASSED' if net>=minimum else 'NET_EDGE_TOO_SMALL',
            'gross_scenario_return': str(number(f['center'])/number(mid)-1), 'net_scenario_return': str(net),
            'scenario_exit_per_unit_after_cost': str(exit_unit), 'minimum_net_return': str(minimum),
            'horizon_hours': 6, 'model': f['model'], 'fee_bps_each_way': str(fee*10000)}
    return {'decision': decision, 'reason_codes': [reason], 'strategy_version': VERSION, 'allocation': allocation,
            'sell_fraction': fraction, 'regime': {'name': f['regime']}, 'signals': [], 'net_edge': edge, 'plan': plan}
