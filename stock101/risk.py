"""Fail-closed live preflight, independent of strategies and brokers."""
import math

DEFAULTS={'max_position':.05,'max_trade':.02,'max_daily_loss':.03,'max_drawdown':.10,'max_exposure':1.0,'max_price_deviation':.01,'max_volume_participation':.01}


def check(order,context,limits=None):
    limits={**DEFAULTS,**(limits or {})};fail=[]
    required=['equity','position_value','exposure','daily_loss','drawdown','price','reference_price','daily_volume','market_open','quote_age_seconds','duplicate','halted']
    if any(k not in context or context[k] is None for k in required): return ['RISK_DATA_UNAVAILABLE']
    try:
        numeric=[context[k] for k in required if k not in ('market_open','duplicate','halted')]+[order['quantity']]+list(limits.values())
        if not all(math.isfinite(float(v)) for v in numeric): return ['INVALID_RISK_DATA']
        equity=context['equity'];value=context['price']*order['quantity']
        if equity<=0 or min(context['reference_price'],context['price'],order['quantity'])<=0 or context['daily_volume']<0: return ['INVALID_RISK_DATA']
        if order['side']=='BUY' and (context['position_value']+value)/equity>limits['max_position']: fail.append('POSITION_LIMIT')
        if value/equity>limits['max_trade']: fail.append('TRADE_LIMIT')
        if order['side']=='BUY' and (context['exposure']+value)/equity>limits['max_exposure']: fail.append('EXPOSURE_LIMIT')
        if context['daily_loss']>=limits['max_daily_loss']: fail.append('DAILY_LOSS_LIMIT')
        if context['drawdown']>=limits['max_drawdown']: fail.append('DRAWDOWN_LIMIT')
        if abs(context['price']/context['reference_price']-1)>limits['max_price_deviation']: fail.append('PRICE_DEVIATION')
        if order['quantity']>context['daily_volume']*limits['max_volume_participation']: fail.append('LIQUIDITY_LIMIT')
        if not context['market_open'] or context['halted']: fail.append('MARKET_UNAVAILABLE')
        if context['quote_age_seconds']>30 or context['quote_age_seconds']<0: fail.append('STALE_QUOTE')
        if context['duplicate']: fail.append('DUPLICATE_ORDER')
    except (TypeError,ValueError,KeyError): return ['INVALID_RISK_DATA']
    return fail
