# NEXORA FX — Current LIVE Implementation Status

**Audit date:** 2026-11-25
**Scope:** Account & Trading Mode Manager, persistent GitHub operator authorization,
LIVE permission gates, uncertain MT5 order outcomes, offline tests, and current
operational limitations.

## Status summary

NEXORA FX supports RESEARCH, DEMO, and LIVE modes. LIVE is no longer
hard-disabled: the existing order engine can submit through MT5 only after
the account-bound authorization and execution gates described below pass.
The dashboard defaults to disarmed execution; implementation and tests did
not submit any DEMO or LIVE broker order. No claim is made that the actual
terminal, broker, OAuth app, or Windows Credential Manager was successfully
verified with the operator's credentials.

### Capability status

| Area | Status |
|---|---|
| Account profiles | **Implemented; offline tested.** Multiple persistent profiles, switch, connect, verify, disconnect, and reconnect controls share the SQLite configuration used by the runner. Actual login, server, and DEMO/LIVE type are checked against MT5 responses. Tests use a mock terminal only. |
| MT5 credential storage | **Implemented; backend selection previously checked.** Account passwords use the Windows Credential Manager `WinVaultKeyring`; no plaintext fallback is allowed. Unit tests use an isolated fake vault. A real user password was not written or tested. |
| GitHub operator auth | **Implemented; mocked only.** Official OAuth device-flow HTTP requests are implemented; the allowlist is `Silas-AFK`. Only the public client ID and authorization facts persist; device codes/tokens are not written to SQLite or logs. No OAuth App or real sign-in was used. |
| RESEARCH / DEMO / LIVE | **Implemented.** RESEARCH cannot submit. DEMO and LIVE use the existing order engine. LIVE permission is bound to the verified LIVE account, GitHub authorization, current risk-policy hash, readiness items, supervisor health, baseline/risk gates, explicit real-money/risk acknowledgements, separate first-order authorization, and a distinct execution-arm control. No mode or UI label alone grants permission. |
| Order execution / position protection | **Offline tested with mocked MT5.** Broker sizing, order checks, fill lookup, attached SL/TP verification, risk limits, duplicate reservation, stop management, break-even, partial close, trailing, and trade-history reconciliation use the existing engine. This is not a broker execution test. |
| Uncertain order results | **Hardened and offline tested.** A lost response/timeout triggers immediate position lookup; if no fill is verified, a durable pending-confirmation record and emergency lockout prevent retry. Explicit broker rejection is recorded separately. Manual broker reconciliation may still be required when MT5 cannot establish the result. |
| Supervisor / recovery | **Implemented and unit-tested.** Runner startup disarms scanning/execution and invalidates account verification and GitHub/LIVE permissions; it then reconciles recorded positions. No real process/terminal recovery test was run. |
| Dashboard | **Offline AppTest passed.** Account Management and LIVE arm/disarm controls render and connect to the backend. Streamlit binds to `127.0.0.1`. No browser-based visual test or live MT5 interaction was performed. |
| Dependencies | **Installed environment passes `pip check`.** Python 3.12 project dependencies; `keyring` is already listed in `requirements.txt`. |
| Broker / performance evidence | **Not verified.** No real or demo broker order, fill, stop attachment, or forward test occurred. No profitability claim is made. |

## Validation performed

- Full offline test suite: **109 passed** (all MT5 execution behavior is mocked).
- Python compilation: **passed** for project Python modules.
- `pip check`: **No broken requirements found**.
- `git diff --check`: **passed** before publishing.
- No real or demo order was submitted.
- No live MT5 terminal identity, GitHub OAuth sign-in, or password write to
  Windows Credential Manager was tested.
- Streamlit AppTest is not a visual browser test; no broker account was used.

## Remaining requirements before any LIVE activation

1. Create a GitHub OAuth App with device flow enabled and enter its public
   client ID in Account Management. Authenticate as the allowlisted
   `Silas-AFK` operator while the intended LIVE MT5 profile is connected.
2. Save the account password through the UI and confirm the Windows Credential
   Manager backend works for the signed-in Windows user.
3. Connect and verify the actual LIVE account identity and exact broker server.
   Do not rely on the profile values alone.
