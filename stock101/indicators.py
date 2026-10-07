"""Causal indicators. None means insufficient observations, never zero."""
import math
import statistics as stats


def sma(values, period):
    return [sum(values[i-period+1:i+1])/period if i >= period-1 else None for i in range(len(values))]


def ema(values, period):
    out = [None]*len(values)
    valid = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(valid) < period:
        return out
    first = valid[period-1][0]
    out[first] = sum(v for _, v in valid[:period])/period
    for i in range(first+1, len(values)):
        if values[i] is not None:
            out[i] = values[i]*2/(period+1) + out[i-1]*(1-2/(period+1))
    return out


def rsi(values, period=14):
    out = [None]*len(values)
    if len(values) <= period:
        return out
    delta = [values[i]-values[i-1] for i in range(1, len(values))]
    gain = sum(max(x, 0) for x in delta[:period])/period
    loss = sum(max(-x, 0) for x in delta[:period])/period
    def value():
        return 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100-100/(1+gain/loss)
    out[period] = value()
    for i in range(period+1, len(values)):
        gain = (gain*(period-1)+max(delta[i-1], 0))/period
        loss = (loss*(period-1)+max(-delta[i-1], 0))/period
        out[i] = value()
    return out


def calculate(bars):
    close = [b['close'] for b in bars]
    out = {'price': close, 'volume': [b['volume'] for b in bars], 'rsi': rsi(close)}
    for p in (5, 10, 20, 50, 60, 120, 200):
        out['ma'+str(p)] = sma(close, p)
    fast, slow = ema(close, 12), ema(close, 26)
    out['macd'] = [a-b if a is not None and b is not None else None for a, b in zip(fast, slow)]
    out['signal'] = ema(out['macd'], 9)
    out['histogram'] = [a-b if a is not None and b is not None else None for a, b in zip(out['macd'], out['signal'])]
    out['bb_mid'] = out['ma20']
    dev = [stats.pstdev(close[i-19:i+1]) if i >= 19 else None for i in range(len(close))]
    out['bb_upper'] = [m+2*s if m is not None else None for m, s in zip(out['ma20'], dev)]
    out['bb_lower'] = [m-2*s if m is not None else None for m, s in zip(out['ma20'], dev)]
    out['bb_width'] = [(u-l)/m if m else None for u, l, m in zip(out['bb_upper'], out['bb_lower'], out['ma20'])]
    out['volume_ma20'] = sma(out['volume'], 20)
    return out


def pivots(values, high=False, radius=3):
    # A pivot becomes knowable only radius bars later.
    result = []
    for i in range(radius, len(values)-radius):
        window = values[i-radius:i+radius+1]
        if values[i] == (max(window) if high else min(window)) and window.count(values[i]) == 1:
            result.append(i)
    return result


def divergences(bars, ind):
    result = []
    for high in (False, True):
        prices = [b['high' if high else 'low'] for b in bars]
        points = pivots(prices, high)
        for a, b in zip(points, points[1:]):
            if b-a > 60:
                continue
            for key in ('rsi', 'macd'):
                x, y = ind[key][a], ind[key][b]
                if x is None or y is None or (prices[b]-prices[a])*(y-x) >= 0:
                    continue
                kind = ('Bearish' if prices[b] > prices[a] else 'Hidden bearish') if high else ('Bullish' if prices[b] < prices[a] else 'Hidden bullish')
                result.append({'kind': kind, 'indicator': key, 'from': a, 'to': b, 'confirmed_at': b+3,
                               'price_from': prices[a], 'price_to': prices[b], 'indicator_from': x, 'indicator_to': y})
    return result[-12:]


def zones(bars, ind):
    if not bars:
        return []
    price = bars[-1]['close']
    candidates = []
    for high in (False, True):
        values = [b['high' if high else 'low'] for b in bars]
        candidates += [(values[i], 'swing', i) for i in pivots(values, high) if i >= len(bars)-120]
    for p in (20, 60, 200):
        if ind['ma'+str(p)][-1] is not None:
            candidates.append((ind['ma'+str(p)][-1], 'MA'+str(p), len(bars)-1))
    recent = bars[-120:]
    if recent:
        lo, hi = min(b['low'] for b in recent), max(b['high'] for b in recent)
        step = max((hi-lo)/16, price*.005)
        buckets = {}
        for b in recent:
            key = int((b['close']-lo)/step)
            buckets[key] = buckets.get(key, 0)+b['volume']
        for k in sorted(buckets, key=buckets.get, reverse=True)[:2]:
            candidates.append((lo+(k+.5)*step, 'daily volume cluster proxy', len(bars)-1))
    groups = []
    for value, source, i in sorted(candidates):
        group = next((g for g in groups if abs(value-g['center'])/price < .015), None)
        if group is None:
            group = {'center': value, 'values': [], 'sources': set()}; groups.append(group)
        group['values'].append(value); group['sources'].add(source)
        group['center'] = stats.mean(group['values'])
    result = []
    for g in groups:
        center = g['center']; low, high = center*.993, center*1.007
        touches = sum(b['low'] <= high and b['high'] >= low for b in recent)
        result.append({'low': low, 'high': high, 'kind': 'Support' if center < price else 'Resistance',
                       'distance_pct': (center/price-1)*100, 'touches': touches,
                       'strength': 'Repeated' if touches >= 5 else 'Developing', 'basis': sorted(g['sources'])})
    return sorted(result, key=lambda z: abs(z['distance_pct']))[:6]


