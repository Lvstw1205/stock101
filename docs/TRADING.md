# Connections, execution and accounting

PAPER simulates fills using available market data. It is not an exchange sandbox and its
results differ from live execution. LIVE uses the Coinbase adapter only after setup and
explicit arming. Exchange availability, account permissions and product restrictions apply.

On macOS, connect a user-owned Coinbase key locally using:

```bash
.venv/bin/python -m stock101.crypto.connect_local --json-file /path/to/key.json
```

Add `--live` only when intentionally configuring the separate live credential store. Keep
the key file out of the repository and chat logs. Community Keychain namespaces are separate
from other AIRI installations. Review permissions and the live setup screen before enabling
anything. No withdrawal automation is provided.

The example budget is a local limit, not a Coinbase deposit or a recurring bank transfer.
The optional bonus rule is also a local policy, not a transfer of money. Compare realized
P&L with entry/exit costs; compare marked equity with cumulative funding. The profit calendar
uses UTC in the public default. Its return percentage uses the matched cost basis of sold
units, not the entire account. Unrealized losses can offset positive realized gains.

Optional agent research uses a separately installed upstream package:
`.venv/bin/python scripts/install_tradingagents.py`. This pins a specific TradingAgents commit;
configure its model provider separately. Agent reports are research inputs, not guarantees.
The AIRI Assistant distribution includes the Telegram receiver; this standalone repository
provides the authenticated read-only `/api/stock101/crypto/telegram` status representation.