4. Confirm baseline and supervisor health, then complete each LIVE readiness
   item only after reviewing real evidence. These records are operator
   attestations, not independent proof of broker behavior.
5. Before arming LIVE, independently complete successful DEMO forward testing
   and verify account identity, sizing, execution, protective stops, daily
   loss, emergency stop, duplicate prevention, and trade recovery. Those
   broker-side checks remain **not verified** here.
6. Review the current risk policy. Explicitly acknowledge real-money risk,
   activate LIVE, separately authorize the first LIVE order, then use
   **Execution Safety → Arm LIVE Order Execution**. The execution gate remains
   disarmed by default and is invalidated on restart, account change, or risk
   policy change.

LIVE activation controls are implemented, but this report does not recommend
funding or arming a LIVE account based only on mocked tests. Realized loss can
exceed configured limits due to gaps, slippage, outages, and rejected closes.

## Files changed in the current implementation

**Created**

- `execution/account_manager.py`
- `execution/github_device_auth.py`
- `tests/test_account_manager.py`
- `tests/test_github_device_auth.py`
- `tests/test_live_activation.py`

**Modified**

- `README.md`
- `PROJECT_AUDIT_REPORT.md`
- `dashboard/dashboard.py`
- `execution/credential_store.py`
- `execution/mt5_connection.py`
- `execution/operating_mode.py`
- `execution/runner.py`
- `execution/trading_engine.py`
- `execution/trading_store.py`
- `tests/test_account_connections.py`
- `tests/test_autonomous_trading.py`
- `tests/test_dashboard_execution_status.py`
- `tests/test_mt5_connection.py`

No files were deleted. `requirements.txt` already contains
`keyring>=25,<26` in the current repository baseline.

## Windows launch commands

Dashboard:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m streamlit run '.\dashboard\dashboard.py' --server.address 127.0.0.1
```

Supervisor (separate PowerShell window):

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m execution.runner
```