def evidence(bars, ind):
    if not bars:
        return {'bias': 'Unavailable', 'confidence': 'Unavailable', 'positive': [], 'caution': []}
    v = {k: a[-1] for k, a in ind.items()}
    positive, caution = [], []
    if v['ma60'] is not None:
        (positive if v['ma20'] > v['ma60'] else caution).append('MA20 is above MA60.' if v['ma20'] > v['ma60'] else 'MA20 is below MA60.')
    if v['histogram'] is not None:
        (positive if v['histogram'] > 0 else caution).append('MACD is above its signal line.' if v['histogram'] > 0 else 'MACD is below its signal line.')
    if v['rsi'] is not None:
        if v['rsi'] >= 70:
            caution.append('RSI is above 70: strong buying pressure can persist; a fall is not implied.')
        elif v['rsi'] <= 30:
            caution.append('RSI is below 30: selling pressure is strong; a rebound is not assured.')
        else:
            positive.append('RSI is within its neutral 30–70 range.')
    if v['volume_ma20']:
        ratio = v['volume']/v['volume_ma20']
        if ratio > 1.4:
            (positive if len(bars)>1 and bars[-1]['close']>bars[-2]['close'] else caution).append(f'Volume is {ratio:.2f}× its 20-bar average.')
    levels = zones(bars, ind)
    if any(z['kind']=='Resistance' and 0<z['distance_pct']<3 for z in levels):
        caution.append('Price is within 3% of a detected resistance zone.')
    if v['bb_upper'] and v['price'] >= v['bb_upper']:
        caution.append('Price touches or exceeds the upper volatility band.')
    if v['bb_lower'] and v['price'] <= v['bb_lower']:
        caution.append('Price touches or falls below the lower volatility band.')
    widths = [x for x in ind['bb_width'][-120:] if x is not None]
    if widths and v['bb_width'] <= sorted(widths)[int(len(widths)*.2)]:
        caution.append('Bollinger bands are relatively narrow: volatility may expand in either direction.')
    trend = sum(1 if v['price']>v['ma'+str(p)] else -1 for p in (20,60,200) if v['ma'+str(p)] is not None)
    momentum = 1 if (v['histogram'] or 0)>0 else -1
    bias = 'Bullish' if trend>=1 and momentum>0 else 'Bearish' if trend<=-1 and momentum<0 else 'Mixed'
    trends = {name: ('Insufficient history' if v['ma'+str(p)] is None else 'Bullish' if v['price']>v['ma'+str(p)] else 'Bearish') for name,p in [('Short',20),('Medium',60),('Long',200)]}
    events = []
    for a,b,name in [('ma50','ma200','Golden / death cross'),('price','ma20','Price / MA20 cross'),('macd','signal','MACD crossover')]:
        if len(bars)>1 and all(ind[k][i] is not None for k in (a,b) for i in (-1,-2)):
            prev, curr = ind[a][-2]-ind[b][-2], ind[a][-1]-ind[b][-1]
            if prev<=0<curr or prev>=0>curr: events.append(name+(' ↑' if curr>0 else ' ↓'))
    if len(bars)>1 and ind['histogram'][-2] is not None:
        events.append('Momentum magnitude '+('strengthening' if abs(v['histogram'])>abs(ind['histogram'][-2]) else 'weakening'))
    percentile = sum(x<=v['rsi'] for x in ind['rsi'][-252:] if x is not None)/max(1,sum(x is not None for x in ind['rsi'][-252:]))*100 if v['rsi'] is not None else None
    return {'bias': bias, 'confidence': 'Moderate' if bias!='Mixed' and len(bars)>=200 else 'Low',
            'positive': positive, 'caution': caution, 'trends': trends, 'events': events, 'rsi_percentile': percentile,
            'limitations': 'Heuristic technical agreement, not a calibrated probability. News is evaluated separately; missing news never adds confidence.'}
