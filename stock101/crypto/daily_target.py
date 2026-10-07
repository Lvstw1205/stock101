"""Observed daily net-liquidation target, never an order or profit guarantee."""
import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo
from .exchange import number

KEY='daily_net_target_v1'
ZONE=ZoneInfo('UTC')
TARGET=Decimal('.10')

def observe(repo,c,mode,now,net_assets,valuation_at,reward):
    if net_assets is None or not -5<=now-valuation_at<=15:return
    value=number(net_assets)-number(reward)
    if value<=0:return
    day=dt.datetime.fromtimestamp(valuation_at,ZONE).date().isoformat()
    saved=repo.get(c,KEY,{'days':{}});days=saved['days'];previous=saved.get('last')
    midnight=dt.datetime.fromisoformat(day).replace(tzinfo=ZONE).timestamp()
    row=days.get(day)
    if row is None:
        # Only a recent pre-midnight valuation establishes a full-day baseline.
        # Installing midday or returning after an outage is visibly partial.
        boundary=previous and 0<midnight-previous['at']<=120 and valuation_at-midnight<=120
        row=dict(date=day,started_at=previous['at'] if boundary else valuation_at,
                 start_net_cad=previous['value'] if boundary else str(value),partial_day=not bool(boundary))
        days[day]=row
    start=number(row['start_net_cad']);profit=value-start
    row.update(net_assets_cad=str(value),net_profit_cad=str(profit),return_ratio=str(profit/start),
               target_profit_cad=str(start*TARGET),target_ratio=str(TARGET),
               reached=profit>=start*TARGET,valuation_at=valuation_at,mode=mode)
    saved.update(days={d:days[d] for d in sorted(days)[-370:]},last={'value':str(value),'at':valuation_at})
    repo.set(c,KEY,saved)

def state(repo,c,mode,now):
    day=dt.datetime.fromtimestamp(now,ZONE).date().isoformat()
    row=repo.get(c,KEY,{'days':{}})['days'].get(day,{})
    stamp=row.get('valuation_at')
    return dict(row,mode=mode,date=day,target_ratio=str(TARGET),timezone=ZONE.key,
                fresh=stamp is not None and -5<=now-stamp<=15,
                guaranteed=False,basis='net_liquidation_after_costs_excluding_reward',
                status='TARGET_REACHED' if row.get('reached') else 'TRACKING' if row else 'WAITING_VALUATION')
