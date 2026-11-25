# NEXORA FX

NEXORA FX is a local Streamlit Forex-analysis application with a persistent,
fail-closed MT5 supervisor. DEMO is the selected default mode, but execution,
agent scanning, and the supervisor all start disarmed. RESEARCH remains
available as a signals-only fallback.

## Operating modes and safety gates

- **DEMO (selected default):** can submit and manage demo-account orders only
  after a saved account profile is verified against the connected terminal, a
  confirmed demo account, a running supervisor, enabled signal scanning,
  explicit dashboard confirmation, daily/weekly/monthly equity baselines, no
  persistent emergency lockout, and all per-order and portfolio checks.
  Execution remains disarmed by default.
- **RESEARCH:** analyzes and persists signals when the agent is enabled; it
  never submits orders and remains available as a fallback.
- **LIVE:** available in the dashboard but locked behind verified LIVE account
  identity, allowlisted GitHub device-flow authentication, account-bound
  readiness evidence, healthy supervision, account-bound risk baselines,
  explicit real-money/risk acknowledgements, a separate first-order
  authorization, and the normal execution permission gate. Account, mode,
  risk-policy, and restart changes invalidate the relevant authorization.
  No live order has been submitted or tested against a broker.

The dashboard can request a mode or DEMO permission change, but cannot bypass
backend checks. The supervisor reloads persistent settings and validates the
backend permission gate for every entry. Its state and execution ledger are
stored in `data\trading.sqlite3`; signals and research history use
`data\nexora_fx.sqlite3`.

Default risk limits (used after the threshold form was returned blank):

- Maximum planned risk per order: **0.25% of current equity**.
- Maximum daily equity loss: **1.0%** of the once-per-UTC-day starting equity.
- Maximum weekly/monthly equity loss: **2.0% / 4.0%**.
- Maximum aggregate open stop-loss risk: **0.5%** of current equity.
- Maximum open positions plus pending orders: **2**.
- Maximum positions per symbol: **1**; maximum trades per UTC day: **5**;
  consecutive-loss limit: **3**.
- Maximum spread/slippage: **2 / 1 pips**; minimum planned reward-to-risk:
  **1.5:1**; no default session restriction.

These are configured development defaults, not financial recommendations.
Per-order risk uses broker `order_calc_profit`, rounds volume down to a valid
volume step, and verifies the actual fill risk again. Entries are rejected if
the broker cannot provide reliable sizing or margin estimates. A fill that
exceeds a risk limit is closed if possible and causes a persistent lockout.
Market gaps, broker outages, slippage, and rejected close requests can still
produce losses beyond modeled limits.

Daily, weekly, and monthly baselines are immutable for their UTC periods and
can only be captured during the first five minutes of each period. Missing a
baseline window keeps entries locked until the next period; the baseline
cannot be reset to bypass loss limits.

An emergency lockout blocks new entries and persists across application
restarts. The runner continues managing already recorded positions while the
lockout is active. Break-even is attempted at +1R, half-size partial close at
+1.5R where broker volume constraints allow it, and an ATR-based trailing stop
from +2R. Protective stops/targets are verified after broker acknowledgement.
Untracked positions, unresolved orders, account mismatches, missing protection,
or uncertain broker actions lock new entries pending reconciliation.

`CONFIRMED` is a signal classification, not an order approval, successful
trade, or calibrated win probability. Offline backtests are simulations, not
broker fills or evidence of profitability. No demo forward test or live order
has been performed as part of this implementation.

## Requirements

- Windows 10/11
- Python 3.12
- MetaTrader 5 desktop terminal installed for broker data and account
  connection. MT5 credentials are stored in Windows Credential Manager.
- A GitHub OAuth App with device flow enabled is needed only for operator
  identity verification; the dashboard accepts its public client ID and does
  not require a client secret.
- DEMO/LIVE orders remain subject to backend permission gates. Keep execution
  disarmed except during deliberate, controlled testing.

Install from PowerShell in the project directory:

```powershell
py -3.12 -m venv .\venv
& '.\venv\Scripts\python.exe' -m pip install --upgrade pip
& '.\venv\Scripts\python.exe' -m pip install -r '.\requirements.txt'
```

The tests use mocked MT5 calls and do not require a terminal or broker account.
They do not replace broker-specific demo execution and sustained forward tests.

## Launch the dashboard

From PowerShell:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m streamlit run '.\dashboard\dashboard.py' --server.address 127.0.0.1
```

The dashboard binds to `127.0.0.1` only. Open the local URL printed by
Streamlit, normally `http://localhost:8501`.

## Start the autonomous scanner/supervisor

