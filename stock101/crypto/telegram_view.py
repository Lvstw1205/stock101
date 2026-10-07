"""Read-only, bounded Telegram views of the bot's existing ledger.

No market request, credential lookup, order submission or mode change occurs
here. Prices are labelled with the time of their stored valuation snapshot.
"""
import datetime as dt
import json
import time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from .execution_strategies import CATALOG, NAMES
from . import performance_bonus

ZONE=ZoneInfo('UTC')
VIEWS={'status','positions','fills','strategies'}
MODES={'paper','live'}
REASONS={
 'USER_KILL_SWITCH':'사용자가 중지함','USER_STOP':'사용자가 중지함',
 'STARTUP_RECONCILIATION_REQUIRED':'Mac 앱 재시작 후 시작 대기','STARTUP_CONFIRMATION_REQUIRED':'실제매매 시작 확인 대기',
 'PORTFOLIO_MARK_FAILED':'보유 자산 평가 확인 필요','STALE_MARKET_DATA':'최신 호가 확인 필요',
 'EXECUTION_OR_DATABASE_FAILURE':'실행 또는 거래 기록 오류','AUTOMATION_CYCLE_FAILED':'자동매매 처리 오류',
 'LOSS_LIMIT':'설정된 손실 한도 도달','CONSECUTIVE_LOSSES':'연속 손실 한도 도달',
 'LOSS_COOLDOWN':'손실 후 신규 매수 대기',
 'BREAKOUT_NET_SESSION_EXIT':'돌파 전략 다음 날 비용 차감 순이익 청산','BREAKOUT_COST_RECOVERY_HOLD':'돌파 전략 비용 회수 대기 · 손실 방어선 감시',
 'STRATEGY_HOLD':'매수한 전략의 청산 조건을 감시하며 보유','DCA_WEEKLY_BUY':'이번 주 BTC 정기 매수 조건',
 'MA200_SWING_BUY':'200일선 추세 진입 조건','BREAKOUT_BUY':'변동성 돌파 조건','RANGE_BUY':'박스권 하단 반등 조건',
 'VOLUME_BREAKOUT_BUY':'거래량 확인 돌파','BOLLINGER_REENTRY_BUY':'볼린저 복귀 조건','FX_USDC_BUY':'USD/CAD 하락 · USDC 적립',
 'FX_DISCOUNT_REQUIRED':'USD/CAD 할인 조건 확인','CAD_FUNDING_ROUTE_REQUIRED':'CAD 결제 경로 확인 필요','USDC_ONLY':'USDC 전용 전략',
 'DIP_GRID_BUY':'급락 분할 매수 단계','MAYER_ACCUMULATE':'메이어 분할 매수 조건',
 'STRATEGY_STOP':'손실 방어선 도달','STRATEGY_TARGET':'전략 목표 도달','BREAKOUT_SESSION_EXIT':'돌파 전략 UTC 날짜 변경 청산',
 'MAYER_PARTIAL_EXIT':'메이어 분할 익절','MAYER_FINAL_EXIT':'메이어 잔여 보유분 익절',
 'EXPOSURE_OR_CASH_LIMIT':'신규 매수 예산·보유 한도 도달','QUOTE_SIZE_LIMIT':'주문 예산이 거래소 최소액보다 작음',
 'INVALID_SIZE_INCREMENT':'현재 주문액으로 거래 가능한 수량 부족','TRADE_FREQUENCY_LIMIT':'시간당 체결 한도 도달',
 'TARGET_NET_TOO_SMALL':'전략 목표의 비용 차감 수익 부족','NO_EXECUTABLE_SIGNAL':'전략 조건 감시 중',
 'RANGE_NOT_ESTABLISHED':'지지·저항 구간 확인 중','RANGE_LOWER_BAND_REQUIRED':'박스권 하단 진입 감시',
 'DAILY_200_HISTORY_REQUIRED':'200일선 계산용 일봉 수집 중','BTC_ONLY':'BTC 전용 전략',
 'ORDER_STATUS_UNVERIFIED':'거래소 주문 결과 대조 중','ORDER_PREVIEW_REJECTED':'거래소 주문 미리보기 거절',
}

