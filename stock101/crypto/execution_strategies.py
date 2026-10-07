"""Executable spot strategies sharing one budget and one owner per asset.

Targets are conditional exit levels, never forecasts or profit guarantees.
Only completed candles and a fresh order book enter the calculations. A filled
strategy token, stored atomically with the fill, prevents repeat installments.
"""
import math
import statistics
from decimal import Decimal
from . import planner,risk,net_edge,reference_strategies,market_strategies,fx_reserve,sizing,profit_policy
from .exchange import number

VERSION='multi_strategy_v5'
DCA_CAD=Decimal('2')
PROTECTIVE={'STRATEGY_STOP','PORTFOLIO_REBALANCE','REFERENCE_TREND_EXIT'}
CATALOG=[
 {'id':'dca_weekly','name':'BTC 주간 적립','interval':'주 1회','rule':'2 CAD 정기 매수 · 단기 예측 필터 제외 · 메이어 2.4 이상에서 분할 익절','scope':'BTC'},
 {'id':'ma200_swing','name':'200일선 추세 스윙','interval':'완료 일봉','rule':'가격·50일선이 200일선 위 · 지지선 반등 또는 최근 골든크로스 · ATR 방어','scope':'모든 후보'},
 {'id':'range','name':'박스권 매매','interval':'완료 1시간봉','rule':'횡보 구간 하단 반등 매수 · 상단 부근 익절 · 하단 이탈 방어','scope':'모든 후보'},
 {'id':'breakout','name':'변동성 돌파','interval':'UTC 일봉 + 1시간봉','rule':'당일 시가 + 전일 변동폭 × 0.5 돌파 · 다음 날 비용 차감 순이익이면 청산 · 손실 방어선 유지','scope':'모든 후보'},
 {'id':'dip_grid','name':'급락 분할매수','interval':'UTC 시가 기준 앱 감시','rule':'−10% / −12.5% / −15% · 단계별 1회 · 현재 호가가 지정 가격 이내일 때 IOC','scope':'모든 후보'},
 {'id':'mayer','name':'BTC 메이어 배수','interval':'완료 200일봉','rule':'배수 ≤0.8 주간 분할 매수 · ≥2.4 분할 익절','scope':'BTC'}]+reference_strategies.CATALOG+market_strategies.CATALOG+fx_reserve.CATALOG
NAMES={s['id']:s['name'] for s in CATALOG}
ENTRY_FIELDS=('target','invalidation','kind','evaluated_at','strategy_id','strategy_name','entry_token','exit_at','grid_anchor','entry_limit','exit_policy','sizing')

def entry_plan(signal):
    return {k:signal['plan'].get(k) for k in ENTRY_FIELDS if k in signal.get('plan',{})}

def scheduled_dca(signal):
    return signal.get('strategy_version')==VERSION and signal.get('strategy_id')=='dca_weekly' and 'DCA_WEEKLY_BUY' in signal.get('reason_codes',[]) and bool((signal.get('plan') or {}).get('entry_token'))

def atr(bars):
    return max(float(bars[-1]['close'])*.001,statistics.mean(max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(bars[-15:-1],bars[-14:])))

def completed(rows,step,now,minimum):
    rows=[b for b in (rows or []) if b['start']+step<=now]
    if len(rows)<minimum or rows[-1]['start']!=int(now)//step*step-step:return None
    if any(b['start']-a['start']!=step for a,b in zip(rows,rows[1:])):return None
    if any(not math.isfinite(float(b[k])) or float(b[k])<=0 for b in rows for k in ('open','high','low','close')):return None
    return rows

