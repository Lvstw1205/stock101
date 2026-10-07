# Architecture and engineering walkthrough

## End-to-end flow

1. Market-data adapters collect product metadata, candles, quotes and FX inputs. Timestamps
   and availability matter: missing or stale evidence is not a valid trade signal.
2. Strategy and research modules evaluate candidates. Estimated profitability is a model
   assumption, not a verified future return.
3. Cost, sizing and budget checks precede execution. PAPER simulates fills with configured
   assumptions. The separately gated LIVE path uses the user's exchange account and needs
   explicit configuration; the release starts with live execution disabled.
4. Orders, fills, positions and runtime records are persisted. LIVE reconciliation checks
   exchange state rather than treating a submitted request as a confirmed fill.
5. CAD valuation, portfolio charts and the calendar read the ledger. UI refresh is distinct
   from execution; a moving chart does not prove that the trading loop is running.

| Responsibility | Implementation | Verification examples |
| --- | --- | --- |
| Runtime and restart handling | [`runtime.py`](../stock101/crypto/runtime.py) | [`test_runtime_reliability.py`](../tests/test_runtime_reliability.py) |
| Candidate and strategy evaluation | [`multi.py`](../stock101/crypto/multi.py), [`market_strategies.py`](../stock101/crypto/market_strategies.py) | Deterministic strategy tests under `tests/` |
| Sizing and budget policy | [`sizing.py`](../stock101/crypto/sizing.py), [`budget_policy.py`](../stock101/crypto/budget_policy.py) | Synthetic portfolios, no real orders |
| PAPER books and execution | [`paper_books.py`](../stock101/crypto/paper_books.py) | [`test_paper_execution.py`](../tests/test_paper_execution.py), [`test_paper_recovery.py`](../tests/test_paper_recovery.py) |
| LIVE exchange path | [`live.py`](../stock101/crypto/live.py), [`live_exchange.py`](../stock101/crypto/live_exchange.py) | Mocked adapters; no live orders in release verification |
| Persistence and exact monetary values | [`repository.py`](../stock101/crypto/repository.py), [`ledger_codec.py`](../stock101/crypto/ledger_codec.py) | Decimal preservation and corruption handling |
| Portfolio and daily realized results | [`portfolio_charts.py`](../stock101/crypto/portfolio_charts.py), [`profit_calendar.py`](../stock101/crypto/profit_calendar.py) | [`test_portfolio_charts.py`](../tests/test_portfolio_charts.py), [`test_profit_calendar.py`](../tests/test_profit_calendar.py) |

## Reading the numbers

**Contributed virtual funds + cumulative total P&L = total virtual equity.**
Total P&L combines realized and unrealized components. Positive realized profit alone does
not imply that total equity exceeds contributed funds. Valuation before future sale costs
and estimated liquidation value are separate measures. The UI labels those bases and their
timestamps; additional deposits and bonuses are not trading profit.

## Design decisions and limits

- Separate execution modes prevent a PAPER result from being presented as a LIVE fill.
- Durable records and reconciliation help recover state; they cannot remove market risk,
  exchange downtime or computer shutdowns.
- Decimal ledger values preserve monetary precision. Charts round presentation values.
- Unit tests use synthetic scenarios and mocked dependencies, not evidence of profitability.
- A local process must remain running. The optional login launcher cannot trade while the
  computer is powered off. Data availability and fee assumptions must be checked by the user.

The screenshot uses fresh virtual example funds with simulation stopped. See
[execution modes](TRADING.md), [validation limits](VALIDATION.md) and the unit suite for evidence.