The dashboard's **Start Supervisor** control starts a separate process.
Alternatively, start it in a separate PowerShell window:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m execution.runner
```

On startup the runner disarms signal scanning and order execution. Enable
those controls separately in the dashboard after it reports the supervisor
heartbeat. It monitors selected symbols every 15 seconds when scanning is
enabled; set `NEXORA_SCAN_INTERVAL_SECONDS` to adjust the interval. A safe
stop disarms new scans and entries; existing managed positions continue to be
supervised until reconciled.

Manage profiles in **Account Management**. Login, broker server, account type,
and a credential reference are stored in the shared local
`data\trading.sqlite3`; the MT5 password itself is stored only in Windows
Credential Manager. Save each account with its password, select **Switch
Account**, then **Connect selected account**. NEXORA logs into the selected
account through MT5 and verifies the actual terminal login, server, and
DEMO/LIVE type. User-entered values alone never verify identity. **Verify
active account** rechecks the terminal. Multiple profiles persist across
restarts. Switching or disconnecting disarms scanning/execution and invalidates
account-bound authorization; the MT5 desktop terminal is not shut down. A
saved profile can be reconnected through **Connect selected account**.

The same page selects RESEARCH, DEMO, or LIVE. Changing mode disarms execution.
For GitHub operator verification, create an OAuth App with device flow
enabled, save its public client ID, then start sign-in and approve the displayed
device code at GitHub. Authentication is restricted to `Silas-AFK`; the
short-lived device code is held in Streamlit session memory, and the access
token is used only to fetch the GitHub username and is not stored. Do not enter
a GitHub client secret or any secret into an evidence-reference field.

For deliberate DEMO operation, ensure the terminal is connected to the intended
DEMO account and terminal trading permissions are enabled. Select DEMO mode,
verify the saved profile, capture each available baseline during its UTC
window, start the supervisor, enable the signal agent, and explicitly arm DEMO
execution only after reviewing all backend gates. These steps can result in
demo orders if a qualifying signal occurs. Do not follow them while connected
to a LIVE account. No real or demo order is submitted by account-profile
management or its automated tests.

To restore safe signals-only behavior, select RESEARCH mode, pause the agent,
disable execution, and safely stop the supervisor.

### Live readiness file

The default path is `config\live_readiness.json`; it is not created or marked
ready until an operator records evidence in Account Management. Account
identity evidence is bound to the verified LIVE login/server. Other entries
are operator attestations, not independent technical verification; only mark
them after the corresponding checks have actually passed:

```json
{
  "account_identity_verified": {
    "verified": true,
    "evidence": "reference to reviewed account/server verification",
    "verified_at": "2026-01-01T00:00:00+00:00",
    "account_login": 123456,
    "account_server": "Broker-Live"
  },
  "order_execution_verified": {},
  "stop_loss_verified": {},
  "position_sizing_verified": {},
  "daily_loss_protection_verified": {},
  "emergency_stop_verified": {},
  "duplicate_prevention_verified": {},
  "demo_forward_test_verified": {}
}
```

Readiness alone does not activate LIVE. The backend additionally requires
current account identity, an authorized GitHub identity, current risk policy,
healthy runner and risk baselines, explicit activation acknowledgements, and
separate first-order authorization. A restart, account switch, or risk-policy
change disarms and invalidates LIVE permission. No real-money order was placed
or tested during this implementation.

The optional console analysis is:

```powershell
& '.\venv\Scripts\python.exe' '.\app.py'
```

## Dashboard features

- **Signal Intelligence:** analyzes selected symbols using completed
  H4/H1/M15 candles. The persistent sidebar agent switch controls background
  scanning; it is separate from execution permission.
- **Advisory Trade Planner:** uses broker tick and volume specifications to
  estimate risk and volume. It never sends orders.
- **Risk Calculator:** calculates the selected cash risk budget.
- **Historical Backtesting:** recomputes SMC snapshots from uploaded M15, H1,
  and H4 CSV files, then replays entries no earlier than the next M15 candle.
- **Signal History:** stores de-duplicated observations in
  `data\nexora_fx.sqlite3`.
- **Signal Outcome Analysis:** compares stored signal reference levels with
  subsequent completed MT5 candles. It is not actual trade history.
- **Data Quality Monitor:** displays validated completed MT5 candles and
  checks live quote freshness.
- **Account Management:** stores multiple account profiles, securely saves
  MT5 passwords, connects/switches/verifies accounts, selects operating mode,
  performs GitHub device-flow authentication, and presents guarded LIVE
  readiness/activation controls.
- **Execution Safety:** configures validated risk limits, shows
  account/baseline/lockout state, and exposes guarded execution controls.
- **Trade History & Performance:** reports only closed trades reconciled
  against broker deal history. Cumulative realized P/L is not an equity curve.
- MT5 server timestamps are normalized to UTC when a positive quarter-hour
  offset can be inferred safely; unexplained future timestamps are rejected.

## Offline backtest CSV format

Upload three CSV files for the same broker symbol. Each needs:

```text
time,open,high,low,close
```

Timestamps must be UTC, unique, and in chronological order. Only completed
candles should be included. The M15 file may also include a `spread` column.
Spread, fixed spread, and slippage are expressed in price units (not pips or
broker points). If the M15 file has no spread column, the dashboard's fixed
spread input is used; its initial value is illustrative and should be replaced
with a broker-appropriate value. Commission is configured as basis points of
notional per side.

The replay treats uploaded OHLC as mid-price, excludes forming candles by
requiring time-aligned completed bars, enters no earlier than the next candle,
and resolves same-candle stop/target ambiguity conservatively as stop-first.
The chronological split reports in-sample and out-of-sample statistics. Results
below the displayed minimum trade count are marked insufficient for conclusions.

## Tests and checks

Run the offline automated tests:

```powershell
& '.\venv\Scripts\python.exe' -m pytest -q
& '.\venv\Scripts\python.exe' -m compileall -q core dashboard execution risk tests
& '.\venv\Scripts\python.exe' -m pip check
```

Check dependency compatibility:

```powershell
& '.\venv\Scripts\python.exe' -m pip check
```

Execution and account-connection unit tests use mocked MT5 responses only. No
terminal identity verification or order submission was performed for this
implementation. Mocked tests do not establish broker acceptance, actual stop
placement, fill behavior, or LIVE readiness.