def evaluate(bars,position,book,*,daily=None,news=None,previous_price=None,fills=(),anchor=0,fee_bps=None,fx_bps=0,minimum=net_edge.MIN_NET,benchmark=None,fx_context=None,only_strategy=None,evidence=None,slippage_bps=None,cad_rate=1,cost_basis=None):
    now=float(book['timestamp']);day=int(now)//86400*86400;week=max(0,int((now-anchor)//604800))
    pid=book['product_id'];btc=pid.split('-')[0]=='BTC';mid=float(book['mid'])
    hour=completed(bars,3600,now,50);days=completed(daily,86400,now,15)
    long=completed(daily,86400,now,201)
    ma200=statistics.mean(b['close'] for b in long[-200:]) if long else None
    multiple=mid/ma200 if ma200 else None
    a=atr(hour) if hour else mid*.02;day_atr=atr(days) if days else a
    today_open=next((b['open'] for b in (hour or []) if b['start']==day),None)
    fee=number(risk.DEFAULTS['fee_bps'] if fee_bps is None else fee_bps)/10000
    slip=number(risk.DEFAULTS['slippage_bps'] if slippage_bps is None else slippage_bps)/10000;fx=number(fx_bps)/10000
    ask=number(book['asks'][0]['price'])*(1+fx);bid=number(book['bids'][0]['price'])*(1-fx)
    entry=ask*(1+slip)*(1+fee);exit_now=bid*(1-slip)*(1-fee)
    costs=1-exit_now/entry;minimum=number(minimum)
    news=news or {'status':'NOT_CHECKED','allocation_multiplier':1,'articles':[]}
    allocation=.02*float(news.get('allocation_multiplier',1))
    held=bool(position and number(position.get('quantity',0))>0)
    original=(position or {}).get('entry_plan') or {};owner=original.get('strategy_id') if held else None
    history=[f for f in fills if f.get('product')==pid and number(f.get('quantity',0))>0]
    bought={f.get('strategy_token') for f in history if f['side']=='BUY'}
    sold_today={f.get('strategy_id') for f in history if f['side']=='SELL' and f.get('created',0)>=day}
    # LIVE supplies the historical CAD basis including entry FX costs. Keep
    # that basis in CAD; do not overwrite it with the quote-currency cost.
    basis=number(cost_basis) if cost_basis is not None else (None if held and 'cost_cad' in position else number(position['cost']) if held else Decimal(0))
    current_net=(number(position['quantity'])*exit_now*number(cad_rate)/basis-1) if held and basis is not None and basis>0 else None
    profitable=current_net is not None and current_net>=minimum
    candidates=[]
    improving=mid>=float(previous_price if previous_price is not None else (hour[-1]['close'] if hour else mid))*(1-1e-12)

    def candidate(sid,reason,**extra):
        row={'strategy_id':sid,'name':NAMES[sid],'decision':'WAIT','reason':reason,'target':None,'invalidation':None,
             'entry_low':None,'entry_high':None,'allocation':allocation,'sell_fraction':0,'facts':{},**extra}
        candidates.append(row);return row
    def buy(row,token,target,stop,low=None,high=None):
        row.update(target=target,invalidation=stop,entry_low=low,entry_high=high,entry_token=token)
        if token in bought:row['reason']='INSTALLMENT_ALREADY_FILLED';return
        if target is not None:
            proceeds=number(target)*(bid/number(mid))*(1-slip)*(1-fee)
            row['net_at_target']=str(proceeds/entry-1)
            if proceeds/entry-1<minimum:row['reason']='TARGET_NET_TOO_SMALL';return
        row['decision']='BUY'

    r=candidate('dca_weekly','BTC_ONLY' if not btc else 'DCA_WEEKLY_BUY',allocation=.02,
                facts={'weekly_cad':str(DCA_CAD),'period':week,'max_cost_loss_pct':3,'short_term_forecast_required':False})
    if btc:
        if costs>Decimal('.03'):r['reason']='DCA_COST_LIMIT'
        else:buy(r,f'dca:{week}',None,None)

    r=candidate('ma200_swing','DAILY_200_HISTORY_REQUIRED')
    if long:
        close=[b['close'] for b in long];ma50=statistics.mean(close[-50:]);ma20=statistics.mean(close[-20:])
        support=max(ma200,min(b['low'] for b in long[-20:]));recent_cross=False
        for i in range(max(200,len(close)-10),len(close)):
            if statistics.mean(close[i-50:i])<=statistics.mean(close[i-200:i]) and statistics.mean(close[i-49:i+1])>statistics.mean(close[i-199:i+1]):recent_cross=True
        bounce=mid>=support and mid<=max(support,ma20)+.5*day_atr and min(b['low'] for b in long[-3:])<=max(support,ma20)+day_atr
        r['facts']={'ma200':ma200,'ma50':ma50,'ma20':ma20,'atr':day_atr,'support':support,'recent_golden_cross':recent_cross}
        r.update(reason='ABOVE_200MA_REQUIRED',entry_low=support,entry_high=max(support,ma20)+.5*day_atr)
        if mid>ma200 and ma50>ma200:
            r['reason']='TREND_SUPPORT_BOUNCE_REQUIRED'
            if (bounce or recent_cross) and improving:
                stop=max(support-day_atr,mid-2*day_atr);target=max(b['high'] for b in long[-20:])
                target=max(target,mid+2*day_atr)
                r['reason']='MA200_SWING_BUY';buy(r,f'ma200:{day}',target,stop,support,max(support,ma20)+.5*day_atr)

    r=candidate('range','HOURLY_HISTORY_REQUIRED')
    if hour:
        support=min(b['low'] for b in hour[-24:]);resistance=max(b['high'] for b in hour[-24:]);width=resistance-support
        ma20=statistics.mean(b['close'] for b in hour[-20:]);ma50=statistics.mean(b['close'] for b in hour[-50:])
        flat=abs(ma20/ma50-1)<=.02 and width/ma20<=.20+1e-12
        r.update(reason='RANGE_NOT_ESTABLISHED',entry_low=support,entry_high=support+.25*width,
                 facts={'support':support,'resistance':resistance,'atr':a,'flat':flat})
        if flat:
            r['reason']='RANGE_LOWER_BAND_REQUIRED'
            if support<=mid<=support+.25*width:
                r['reason']='PRICE_RECOVERY_REQUIRED'
                if improving:
                    r['reason']='RANGE_BUY';buy(r,f'range:{hour[-1]["start"]}',resistance-.1*width,support-a,support,support+.25*width)

    r=candidate('breakout','DAILY_HISTORY_REQUIRED')
    if days:
        r['reason']='UTC_SESSION_OPEN_REQUIRED'
        if today_open:
            trigger=today_open+(days[-1]['high']-days[-1]['low'])*.5
            r.update(reason='BREAKOUT_PRICE_REQUIRED',entry_low=trigger,entry_high=trigger+.5*day_atr,
                     exit_at=day+86400,facts={'utc_open':today_open,'prior_range':days[-1]['high']-days[-1]['low'],'k':.5,'trigger':trigger})
            if trigger<=mid<=trigger+.5*day_atr:
                r['reason']='BREAKOUT_BUY';buy(r,f'breakout:{day}',trigger+2*day_atr,trigger-day_atr,trigger,trigger+.5*day_atr)

    r=candidate('dip_grid','UTC_SESSION_OPEN_REQUIRED')
    if today_open:
        anchor_price=float(original.get('grid_anchor') or today_open) if owner=='dip_grid' else today_open
        levels=[anchor_price*(1-x) for x in (.10,.125,.15)]
        r.update(reason='DIP_LIMIT_NOT_REACHED',grid_anchor=anchor_price,entry_low=levels[-1],entry_high=levels[0],
                 facts={'anchor':anchor_price,'anchor_basis':'UTC opening hour; completed candle','levels':levels,'execution':'app monitored limit IOC; not exchange resting GTC'})
        for i,limit in reversed(list(enumerate(levels))):
            token=f'grid:{int(original.get("evaluated_at",day))//86400*86400 if owner=="dip_grid" else day}:{i}'
            if ask*(1+slip)<=number(limit) and token not in bought:
                r.update(reason='DIP_GRID_BUY',entry_limit=limit)
                buy(r,token,mid*1.07,mid*.95,levels[-1],levels[0]);break

    r=candidate('mayer','BTC_ONLY' if not btc else 'DAILY_200_HISTORY_REQUIRED')
    if btc and long:
        r.update(reason='MAYER_UNDER_08_REQUIRED',facts={'multiple':multiple,'ma200':ma200,'entry_multiple':.8,'exit_multiple':2.4})
        if multiple<=.8:
            r['reason']='MAYER_ACCUMULATE';buy(r,f'mayer:{week}',ma200*2.4,None)

    for raw in reference_strategies.evaluate(hour,days,now,mid,pid,benchmark=benchmark,fills=fills,anchor=anchor)+market_strategies.evaluate(hour,now,mid)+[fx_reserve.evaluate(book,fx_context)]:
        spec=dict(raw);sid=spec.pop('strategy_id');reason=spec.pop('reason');entry_signal=spec.pop('entry')
        r=candidate(sid,reason,**spec)
        if entry_signal:buy(r,r['entry_token'],r['target'],r['invalidation'],r.get('entry_low'),r.get('entry_high'))

    if pid.startswith('USDC-'):
        for row in candidates:
            if row['strategy_id']!='fx_usdc' and row['strategy_id']!=owner:row.update(decision='WAIT',reason='USDC_FX_STRATEGY_ONLY')
    if only_strategy is not None:
        if only_strategy not in NAMES:raise ValueError('UNKNOWN_STRATEGY')
        for row in candidates:
            if row['strategy_id']!=only_strategy:row.update(decision='WAIT',reason='RESEARCH_OTHER_STRATEGY')

    def manage_profit(signal):
        return profit_policy.apply(signal,hour,position,book,fee_bps=fee*10000,slippage_bps=slip*10000,
                                   fx_bps=fx*10000,cad_rate=cad_rate,cost_basis=basis)

    # Preserve the entry owner and its protections; common profit management
    # may secure cash when its trend/resistance evidence supports an exit.
    if held and owner in NAMES:
        chosen=next(r for r in candidates if r['strategy_id']==owner)
        stop=original.get('invalidation');target=original.get('target')
        exit_at=original.get('exit_at')
        if position.get('overweight'):
            chosen.update(decision='REDUCE',reason='PORTFOLIO_REBALANCE',sell_fraction=.25)
        elif stop is not None and mid<=float(stop):
            chosen.update(decision='SELL',reason='STRATEGY_STOP',sell_fraction=1)
        elif owner=='breakout' and exit_at and now>=exit_at:
            if current_net is not None and current_net>0:
                chosen.update(decision='SELL',reason='BREAKOUT_NET_SESSION_EXIT',sell_fraction=1)
            else:
                chosen.update(decision='HOLD',reason='BREAKOUT_COST_RECOVERY_HOLD',sell_fraction=0)
        elif owner in (reference_strategies.TREND_IDS|{'volume_breakout'}) and chosen.get('exit_signal'):
            chosen.update(decision='SELL',reason='REFERENCE_TREND_EXIT',sell_fraction=1)
        elif owner=='rsi_reversion' and chosen.get('profit_exit') and profitable:
            chosen.update(decision='SELL',reason='RSI_RECOVERY_EXIT',sell_fraction=1)
        elif owner in ('dca_weekly','mayer') and multiple is not None and multiple>=2.4 and profitable:
            if owner in sold_today:chosen.update(decision='HOLD',reason='LONG_TERM_EXIT_TODAY_DONE')
            else:chosen.update(decision='SELL',reason='MAYER_PARTIAL_EXIT' if not position.get('profit_taken') else 'MAYER_FINAL_EXIT',sell_fraction=.5 if not position.get('profit_taken') else 1)
        elif owner not in ('dca_weekly','mayer') and target is not None and mid>=float(target) and profitable:
            chosen.update(decision='SELL',reason='STRATEGY_TARGET',sell_fraction=1)
        elif owner not in ('dca_weekly','mayer','dip_grid','red_day','fx_usdc') or chosen['decision']!='BUY':
            chosen.update(decision='HOLD',reason='STRATEGY_HOLD')
        chosen.update(target=target,invalidation=stop)
        if owner in ('dca_weekly','mayer'):
            chosen['facts'].update(mayer_multiple=multiple,exit_multiple=2.4)
        for row in candidates:
            if row is not chosen:row.update(would_decide=row['decision'],decision='WAIT',reason='OTHER_STRATEGY_OWNS_POSITION')
    elif held:
        # Existing v2 holdings retain the exact previously stored protection.
        legacy=planner.evaluate(bars,position,book,news=news,previous_price=previous_price,fee_bps=fee_bps,fx_bps=fx_bps,minimum=minimum)
        legacy.update(strategy_candidates=candidates,strategy_id='legacy_plan',strategy_version=VERSION)
        return manage_profit(legacy)
    else:
        priority={sid:i for i,sid in enumerate(('fx_usdc','mayer','volume_breakout','bollinger_reentry','ma200_swing','supertrend','ichimoku','hull','macd_trend','rsi_cross','rsi_reversion','breakout','range','red_day','dip_grid','dca_weekly'))}
        buys=sorted([r for r in candidates if r['decision']=='BUY'],key=lambda r:priority[r['strategy_id']])
        chosen=buys[0] if buys else next((r for r in candidates if r['strategy_id']==(only_strategy or ('fx_usdc' if pid.startswith('USDC-') else 'range'))),candidates[0])

    sid=chosen['strategy_id'];target=chosen['target']
    target_exit=number(target)*(bid/number(mid))*(1-slip)*(1-fee) if target is not None else None
    net=target_exit/entry-1 if target_exit is not None else None
    plan={'kind':sid,'strategy_id':sid,'strategy_name':NAMES[sid],'currency':book.get('valuation_currency') or pid.split('-')[-1],
          'current':mid,'reference_price':mid,'target':target,'invalidation':chosen['invalidation'],
          'entry_low':chosen['entry_low'],'entry_high':chosen['entry_high'],
          'cost_entry_ceiling':float(number(target)/(1+minimum)/(entry/number(mid))*(bid/number(mid))*(1-slip)*(1-fee)) if target is not None else None,
          'evaluated_at':now,'entry_token':chosen.get('entry_token'),'entry_limit':chosen.get('entry_limit'),
          'exit_at':chosen.get('exit_at') or original.get('exit_at'),'grid_anchor':chosen.get('grid_anchor') or original.get('grid_anchor'),
          'exit_policy':'MAYER_24_PARTIAL' if sid in ('dca_weekly','mayer') else 'TARGET_OR_STOP_OR_NET_SESSION' if sid=='breakout' else 'TARGET_OR_STOP_OR_SESSION',
          'news':news,'net_center':str(net) if net is not None else None,'net_low':None,'net_high':None,
          'position_net_exit':str(current_net) if current_net is not None else None,'facts':chosen['facts'],
          'scenario_note':'Conditional target proceeds, not a forecast. DCA has no short-term return forecast.'}
    reason=[chosen['reason']]
    if chosen['reason']=='MAYER_PARTIAL_EXIT':reason.append('PARTIAL_PROFIT_TAKING')
    signal={'decision':chosen['decision'],'reason_codes':reason,'strategy_id':sid,'strategy_version':VERSION,
            'allocation':chosen['allocation'],'sell_fraction':chosen['sell_fraction'],'plan':plan,
            'strategy_candidates':candidates,'regime':{'name':sid},'signals':[],
            'net_edge':{'passed':net is not None and net>=minimum,'reason':'SCHEDULED_DCA_COST_CHECK' if sid=='dca_weekly' else 'TARGET_COST_CHECK',
                        'net_scenario_return':str(net) if net is not None else None,'scenario_exit_per_unit_after_cost':str(target_exit) if target_exit is not None else None,
                        'immediate_exit_per_unit_after_cost':str(exit_now),'estimated_round_trip_cost':str(costs),
                        'minimum_net_return':str(minimum),'horizon_hours':None,'model':'strategy_exit_target'}}
    if sid=='dca_weekly':signal['requested_cash_cad']=str(DCA_CAD)
    return manage_profit(sizing.apply(signal,hour,book,evidence=(evidence or {}).get(sid)))