Offline verification:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m pytest -q
& '.\venv\Scripts\python.exe' -m compileall -q core dashboard execution risk tests
& '.\venv\Scripts\python.exe' -m pip check
```

---

# Archived Account Connections Audit (superseded)

**Scope:** Persistent MT5 account profiles, account verification, safe switching,
execution disarming, dashboard behavior, and offline verification.
**Audit date:** 2026-11-25

This audit supersedes the historical status below where they differ. **No LIVE
or DEMO order was submitted during this change.** No MT5 terminal identity was
verified during implementation or tests. No account identifiers are recorded
in this report.

The Account Connections page now persists one intended account profile in the
shared local trading database. The profile stores login, broker server, and
account type only; it does not store a password. A profile is eligible for
identity verification only after comparing it with the actual connected MT5
terminal. The current profile must still be configured and verified by the
operator; backend tests use only mocked MT5 responses.

## A. Overall project status

The project has a persistent RESEARCH/DEMO/LIVE mode setting, separate scanner
and execution permissions, an MT5 supervisor, order and position risk gates,
broker-history reconciliation, and an expanded dashboard. DEMO is the selected
default; the agent, supervisor, and execution permission remain disarmed by
default. Account switching and disconnecting disarm the runner, agent, and
execution and clear account verification. RESEARCH remains available as a
signals-only fallback. Offline/mock checks cover account persistence,
verification, switching, and fail-closed behavior. No broker order or live
terminal verification was performed. LIVE execution is hard-disabled in the
backend.

### Capability status

| Area | Status and evidence |
|---|---|
| Account Connections | **Implemented and mocked-test-covered.** The dashboard supports Connect, Verify, Disconnect, Save, and Switch. Identity verification reads the connected MT5 terminal; user-entered values alone do not verify. Profile changes disarm execution, agent scanning, and runner, clear verification, and are blocked while managed trades need reconciliation. Disconnect does not shut down an externally owned MT5 terminal. No real terminal verification was performed. |
| MT5 account/identity | **Mocked-test-covered only.** Login, broker server, and DEMO/LIVE type are compared with actual terminal/account responses. No MT5 terminal identity was verified in this implementation pass. Missing, mismatched, disconnected, or invalid configuration fails closed. |
| Operating modes and permissions | **Implemented and unit-tested.** DEMO is the selected default, but execution/agent/supervisor all start disarmed. RESEARCH remains selectable as a signals-only fallback. DEMO requires a verified saved DEMO profile, supervisor and agent enabled, explicit dashboard confirmation, all period baselines, and risk gates. LIVE execution is unconditionally disabled in the backend. |
| Broker-aware position sizing | **Mocked-test-covered.** Uses broker profit/spec data, rounds volume down, verifies actual fill risk and required margin, and rejects invalid calculations. Realized loss can still exceed modeled risk. |
| Risk controls | **Mocked-test-covered.** Defaults: 0.25% per trade; 1% daily, 2% weekly, 4% monthly loss; 0.5% aggregate open stop-risk; 2 positions; 1 per symbol; 5 entries/day; 3 consecutive losses; 2-pip spread; 1-pip slippage; 1.5:1 minimum reward/risk. Missing or wrong-account baselines fail closed. |
| Order submission and protective SL/TP | **Mocked-test-covered only.** Persistent order reservation, broker response checks, and protective-stop/target verification exist. No broker order, fill, or stop placement has been verified. |
| Duplicate prevention | **Mocked-test-covered.** Unique persistent signal reservation, SQLite entry lease, and runner single-instance claim are covered offline. |
| Position management | **Mocked-test-covered.** Break-even at +1R, partial close at +1.5R when broker volume permits, ATR trailing from +2R, explicit managed-position close, and stop verification. No broker-side behavior has been tested. |
| Closed trade history and analytics | **Implemented; mocked history reconciliation tested.** Closed results are persisted only after deal-history validation; period/per-symbol reporting uses verified records. Statistics are not evidence of profitability. |
| Emergency stop and restart | **Mocked-test-covered.** Lockout persists and blocks new entries; restart disarms scanning/execution. The supervisor continues management while running. Actual process/terminal restart recovery has not been exercised with MT5. |
| Automated scanning and supervisor | **Implemented and mock-tested; not running in the last operational check (2026-11-25).** Persistent flags were enabled, but the supervisor process was absent and its heartbeat stale. The dashboard server was also not listening. No continuous scanning is claimed. |
| Dashboard | **Account Connections page passed Streamlit AppTest** without MT5 access; profile form persistence, Connect/Verify/Disconnect controls, and inputs rendered. The earlier 18-page check predates this addition. This is not a visual browser test. |
| Live market data and signal intelligence | **Partially revalidated read-only.** The enabled-symbol list contained EURUSD, whose analysis completed validly with classification `WAIT`. No confirmed entry was found, and no order was sent. Other symbols were not enabled or tested. |
| Historical backtesting | **Implemented in the existing offline replay.** No representative broker dataset or meaningful profitable sample was verified in this completion pass. |
| Demo forward test / LIVE readiness | **Not performed / not satisfied.** No real or demo order was submitted. |

### Validation run

- `python -m pytest -q`: **86 passed** (offline; all MT5 responses are mocked).
- Python compilation: **49 Python files compiled successfully** with
  `compileall`.
- `python -m pip check`: **No broken requirements found**.
- Streamlit AppTest: Account Connections renders without MT5 access; the
  profile form persists into an isolated SQLite database. The separate prior
  run tested 18 existing pages before this page was added.
- Account tests cover validation, persistence/shared configuration, mocked
  identity comparison, invalidation on mismatch/disconnection, disarm-on-switch,
  unresolved-trade blocking, disconnect ownership, and unconditional LIVE denial.
- No MT5 terminal identity was verified and no real or demo order was
  submitted, modified, or closed.
- `mypy` and `ty` are not installed; no static type-checker was run. No
  browser-based visual test was run.

## B. Remaining problems and activation blockers

1. **Keep LIVE disabled.** Do not create readiness evidence or set LIVE
   authorization for checks that have not actually passed. The dashboard does
   not expose LIVE arming.
2. The operator must save the intended account profile and verify it against
   the connected MT5 terminal. No DEMO order, broker acceptance, fill, stop
   placement, or forward test has been verified.
3. LIVE execution is unavailable in this build. Do not treat readiness
   records, account verification, or dashboard state as permission to enable
   LIVE trading.
4. Daily/weekly/monthly baseline windows are the first five minutes of their
   respective UTC period. Missing one locks entries until the next period.
5. Market gaps, slippage, MT5 outages, and rejected close requests can cause
   losses beyond modeled limits. No risk system can guarantee a close.
6. Strategy performance/profitability is not established by unit tests or
   simulated results.

## C. Files changed for Account Connections

**Created**

- `execution/account_connections.py`
- `tests/test_account_connections.py`

**Modified**

- `data/trading.sqlite3` (runtime schema migration added the empty
  `account_connections` row; no account profile or password was entered)
- `execution/operating_mode.py`
- `execution/trading_store.py`
- `execution/trading_engine.py`
- `dashboard/dashboard.py`
- `tests/test_autonomous_trading.py`
- `tests/test_dashboard_execution_status.py`
- `README.md`
- `PROJECT_AUDIT_REPORT.md`
- `.streamlit/config.toml`

**Deleted:** none.

The workspace is not a Git repository, so this is the files changed by this
implementation session and cannot be compared against a tracked baseline.

## D. PowerShell commands

Launch NEXORA FX:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m streamlit run '.\dashboard\dashboard.py'
```