def amount(value,places=2):
    if value is None:return '조회 전'
    try:
        v=Decimal(str(value))
        return f'{v:,.{places}f}' if v.is_finite() else '조회 불가'
    except (InvalidOperation,ValueError,TypeError):return '조회 불가'

def stamp(value):
    return dt.datetime.fromtimestamp(value,ZONE).strftime('%m/%d %H:%M:%S') if value else '기록 없음'

def name(sid):return NAMES.get(sid,'기존 매매 계획')

def keyboard(mode,view='status'):
    def button(text,target):return {'text':text,'callback_data':f'st101:{mode}:{target}'}
    other='live' if mode=='paper' else 'paper'
    return {'inline_keyboard':[
        [button('📊 상태·손익','status'),button('🪙 보유 코인','positions')],
        [button('🧾 최근 체결','fills'),button('🧭 실행 전략','strategies')],
        [button('🔄 새로고침',view),{'text':'실제매매 보기' if other=='live' else '모의매매 보기','callback_data':f'st101:{other}:status'}]]}

def snapshot(engine,mode,now):
    if mode not in MODES:raise ValueError('지원하지 않는 조회 모드입니다.')
    repo=engine.repo
    with repo.db() as c:
        c.execute('BEGIN')
        # One consistent read transaction; no Repository constructor or writes.
        positions=[p for p in repo.rows(c,'positions',10000) if Decimal(p['quantity'])>0]
        fills=repo.rows(c,'fills',8)
        totals=Decimal(0);count=0
        for row in c.execute('SELECT payload FROM fills'):
            f=json.loads(row[0]);totals+=Decimal(f.get('realized' if mode=='paper' else 'realized_cad','0'));count+=1
        marks=repo.rows(c,'portfolio_snapshots',1);mark=marks[0] if marks else {}
        decisions=repo.rows(c,'trade_decisions',1);decision=decisions[0] if decisions else {}
        bonus=performance_bonus.state(repo,c,mode,now)
        if mode=='paper':
            account=c.execute("SELECT cash,initial FROM paper_accounts WHERE currency='CAD'").fetchone()
            running=bool(repo.get(c,'multi_running',False)) and not repo.get(c,'halted') and not engine._fatal
            reason=repo.get(c,'halt_reason');equity=mark.get('equity');cash=account['cash'] if account else None
            capital=account['initial'] if account else None;unrealized=mark.get('unrealized')
            completed=engine.last_cycle_completed_at;recovery={**engine.recovery,'requested':engine._resume_requested}
        else:
            running=bool(repo.get(c,'live_running',False)) and not engine.stop_event.is_set()
            reason=repo.get(c,'live_reason');equity=mark.get('equity_cad');cash=mark.get('available_cad')
            capital=bonus['principal_cad']
            unrealized=str(Decimal(mark['pnl_cad'])-Decimal(mark['realized_cad'])) if 'pnl_cad' in mark else None
            completed=engine.last_completed;recovery={}
        row_signal=decision.get('analysis',decision.get('signal',{}));risk=decision.get('risk',{})
        why=risk.get('reason') if risk.get('reason') not in (None,'NO_EXECUTABLE_SIGNAL','WITHIN_LIMITS','HARD_LIMIT_REDUCED') else next(iter(row_signal.get('reason_codes',[])),None)
        owners={p['product']:p for p in positions}
        for p in mark.get('positions',[]):
            if p['product'] in owners:
                # Only a snapshot matching current tracked size may label its value.
                if Decimal(p['quantity'])==Decimal(owners[p['product']]['quantity']):owners[p['product']].update({k:p[k] for k in ('market_value','unrealized','value_cad') if k in p})
    return dict(mode=mode,running=running,reason=reason,equity=equity,cash=cash,capital=capital,realized=str(totals),
                unrealized=unrealized,mark_status=mark.get('mark_status'),unavailable_products=mark.get('unavailable_products',[]),marked_at=mark.get('created'),completed_at=completed,
                last_assessment_at=decision.get('timestamp'),last_product=decision.get('product'),last_reason=why,
                positions=positions,fills=fills,fill_count=count,now=now,performance_bonus=bonus,recovery=recovery,
                loss_observation=paper_observation(engine,mode))

