"""User's performance criterion; a recorded evaluation, never a delete command."""
from decimal import Decimal
from .budget_policy import INITIAL_CAD
from .exchange import number

KEY = 'stock101_evaluation_policy_v1'
MARK_KEY = KEY + '_mark'
BREACH_KEY = KEY + '_breach'
LOSS_LIMIT = Decimal('.10')


def description():
    return {'policy_version': KEY,
            'user_statement': '손실이 10프로 이상이면 Stock101의 존재를 없애버릴 것',
            'label': '총손실 10% 이상이면 운용 실패 · 폐기 검토',
            'principal_cad': str(INITIAL_CAD), 'loss_limit_ratio': str(LOSS_LIMIT),
            'loss_limit_cad': str(INITIAL_CAD * LOSS_LIMIT),
            'net_asset_floor_cad': str(INITIAL_CAD * (1-LOSS_LIMIT)),
            'basis': 'net_liquidation_after_costs_excluding_funding_rewards',
            'paper_continues': True, 'automatic_deletion': False,
            'instruction': 'Evaluate the Stock101-managed portfolio against its fixed 1000 CAD principal. '
                'Total loss of 100 CAD or more after entry/exit fees, spread, slippage and FX costs meets '
                'the user\'s failure criterion for reviewing discontinuation. Include open-position losses. '
                'Exclude deposits and the 20 CAD reward from trading returns. Record losses honestly. '
                'Continue PAPER observation after losses; this criterion does not authorize file deletion, '
                'risk-limit overrides, forced trades, loss chasing, or guaranteed-return claims. LIVE controls remain unchanged.'}


def observe(repo, c, mode, now, net_assets, valuation_at, funding_reward):
    if repo.get(c, KEY) is None:
        repo.set(c, KEY, description())
    net = number(net_assets) - number(funding_reward) if net_assets is not None else None
    profit = net - INITIAL_CAD if net is not None else None
    mark = {'mode': mode, 'net_assets_cad': str(net) if net is not None else None,
            'excluded_reward_cad': str(funding_reward),
            'net_profit_cad': str(profit) if profit is not None else None,
            'loss_ratio': str(max(Decimal(0), -profit / INITIAL_CAD)) if profit is not None else None,
            'valuation_at': valuation_at, 'observed_at': now}
    repo.set(c, MARK_KEY, mark)
    if (profit is not None and -5 <= now-valuation_at <= 15
            and profit <= -INITIAL_CAD * LOSS_LIMIT and repo.get(c, BREACH_KEY) is None):
        breach = {**mark, 'breached_at': now}
        repo.set(c, BREACH_KEY, breach)
        repo.put(c, 'system_events', BREACH_KEY, {'category': 'evaluation',
                 'code': 'TOTAL_LOSS_TEN_PERCENT_REACHED', **breach})


def state(repo, c, mode, now):
    mark = repo.get(c, MARK_KEY, {})
    breach = repo.get(c, BREACH_KEY)
    stamp = mark.get('valuation_at')
    fresh = mark.get('net_assets_cad') is not None and stamp is not None and -5 <= now-stamp <= 15
    return {**description(), **mark, 'mode': mode, 'fresh': fresh,
            'status': 'FAILED' if breach else 'WITHIN_LIMIT' if fresh else 'VALUATION_PENDING',
            'breached': bool(breach), 'first_breach': breach}