Start the supervisor in a separate window if not started from the dashboard:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m execution.runner
```

Run tests, compile checks, and dependency validation:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m pytest -q
& '.\venv\Scripts\python.exe' -m compileall -q core dashboard execution risk tests
& '.\venv\Scripts\python.exe' -m pip check
```

## E. Account setup and DEMO/LIVE conditions

1. Log the MT5 desktop terminal into the intended account. Confirm account
   type, login, and broker server in MT5.
2. Launch the dashboard and open **Account Connections**.
3. Select **Connect to MT5** to inspect actual terminal identity. Enter the
   matching login, exact broker server, and account type, then select **Save
   Account Profile**. The account profile is local and stores no password.
4. Select **Verify Saved Account**. A mismatch or disconnection invalidates
   verification and disarms execution. This action does not arm trading.
5. Account profile switching is blocked while managed trades need
   reconciliation. Switching/disconnecting disarms the runner and requires
   fresh verification. Disconnect does not close the MT5 terminal.
6. DEMO order testing requires all independent backend risk gates, fresh
   baseline windows, active supervisor, explicit signal-agent and operator
   approval. No such order was submitted during this implementation.
7. **LIVE execution is hard-disabled.** No dashboard or environment setting
   can activate it.

---

# Archived Phase 2 Project Audit (historical, superseded)

**Audit date:** 2026-11-25  
**Scope:** Phase 2 backend completion, offline verification, and signals-only safety review.

This update supersedes the earlier audit snapshot below wherever its status differs. No MT5 order was submitted.

## A. Overall project status

NEXORA FX is now a **signals-only analysis application** with hardened read-only MT5 data access, advisory risk planning, offline SMC replay, persistent signal observations, and 16 Streamlit dashboard pages. It is **not an autonomous or execution-ready trading system**. Demo and live order submission, trade management, and order-level risk controls were deliberately not implemented.

### Capability status