def paper_observation(engine,mode):
    return mode=='paper' and bool(getattr(engine,'observe_paper_losses',lambda:False)())

def render(data,view='status'):
    if view not in VIEWS:raise ValueError('지원하지 않는 조회 화면입니다.')
    mode=data['mode'];paper=mode=='paper';now=data['now']
    heading='🟦 Stock101 · 모의매매 (가상 자금)' if paper else '🟥 Stock101 · 실제매매 (Coinbase 자금)'
    lines=[heading]
    if data['running']:
        active=data.get('completed_at')
        state='실행 중' if active and now-active<=90 else '처리 확인 중 · 마지막 판단 시각 확인'
    else:state='자동 복구 중' if paper and data.get('recovery',{}).get('requested') and data['recovery'].get('status')=='RECOVERING' else '중지됨' if paper else '비활성'
    lines+=['실행 상태: '+state]
    if data.get('mark_status')=='PARTIAL':lines+=['일부 지난 시세 포함: '+', '.join(data.get('unavailable_products',[]))+' · 총자산·평가손익은 참고값', '다른 종목 분석·매도 관리 계속 · 신규 매수는 전체 시세 확인까지 대기']
    if paper and data.get('loss_observation'):lines+=['모의 검증 계속: 손실은 기록하며 매매 판단 유지 · 실제매매 손실 중단 기준은 별도']
    if state=='자동 복구 중':lines+=['다음 원장·시세 확인: '+stamp(data['recovery'].get('next_retry_at'))]
    if not data['running'] and data.get('reason'):lines+=['사유: '+REASONS.get(data['reason'],'상세 원인 확인 필요')]
    if view=='status':
        lines+=['',('총 가상 자산: ' if paper else '프로그램 자산 · CAD 참고값: ')+amount(data['equity'])+' CAD',
                ('가상 현금: ' if paper else '사용 가능 자금 · CAD 참고값: ')+amount(data['cash'])+' CAD',
                '확정 손익: '+amount(data['realized'])+' CAD','평가 손익: '+amount(data['unrealized'])+' CAD',
                ('누적 가상 입금: ' if paper else '현재 예산 한도: ')+amount(data['capital'])+' CAD',
                f"보유 {len(data['positions'])}개 코인 · {'모의' if paper else '실제'} 체결 {data['fill_count']}건",'',
                '마지막 판단: '+stamp(data.get('last_assessment_at'))]
        if data.get('last_product'):lines+=[data['last_product']+' · '+REASONS.get(data.get('last_reason'),'전략 조건·예산 확인')]
        bonus=data.get('performance_bonus',{})
        lines+=['예산: 총 원금 1,000 CAD · 정기 추가 납입 없음']
        if bonus:
            ratio=Decimal(bonus['return_ratio'])*100 if bonus.get('return_ratio') is not None else None
            lines+=['성과 보너스: '+('20 CAD 1회 적용 완료' if bonus['earned'] else '누적 실현 순이익 '+amount(bonus.get('net_profit_cad'))+' / 200 CAD'),
                    '기준: 모든 비용 차감 실현 순이익 '+amount(ratio)+'% / 목표 20%',
                    '평가이익·입금 제외 · 200 CAD 이상이면 20 CAD 한 번만']
            if not paper:lines+=['실제 성과는 거래 수수료와 설정한 환전 비용 여유를 차감합니다.']
        if data['positions']:lines+=['보유 코인: '+', '.join(p['product'] for p in data['positions'][:5])]
        evaluation=bonus.get('evaluation',{})
        if evaluation:
            lines+=['운용 평가: 총손실 100 CAD(10%) 이상이면 실패 · 폐기 검토',
                    '비용 차감 총손익: '+amount(evaluation.get('net_profit_cad'))+' CAD · '+stamp(evaluation.get('valuation_at')),
                    '실패 기준 도달 기록 있음' if evaluation.get('breached') else '현재 한도 이내' if evaluation.get('fresh') else '최신 평가 대기']
            if paper:lines+=['모의매매는 손실도 계속 기록합니다.']
        if data['fills']:
            f=data['fills'][0];lines+=['최근 체결: '+stamp(f['created'])+' '+f['product']+' '+('매수' if f['side']=='BUY' else '매도')]
        lines+=['유동 매수 · 현금 40% 유지 / 투자 최대 60%','코인당 최대 10% · BTC 정기매수 주 2 CAD']
        if not paper:lines+=['예산 한도는 Coinbase 잔액이 아닙니다. 자동 입금·환전 없음.']
    elif view=='positions':
        lines+=['','🪙 보유 코인']
        for p in data['positions'][:10]:
            value=p.get('market_value' if paper else 'value_cad')
            pnl=p.get('unrealized') if paper else str(Decimal(value)-Decimal(p.get('cost_cad','0'))) if value is not None else None
            lines +=[f"• {p['product']} · {name((p.get('entry_plan') or {}).get('strategy_id'))}",
                     f"  수량 {amount(p['quantity'],8)} · 평가 {amount(value)} CAD · 손익 {amount(pnl)} CAD"]
        if not data['positions']:lines+=['이 프로그램이 보유한 코인이 없습니다.']
        if len(data['positions'])>10:lines+=[f"외 {len(data['positions'])-10}개 · 전체 목록은 Mac에서 확인"]
        lines+=['매수 직후 비용 때문에 평가손익이 음수일 수 있습니다.']
    elif view=='fills':
        lines+=['',f"🧾 최근 {'모의' if paper else '실제'} 체결 · 전체 {data['fill_count']}건"]
        for f in data['fills']:
            quote=f.get('currency' if paper else 'quote','CAD' if paper else '')
            value=Decimal(f['notional'])+(Decimal(f['fee']) if f['side']=='BUY' else -Decimal(f['fee']))
            lines+=[f"• {stamp(f['created'])} {f['product']} {'매수' if f['side']=='BUY' else '매도'}",f"  {name(f.get('strategy_id'))} · {'총 지출' if f['side']=='BUY' else '순 수령'} {amount(value)} {quote} (수수료 포함)"]
        if not data['fills']:lines+=['체결 기록이 없습니다. 주문 요청과 확정 체결은 다릅니다.']
    elif view=='strategies':
        lines+=['',f'🧭 적용된 실행 전략 · {len(CATALOG)}개']
        for rule in CATALOG:lines+=[rule['name']+' · '+rule['scope'],rule['rule']]
        lines+=['','같은 코인은 매수한 전략이 보유·청산을 관리합니다.','목표 가격은 수익 보장이 아닙니다. 모든 전략에 예산·비용·위험 한도가 적용됩니다.']
    lines+=['','평가 기준: '+stamp(data.get('marked_at'))+' · 설정한 지역 시간']
    if not data.get('marked_at') or not -5<=now-data['marked_at']<=120:lines+=['⚠️ 최신 평가액을 확인하지 못했습니다. 표시된 값은 마지막 기록입니다.']
    lines+=['조회 전용 · 버튼으로 갱신 · Mac의 AIRI가 실행 중이어야 합니다.']
    text='\n'.join(lines)
    if len(text.encode('utf-16-le'))//2>3400:raise ValueError('요약이 길어졌습니다. Mac에서 상세 기록을 확인해 주세요.')
    return {'text':text,'reply_markup':keyboard(mode,view),'protect_content':True,'mode':mode,'view':view,'snapshot_at':now}

def response(engine,mode='paper',view='status',now=None):
    if mode not in MODES or view not in VIEWS:raise ValueError('지원하지 않는 조회 화면입니다.')
    return render(snapshot(engine,mode,time.time() if now is None else now),view)
