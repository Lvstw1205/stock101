"""Reuse the existing PAPER engine in isolated experiment ledgers, never LIVE."""
import copy,time
from decimal import Decimal
from ..multi import MultiEngine
from .. import execution_strategies as rules,performance_bonus
from ..exchange import ExchangeError,number
from . import specs

class CachedPublicMarket:
    def __init__(self,base,products):self.base=base;self.products=tuple(products)
    def get_products(self):return [p for p in self.base.discover() if p['product_id'] in self.products]
    def get_product(self,pid):
        if pid not in self.products:raise ExchangeError('QUANT_PRODUCT_OUTSIDE_SCOPE')
        return self.base.adapter.get_product(pid)
    def get_candles(self,pid,*args,**kwargs):
        row=self.base.intelligence.get(pid)
        if not row:raise ExchangeError('STALE_CANDLES')
        rate=float(self.base.adapter.fx()['rate']) if pid.endswith('-USD') else 1.
        factor=rate/float(row.get('fx_rate',rate))
        return [{**b,**{k:b[k]*factor for k in ('open','high','low','close')}} for b in row['bars']]
    def get_daily_candles(self,pid):return self.base.intelligence.get_daily(pid) or []
    def get_orderbook(self,pid):return self.base.adapter.get_orderbook(pid)
    def get_orderbooks(self,products):return self.base.adapter.get_orderbooks(products)
    def validate_book(self,book):return self.base.adapter.validate_book(book)
    def set_held_products(self,products):
        # Do not replace the primary portfolio's L2 subscriptions.
        pass

class ShadowEngine(MultiEngine):
    def __init__(self,path,base,spec):
        self.base_engine=base;self.spec=specs.validate(spec);self.research_evaluate=specs.evaluator(self.spec)
        super().__init__(path,CachedPublicMarket(base,self.spec['products']),rules.VERSION)
    def evaluate_signal(self,bars,pos,config,book):
        intel=self.base_engine.intelligence;pid=book['product_id']
        with self.repo.db() as c:
            fills=self.repo.rows(c,'fills',10000);anchor=self.repo.get(c,'cad_plan')['anchor']
        return self.research_evaluate(bars,pos,book,daily=intel.get_daily(pid),
            news=intel.news.context(intel.products.get(pid,{'base_currency_id':pid.split('-')[0]})),
            previous_price=(self.latest.get(pid,{}).get('plan') or {}).get('current'),fills=fills,anchor=anchor,
            benchmark=intel.bitcoin_context(book['timestamp']),fx_context=intel.fx_context(book['timestamp']))
    def validate_execution(self,side,quantity,notional,fee,cost,held,signal):
        super().validate_execution(side,quantity,notional,fee,cost,held,signal)
        if side=='BUY' and not rules.scheduled_dca(signal):
            proceeds=quantity*number(signal['net_edge']['scenario_exit_per_unit_after_cost'])
            if proceeds/(notional+fee)-1<number(self.spec['parameters']['minimum_net_return']):raise ValueError('QUANT_NET_EDGE_LOST_AT_FILL')

def observation(engine,phase,started_at):
    now=time.time()
    with engine.repo.db() as c:
        fills=engine.repo.rows(c,'fills',10000);last=engine.repo.rows(c,'trade_decisions',1)
        account=dict(c.execute("SELECT * FROM paper_accounts WHERE currency='CAD'").fetchone())
        bonus=performance_bonus.state(engine.repo,c,'paper',now)
        running=engine.repo.get(c,'multi_running') and not engine.repo.get(c,'halted')
        positions=engine.positions(c);snapshots=engine.repo.equity_series(c,'CAD')
    sales=[f for f in fills if f['side']=='SELL'];returns=[]
    for first,second in zip(reversed(snapshots[1:]),reversed(snapshots[:-1])):
        previous=number(first['equity']);value=number(second['equity'])-(number(second['initial'])-number(first['initial']))
        if previous>0:returns.append(dict(at=second['created'],value=float(value/previous-1)))
    net=bonus['evaluation'].get('net_assets_cad');age=now-(bonus.get('valuation_at') or 0)
    realized=sum((number(f['realized']) for f in sales),Decimal(0));fees=sum((number(f['fee']) for f in fills),Decimal(0))
    profit=number(net)-1000 if net is not None else None
    return dict(phase=phase,running=bool(running),as_of=now,started_at=started_at,days=max(0,(now-started_at)/86400),
        mark_age_seconds=age,fresh=net is not None and -5<=age<=15,net_assets_cad=net,
        net_profit_cad=str(profit) if profit is not None else None,return_ratio=str(profit/1000) if profit is not None else None,
        realized_cad=str(realized),fees_cad=str(fees),fills=len(fills),closed_exits=len(sales),
        positions=[{k:p.get(k) for k in ('product','quantity','cost')} for p in positions if number(p['quantity'])>0],
        cash=account['cash'],last_decision_at=last[0]['timestamp'] if last else None,
        last_reason=last[0]['risk']['reason'] if last else None,
        drawdown=last[0].get('risk_context',{}).get('drawdown') if last else None,
        return_series=returns[-2000:],halt_reason=engine.health.get('code') if engine._fatal else None,
        wallet='ISOLATED_RESEARCH_PAPER',real_orders=False,core_funds_used=False,
        cadence_seconds=15 if phase=='SHADOW_LIVE' else 60)