| Area | Status and evidence |
|---|---|
| Python modules | **Tested — pass.** All 46 project Python files compile. |
| Dependencies | **Tested — pass in this environment.** `pip check` reports no broken requirements; `requirements.txt` pins the direct runtime dependencies. |
| MT5 connection ownership | **Tested — mocked lifecycle tests pass.** A read-only connection to the configured terminal also succeeded. |
| Live market data | **Tested — read-only.** EURUSD, GBPUSD, and USDJPY quotes were fresh (0.1–1.2 seconds); 9 completed M15 candles were returned for each. The observed broker clock offset was +7200 seconds and normalized to UTC. |
| Dashboard | **Tested — pass.** Streamlit AppTest rendered all 16 pages; the Data Quality Monitor form was exercised. This is not a full visual/browser or cross-platform test. |
| Signal Intelligence | **Tested — pass for the 3 currently selected pairs.** EURUSD, GBPUSD, and USDJPY each produced valid `WAIT` results because timeframes were not aligned. This does not establish behavior for every broker symbol. |
| Advisory planner / sizing | **Tested with mocked broker specifications.** Uses tick/volume parameters and rounds volume down within the selected risk budget. It is advisory only and does not guarantee realized loss. |
| Risk limits | **Partially implemented.** The planner enforces its per-plan risk budget; no order-level, aggregate-exposure, daily-loss, or open-position enforcement exists because there is no execution path. `TRADING_ENABLED` remains `False`. |
| Backtesting | **Pipeline tested synthetically.** A synthetic SMC replay processed 4 decision snapshots and produced 0 confirmed setups/trades. No profitability conclusion or real historical performance claim is supported. |
| Signal persistence/outcomes | **Implemented and unit-tested.** Signal observations are de-duplicated by symbol/timestamp; `CONFIRMED` is a classification, not a successful trade. Outcomes remain pending until later completed candles touch reference levels. |
| Demo/live order execution | **Not implemented or tested, by design.** No production `order_send()` call exists. |
| Break-even / trailing / partial close | **Not implemented.** |
| Duplicate-order prevention | **Not implemented.** Signal-row de-duplication is not order idempotency. |
| Emergency stop | **Not implemented.** |
| Restart/disconnection recovery | **Partial.** Signal history is persistent; a new MT5 lease can initialize a connection when requested. There is no trade-state recovery or automatic position recovery. |
| Automated tests | **Tested — 39 passed.** The suite is offline and uses mocked MT5 calls. |

### Validation run

- `python -m pytest -q`: **39 passed**.
- Python compilation: **46 project source files passed**.
- `python -m pip check`: **No broken requirements found**.
- Pylance workspace diagnostics: **no errors**; one pre-existing unused-import warning for `pd` in `core/market_regime.py`.
- Read-only MT5 check: all three configured symbols returned fresh quotes and validated completed M15 candles; no orders were submitted. The connected account's demo/live status was not verified.
- Earlier Streamlit AppTest verification covered all 16 pages and a mocked signal scan that persisted three `WAIT` observations without marking them successful.
- Production source search found no `order_send()` call; `risk/risk_gate.py` keeps `TRADING_ENABLED = False`.

The quote timestamp correction only infers clear positive quarter-hour offsets. Other unexplained future timestamps are rejected; other broker clock arrangements may require further handling.

## B. Remaining problems and limitations

1. No demo or live order submission, active position management, order-level risk enforcement, or execution idempotency exists. These omissions are intentional under the approved scope.
2. No representative historical dataset has been used to establish strategy performance. Backtest results depend on uploaded data and user-provided spread, commission, and slippage assumptions; samples below 30 trades are marked insufficient.
3. Backtest results are simulated from OHLC as mid-prices, not broker fills. Same-candle stop/target ambiguity is conservatively resolved stop-first.
4. Signal quality is rule-based, not calibrated against historical outcomes. Signal outcomes are reference-level observations, not actual trade history.
5. Account demo/live status and broker-specific execution suitability have not been verified. Quote/data checks do not imply execution readiness.
6. MT5 clock normalization has the bounded positive-offset limitation stated above. Dashboard AppTest does not substitute for visual browser, prolonged runtime, or multi-broker testing.
7. Workspace is not a Git repository; the inventory below is based on available session history and current files rather than a tracked diff.

## C. Change inventory

**Created**

- `README.md`
- `requirements.txt`
- `core/backtesting.py`
- `core/data_validation.py`
- `core/signal_store.py`
- `risk/trade_planner.py`
- `tests/test_advisory_trade_planner.py`
- `tests/test_backtesting.py`
- `tests/test_market_data_validation.py`
- `tests/test_mt5_connection.py`
- `tests/test_signal_store.py`

**Modified**

- `.streamlit/config.toml`
- `app.py`
- `core/market_data.py`
- `core/multi_timeframe_smc.py`
- `core/signal_engine.py`
- `core/signal_service.py`
- `core/zone_validator.py`
- `dashboard/dashboard.py`
- `dashboard/dashboard_backup.py`
- `dashboard/dashboard_backup2.py`
- `execution/mt5_connection.py`
- `risk/risk_manager.py`
- `tests/test_signal_engine_quality.py`
- `tests/test_zone_validator.py`
- `PROJECT_AUDIT_REPORT.md`

**Deleted:** no deletions were recorded. Because the folder is not under Git, this cannot be confirmed against a complete baseline.

