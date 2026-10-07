"""Hard limits: no LLM, strategy or browser payload can override these defaults."""
from decimal import Decimal
from .exchange import number
DEFAULTS={'single_order':'.02','asset_exposure':'.10','total_exposure':'.50','daily_deployment':'.10','daily_loss':'.03','drawdown':'.10','cash_reserve':'.50','trades_per_hour':6,'consecutive_losses':3,'cooldown_seconds':3600,'max_spread_bps':'50','max_age_seconds':15,'max_signal_deviation':'.03','fee_bps':'60','slippage_bps':'5'}

def assess(requested,side,context,limits=None,*,paper_observation=False):
    l=limits or DEFAULTS
    warnings=[]
    def result(status,reason,amount='0'):
        row={'status':status,'reason':reason,'approved_notional':str(amount),'requested_notional':str(requested)}
        if paper_observation:row.update(loss_policy='CONTINUOUS_PAPER_OBSERVATION',warnings=list(warnings))
        return row
    try:
        amount=number(requested);equity=number(context['equity']);cash=number(context['cash'])
        if amount<=0 or equity<=0:return result('BLOCK','INVALID_AMOUNT_OR_EQUITY')
        if context['halted']:return result('HALT','TRADING_HALTED')
        if context['age']< -5 or context['age']>l['max_age_seconds']:return result('HALT','STALE_MARKET_DATA')
        if number(context['daily_loss'])>=number(l['daily_loss']) or number(context['drawdown'])>=number(l['drawdown']):
            if not paper_observation:return result('HALT','LOSS_LIMIT')
            warnings.append('PAPER_LOSS_LIMIT_OBSERVED')
        if context['consecutive_losses']>=l['consecutive_losses']:
            if not paper_observation:return result('HALT','CONSECUTIVE_LOSSES')
            warnings.append('PAPER_CONSECUTIVE_LOSSES_OBSERVED')
        if number(context.get('signal_deviation','0'))>number(l['max_signal_deviation']):return result('BLOCK','SIGNAL_PRICE_DEVIATION')
        if number(context['spread_bps'])>number(l['max_spread_bps']):return result('BLOCK','WIDE_SPREAD')
        entry_only=paper_observation and l.get('entry_rate_only',False)
        if (side=='BUY' or not entry_only) and context.get('buys_hour' if entry_only else 'trades_hour',context['trades_hour'])>=l['trades_per_hour']:return result('BLOCK','TRADE_FREQUENCY_LIMIT')
        if not paper_observation and context['since_loss']<l['cooldown_seconds'] and side=='BUY':return result('BLOCK','LOSS_COOLDOWN')
        if side=='BUY':
            fee=1+number(l['fee_bps'])/10000
            ceiling=min(equity*number(l['single_order']),equity*number(l['asset_exposure'])-number(context['asset_value']),equity*number(l['total_exposure'])-number(context['invested']),equity*number(l['daily_deployment'])-number(context['deployed_today']),(cash-equity*number(l['cash_reserve']))/fee,cash/fee)
        # An unlevered sale reduces existing exposure. Applying the entry cap
        # leaves an unsellable sub-minimum remainder after a profitable move.
        elif side=='SELL':ceiling=number(context['asset_value'])
        else:return result('BLOCK','INVALID_SIDE')
        if ceiling<=0:return result('BLOCK','EXPOSURE_OR_CASH_LIMIT')
        return result('REDUCE' if ceiling<amount else 'PASS','HARD_LIMIT_REDUCED' if ceiling<amount else 'WITHIN_LIMITS',min(amount,ceiling))
    except Exception:return result('HALT','RISK_CALCULATION_FAILED')

def validate_order(product,book,quantity,notional,side,cash,held,assessment,now):
    if assessment['status'] not in ('PASS','REDUCE'):raise ValueError('RISK_NOT_APPROVED')
    if not product['tradable'] or book['product_id']!=product['product_id']:raise ValueError('MARKET_NOT_TRADABLE')
    if not -5<=now-book['timestamp']<=DEFAULTS['max_age_seconds']:raise ValueError('STALE_MARKET_DATA')
    if quantity<=0 or quantity%number(product['base_increment'])!=0:raise ValueError('INVALID_SIZE_INCREMENT')
    if not number(product['base_min_size'])<=quantity<=number(product['base_max_size']):raise ValueError('BASE_SIZE_LIMIT')
    if not number(product['quote_min_size'])<=notional<=number(product['quote_max_size']):raise ValueError('QUOTE_SIZE_LIMIT')
    if notional>number(assessment['approved_notional']):raise ValueError('APPROVED_AMOUNT_EXCEEDED')
    if side=='BUY' and notional*(1+number(DEFAULTS['fee_bps'])/10000)>cash:raise ValueError('INSUFFICIENT_FUNDS')
    if side=='SELL' and quantity>held:raise ValueError('INSUFFICIENT_POSITION')
