"""Experimental trend cost gate, NOT a calibrated profit prediction."""
import math,statistics
from decimal import Decimal
from .exchange import number
from . import risk
MIN_NET=Decimal('.005')
def estimate(bars,book,fee_bps=None,fx_bps=0,minimum=MIN_NET):
    if len(bars)<60:return {'passed':False,'reason':'INSUFFICIENT_HISTORY'}
    closes=[float(b['close']) for b in bars[-48:]]
    if any(not math.isfinite(x) or x<=0 for x in closes):return {'passed':False,'reason':'INVALID_HISTORY'}
    y=[math.log(x) for x in closes];n=len(y);center=(n-1)/2;mean=statistics.mean(y)
    ss=sum((i-center)**2 for i in range(n));slope=sum((i-center)*(v-mean) for i,v in enumerate(y))/ss
    residual=sum((v-mean-slope*(i-center))**2 for i,v in enumerate(y))/(n-2)
    lower=slope-2*math.sqrt(residual/ss)
    gross=Decimal(str(math.expm1(max(-.5,min(.5,lower*6)))))
    fee=number(risk.DEFAULTS['fee_bps'] if fee_bps is None else fee_bps)/10000;slip=number(risk.DEFAULTS['slippage_bps'])/10000
    ask=number(book['asks'][0]['price'])*(1+number(fx_bps)/10000);bid=number(book['bids'][0]['price'])*(1-number(fx_bps)/10000)
    # Both FX markups are already in the CAD book. Round trip includes entry/exit fees.
    net=(bid*(1+gross)*(1-slip)*(1-fee))/(ask*(1+slip)*(1+fee))-1
    return {'passed':net>=minimum,'reason':'NET_EDGE_PASSED' if net>=minimum else 'NET_EDGE_TOO_SMALL','gross_scenario_return':str(gross),'net_scenario_return':str(net),'scenario_exit_per_unit_after_cost':str(bid*(1+gross)*(1-slip)*(1-fee)),'minimum_net_return':str(minimum),'horizon_hours':6,'model':'48-hour log trend minus 2 slope SE; 6-hour extrapolation, experimental and uncalibrated','fx_reference':book.get('fx_reference'),'fx_markup_bps':book.get('fx_markup_bps','0'),'fee_bps_each_way':str(fee*10000),'slippage_bps_each_way':risk.DEFAULTS['slippage_bps']}