## D. Exact PowerShell commands

Launch from PowerShell:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m streamlit run '.\dashboard\dashboard.py'
```

Run tests and dependency validation:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m pytest -q
& '.\venv\Scripts\python.exe' -m pip check
```

## E. Safe demo-data check and future live prerequisites

**Demo-feed-only check (no orders):**

1. Log in to the MetaTrader 5 desktop terminal using a broker demo account and independently confirm the terminal is on its demo server.
2. Launch NEXORA FX with the command above.
3. Open System Status and Data Quality Monitor; confirm MT5 connectivity, recent quotes, and completed candles.
4. Enable the manual Signal Intelligence scan and inspect the selected symbols' results. `WAIT` is a valid result.
5. Confirm the terminal's Orders/Positions are unchanged. This project has no order-submission path; this procedure does **not** test demo execution.

**Before any future live execution could be considered:** explicitly change the approved signals-only scope; implement and independently review execution, broker acceptance/error handling, duplicate-order prevention, emergency stop, active trade management, restart recovery, and enforced per-trade plus aggregate risk limits; validate broker specifications and account mode; pass extensive mocked tests and isolated demo-account tests; forward-test for a pre-agreed meaningful sample; and assess out-of-sample results after realistic costs against pre-defined drawdown and risk criteria. Profitability is not guaranteed, even if those conditions are met.

---

## Prior audit snapshot (before Phase 2; superseded by this update)

The following is retained as historical context. Its statements about missing planners, backtests, persistence, dependencies, and test coverage describe the pre-Phase-2 state and are no longer current.

## Executive summary

NEXORA FX is a functional, read-only market-analysis and signal dashboard. It connects to MetaTrader 5 for quotes and candles, analyzes completed candles across H4/H1/M15, validates SMC zones, and presents signal classifications and explanations in Streamlit.

It is **not an autonomous trading system** and is not ready to manage trades. MT5 order execution is disabled, there is no backtesting or persistent outcome tracking, and the user's intended 0.25% risk per trade is not currently enforced by the signal pipeline.

This audit follows the confirmed operating preferences: **signals only**, **dashboard-selected markets**, and **0.25% intended risk for future planning**. No trade execution or project-code changes were made as part of this audit.

## Current capabilities

- Streamlit dashboard with Overview, Market Analysis, indicators, regime/structure views, SMC diagnostics, Signal Intelligence, and system status.
- Dashboard-managed market selections, saved locally in `config/market_settings.json`.
- MT5 quote and candle retrieval; current-candle exclusion is used in the analysis paths reviewed.
- Technical indicators, regime and market-structure analysis, liquidity/OB/FVG detection, multi-timeframe SMC, zone validation, setup diagnostics, and rule-based signal evaluation.
- The signal engine returns `CONFIRMED`, `WAIT`, or `REJECTED` classifications and rule-completeness scores. The UI explicitly states that scores are not win probabilities.
- The dashboard's “agent” toggle gates manual signal scanning; it does not continuously scan in the background.
- Risk calculation and a hard-disabled risk gate exist separately from the signal service.

## Findings and recommended actions

### High priority

1. **The selected 0.25% risk is not applied.** The signal engine can calculate analytical entry, stop, and target levels, but the service does not call `risk/risk_manager.py` or produce an approved risk-sized plan. The risk gate has a hard-coded 1% daily-loss threshold and always denies approval because `TRADING_ENABLED` is false. This is appropriate for signals-only operation, but the dashboard must not imply that position sizing or account risk is already enforced.
2. **MT5 connection ownership is inconsistent.** `risk/risk_manager.py::calculate_position_size` calls `mt5.initialize()` and unconditionally calls `mt5.shutdown()` in `finally`. That can tear down a connection owned by another caller. `core/market_data.py::ensure_mt5_connection` can also return after initialization when `terminal_info()` is still unavailable without shutting that connection down. Define and test a single connection-lifecycle policy before expanding risk planning.
3. **Market-data and indicator input validation needs strengthening.** `core/market_data.py::get_historical_data` checks the returned time column, but not required OHLC columns, finite prices, or quote freshness. `core/indicators.py::calculate_indicators` assumes valid `high`, `low`, and `close` columns and does not reject nonnumeric or non-finite values. Malformed or stale broker data can therefore raise exceptions or propagate invalid indicator values.

### Medium priority

4. **The signal engine does not enforce symbol consistency.** `core/signal_engine.py::evaluate_signal` checks that the MTF and zone results are marked valid, but does not confirm their symbols match its `symbol` argument or each other. The current service passes matching results, but direct callers could combine analysis from different instruments.
5. **No historical evaluation or outcome calibration exists.** There is no implemented backtesting engine, persistent signal/outcome store, or forward-simulation ledger. The quality score is a rule-completeness heuristic—not an empirically calibrated confidence estimate—and must remain labeled that way until representative historical outcomes support calibration.
6. **Automated test coverage is limited and mixed with live diagnostics.** The two isolated test scripts run successfully, but there is no configured test runner in the project environment (`pytest` is unavailable). `tests/test_zone_validator.py` initializes MT5 and requests live broker data; it is an integration diagnostic, not a safe offline unit test. Indicator, risk-sizing, invalid-input, and data-lifecycle cases need isolated tests with mocked MT5 dependencies.
7. **The project is not reproducible from its files alone.** No README or dependency manifest (`requirements.txt`, `pyproject.toml`, or equivalent) is present. The current environment has the required third-party modules, but a fresh setup has no authoritative install/run instructions or pinned versions.

### Lower priority

8. **Operational observability is basic.** MT5 and service errors are mostly printed or returned as text; there is no structured logging or persistent scan history. The System Status page confirms imports and MT5 connectivity, but not the health or data freshness of each analysis stage.
9. **Some defensive parsing is inconsistent.** `core/smc_confluence.py` assumes nested `analysis` values are dictionaries; malformed or partial results may raise rather than produce a clear invalid result. Other modules are more defensive, so a consistent result-validation boundary would improve resilience.
10. **Backtesting, data, logs, and services areas are not implemented.** The corresponding directories are empty; there is no persistent reporting, historical replay, or paper-trade lifecycle in the current project.

## Safety and scope

- Source review found no MT5 order-submission implementation; `execution/mt5_connection.py` handles connection/account information only.
- The risk gate and dashboard explicitly report execution as disabled.
- Keep this product signals-only as requested. Demo/live order execution, active position management, and autonomous-operation controls are outside the approved scope.
- An analytical `CONFIRMED` result is not a trade approval, and profitability is not established by the current code or tests.

## Recommended signals-only roadmap

1. **Harden analysis inputs and lifecycle:** validate OHLC/quote values and freshness, enforce symbol consistency, and standardize MT5 connection ownership.
2. **Add an advisory trade-plan layer:** calculate entry/SL/TP and a position-size estimate at the selected 0.25% risk, with broker tick/volume-step checks. Keep this informational; do not connect it to order submission.
3. **Build offline historical evaluation:** model spread and slippage, avoid look-ahead bias, report drawdown/profit factor and per-symbol results, and separate in-sample from out-of-sample data.
4. **Add paper-signal persistence and calibration:** record every signal and subsequent outcome, then assess whether any confidence calibration is justified by adequate data.
5. **Improve repeatability:** add a dependency manifest and run instructions, then expand offline tests before considering any additional operating mode.

## Verification performed

- **Compilation:** compiled all 37 `.py` files outside the virtual environment without writing bytecode; all passed.
- **Dependencies:** `pip check` reported no broken requirements. Python 3.12.2, Streamlit 1.67.0, pandas 3.1.1, Plotly 7.1.0, MetaTrader5 5.0.6231, and NumPy 2.5.5 imported successfully. No dependency manifest is present, so reproducible dependency completeness is not established.
- **MT5 and data:** `mt5.initialize()`, terminal information, and account information were available. The read-only zone-validation script completed for EURUSD, GBPUSD, and USDJPY with no timeframe errors. No order was submitted; the connected account's demo/live status was not verified.
- **Signal Intelligence:** read-only signal analysis returned valid results for all three configured symbols. A Streamlit AppTest scan also completed for all three with no Python exceptions; each classified `WAIT` because timeframe trends were not aligned.
- **Dashboard:** Overview, Currency Pair Manager, Market Analysis, Technical Indicators, Market Regime, Market Structure, SMC Setup Diagnostics, SMC Zone Diagnostics, Signal Intelligence, and System Status all rendered through Streamlit AppTest without Python exceptions. The Signal Intelligence toggle and scan action were exercised. The shared browser tab could not connect to its original server; a temporary local server returned HTTP 200 on its health and home endpoints. This was not a full visual or cross-browser test.
- **Automated scripts:** `tests/test_signal_engine_quality.py`, `tests/test_smc_setup_evaluator.py`, and `tests/test_zone_validator.py` all passed. The zone test uses live MT5 market data but only reads it. `pytest` is not installed; these are standalone scripts, not a configured test suite.
- **Risk arithmetic:** a mocked broker-spec case stayed within its 0.25% budget, the 1% daily-loss boundary was rejected, and the risk gate remained locked. This is a narrow synthetic check, not certification of broker-specific sizing. Floating-point truncation rounded the sample down to 0.24 lots instead of 0.25; the estimate remained below budget.
- No real-money trades or demo orders were placed.

## Final status of requested capabilities

| Capability | Final status |
|---|---|
| Python compilation | **Tested — pass** for all 37 Python files |
| Installed dependencies | **Partially verified —** imports and `pip check` pass; no manifest/pinned environment |
| MT5 connection | **Tested — pass** for read-only initialization and account/terminal availability |
| Market data | **Tested — pass** for configured symbols during this audit; no freshness guarantee |
| Dashboard pages | **Tested — pass** for all 10 page renders with AppTest; not full visual browser coverage |
| Signal Intelligence | **Tested — pass** for EURUSD, GBPUSD, USDJPY; all returned valid `WAIT` results |
| Trade planning and position sizing | **Incomplete —** no integrated planner; a sizing helper exists but is not fully validated or used by signal output |
| Backend risk limits | **Fail-closed only —** execution is locked; the hypothetical 1% daily-loss check is not connected to an execution path and 0.25% is not enforced |
| Demo order execution | **Not implemented or tested**; no order-submission call or mocked/demo order test was found |
| Break-even, trailing stop, partial close | **Not implemented or tested** |
| Duplicate-order prevention | **Not implemented or tested** |
| Emergency stop | **Not implemented or tested** |
| Restart/disconnection recovery | **Partial —** reconnect can be attempted during a later data request; no persistent trade state/recovery or automated retry was tested |
| Available test scripts | **Tested — pass**; no installed pytest runner or broad offline unit suite |

## Change inventory limits

The workspace is not a Git repository, so there is no reliable baseline from which to enumerate every created, changed, or deleted file. The recorded development changes are `.streamlit/config.toml`, `dashboard/dashboard.py`, `core/signal_engine.py`, and `tests/test_signal_engine_quality.py`; this report, `PROJECT_AUDIT_REPORT.md`, was added during the audit. No deletions were recorded. `config/market_settings.json` currently selects EURUSD, GBPUSD, and USDJPY, but its provenance cannot be determined. A zero-byte `dashboard/dashboard.pycode` file is also present; its origin is unknown.

## Safe launch and demo-data check

From PowerShell:

```powershell
Set-Location 'C:\Users\user\Desktop\ForexAI'
& '.\venv\Scripts\python.exe' -m streamlit run '.\dashboard\dashboard.py'
```

For a demo-data-only check, log MetaTrader 5 into a broker demo account, launch the dashboard, open System Status, then turn on the agent switch and run Signal Intelligence. The agent switch only enables a manual signal scan; it does not place orders. This workflow checks demo-feed connectivity and analysis, **not demo order execution**.

Live execution must remain disabled unless the user explicitly changes the signals-only scope and the project first implements and independently verifies broker-aware planning, enforced per-trade and aggregate risk limits, order idempotency, emergency stop, trade management, restart recovery, comprehensive mocked tests, and sustained demo-forward testing against pre-agreed out-of-sample performance and drawdown criteria.

## Audit limits

This audit is not a profitability assessment, broker certification, or formal security review. MT5 integration was tested read-only against the configured terminal; account demo/live status was not verified. The workspace is not a Git repository, so the file-change inventory is based on available session history and current files rather than a tracked baseline.
