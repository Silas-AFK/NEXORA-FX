
import streamlit as st
import MetaTrader5 as mt5
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time

from execution.account_manager import AccountManager
from execution.credential_store import CredentialStoreError
from execution.github_device_auth import (
    GitHubAuthError,
    poll_device_authorization,
    start_device_authorization,
)
from execution.operating_mode import (
    OperatingModeError,
    execution_state,
    read_live_readiness,
    read_live_readiness_records,
    save_live_readiness_item,
)
from execution.account_connections import (
    account_type_from_trade_mode,
    mask_account_login,
)
from execution.trading_engine import TradeRejected, TradingEngine
from execution.trade_analytics import performance_report, utc_period_bounds

from config.market_settings import (
    AVAILABLE_SYMBOLS,
    load_symbols,
    save_symbols,
)

from execution.mt5_connection import (
    MT5ConnectionError,
    connect,
    get_account_info,
    disconnect,
    mt5_connection,
)

from core.market_data import (
    MarketDataError,
    get_live_price,
    get_historical_data,
)

from core.indicators import calculate_indicators
from core.market_regime import detect_market_regime
from core.market_structure import detect_market_structure

from core.multi_timeframe_smc import analyze_multi_timeframe_smc
from core.zone_validator import validate_smc_zones
from core.smc_setup_evaluator import evaluate_smc_setup
from core.setup_diagnostics import generate_setup_diagnostics

# Signal intelligence service
from core.signal_service import analyze_signal_for_symbol
from core.backtesting import BacktestConfig, run_smc_backtest
from core.signal_store import (
    get_recent_signals,
    record_signal,
    resolve_pending_outcomes,
)
from risk.risk_manager import calculate_risk_amount
from risk.trade_planner import (
    DEFAULT_RISK_PERCENT,
    get_advisory_trade_plan,
)


def _execution_snapshot():
    engine = TradingEngine(api=mt5)
    try:
        with mt5_connection():
            state = execution_state(
                config=engine.config,
                api=mt5,
                store=engine.store,
            )
            if engine.config.account_verified and not state["account_verified"]:
                engine.store.invalidate_account_connection_verification()
                engine.refresh_configuration()
                state = execution_state(
                    config=engine.config,
                    api=mt5,
                    store=engine.store,
                )
    except MT5ConnectionError as error:
        if engine.config.account_verified:
            engine.store.invalidate_account_connection_verification()
            engine.refresh_configuration()
        state = execution_state(
            config=engine.config,
            api=mt5,
            store=engine.store,
        )
        state["entry_permission"] = False
        state["reasons"].insert(0, str(error))
    return engine, state


def _runner_is_recent(store):
    heartbeat = store.runner_heartbeat()
    value = heartbeat.get("heartbeat_at")
    if not value:
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return False
    if moment.tzinfo is None or moment.utcoffset() is None:
        return False
    age = (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds()
    return 0 <= age < 60


def _start_runner(engine):
    if _runner_is_recent(engine.store):
        engine.store.set_runner_enabled(True)
        return engine.store.runner_heartbeat().get("process_id")
    project_root = Path(__file__).resolve().parent.parent
    log_path = project_root / "data" / "runner.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with engine.store.runner_start_lease():
        if _runner_is_recent(engine.store):
            engine.store.set_runner_enabled(True)
            return engine.store.runner_heartbeat().get("process_id")
        engine.store.set_runner_enabled(True)
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                [sys.executable, "-m", "execution.runner"],
                cwd=str(project_root),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creation_flags,
            )
        return process.pid


# ==================================================
# PAGE CONFIGURATION
# ==================================================

st.set_page_config(
    page_title="NEXORA FX | Trading Intelligence",
    page_icon=":material/monitoring:",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==================================================
# NEXORA FX BRANDING
# ==================================================

st.sidebar.markdown("# NEXORA :blue[FX]")

st.sidebar.caption(
    "INTELLIGENCE. PRECISION. DISCIPLINE."
)

st.sidebar.caption(
    "TRADING INTELLIGENCE PLATFORM"
)

st.sidebar.divider()


# ==================================================
# NAVIGATION
# ==================================================

page = st.sidebar.radio(
    "NAVIGATION",
    [
        "Overview",
        "Currency Pair Manager",
        "Market Analysis",
        "Technical Indicators",
        "Market Regime",
        "Market Structure",
        "SMC Setup Diagnostics",
        "SMC Zone Diagnostics",
        "Signal Intelligence",
        "Advisory Trade Planner",
        "Risk Calculator",
        "Historical Backtesting",
        "Signal History",
        "Signal Outcome Analysis",
        "Trade History & Performance",
        "Data Quality Monitor",
        "Account Management",
        "Execution Safety",
        "System Status",
    ],
)

st.sidebar.divider()


# ==================================================
# PERSISTENT AGENT AND RUNNER CONTROLS
# ==================================================

_agent_enabled = False
try:
    _sidebar_engine, _sidebar_state = _execution_snapshot()
    _agent_enabled = _sidebar_state["agent_enabled"]
    _sidebar_config = _sidebar_engine.store.get_operational_config()
    st.sidebar.subheader("Automated Supervisor")
    if st.sidebar.button(
        "Pause Signal Agent" if _agent_enabled else "Enable Signal Agent",
        key="persistent_agent_toggle",
        width="stretch",
        type="primary" if _agent_enabled else "secondary",
    ):
        _sidebar_engine.store.set_agent_enabled(not _agent_enabled)
        st.rerun()

    _runner_recent = _runner_is_recent(_sidebar_engine.store)
    if st.sidebar.button(
        "Stop Supervisor Safely"
        if _sidebar_config["runner_enabled"]
        else "Start Supervisor",
        key="persistent_runner_toggle",
        width="stretch",
        disabled=(
            _sidebar_config["runner_enabled"]
            and not _runner_recent
            and bool(
                _sidebar_engine.store.list_trades(
                    statuses=("OPEN", "UNPROTECTED", "PENDING")
                )
            )
        ),
    ):
        try:
            if _sidebar_config["runner_enabled"]:
                _sidebar_engine.store.disarm_supervisor()
            else:
                _start_runner(_sidebar_engine)
            st.rerun()
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            st.sidebar.error(f"Supervisor control failed: {error}")

    if _sidebar_config["runner_enabled"] and _runner_recent:
        st.sidebar.success("Supervisor heartbeat is current")
    elif _sidebar_config["runner_enabled"]:
        st.sidebar.warning("Supervisor requested; heartbeat is stale or pending")
    else:
        st.sidebar.caption("Supervisor is stopped.")
    if _agent_enabled:
        st.sidebar.success("Signal scanning is enabled.")
    else:
        st.sidebar.warning("Signal scanning is paused.")
    st.sidebar.caption(
        "Scanning and order execution are separate backend permissions."
    )
except (OperatingModeError, sqlite3.Error, MT5ConnectionError, ValueError) as error:
    st.sidebar.error(f"Persistent agent controls unavailable: {error}")


st.sidebar.divider()

_display_mode = "UNAVAILABLE"
try:
    _, _mode_state = _execution_snapshot()
    _display_mode = _mode_state["mode"]
    st.sidebar.info(f"OPERATING MODE: {_display_mode}")
    if _mode_state["entry_permission"]:
        st.sidebar.success("Backend entry permission: ARMED")
    else:
        st.sidebar.error("Backend entry permission: LOCKED")
        if _mode_state["reasons"]:
            st.sidebar.caption(_mode_state["reasons"][0])
except (OperatingModeError, sqlite3.Error, MT5ConnectionError) as error:
    st.sidebar.error("Backend entry permission: LOCKED")
    st.sidebar.caption(f"Execution configuration unavailable: {error}")


# ==================================================
# MARKET CONFIGURATION
# ==================================================

SYMBOLS = load_symbols()

TIMEFRAMES = {
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


# ==================================================
# MT5 MARKET DATA
# ==================================================

@st.cache_data(ttl=5)
def load_market_data(symbol, timeframe, bars=500):
    try:
        with mt5_connection():
            data = get_historical_data(symbol, timeframe, bars)

        if len(data) < 2:
            return None, "Not enough completed historical candles."

        analysis = calculate_indicators(data)

        if analysis is None:
            return None, "Indicator calculation failed"

        if analysis.empty:
            return None, "No completed analysis data available"

        return analysis, None

    except (MarketDataError, MT5ConnectionError, ValueError) as error:

        return None, str(error)


# ==================================================
# LIVE ACCOUNT AND PRICE SNAPSHOT
# ==================================================

def get_market_snapshot():

    if not connect():
        return None, None

    try:

        account = get_account_info()

        if account is None:
            return None, None

        prices = {}

        for symbol in SYMBOLS:

            if not mt5.symbol_select(symbol, True):
                prices[symbol] = None
                continue

            prices[symbol] = get_live_price(symbol)

        return account, prices

    except Exception as error:

        st.error(f"Market connection error: {error}")

        return None, None

    finally:

        disconnect()


# ==================================================
# MT5 CONNECTION HEALTH CHECK
# ==================================================

def check_mt5_connection():

    if not connect():
        return False

    disconnect()

    return True


# ==================================================
# MULTI-TIMEFRAME SMC ANALYSIS
# ==================================================

def run_mtf_analysis(symbol):
    try:
        with mt5_connection():
            result = analyze_multi_timeframe_smc(symbol, bars=500)

        if not isinstance(result, dict):

            return {
                "valid": False,
                "reason": "Invalid multi-timeframe analysis result.",
            }

        if not result.get("valid", False):

            return {
                "valid": False,
                "reason": result.get(
                    "reason",
                    "Multi-timeframe analysis failed.",
                ),
                "mtf_result": result,
            }

        return {
            "valid": True,
            "symbol": symbol,
            "mtf_result": result,
            "execution_permitted": False,
        }

    except (MT5ConnectionError, MarketDataError, ValueError) as error:

        return {
            "valid": False,
            "reason": f"Multi-timeframe analysis error: {error}",
        }


# ==================================================
# ZONE-ONLY ANALYSIS WORKFLOW
# ==================================================

def run_zone_analysis(symbol):
    try:
        with mt5_connection():
            mtf_result = analyze_multi_timeframe_smc(symbol, bars=500)

        if (
            not isinstance(mtf_result, dict)
            or not mtf_result.get("valid", False)
        ):

            return {
                "valid": False,
                "symbol": symbol,
                "reason": (
                    mtf_result.get(
                        "reason",
                        "Multi-timeframe analysis failed.",
                    )
                    if isinstance(mtf_result, dict)
                    else "Invalid multi-timeframe result."
                ),
                "mtf_result": mtf_result,
                "execution_permitted": False,
            }

        zone_result = validate_smc_zones(
            symbol,
            mtf_result,
            max_distance_atr=1.0,
            max_age_bars=100,
        )

        if (
            not isinstance(zone_result, dict)
            or not zone_result.get("valid", False)
        ):

            return {
                "valid": False,
                "symbol": symbol,
                "reason": (
                    zone_result.get(
                        "reason",
                        "SMC zone validation failed.",
                    )
                    if isinstance(zone_result, dict)
                    else "Invalid zone validation result."
                ),
                "mtf_result": mtf_result,
                "zone_result": zone_result,
                "execution_permitted": False,
            }

        return {
            "valid": True,
            "symbol": symbol,
            "mtf_result": mtf_result,
            "zone_result": zone_result,
            "execution_permitted": False,
        }

    except (MT5ConnectionError, MarketDataError, ValueError) as error:

        return {
            "valid": False,
            "symbol": symbol,
            "reason": f"Zone analysis error: {error}",
            "execution_permitted": False,
        }


# ==================================================
# COMPLETE SMC SETUP ANALYSIS
# ==================================================

def run_smc_analysis(symbol):
    try:
        with mt5_connection():
            mtf_result = analyze_multi_timeframe_smc(symbol, bars=500)

        if (
            not isinstance(mtf_result, dict)
            or not mtf_result.get("valid", False)
        ):

            return {
                "valid": False,
                "reason": (
                    mtf_result.get(
                        "reason",
                        "Multi-timeframe analysis failed.",
                    )
                    if isinstance(mtf_result, dict)
                    else "Invalid multi-timeframe result."
                ),
                "mtf_result": mtf_result,
            }

        zone_result = validate_smc_zones(
            symbol,
            mtf_result,
            max_distance_atr=1.0,
            max_age_bars=100,
        )

        if (
            not isinstance(zone_result, dict)
            or not zone_result.get("valid", False)
        ):

            return {
                "valid": False,
                "reason": (
                    zone_result.get(
                        "reason",
                        "SMC zone validation failed.",
                    )
                    if isinstance(zone_result, dict)
                    else "Invalid zone validation result."
                ),
                "mtf_result": mtf_result,
                "zone_result": zone_result,
            }

        setup_result = evaluate_smc_setup(
            symbol,
            mtf_result,
            zone_result,
        )

        if not isinstance(setup_result, dict):

            return {
                "valid": False,
                "reason": "Setup evaluator returned an invalid result.",
                "mtf_result": mtf_result,
                "zone_result": zone_result,
            }

        diagnostics = generate_setup_diagnostics(
            setup_result,
            mtf_result,
            zone_result,
        )

        return {
            "valid": True,
            "symbol": symbol,
            "mtf_result": mtf_result,
            "zone_result": zone_result,
            "setup_result": setup_result,
            "diagnostics": diagnostics,
            "execution_permitted": False,
        }

    except (MT5ConnectionError, MarketDataError, ValueError) as error:

        return {
            "valid": False,
            "reason": f"SMC analysis error: {error}",
        }


# ==================================================
# SIGNAL ANALYSIS WORKFLOW
# ==================================================

def run_signal_analysis(symbol):

    try:

        result = analyze_signal_for_symbol(
            symbol,
            bars=500,
        )

        if not isinstance(result, dict):

            return {
                "valid": False,
                "symbol": symbol,
                "reason": "Signal service returned invalid data.",
                "execution_permitted": False,
            }

        # Explicitly keep execution disabled.
        result["execution_permitted"] = False

        return result

    except Exception as error:

        return {
            "valid": False,
            "symbol": symbol,
            "reason": f"Signal analysis error: {error}",
            "execution_permitted": False,
        }


def _read_uploaded_candles(uploaded_file):
    if uploaded_file is None:
        return None
    data = pd.read_csv(uploaded_file)
    data.columns = [
        str(column).strip().lower().replace(" ", "_")
        for column in data.columns
    ]
    if "time" not in data.columns:
        for alias in ("timestamp", "datetime", "date"):
            if alias in data.columns:
                data = data.rename(columns={alias: "time"})
                break
    return data


def _show_backtest_statistics(label, statistics):
    st.markdown(f"#### {label}")
    columns = st.columns(4)
    columns[0].metric("Trades", statistics["trades"], border=True)
    columns[1].metric(
        "Win rate",
        f"{statistics['win_rate_percent']:.1f}%",
        border=True,
    )
    profit_factor = statistics["profit_factor"]
    if isinstance(profit_factor, (int, float)):
        profit_factor_text = f"{profit_factor:.2f}"
    elif profit_factor == "unbounded":
        profit_factor_text = "N/A"
    else:
        profit_factor_text = "N/A"
    columns[2].metric("Profit factor", profit_factor_text, border=True)
    columns[3].metric(
        "Net return",
        f"{statistics['net_return_percent']:.2f}%",
        border=True,
    )

    details = st.columns(4)
    details[0].metric(
        "Max drawdown",
        f"{statistics['maximum_drawdown_percent']:.2f}%",
        border=True,
    )
    avg_rr = statistics["average_reward_to_risk"]
    details[1].metric(
        "Average planned R:R",
        f"{avg_rr:.2f}" if avg_rr is not None else "N/A",
        border=True,
    )
    expectancy = statistics["expectancy_r"]
    details[2].metric(
        "Expectancy",
        f"{expectancy:.3f} R" if expectancy is not None else "N/A",
        border=True,
    )
    details[3].metric(
        "Max consecutive losses",
        statistics["maximum_consecutive_losses"],
        border=True,
    )
    if statistics["sample_status"] == "INSUFFICIENT_SAMPLE":
        st.warning(
            f"Insufficient sample: {statistics['trades']} trades; "
            f"{statistics['sample_threshold']} are required before "
            "drawing even preliminary conclusions."
        )


# ==================================================
# MAIN HEADER
# ==================================================

st.title("NEXORA :blue[FX]")

st.caption(
    "INTELLIGENCE. PRECISION. DISCIPLINE."
)

st.caption(
    f"Professional Forex analysis | Operating mode: {_display_mode}"
)


# ==================================================
# CURRENCY PAIR MANAGER
# ==================================================

if page == "Currency Pair Manager":

    st.subheader("Currency Pair Manager")

    st.write(
        "Choose the instruments NEXORA FX should monitor."
    )

    st.info(
        "Selected instruments are monitored by the system. "
        "Trading remains disabled."
    )

    current_symbols = load_symbols()

    selected_symbols = st.multiselect(
        "Select currency pairs and instruments",
        options=AVAILABLE_SYMBOLS,
        default=current_symbols,
        help=(
            "Choose the markets you want NEXORA FX "
            "to monitor."
        ),
    )

    st.divider()

    st.subheader("Your Selected Instruments")

    if selected_symbols:

        for symbol in selected_symbols:
            st.write(f"✓ {symbol}")

    else:

        st.warning(
            "No instruments selected. "
            "Select at least one instrument."
        )

    st.divider()

    if st.button(
        "Save Market Configuration",
        type="primary",
        disabled=len(selected_symbols) == 0,
    ):

        save_symbols(selected_symbols)

        st.cache_data.clear()

        st.success(
            "Your market configuration has been saved!"
        )

        st.rerun()


# ==================================================
# OVERVIEW PAGE
# ==================================================

elif page == "Overview":

    account, prices = get_market_snapshot()

    if account is None:

        st.error(
            "Unable to connect to MetaTrader 5. "
            "Please make sure the MT5 terminal is open and connected."
        )

        st.stop()

    st.subheader("Account Overview")

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Balance",
        f"{account['balance']:,.2f} {account['currency']}",
        border=True,
    )

    col2.metric(
        "Equity",
        f"{account['equity']:,.2f} {account['currency']}",
        border=True,
    )

    col3.metric(
        "Currency",
        account["currency"],
        border=True,
    )

    col4.metric(
        "Leverage",
        f"1:{account['leverage']}",
        border=True,
    )

    st.divider()

    st.subheader("Live Market Watch")

    rows = []

    for symbol, price in prices.items():

        if price:

            rows.append({
                "Currency Pair": symbol,
                "Bid": price["bid"],
                "Ask": price["ask"],
                "Spread": price["spread"],
            })

    if rows:

        market_df = pd.DataFrame(rows)

        st.dataframe(
            market_df,
            width="stretch",
            hide_index=True,
            alt="Live bid, ask, and spread quotes for selected markets",
        )

    else:

        st.warning(
            "No live market prices are currently available."
        )

    st.divider()

    st.subheader("Market Summary")

    for symbol in SYMBOLS:

        data, error = load_market_data(
            symbol,
            mt5.TIMEFRAME_H1,
            500,
        )

        if data is not None and not data.empty:

            latest = data.iloc[-1]

            with st.container(border=True):

                st.markdown(f"### {symbol}")

                col1, col2, col3 = st.columns(3)

                col1.metric(
                    "Last Close",
                    f"{latest['close']:.5f}",
                    border=True,
                )

                col2.metric(
                    "RSI 14",
                    f"{latest['RSI_14']:.2f}",
                    border=True,
                )

                col3.metric(
                    "ADX 14",
                    f"{latest['ADX_14']:.2f}",
                    border=True,
                )

        else:

            st.warning(
                f"{symbol}: {error or 'Market data unavailable'}"
            )


# ==================================================
# MARKET ANALYSIS PAGE
# ==================================================

elif page == "Market Analysis":

    st.subheader("Market Analysis")

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

        st.stop()

    col1, col2 = st.columns(2)

    with col1:

        selected_symbol = st.selectbox(
            "Select Currency Pair",
            SYMBOLS,
        )

    with col2:

        selected_timeframe = st.selectbox(
            "Select Timeframe",
            list(TIMEFRAMES.keys()),
            index=1,
        )

    data, error = load_market_data(
        selected_symbol,
        TIMEFRAMES[selected_timeframe],
        500,
    )

    if data is None or data.empty:

        st.error(
            error or "No market data available."
        )

    else:

        has_volume = "tick_volume" in data.columns
        fig = make_subplots(
            rows=2 if has_volume else 1,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.035,
            row_heights=[0.78, 0.22] if has_volume else [1],
        )

        fig.add_trace(
            go.Candlestick(
                x=data["time"],
                open=data["open"],
                high=data["high"],
                low=data["low"],
                close=data["close"],
                name="Price",
                increasing_line_color="#34D399",
                increasing_fillcolor="#34D399",
                decreasing_line_color="#FB7185",
                decreasing_fillcolor="#FB7185",
            ),
            row=1,
            col=1,
        )

        for column, label, color, width in (
            ("EMA_20", "EMA 20", "#60A5FA", 1.4),
            ("EMA_50", "EMA 50", "#FBBF24", 1.4),
            ("EMA_200", "EMA 200", "#C084FC", 1.8),
        ):
            fig.add_trace(
                go.Scatter(
                    x=data["time"],
                    y=data[column],
                    name=label,
                    line=dict(color=color, width=width),
                    mode="lines",
                ),
                row=1,
                col=1,
            )

        if has_volume:
            volume_colors = [
                "#34D399" if close >= open_price else "#FB7185"
                for open_price, close in zip(data["open"], data["close"])
            ]
            fig.add_trace(
                go.Bar(
                    x=data["time"],
                    y=data["tick_volume"],
                    name="Tick volume",
                    marker_color=volume_colors,
                    opacity=0.65,
                ),
                row=2,
                col=1,
            )
            fig.update_yaxes(
                title_text="Tick volume",
                showgrid=False,
                row=2,
                col=1,
            )

        fig.update_layout(
            template="plotly_dark",
            height=720 if has_volume else 620,
            title=dict(
                text=f"{selected_symbol} · {selected_timeframe}",
                x=0.02,
                xanchor="left",
            ),
            paper_bgcolor="#0B1120",
            plot_bgcolor="#111B2E",
            font=dict(color="#D8E2F0", family="Arial, sans-serif"),
            hovermode="x unified",
            dragmode="pan",
            margin=dict(l=56, r=28, t=76, b=36),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
                bgcolor="rgba(17, 27, 46, 0.72)",
                bordercolor="#25334A",
                borderwidth=1,
            ),
            xaxis_rangeslider_visible=False,
            uirevision=f"{selected_symbol}-{selected_timeframe}",
        )
        fig.update_xaxes(
            showgrid=False,
            showspikes=True,
            spikemode="across",
            spikesnap="cursor",
            spikecolor="#64748B",
            spikethickness=1,
        )
        fig.update_yaxes(
            showgrid=True,
            gridcolor="rgba(148, 163, 184, 0.12)",
            zeroline=False,
            fixedrange=False,
        )

        st.plotly_chart(
            fig,
            width="stretch",
            config={
                "displaylogo": False,
                "scrollZoom": True,
            },
            alt=(
                f"{selected_symbol} {selected_timeframe} candlestick chart "
                "with EMA overlays and tick volume"
                if has_volume
                else f"{selected_symbol} {selected_timeframe} candlestick chart "
                "with EMA overlays"
            ),
        )

        latest = data.iloc[-1]

        st.divider()

        st.subheader("Latest Completed Candle")

        col1, col2, col3, col4 = st.columns(4)

        col1.metric("Open", f"{latest['open']:.5f}", border=True)
        col2.metric("High", f"{latest['high']:.5f}", border=True)
        col3.metric("Low", f"{latest['low']:.5f}", border=True)
        col4.metric("Close", f"{latest['close']:.5f}", border=True)


# ==================================================
# TECHNICAL INDICATORS PAGE
# ==================================================

elif page == "Technical Indicators":

    st.subheader("Technical Analysis")

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

        st.stop()

    col1, col2 = st.columns(2)

    with col1:

        selected_symbol = st.selectbox(
            "Currency Pair",
            SYMBOLS,
        )

    with col2:

        selected_timeframe = st.selectbox(
            "Timeframe",
            list(TIMEFRAMES.keys()),
            index=1,
        )

    data, error = load_market_data(
        selected_symbol,
        TIMEFRAMES[selected_timeframe],
        500,
    )

    if data is None or data.empty:

        st.error(
            error or "Technical analysis unavailable."
        )

    else:

        latest = data.iloc[-1]

        st.caption(
            f"Latest completed candle: {latest['time']}"
        )

        st.subheader("Momentum & Volatility")

        c1, c2, c3 = st.columns(3)

        c1.metric("RSI 14", f"{latest['RSI_14']:.2f}", border=True)
        c2.metric("ADX 14", f"{latest['ADX_14']:.2f}", border=True)
        c3.metric("ATR 14", f"{latest['ATR_14']:.5f}", border=True)

        st.divider()

        st.subheader("Moving Averages")

        c1, c2, c3 = st.columns(3)

        c1.metric("EMA 20", f"{latest['EMA_20']:.5f}", border=True)
        c2.metric("EMA 50", f"{latest['EMA_50']:.5f}", border=True)
        c3.metric("EMA 200", f"{latest['EMA_200']:.5f}", border=True)

        st.divider()

        st.subheader("MACD")

        c1, c2, c3 = st.columns(3)

        c1.metric("MACD", f"{latest['MACD']:.6f}", border=True)
        c2.metric("Signal", f"{latest['MACD_SIGNAL']:.6f}", border=True)
        c3.metric("Histogram", f"{latest['MACD_HIST']:.6f}", border=True)

        st.divider()

        st.subheader("Bollinger Bands")

        c1, c2, c3 = st.columns(3)

        c1.metric("Upper Band", f"{latest['BB_UPPER']:.5f}", border=True)
        c2.metric("Middle Band", f"{latest['BB_MIDDLE']:.5f}", border=True)
        c3.metric("Lower Band", f"{latest['BB_LOWER']:.5f}", border=True)

        st.divider()

        with st.expander("View Historical Indicator Data"):

            st.dataframe(
                data.tail(50),
                width="stretch",
                hide_index=True,
                alt="Most recent completed candles with calculated technical indicators",
            )


# ==================================================
# SECTION 1 ENDS HERE
# SECTION 2 CONTINUES WITH MARKET REGIME
# ==================================================

# ==================================================
# MARKET REGIME PAGE
# ==================================================

elif page == "Market Regime":

    st.header("Market Regime Analysis")

    st.caption(
        "H1 timeframe | Completed candles | Research mode"
    )

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

    else:

        for symbol in SYMBOLS:

            st.subheader(symbol)

            analysis, error = load_market_data(
                symbol,
                mt5.TIMEFRAME_H1,
            )

            if error:

                st.error(f"{symbol}: {error}")
                continue

            regime_data = detect_market_regime(analysis)

            if regime_data["regime"] == "INSUFFICIENT_DATA":

                st.warning(regime_data["reason"])
                continue

            col1, col2, col3, col4 = st.columns(4)

            col1.metric(
                "Market Regime",
                regime_data["regime"],
            )

            col2.metric("ADX", regime_data["adx"])
            col3.metric("ATR", regime_data["atr"])

            col4.metric(
                "Volatility Ratio",
                regime_data["volatility_ratio"],
            )

            st.write(
                "EMA Alignment:",
                regime_data["ema_alignment"],
            )

            st.caption(regime_data["reason"])

            st.divider()


# ==================================================
# MARKET STRUCTURE PAGE
# ==================================================

elif page == "Market Structure":

    st.header("Market Structure Analysis")

    st.caption(
        "H1 timeframe | Confirmed swing points | Research mode"
    )

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

    else:

        for symbol in SYMBOLS:

            st.subheader(symbol)

            analysis, error = load_market_data(
                symbol,
                mt5.TIMEFRAME_H1,
            )

            if error:

                st.error(f"{symbol}: {error}")
                continue

            result = detect_market_structure(
                analysis,
                swing_length=3,
            )

            if not result["valid"]:

                st.warning(result["reason"])
                continue

            col1, col2, col3 = st.columns(3)

            col1.metric(
                "Market Direction",
                result["trend"],
            )

            col2.metric(
                "Structure Event",
                result["event"],
            )

            col3.metric(
                "Confirmed Swing Highs",
                result["swing_high_count"],
            )

            st.markdown("#### Swing Levels")

            col4, col5 = st.columns(2)

            col4.metric(
                "Latest Swing High",
                f'{result["latest_swing_high"]:.5f}',
            )

            col5.metric(
                "Previous Swing High",
                f'{result["previous_swing_high"]:.5f}',
            )

            col6, col7 = st.columns(2)

            col6.metric(
                "Latest Swing Low",
                f'{result["latest_swing_low"]:.5f}',
            )

            col7.metric(
                "Previous Swing Low",
                f'{result["previous_swing_low"]:.5f}',
            )

            st.markdown("#### Structure Characteristics")

            st.write(
                f'Higher High (HH): '
                f'{"Yes" if result["higher_high"] else "No"}'
            )

            st.write(
                f'Higher Low (HL): '
                f'{"Yes" if result["higher_low"] else "No"}'
            )

            st.write(
                f'Lower High (LH): '
                f'{"Yes" if result["lower_high"] else "No"}'
            )

            st.write(
                f'Lower Low (LL): '
                f'{"Yes" if result["lower_low"] else "No"}'
            )

            if result["broken_level"] is not None:

                st.write(
                    f'Broken structural level: '
                    f'{result["broken_level"]}'
                )

            st.caption(result["reason"])

            st.divider()


# ==================================================
# SMC SETUP DIAGNOSTICS PAGE
# ==================================================

elif page == "SMC Setup Diagnostics":

    st.header("SMC Setup Diagnostics")

    st.caption(
        "H4 / H1 / M15 | Smart Money Concepts | Research mode"
    )

    st.info(
        "This module evaluates market structure, liquidity, "
        "order blocks and fair value gaps. "
        "It does not place or modify trades."
    )

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

    else:

        selected_symbol = st.selectbox(
            "Select Currency Pair",
            SYMBOLS,
            key="smc_diagnostics_symbol",
        )

        st.write(
            f"Selected instrument: **{selected_symbol}**"
        )

        if st.button(
            "Run SMC Analysis",
            type="primary",
            key="run_smc_diagnostics",
        ):

            with st.spinner(
                "Running H4, H1 and M15 SMC analysis..."
            ):

                analysis_bundle = run_smc_analysis(
                    selected_symbol
                )

            st.session_state["smc_setup_bundle"] = analysis_bundle
            st.session_state["smc_setup_symbol"] = selected_symbol

        analysis_bundle = st.session_state.get(
            "smc_setup_bundle"
        )

        stored_symbol = st.session_state.get(
            "smc_setup_symbol"
        )

        if analysis_bundle is None:

            st.info(
                "Click Run SMC Analysis to begin."
            )

        elif stored_symbol != selected_symbol:

            st.info(
                "Run SMC Analysis for the newly selected instrument."
            )

        elif not analysis_bundle.get("valid", False):

            st.error(
                analysis_bundle.get(
                    "reason",
                    "SMC analysis failed.",
                )
            )

            if "mtf_result" in analysis_bundle:

                with st.expander("Multi-timeframe analysis details"):
                    st.json(analysis_bundle["mtf_result"])

            if "zone_result" in analysis_bundle:

                with st.expander("Zone validation details"):
                    st.json(analysis_bundle["zone_result"])

        else:

            mtf_result = analysis_bundle["mtf_result"]
            zone_result = analysis_bundle["zone_result"]
            setup_result = analysis_bundle["setup_result"]
            diagnostics = analysis_bundle["diagnostics"]

            st.success(
                f"SMC analysis completed for {selected_symbol}."
            )

            st.divider()

            st.subheader("Setup Classification")

            col1, col2, col3 = st.columns(3)

            col1.metric(
                "Market Bias",
                setup_result.get("market_bias", "UNKNOWN"),
            )

            col2.metric(
                "Classification",
                setup_result.get("classification", "UNKNOWN"),
            )

            col3.metric("Execution", "DISABLED")

            st.write(
                "**Evaluation explanation:**",
                setup_result.get(
                    "reason",
                    "No explanation available.",
                ),
            )

            st.divider()

            st.subheader("Multi-Timeframe Structure")

            timeframes = mtf_result.get("timeframes", {})

            for timeframe_name in ("H4", "H1", "M15"):

                timeframe_data = timeframes.get(
                    timeframe_name,
                    {},
                )

                with st.container(border=True):

                    st.markdown(f"#### {timeframe_name}")

                    c1, c2, c3 = st.columns(3)

                    c1.metric(
                        "Trend",
                        timeframe_data.get("trend", "UNKNOWN"),
                    )

                    c2.metric(
                        "Structure Event",
                        timeframe_data.get("event", "NONE") or "NONE",
                    )

                    c3.metric(
                        "Event Direction",
                        timeframe_data.get(
                            "event_direction",
                            "UNKNOWN",
                        ) or "UNKNOWN",
                    )

                    st.caption(
                        "Event age: "
                        f"{timeframe_data.get('event_age_bars', 'N/A')} bars"
                    )

                    st.caption(
                        "Event count: "
                        f"{timeframe_data.get('event_count', 0)}"
                    )

            st.divider()

            st.subheader("M15 Liquidity Analysis")

            liquidity = setup_result.get("liquidity_sweep", {})

            c1, c2, c3 = st.columns(3)

            c1.metric(
                "Supports Market Bias",
                "YES" if liquidity.get("supports_bias", False) else "NO",
            )

            c2.metric(
                "Matching Unique Levels",
                liquidity.get("matching_unique_level_count", 0),
            )

            c3.metric(
                "Opposing Unique Levels",
                liquidity.get("opposing_unique_level_count", 0),
            )

            st.write(
                liquidity.get(
                    "reason",
                    "Liquidity explanation unavailable.",
                )
            )

            with st.expander("View M15 liquidity sweep details"):
                st.json(liquidity)

            st.divider()

            st.subheader("SMC Zone Validation")

            eligible_zones = zone_result.get("eligible_zones", [])
            eligible_m15_zones = setup_result.get("eligible_m15_zones", [])
            touched_m15_zones = setup_result.get("touched_m15_zones", [])

            if not isinstance(eligible_zones, list):
                eligible_zones = []

            if not isinstance(eligible_m15_zones, list):
                eligible_m15_zones = []

            if not isinstance(touched_m15_zones, list):
                touched_m15_zones = []

            c1, c2, c3 = st.columns(3)

            c1.metric("All Eligible Zones", len(eligible_zones))
            c2.metric("Eligible M15 Zones", len(eligible_m15_zones))
            c3.metric("Touched M15 Zones", len(touched_m15_zones))

            st.write(
                "**Zone validation:**",
                zone_result.get("reason", "No explanation available."),
            )

            with st.expander("View all eligible validated zones"):
                st.json(eligible_zones)

            with st.expander("View eligible M15 setup zones"):
                st.json(eligible_m15_zones)

            with st.expander("View complete zone validation output"):
                st.json(zone_result)

            st.divider()

            st.subheader("Setup Evidence")

            evidence = setup_result.get("evidence", [])

            if evidence:

                for item in evidence:
                    st.success(item)

            else:

                st.info("No qualifying setup evidence recorded.")

            st.divider()

            st.subheader("Missing Conditions")

            missing = setup_result.get("missing_conditions", [])

            if missing:

                for item in missing:
                    st.warning(item)

            else:

                st.success("No missing conditions reported.")

            st.divider()

            st.subheader("Warnings")

            warnings = setup_result.get("warnings", [])

            if warnings:

                for item in warnings:
                    st.warning(item)

            else:

                st.info("No warnings reported.")

            st.divider()

            st.subheader("Recommended Waiting Conditions")

            if isinstance(diagnostics, dict):

                waiting_conditions = diagnostics.get(
                    "recommended_actions",
                    [],
                )

                if waiting_conditions:

                    for condition in waiting_conditions:
                        st.info(condition)

                else:

                    st.write(
                        "No additional waiting conditions were returned."
                    )

            else:

                st.write(
                    "Detailed waiting conditions are available "
                    "in the diagnostics output below."
                )

            st.divider()

            st.subheader("Detailed Setup Diagnostics")

            if isinstance(diagnostics, dict):
                st.json(diagnostics)
            else:
                st.write(diagnostics)

            with st.expander("View complete setup evaluator output"):
                st.json(setup_result)

            with st.expander("View complete multi-timeframe output"):
                st.json(mtf_result)

            st.divider()

            st.info("This zone diagnostics page is read-only.")

            st.caption(
                "Order entry is handled only by the separate runner and "
                "its backend permission gate."
            )


# ==================================================
# SMC ZONE DIAGNOSTICS PAGE
# ==================================================

elif page == "SMC Zone Diagnostics":

    st.header("SMC Zone Diagnostics")

    st.caption(
        "H4 / H1 / M15 | Order Blocks and Fair Value Gaps | Research mode"
    )

    st.info(
        "This page examines detected SMC zones and explains "
        "which zones pass or fail validation. "
        "It does not place or modify trades."
    )

    st.markdown(
        """
        **Validation examines:**
        - Zone age
        - ATR distance from current price
        - Market direction
        - Price position
        - Zone status
        - Previous touches and invalidation
        - Fair Value Gap filling
        """
    )

    if not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

    else:

        selected_symbol = st.selectbox(
            "Select Currency Pair",
            SYMBOLS,
            key="zone_diagnostics_symbol",
        )

        if st.button(
            "Run Zone Diagnostics",
            type="primary",
            key="run_zone_diagnostics",
        ):

            with st.spinner(
                "Analyzing market structure and validating SMC zones..."
            ):

                zone_bundle = run_zone_analysis(
                    selected_symbol
                )

            st.session_state["zone_diagnostics_bundle"] = zone_bundle

            st.session_state["zone_diagnostics_result_symbol"] = (
                selected_symbol
            )

        bundle = st.session_state.get(
            "zone_diagnostics_bundle"
        )

        stored_symbol = st.session_state.get(
            "zone_diagnostics_result_symbol"
        )

        if bundle is None:

            st.info(
                "Select a currency pair and click "
                "'Run Zone Diagnostics' to begin."
            )

        elif stored_symbol != selected_symbol:

            st.info(
                "The selected currency pair has changed. "
                "Click Run Zone Diagnostics to analyze the new pair."
            )

        elif not bundle.get("valid", False):

            st.error(
                bundle.get(
                    "reason",
                    "Zone diagnostics could not be completed.",
                )
            )

            if bundle.get("mtf_result") is not None:

                with st.expander("Multi-timeframe analysis details"):
                    st.json(bundle["mtf_result"])

            if bundle.get("zone_result") is not None:

                with st.expander("Zone validation details"):
                    st.json(bundle["zone_result"])

        else:

            zone_result = bundle.get("zone_result", {})

            if not isinstance(zone_result, dict):

                st.error("Invalid zone validation output.")

            else:

                st.success(
                    f"Zone analysis completed for {selected_symbol}."
                )

                st.divider()

                st.subheader("Zone Validation Overview")

                detected = zone_result.get("zones_detected", 0)
                examined = zone_result.get("zones_examined", 0)
                skipped = zone_result.get("zones_skipped", 0)
                eligible_count = zone_result.get("eligible_count", 0)
                rejected_count = zone_result.get("rejected_count", 0)

                c1, c2, c3, c4, c5 = st.columns(5)

                c1.metric("Detected", detected)
                c2.metric("Examined", examined)
                c3.metric("Eligible", eligible_count)
                c4.metric("Rejected", rejected_count)
                c5.metric("Skipped", skipped)

                st.caption(
                    zone_result.get(
                        "reason",
                        "No validation explanation available.",
                    )
                )

                st.divider()

                st.subheader("Zone Activity")

                c1, c2, c3 = st.columns(3)

                c1.metric(
                    "Approaching Zones",
                    zone_result.get("approaching_count", 0),
                )

                c2.metric(
                    "Retest Zones",
                    zone_result.get("retest_count", 0),
                )

                c3.metric(
                    "Eligible Zones",
                    eligible_count,
                )

                st.divider()

                st.subheader("Zone State Breakdown")

                state_counts = zone_result.get(
                    "state_counts",
                    {},
                )

                if isinstance(state_counts, dict) and state_counts:

                    state_df = pd.DataFrame(
                        [
                            {
                                "Zone State": state,
                                "Count": count,
                            }
                            for state, count in state_counts.items()
                        ]
                    )

                    st.dataframe(
                        state_df,
                        width="stretch",
                        hide_index=True,
                        alt="Counts of SMC zones grouped by validation state",
                    )

                    st.bar_chart(
                        state_df.set_index("Zone State"),
                        alt="Number of SMC zones in each validation state",
                    )

                else:

                    st.info(
                        "No zone state statistics are available."
                    )

                st.divider()

                st.subheader("Timeframe Breakdown")

                timeframe_counts = zone_result.get(
                    "timeframe_counts",
                    {},
                )

                if isinstance(timeframe_counts, dict) and timeframe_counts:
                    st.json(timeframe_counts)
                else:
                    st.info("No timeframe breakdown was returned.")

                st.divider()

                st.subheader("Detection Breakdown")

                detected_counts = zone_result.get(
                    "detected_counts",
                    {},
                )

                examined_counts = zone_result.get(
                    "examined_counts",
                    {},
                )

                c1, c2 = st.columns(2)

                with c1:
                    st.markdown("#### Detected zones")
                    st.json(detected_counts)

                with c2:
                    st.markdown("#### Examined zones")
                    st.json(examined_counts)

                st.divider()

                st.subheader("Zone Rejection Analysis")

                rejection_counts = zone_result.get(
                    "rejection_counts",
                    {},
                )

                if isinstance(rejection_counts, dict) and rejection_counts:

                    rejection_df = pd.DataFrame(
                        [
                            {
                                "Rejection Reason": reason,
                                "Count": count,
                            }
                            for reason, count in rejection_counts.items()
                        ]
                    )

                    rejection_df = rejection_df.sort_values(
                        "Count",
                        ascending=False,
                    )

                    st.dataframe(
                        rejection_df,
                        width="stretch",
                        hide_index=True,
                        alt="SMC zone rejection reasons ranked by occurrence count",
                    )

                    st.bar_chart(
                        rejection_df.set_index("Rejection Reason"),
                        alt="Frequency of SMC zone rejection reasons",
                    )

                    st.caption(
                        "Rejection reasons can overlap. One zone may have "
                        "several rejection reasons, so these counts do not "
                        "necessarily add up to the total rejected zones."
                    )

                else:

                    st.info("No rejection reasons were recorded.")

                st.divider()

                st.subheader("Individual Zone Explorer")

                all_zones = zone_result.get(
                    "all_zones",
                    [],
                )

                if not isinstance(all_zones, list):
                    all_zones = []

                if not all_zones:

                    st.warning(
                        "No individual zone records were returned."
                    )

                else:

                    zones_df = pd.DataFrame(all_zones)

                    st.write(
                        f"Total zone records available: {len(zones_df)}"
                    )

                    if "timeframe" in zones_df.columns:

                        timeframe_options = ["All"] + sorted(
                            zones_df["timeframe"]
                            .dropna()
                            .astype(str)
                            .unique()
                            .tolist()
                        )

                        selected_tf = st.selectbox(
                            "Filter by timeframe",
                            timeframe_options,
                            key="zone_filter_timeframe",
                        )

                        if selected_tf != "All":

                            zones_df = zones_df[
                                zones_df["timeframe"].astype(str)
                                == selected_tf
                            ]

                    if "zone_type" in zones_df.columns:

                        type_options = ["All"] + sorted(
                            zones_df["zone_type"]
                            .dropna()
                            .astype(str)
                            .unique()
                            .tolist()
                        )

                        selected_type = st.selectbox(
                            "Filter by zone type",
                            type_options,
                            key="zone_filter_type",
                        )

                        if selected_type != "All":

                            zones_df = zones_df[
                                zones_df["zone_type"].astype(str)
                                == selected_type
                            ]

                    # Support either "state" or "zone_state".
                    state_column = (
                        "state"
                        if "state" in zones_df.columns
                        else (
                            "zone_state"
                            if "zone_state" in zones_df.columns
                            else None
                        )
                    )

                    if state_column:

                        state_options = ["All"] + sorted(
                            zones_df[state_column]
                            .dropna()
                            .astype(str)
                            .unique()
                            .tolist()
                        )

                        selected_state = st.selectbox(
                            "Filter by zone state",
                            state_options,
                            key="zone_filter_state",
                        )

                        if selected_state != "All":

                            zones_df = zones_df[
                                zones_df[state_column].astype(str)
                                == selected_state
                            ]

                    if "eligible" in zones_df.columns:

                        eligibility_filter = st.selectbox(
                            "Filter by validation result",
                            [
                                "All",
                                "Eligible only",
                                "Rejected only",
                            ],
                            key="zone_filter_eligibility",
                        )

                        if eligibility_filter == "Eligible only":

                            zones_df = zones_df[
                                zones_df["eligible"] == True
                            ]

                        elif eligibility_filter == "Rejected only":

                            zones_df = zones_df[
                                zones_df["eligible"] == False
                            ]

                    preferred_columns = [
                        "timeframe",
                        "zone_type",
                        "zone_number",
                        "direction",
                        "status",
                        "state",
                        "zone_state",
                        "zone_high",
                        "zone_low",
                        "age_bars",
                        "distance",
                        "distance_atr",
                        "eligible",
                        "rejection_reasons",
                    ]

                    display_columns = [
                        column
                        for column in preferred_columns
                        if column in zones_df.columns
                    ]

                    if display_columns:

                        st.dataframe(
                            zones_df[display_columns],
                            width="stretch",
                            hide_index=True,
                            alt="SMC zone records matching the selected filters",
                        )

                    else:

                        st.dataframe(
                            zones_df,
                            width="stretch",
                            hide_index=True,
                            alt="SMC zone records matching the selected filters",
                        )

                    st.caption(
                        f"Showing {len(zones_df)} zone records "
                        "after applying filters."
                    )

                    csv_data = zones_df.to_csv(
                        index=False
                    ).encode("utf-8")

                    st.download_button(
                        label="Download Filtered Zone Records (CSV)",
                        data=csv_data,
                        file_name=(
                            f"{selected_symbol}_smc_zone_diagnostics.csv"
                        ),
                        mime="text/csv",
                        key="download_zone_diagnostics",
                    )

                    st.divider()

                    st.subheader("Detailed Zone Inspection")

                    if len(zones_df) > 0:

                        zone_options = list(range(len(zones_df)))

                        selected_zone_index = st.selectbox(
                            "Select a zone record",
                            zone_options,
                            format_func=lambda index: (
                                f"Record {index + 1} | "
                                f"{zones_df.iloc[index].get('timeframe', 'N/A')} | "
                                f"{zones_df.iloc[index].get('zone_type', 'N/A')} | "
                                f"{zones_df.iloc[index].get('direction', 'N/A')} | "
                                f"{zones_df.iloc[index].get(state_column, 'N/A') if state_column else 'N/A'}"
                            ),
                            key="selected_zone_record",
                        )

                        selected_zone = zones_df.iloc[
                            selected_zone_index
                        ].to_dict()

                        st.json(selected_zone)

                    else:

                        st.info(
                            "No zones match the selected filters."
                        )

                st.divider()

                st.subheader("Eligible Zones")

                eligible_zones = zone_result.get(
                    "eligible_zones",
                    [],
                )

                if isinstance(eligible_zones, list) and eligible_zones:

                    st.dataframe(
                        pd.DataFrame(eligible_zones),
                        width="stretch",
                        hide_index=True,
                        alt="SMC zones that passed validation",
                    )

                else:

                    st.info(
                        "No zones passed all validation conditions."
                    )

                st.divider()

                st.subheader("Rejected Zones")

                rejected_zones = zone_result.get(
                    "rejected_zones",
                    [],
                )

                if isinstance(rejected_zones, list) and rejected_zones:

                    with st.expander(
                        f"View {len(rejected_zones)} rejected zones"
                    ):

                        st.dataframe(
                            pd.DataFrame(rejected_zones),
                            width="stretch",
                            hide_index=True,
                            alt="SMC zones rejected during validation",
                        )

                else:

                    st.success(
                        "No rejected zones were returned."
                    )

                st.divider()

                st.subheader("Skipped Zone Records")

                skipped_zones = zone_result.get(
                    "skipped_zones",
                    [],
                )

                if isinstance(skipped_zones, list) and skipped_zones:

                    st.dataframe(
                        pd.DataFrame(skipped_zones),
                        width="stretch",
                        hide_index=True,
                        alt="SMC zone records skipped during validation",
                    )

                else:

                    st.success(
                        "No zones were skipped during validation."
                    )

                st.divider()

                st.subheader("Validation Errors")

                timeframe_errors = zone_result.get(
                    "timeframe_errors",
                    [],
                )

                if timeframe_errors:

                    st.warning(
                        "Some timeframe validation errors were reported."
                    )

                    st.json(timeframe_errors)

                else:

                    st.success(
                        "No timeframe validation errors were reported."
                    )

                with st.expander(
                    "View multi-timeframe analysis output"
                ):
                    st.json(bundle.get("mtf_result", {}))

                with st.expander(
                    "View complete zone validation output"
                ):
                    st.json(zone_result)

                st.divider()

                st.info("This zone diagnostics page is read-only.")

                st.caption(
                    "Order entry is handled only by the separate runner and "
                    "its backend permission gate."
                )


# ==================================================
# SIGNAL INTELLIGENCE PAGE
# ==================================================

elif page == "Signal Intelligence":

    st.header("NEXORA FX Signal Intelligence")

    st.caption(
        "Multi-timeframe signal evaluation | H4 / H1 / M15"
    )

    st.caption(
        "Quality scores rank satisfied rule criteria; they are not "
        "win probabilities and are not calibrated against historical outcomes."
    )

    st.info(
        "The signal engine evaluates market conditions and "
        "identifies potential trading setups. "
        "This page is for research only."
    )

    st.info(
        "This page performs read-only signal analysis and never submits an "
        "order. Automated entries, if configured, are handled separately by "
        "the runner and its backend permission gate."
    )

    if not _agent_enabled:

        st.warning(
            "The trading agent is currently OFF. "
            "Turn it ON using the sidebar button to enable signal scanning."
        )

    elif not SYMBOLS:

        st.warning(
            "No instruments selected. "
            "Open Currency Pair Manager to select instruments."
        )

    else:

        st.success("Manual signal scanning is enabled.")

        st.write(
            f"Selected instruments: {', '.join(SYMBOLS)}"
        )

        if st.button(
            "Run Signal Scan",
            type="primary",
            key="run_signal_scan",
            width="stretch",
        ):

            scan_results = []

            progress = st.progress(0)

            status = st.empty()

            for index, symbol in enumerate(SYMBOLS):

                status.write(
                    f"Analyzing {symbol}..."
                )

                result = run_signal_analysis(symbol)

                signal_result = (
                    result.get("signal_result", {})
                    if isinstance(result, dict)
                    else {}
                )

                if not isinstance(signal_result, dict):
                    signal_result = {}

                persistence_error = None
                if result.get("valid") and signal_result:
                    try:
                        persisted = record_signal(
                            symbol=symbol,
                            signal_result=signal_result,
                            signal_timestamp=result.get("signal_timestamp"),
                        )
                        signal_result["signal_id"] = persisted["signal_id"]
                    except (sqlite3.Error, ValueError, RuntimeError) as error:
                        persistence_error = str(error)

                conditions = signal_result.get(
                    "conditions",
                    {},
                )

                if not isinstance(conditions, dict):
                    conditions = {}

                scan_results.append({
                    "symbol": symbol,
                    "valid": result.get("valid", False),
                    "classification": signal_result.get(
                        "classification",
                        "ERROR",
                    ),
                    "direction": signal_result.get(
                        "direction",
                        "UNKNOWN",
                    ),
                    "quality_score": signal_result.get(
                        "quality_score",
                        0,
                    ),
                    "quality_label": signal_result.get(
                        "quality_label",
                        "UNSCORED",
                    ),
                    "reason": signal_result.get(
                        "reason",
                        result.get("reason", "No explanation returned."),
                    ),
                    "h4": conditions.get("h4", "N/A"),
                    "h1": conditions.get("h1", "N/A"),
                    "m15": conditions.get("m15", "N/A"),
                    "execution_permitted": False,
                    "persistence_error": persistence_error,
                    "full_result": result,
                })

                progress.progress(
                    (index + 1) / len(SYMBOLS)
                )

            scan_results.sort(
                key=lambda item: item["quality_score"],
                reverse=True,
            )
            st.session_state["signal_scan_results"] = scan_results

            status.success(
                "Signal scan completed."
            )

            persistence_failures = [
                f"{item['symbol']}: {item['persistence_error']}"
                for item in scan_results
                if item.get("persistence_error")
            ]
            if persistence_failures:
                st.error(
                    "Signal results could not be persisted:\n"
                    + "\n".join(persistence_failures)
                )

        scan_results = st.session_state.get(
            "signal_scan_results",
            [],
        )

        if scan_results:

            st.divider()

            st.subheader("Signal Scan Results")

            display_rows = []

            for rank, item in enumerate(scan_results, start=1):

                display_rows.append({
                    "Rank": rank,
                    "Symbol": item["symbol"],
                    "Analysis": (
                        "SUCCESS"
                        if item["valid"]
                        else "ERROR"
                    ),
                    "Classification": item["classification"],
                    "Direction": item["direction"],
                    "Quality": item.get("quality_label", "UNSCORED"),
                    "Score": round(item.get("quality_score", 0), 2),
                    "H4": item["h4"],
                    "H1": item["h1"],
                    "M15": item["m15"],
                    "Execution": "DISABLED",
                })

            results_df = pd.DataFrame(display_rows)

            st.dataframe(
                results_df,
                width="stretch",
                hide_index=True,
                alt="Signal scan results and multi-timeframe conditions by symbol",
            )

            st.divider()

            st.subheader("Individual Signal Explanations")

            for item in scan_results:

                with st.expander(
                    f"{item['symbol']} — {item['classification']} "
                    f"({item['direction']})"
                ):

                    if item["valid"]:

                        signal_result = item["full_result"].get(
                            "signal_result",
                            {},
                        )

                        st.write(
                            "**Reason:**",
                            item["reason"],
                        )

                        st.write(
                            "**Execution permitted:** NO"
                        )
                        if signal_result.get("signal_id"):
                            st.caption(
                                f"Persistent signal ID: "
                                f"{signal_result['signal_id']}"
                            )

                        st.metric(
                            "Rule-based quality score",
                            f"{item.get('quality_score', 0):.2f} / 100",
                            border=True,
                        )

                        quality_breakdown = signal_result.get(
                            "quality_breakdown",
                            {},
                        )
                        if isinstance(quality_breakdown, dict):
                            st.json(quality_breakdown)

                        if isinstance(signal_result, dict):

                            missing = signal_result.get(
                                "missing_conditions",
                                [],
                            )

                            warnings = signal_result.get(
                                "warnings",
                                [],
                            )

                            if missing:

                                st.markdown("**Missing conditions**")

                                for condition in missing:
                                    st.warning(condition)

                            if warnings:

                                st.markdown("**Warnings**")

                                for warning in warnings:
                                    st.warning(warning)

                    else:

                        st.error(
                            item["full_result"].get(
                                "reason",
                                "Signal analysis failed.",
                            )
                        )

                    with st.expander("Complete analysis output"):
                        st.json(item["full_result"])

            st.divider()

            st.caption(
                "A CONFIRMED classification is an analytical result, "
                "not a guarantee of profitability. "
                "All signals require independent review."
            )

        else:

            st.info(
                "No scan results yet. Click Run Signal Scan "
                "to analyze the selected instruments."
            )


# ==================================================
# ADVISORY TRADE PLANNER
# ==================================================

elif page == "Advisory Trade Planner":

    st.subheader("Advisory Trade Planner")
    st.caption(
        "Broker-specification-based planning only. This page cannot place, "
        "modify, or close an MT5 order."
    )
    st.info("This advisory form never submits an MT5 order.")

    if not SYMBOLS:
        st.warning("Select at least one instrument in Currency Pair Manager.")
        st.stop()

    with st.form("advisory_trade_plan_form"):
        symbol = st.selectbox("Broker symbol", SYMBOLS, key="plan_symbol")
        direction = st.selectbox(
            "Direction",
            ["BUY", "SELL"],
            key="plan_direction",
        )
        entry = st.number_input(
            "Entry reference",
            min_value=0.0,
            value=0.0,
            format="%.8f",
            key="plan_entry",
        )
        stop_loss = st.number_input(
            "Stop-loss reference",
            min_value=0.0,
            value=0.0,
            format="%.8f",
            key="plan_stop",
        )
        take_profit = st.number_input(
            "Take-profit reference",
            min_value=0.0,
            value=0.0,
            format="%.8f",
            key="plan_target",
        )
        risk_percent = st.number_input(
            "Risk budget (%)",
            min_value=0.01,
            max_value=0.25,
            value=DEFAULT_RISK_PERCENT,
            step=0.05,
            format="%.2f",
            key="plan_risk_percent",
        )
        use_account_equity = st.checkbox(
            "Use connected MT5 account equity",
            value=True,
            key="plan_use_account_equity",
        )
        equity = None
        if not use_account_equity:
            equity = st.number_input(
                "Account equity",
                min_value=0.0,
                value=10000.0,
                step=100.0,
                key="plan_equity",
            )
        calculate_plan = st.form_submit_button(
            "Calculate Advisory Plan",
            type="primary",
            width="stretch",
        )

    if calculate_plan:
        plan = get_advisory_trade_plan(
            symbol=symbol,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            equity=equity,
            risk_percent=risk_percent,
        )
        st.session_state["advisory_trade_plan"] = plan

    plan = st.session_state.get("advisory_trade_plan")
    if plan:
        if plan.get("valid"):
            st.success(plan["reason"])
            st.caption("Advisory estimate only — no order was sent.")
            metrics = st.columns(4)
            metrics[0].metric(
                "Estimated volume",
                f"{plan['estimated_volume']:.8f}".rstrip("0").rstrip("."),
                border=True,
            )
            metrics[1].metric(
                "Estimated monetary risk",
                f"{plan['estimated_monetary_risk']:,.2f}",
                border=True,
            )
            metrics[2].metric(
                "Risk : reward",
                f"1 : {plan['risk_reward']:.2f}",
                border=True,
            )
            metrics[3].metric(
                "Estimated margin",
                (
                    f"{plan['margin_estimate']:,.2f}"
                    if plan["margin_estimate"] is not None
                    else "Unavailable"
                ),
                border=True,
            )
            st.json({
                "entry": plan["entry"],
                "stop_loss": plan["stop_loss"],
                "take_profit": plan["take_profit"],
                "stop_distance": plan["stop_distance"],
                "risk_budget": plan["risk_budget"],
                "tick_size": plan["tick_size"],
                "tick_value": plan["tick_value"],
                "volume_min": plan["volume_min"],
                "volume_max": plan["volume_max"],
                "volume_step": plan["volume_step"],
                "equity": plan["equity"],
                "execution_permitted": False,
            })
            if plan.get("margin_warning"):
                st.warning(plan["margin_warning"])
        else:
            st.error(plan.get("reason", "Trade plan could not be calculated."))
            if plan.get("position"):
                st.json(plan["position"])


# ==================================================
# RISK CALCULATOR
# ==================================================

elif page == "Risk Calculator":

    st.subheader("Risk Calculator")
    st.caption(
        "The selected default is 0.25% per idea. This is a calculation only, "
        "not an authorization or order."
    )
    with st.form("risk_calculator_form"):
        equity = st.number_input(
            "Account equity",
            min_value=0.0,
            value=10000.0,
            step=100.0,
            key="risk_calc_equity",
        )
        risk_percent = st.number_input(
            "Risk per idea (%)",
            min_value=0.01,
            max_value=0.25,
            value=DEFAULT_RISK_PERCENT,
            step=0.05,
            format="%.2f",
            key="risk_calc_percent",
        )
        calculate_risk = st.form_submit_button(
            "Calculate Risk Budget",
            type="primary",
        )
    if calculate_risk:
        risk_amount = calculate_risk_amount(equity, risk_percent)
        if risk_amount <= 0:
            st.error("Enter positive finite equity and risk values.")
        else:
            st.metric(
                "Maximum planned loss at stop",
                f"{risk_amount:,.2f}",
                border=True,
            )
            st.info(
                "This budget excludes gaps, spread, commission and slippage. "
                "The advisory planner estimates broker volume separately."
            )


# ==================================================
# HISTORICAL BACKTESTING
# ==================================================

elif page == "Historical Backtesting":

    st.subheader("Offline SMC Backtesting")
    st.caption(
        "Upload completed M15, H1 and H4 candles for one symbol. "
        "Signals are recomputed from data available at each M15 close; "
        "entries are simulated no earlier than the next candle."
    )
    st.warning(
        "Results are simulated research outputs, not broker fills or evidence "
        "of profitability. Keep the CSV files on your computer."
    )

    if not SYMBOLS:
        st.warning("Select a symbol in Currency Pair Manager first.")
        st.stop()

    with st.form("offline_backtest_form"):
        backtest_symbol = st.selectbox(
            "Symbol represented in the CSV files",
            SYMBOLS,
            key="backtest_symbol",
        )
        m15_upload = st.file_uploader(
            "M15 completed candles (CSV; optional spread column)",
            type=["csv"],
            key="backtest_m15_upload",
        )
        h1_upload = st.file_uploader(
            "H1 completed candles (CSV)",
            type=["csv"],
            key="backtest_h1_upload",
        )
        h4_upload = st.file_uploader(
            "H4 completed candles (CSV)",
            type=["csv"],
            key="backtest_h4_upload",
        )
        default_spread = (
            0.015 if backtest_symbol.endswith("JPY") else 0.00015
        )
        fixed_spread = st.number_input(
            "Fixed spread (price units; used only when M15 CSV has no spread column)",
            min_value=0.000001,
            value=default_spread,
            format="%.8f",
            key="backtest_spread",
        )
        slippage = st.number_input(
            "Slippage per side (price units)",
            min_value=0.0,
            value=0.0,
            format="%.8f",
            key="backtest_slippage",
        )
        commission = st.number_input(
            "Commission per side (basis points of notional)",
            min_value=0.0,
            value=0.35,
            step=0.05,
            format="%.2f",
            key="backtest_commission",
            help="Replace this illustrative value with your broker's actual fee.",
        )
        risk_percent = st.number_input(
            "Theoretical risk per trade (%)",
            min_value=0.01,
            max_value=0.25,
            value=DEFAULT_RISK_PERCENT,
            step=0.05,
            format="%.2f",
            key="backtest_risk",
        )
        initial_equity = st.number_input(
            "Starting simulated equity",
            min_value=1.0,
            value=10000.0,
            step=100.0,
            key="backtest_equity",
        )
        run_backtest = st.form_submit_button(
            "Run Offline Backtest",
            type="primary",
            width="stretch",
        )

    st.caption(
        "CSV timestamps are interpreted as UTC. OHLC is treated as mid-price; "
        "spread values and fixed spread must be in price units, not points or pips."
    )

    if run_backtest:
        st.session_state.pop("offline_backtest_result", None)
        if not all((m15_upload, h1_upload, h4_upload)):
            st.error("Upload all three timeframe CSV files.")
        else:
            try:
                frames = {
                    "M15": _read_uploaded_candles(m15_upload),
                    "H1": _read_uploaded_candles(h1_upload),
                    "H4": _read_uploaded_candles(h4_upload),
                }
                config = BacktestConfig(
                    initial_equity=initial_equity,
                    risk_percent=risk_percent,
                    fixed_spread_price=fixed_spread,
                    slippage_price=slippage,
                    commission_bps_per_side=commission,
                )
                with st.spinner(
                    "Recomputing SMC signals from historical snapshots..."
                ):
                    result = run_smc_backtest(
                        symbol=backtest_symbol,
                        m15_data=frames["M15"],
                        h1_data=frames["H1"],
                        h4_data=frames["H4"],
                        config=config,
                    )
                st.session_state["offline_backtest_result"] = result
            except (ValueError, TypeError, pd.errors.ParserError) as error:
                st.error(f"Could not run the backtest: {error}")

    backtest_result = st.session_state.get("offline_backtest_result")
    if backtest_result:
        if not backtest_result.get("valid"):
            st.error(backtest_result.get("reason", "Backtest failed."))
        else:
            st.info(
                f"Data source: {backtest_result['source']} | "
                f"Symbol: {backtest_result['symbol']} | "
                f"Signals evaluated: {backtest_result['signals_evaluated']} | "
                f"Confirmed setups: {backtest_result['confirmed_signals']}"
            )
            _show_backtest_statistics(
                "Overall simulated results",
                backtest_result["statistics"],
            )
            _show_backtest_statistics(
                "In-sample results",
                backtest_result["in_sample"],
            )
            _show_backtest_statistics(
                "Out-of-sample results",
                backtest_result["out_of_sample"],
            )
            st.caption(
                f"Split index: {backtest_result['out_of_sample_split_index']} "
                f"M15 candles | Spread model: {backtest_result['spread_model']} "
                f"| Price assumption: {backtest_result['price_assumption']}"
            )
            for warning in backtest_result["warnings"]:
                st.warning(warning)
            if backtest_result["trades"]:
                st.dataframe(
                    pd.DataFrame(backtest_result["trades"]),
                    width="stretch",
                    hide_index=True,
                    alt="Simulated offline backtest trades and modeled outcomes",
                )
            else:
                st.info(
                    "No confirmed setups were produced by the selected "
                    "historical sample."
                )


# ==================================================
# SIGNAL HISTORY
# ==================================================

elif page == "Signal History":

    st.subheader("Persistent Signal History")
    st.caption(
        "Local SQLite history. A CONFIRMED analytical signal is not a "
        "successful trade; outcomes remain PENDING until future candles "
        "reach a reference level."
    )
    try:
        history = get_recent_signals(limit=1000)
    except sqlite3.Error as error:
        st.error(f"Signal database is unavailable: {error}")
    else:
        st.info("Source: locally persisted signal observations.")
        if history:
            st.dataframe(
                pd.DataFrame(history),
                width="stretch",
                hide_index=True,
                alt="Persistent signal history and unresolved reference outcomes",
            )
        else:
            st.info("No signals have been persisted yet.")


# ==================================================
# SIGNAL OUTCOME ANALYSIS
# ==================================================

elif page == "Signal Outcome Analysis":

    st.subheader("Signal Outcome Analysis")
    st.caption(
        "Reference-level outcomes are derived from subsequent completed "
        "M15 candles. They are not actual trades or broker fills."
    )
    if not SYMBOLS:
        st.warning("Select a symbol in Currency Pair Manager first.")
        st.stop()

    with st.form("signal_outcome_refresh_form"):
        outcome_symbol = st.selectbox(
            "Symbol",
            SYMBOLS,
            key="outcome_symbol",
        )
        refresh_outcomes = st.form_submit_button(
            "Update Outcomes from MT5 Historical Data",
            type="primary",
        )
    if refresh_outcomes:
        try:
            candles = get_historical_data(
                outcome_symbol,
                mt5.TIMEFRAME_M15,
                bars=5000,
            )
            updated = resolve_pending_outcomes(
                outcome_symbol,
                candles,
                timeframe_minutes=15,
            )
            st.success(
                f"Resolved {len(updated)} signal outcome(s) from "
                "completed historical candles."
            )
        except (MarketDataError, ValueError, sqlite3.Error) as error:
            st.error(f"Outcome update failed: {error}")

    try:
        outcome_rows = get_recent_signals(
            limit=1000,
            symbol=outcome_symbol,
        )
    except sqlite3.Error as error:
        st.error(f"Signal database is unavailable: {error}")
    else:
        if outcome_rows:
            outcomes = pd.DataFrame(outcome_rows)
            counts = outcomes["outcome"].value_counts().to_dict()
            st.info(
                "Outcome counts (price-level simulation only): "
                + ", ".join(
                    f"{name}: {count}"
                    for name, count in sorted(counts.items())
                )
            )
            st.dataframe(
                outcomes,
                width="stretch",
                hide_index=True,
                alt="Signal history with subsequent simulated price-level outcomes",
            )
        else:
            st.info("No persisted signals were found for this symbol.")


# ==================================================
# VERIFIED TRADE HISTORY AND PERFORMANCE
# ==================================================

elif page == "Trade History & Performance":

    st.subheader("Closed Trade History & Performance")
    st.caption(
        "Only trades reconciled against broker deal history appear here. "
        "The cumulative chart is realized P/L, not an account-equity curve."
    )
    try:
        engine, _ = _execution_snapshot()
    except (OperatingModeError, sqlite3.Error) as error:
        st.error(f"Trading ledger is unavailable: {error}")
        st.stop()
    period = st.selectbox(
        "Reporting period",
        ["daily", "weekly", "monthly", "all history"],
        key="trade_history_period",
    )
    try:
        if period == "all history":
            closed_trades = engine.store.list_closed_trades()
        else:
            start, end = utc_period_bounds(period)
            closed_trades = engine.store.list_closed_trades(
                start_time=start.isoformat(),
                end_time=end.isoformat(),
            )
    except (sqlite3.Error, ValueError) as error:
        st.error(f"Verified trade history is unavailable: {error}")
        closed_trades = []

    report = performance_report(closed_trades)
    stats = report["summary"]
    st.info("Source: locally persisted, broker-deal-verified closed trades.")
    if stats["trades"] < 30:
        st.warning(
            f"Only {stats['trades']} verified closed trade(s) are available. "
            "This sample is insufficient for a profitability conclusion."
        )
    metric_columns = st.columns(4)
    metric_columns[0].metric(
        "Closed trades",
        stats["trades"],
        border=True,
    )
    metric_columns[1].metric(
        "Win rate",
        (
            f"{stats['win_rate_percent']:.1f}%"
            if stats["win_rate_percent"] is not None
            else "N/A"
        ),
        border=True,
    )
    metric_columns[2].metric(
        "Profit factor",
        (
            f"{stats['profit_factor']:.2f}"
            if stats["profit_factor"] is not None
            else "N/A"
        ),
        border=True,
    )
    metric_columns[3].metric(
        "Net realized P/L",
        f"{stats['net_profit']:,.2f}",
        border=True,
    )
    secondary = st.columns(4)
    secondary[0].metric(
        "Expectancy per trade",
        f"{stats['expectancy']:,.2f}" if stats["expectancy"] is not None else "N/A",
        border=True,
    )
    secondary[1].metric(
        "Maximum realized drawdown",
        f"{stats['max_drawdown']:,.2f}",
        border=True,
    )
    secondary[2].metric(
        "Average realized R:R",
        (
            f"{stats['average_reward_to_risk']:.2f}:1"
            if stats["average_reward_to_risk"] is not None
            else "N/A"
        ),
        border=True,
    )
    secondary[3].metric(
        "Consecutive losses",
        stats["max_consecutive_losses"],
        border=True,
    )

    daily = report["daily_realized"]
    if not daily.empty:
        st.line_chart(
            daily.set_index("date")["cumulative_realized_profit"],
            x_label="UTC date",
            y_label="Cumulative realized P/L",
        )
    if report["by_symbol"]:
        st.subheader("Performance by symbol")
        st.dataframe(
            [
                {"Symbol": symbol, **values}
                for symbol, values in report["by_symbol"].items()
            ],
            width="stretch",
            hide_index=True,
        )
    if closed_trades:
        st.subheader("Broker-verified closed trades")
        st.dataframe(
            pd.DataFrame(closed_trades),
            width="stretch",
            hide_index=True,
            alt="Closed trades reconciled against MT5 broker deal history",
        )
    else:
        st.info("No closed trades have been reconciled from broker history.")


# ==================================================
# DATA QUALITY MONITOR
# ==================================================

elif page == "Data Quality Monitor":

    st.subheader("Market Data Quality Monitor")
    st.caption(
        "Live broker quotes and MT5 historical candles are validated "
        "separately. This page is read-only."
    )
    if not SYMBOLS:
        st.warning("Select a symbol in Currency Pair Manager first.")
        st.stop()

    with st.form("market_data_quality_form"):
        quality_symbol = st.selectbox(
            "Broker symbol",
            SYMBOLS,
            key="quality_symbol",
        )
        quality_timeframe = st.selectbox(
            "Historical timeframe",
            list(TIMEFRAMES.keys()),
            key="quality_timeframe",
        )
        inspect_quality = st.form_submit_button(
            "Inspect Data",
            type="primary",
        )
    if inspect_quality:
        try:
            candles = get_historical_data(
                quality_symbol,
                TIMEFRAMES[quality_timeframe],
                bars=500,
            )
            quote = get_live_price(quality_symbol)
            st.session_state["quality_report"] = {
                "symbol": quality_symbol,
                "timeframe": quality_timeframe,
                "candles": candles,
                "quote": quote,
            }
        except MarketDataError as error:
            st.session_state.pop("quality_report", None)
            st.error(str(error))

    quality_report = st.session_state.get("quality_report")
    if quality_report:
        candles = quality_report["candles"]
        quote = quality_report["quote"]
        latest = candles.iloc[-1]
        st.success(
            "Historical candles passed OHLC, numeric, finite-price, "
            "timestamp-order and duplicate-time validation."
        )
        st.info("Historical source: MT5 completed candles.")
        metrics = st.columns(4)
        metrics[0].metric("Validated candles", len(candles), border=True)
        metrics[1].metric(
            "Latest completed candle (UTC)",
            latest["time"].strftime("%Y-%m-%d %H:%M"),
            border=True,
        )
        metrics[2].metric(
            "Live bid",
            f"{quote['bid']:.8f}",
            border=True,
        )
        metrics[3].metric(
            "Live ask",
            f"{quote['ask']:.8f}",
            border=True,
        )
        st.caption(
            f"Quote source: MT5 live feed | Quote age: "
            f"{quote['age_seconds']:.1f}s | "
            f"Spread: {quote['spread']:.8f} | "
            f"Broker clock offset normalized: "
            f"{quote['server_time_offset_seconds']}s"
        )
        st.dataframe(
            candles.tail(20),
            width="stretch",
            hide_index=True,
            alt="Latest validated completed market candles",
        )


# ==================================================
# EXECUTION SAFETY PAGE
# ==================================================

elif page == "Account Management":

    st.subheader("Account & Trading Mode Manager")
    st.caption(
        "Manage multiple MT5 profiles, operating mode, and operator "
        "authorization. Passwords are stored only in Windows Credential "
        "Manager. This page never submits an order."
    )
    if st.session_state.pop("clear_account_password_next_run", False):
        st.session_state["account_profile_password"] = ""
    next_profile = st.session_state.pop(
        "account_profile_select_after_save",
        None,
    )
    if next_profile:
        st.session_state["account_profile_selection"] = next_profile

    try:
        engine = TradingEngine(api=mt5)
        store = engine.store
        configuration = store.get_operational_config()
        profile = store.get_account_connection()
        profiles = store.list_account_profiles()
        manager_state = configuration["account_manager"]
    except (OperatingModeError, sqlite3.Error, ValueError) as error:
        st.error(f"Account manager configuration is unavailable: {error}")
        st.stop()

    with st.container(border=True):
        st.markdown("#### Operating mode")
        st.caption(
            "Mode changes disarm order execution. LIVE remains locked "
            "until every backend identity, readiness, risk, and authorization "
            "gate passes."
        )
        with st.form("account_manager_mode_form"):
            chosen_mode = st.segmented_control(
                "Choose operating mode",
                ["RESEARCH", "DEMO", "LIVE"],
                default=configuration["mode"],
                key="account_manager_mode",
            )
            save_mode = st.form_submit_button(
                "Save operating mode",
                type="primary",
            )
        if save_mode:
            try:
                if chosen_mode not in {"RESEARCH", "DEMO", "LIVE"}:
                    raise ValueError("Select RESEARCH, DEMO, or LIVE mode.")
                store.update_operational_config(mode=chosen_mode)
                st.success(
                    f"{chosen_mode} mode saved. Execution remains disarmed."
                )
                st.rerun()
            except (ValueError, sqlite3.Error) as error:
                st.error(f"Operating mode was not changed: {error}")

    st.divider()
    st.markdown("#### Saved MT5 accounts")
    if profile:
        status_columns = st.columns(3)
        status_columns[0].metric(
            "Active account",
            mask_account_login(profile["login"]),
            border=True,
        )
        status_columns[1].metric(
            "Account type",
            profile["account_type"],
            border=True,
        )
        status_columns[2].metric(
            "Identity verification",
            "VERIFIED" if profile["verified"] else "REQUIRED",
            border=True,
        )
        st.caption(f"Broker server: {profile['server']}")
    else:
        st.info("No MT5 account is active in NEXORA.")
    try:
        with mt5_connection():
            terminal = mt5.terminal_info()
            actual_account = mt5.account_info()
        if (
            terminal is not None
            and getattr(terminal, "connected", None) is True
            and actual_account is not None
        ):
            actual_type = account_type_from_trade_mode(
                mt5,
                getattr(actual_account, "trade_mode", None),
            )
            st.caption(
                "Actual connected MT5 identity: "
                f"{mask_account_login(actual_account.login)} @ "
                f"{actual_account.server} "
                f"({actual_type or 'unknown account type'})."
            )
        else:
            st.caption("Actual connected MT5 identity: unavailable.")
    except MT5ConnectionError:
        st.caption("Actual connected MT5 identity: MT5 terminal unavailable.")

    if profiles:
        labels = {
            item["profile_id"]: (
                f"{mask_account_login(item['login'])} | {item['server']} | "
                f"{item['account_type']}"
            )
            for item in profiles
        }
        ids = list(labels)
        active_id = manager_state["active_profile_id"]
        selected_id = st.selectbox(
            "Saved account profiles",
            options=ids,
            index=ids.index(active_id) if active_id in ids else 0,
            format_func=lambda value: labels[value],
            key="account_profile_selection",
        )
        selected_profile = store.get_account_profile(selected_id)
        profile_actions = st.columns(3)
        if profile_actions[0].button(
            "Switch Account",
            key="switch_saved_account",
            disabled=selected_id == active_id,
        ):
            try:
                AccountManager(store).activate_profile(selected_id)
                engine.refresh_configuration()
                st.success(
                    "Active account changed. Scanning, execution, and the "
                    "supervisor were disarmed; connect and verify this profile."
                )
                st.rerun()
            except (ValueError, sqlite3.Error, CredentialStoreError) as error:
                st.error(f"Account switch failed: {error}")
        if profile_actions[1].button(
            "Connect selected account",
            key="connect_saved_account",
            type="primary",
        ):
            try:
                connected = AccountManager(store).connect_profile(
                    selected_id,
                    engine,
                )
                st.success(
                    "MT5 account connected and verified: "
                    f"{mask_account_login(connected['login'])} @ "
                    f"{connected['server']} ({connected['account_type']})."
                )
            except (
                CredentialStoreError,
                MT5ConnectionError,
                TradeRejected,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.error(f"Could not connect the selected profile: {error}")
        if profile_actions[2].button(
            "Verify active account",
            key="verify_saved_account",
            disabled=selected_id != active_id,
        ):
            try:
                verified = engine.verify_account_connection()
                st.success(
                    "Actual MT5 identity verified: "
                    f"{mask_account_login(verified['login'])} @ "
                    f"{verified['server']} ({verified['account_type']})."
                )
            except (
                CredentialStoreError,
                MT5ConnectionError,
                TradeRejected,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.error(f"Account verification failed: {error}")
        if selected_profile:
            credential_status = (
                "secure credential reference saved"
                if selected_profile["credential_ref"]
                else "password must be saved"
            )
            st.caption(
                f"Selected profile: {credential_status}. Passwords are never "
                "shown again after saving."
            )
    else:
        st.info("Save your first MT5 account profile below.")

    disconnect_columns = st.columns([1, 3])
    if disconnect_columns[0].button(
        "Disconnect from NEXORA",
        key="disconnect_active_account",
        disabled=profile is None,
    ):
        try:
            AccountManager(store).disconnect(engine)
            st.success(
                "NEXORA disarmed and disconnected the saved profile. The "
                "MT5 desktop terminal was not shut down; the saved profile "
                "and its secure credential remain available for reconnecting."
            )
            st.rerun()
        except (ValueError, sqlite3.Error) as error:
            st.error(f"Account disconnect failed: {error}")

    with st.expander("Add an account or update its saved password"):
        with st.form("account_profile_save_form"):
            account_login = st.text_input(
                "MT5 account login",
                help="Digits only; checked against the actual MT5 identity.",
                key="account_profile_login",
            )
            broker_server = st.text_input(
                "Broker server",
                help="Use the exact server name reported by MT5.",
                key="account_profile_server",
            )
            account_type = st.selectbox(
                "Account type",
                ["DEMO", "LIVE"],
                key="account_profile_type",
            )
            account_password = st.text_input(
                "MT5 password",
                type="password",
                help="Saved only in Windows Credential Manager; never logged.",
                key="account_profile_password",
            )
            save_profile = st.form_submit_button(
                "Save account securely",
                type="primary",
                key="save_account_profile",
            )
        if save_profile:
            try:
                saved = AccountManager(store).save_profile(
                    account_login,
                    broker_server,
                    account_type,
                    account_password,
                )
                st.session_state["account_profile_select_after_save"] = (
                    saved["profile_id"]
                )
                st.session_state["clear_account_password_next_run"] = True
                st.success(
                    "Account profile and password saved securely. Select "
                    "Switch Account, then Connect selected account."
                )
                st.rerun()
            except (
                CredentialStoreError,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.session_state["clear_account_password_next_run"] = True
                st.error(f"Account profile was not saved: {error}")

    st.caption(
        "Changing or switching accounts invalidates prior verification and "
        "disarms the runner and execution. Account changes are blocked while "
        "managed trades require reconciliation. Disconnect does not log the "
        "MT5 desktop terminal out."
    )

    st.divider()
    st.markdown("#### GitHub operator verification")
    st.caption(
        "Authenticate with the official GitHub device flow as "
        "**Silas-AFK**. Only the public OAuth client ID is saved. The device "
        "code and access token are transient; the access token is discarded "
        "after the username is checked."
    )
    with st.form("github_client_id_form"):
        github_client_id = st.text_input(
            "Public GitHub OAuth client ID",
            value=manager_state["github_client_id"] or "",
            help="Create an OAuth App with device flow enabled. Do not enter a client secret.",
            key="github_oauth_client_id",
        )
        save_client_id = st.form_submit_button("Save public client ID")
    if save_client_id:
        try:
            store.save_github_client_id(github_client_id)
            st.success("Public GitHub client ID saved.")
            st.rerun()
        except (ValueError, sqlite3.Error) as error:
            st.error(f"GitHub client ID was not saved: {error}")

    if manager_state["github_client_id"]:
        device_flow = st.session_state.get("github_device_flow")
        if st.button(
            "Start GitHub sign-in",
            key="start_github_signin",
            disabled=not profile or not profile["verified"],
        ):
            try:
                device_flow = start_device_authorization(
                    manager_state["github_client_id"]
                )
                device_flow["client_id"] = manager_state["github_client_id"]
                device_flow["next_poll_at"] = 0.0
                st.session_state["github_device_flow"] = device_flow
            except (GitHubAuthError, ValueError) as error:
                st.error(f"GitHub sign-in could not start: {error}")
        device_flow = st.session_state.get("github_device_flow")
        if device_flow:
            if time.time() >= device_flow["expires_at"]:
                st.session_state.pop("github_device_flow", None)
                st.warning("GitHub device code expired; start sign-in again.")
            else:
                st.link_button(
                    "Open GitHub device authorization",
                    device_flow["verification_uri"],
                )
                st.code(device_flow["user_code"])
                poll_wait = max(0.0, device_flow["next_poll_at"] - time.time())
                if st.button(
                    "Check GitHub authorization",
                    key="poll_github_signin",
                    disabled=poll_wait > 0,
                ):
                    try:
                        result = poll_device_authorization(
                            device_flow["client_id"],
                            device_flow["device_code"],
                        )
                        if result["status"] in {
                            "authorization_pending",
                            "slow_down",
                        }:
                            if result["status"] == "slow_down":
                                device_flow["interval"] += 5
                            device_flow["next_poll_at"] = (
                                time.time() + device_flow["interval"]
                            )
                            st.info(
                                "GitHub authorization is still pending. "
                                "Approve the request, then check again."
                            )
                        elif result["status"] == "authorized":
                            if result["login"].casefold() != "silas-afk":
                                store.clear_github_authorization()
                                st.session_state.pop(
                                    "github_device_flow",
                                    None,
                                )
                                st.error(
                                    "The authenticated GitHub account is not "
                                    "allowlisted for LIVE activation."
                                )
                            else:
                                engine.authorize_github_operator(
                                    result["login"]
                                )
                                st.session_state.pop(
                                    "github_device_flow",
                                    None,
                                )
                                st.success(
                                    "GitHub operator verified for the active "
                                    "MT5 account. This does not arm execution."
                                )
                                st.rerun()
                        else:
                            st.session_state.pop("github_device_flow", None)
                            st.error(
                                "GitHub authorization was denied or expired."
                            )
                    except (
                        GitHubAuthError,
                        CredentialStoreError,
                        MT5ConnectionError,
                        TradeRejected,
                        ValueError,
                        sqlite3.Error,
                    ) as error:
                        st.error(f"GitHub verification failed: {error}")

    operator_status = (
        "verified"
        if manager_state["github_authorized_login"] == "Silas-AFK"
        else "not verified"
    )
    st.caption(f"GitHub operator status: {operator_status}.")
    st.caption(
        "LIVE activation: "
        f"{'recorded' if manager_state['live_activated_at'] else 'not active'}; "
        "first-order authorization: "
        f"{'recorded' if manager_state['live_first_order_authorized_at'] else 'not recorded'}."
    )

    st.divider()
    st.markdown("#### LIVE eligibility and readiness evidence")
    st.warning(
        "LIVE activation can expose real money to loss. Readiness entries "
        "are operator attestations, not independently tested facts. Do not "
        "mark any item verified without reviewed evidence. No order is sent "
        "by this page."
    )
    live_profile = store.get_account_connection()
    readiness_identity = (
        {
            "account_login": live_profile["login"],
            "account_server": live_profile["server"],
        }
        if live_profile and live_profile["verified"]
        else {}
    )
    readiness = read_live_readiness(
        engine.config.live_readiness_file,
        account_login=readiness_identity.get("account_login"),
        account_server=readiness_identity.get("account_server"),
    )
    readiness_records = read_live_readiness_records(
        engine.config.live_readiness_file
    )
    for requirement in readiness:
        previous = readiness_records.get(requirement, {})
        with st.form(f"live_readiness_form_{requirement}"):
            st.write(requirement.replace("_", " ").capitalize())
            confirmed = st.checkbox(
                "Evidence reviewed and requirement passed",
                value=previous.get("verified") is True,
                key=f"live_readiness_{requirement}",
            )
            evidence = st.text_input(
                "Evidence reference (never enter secrets)",
                value="",
                key=f"live_evidence_{requirement}",
            )
            save_evidence = st.form_submit_button("Save evidence")
        if save_evidence:
            try:
                account_login = None
                account_server = None
                if requirement == "account_identity_verified" and confirmed:
                    verified_account = engine.verify_account_connection()
                    if verified_account["account_type"] != "LIVE":
                        raise TradeRejected(
                            "This readiness item requires an actual LIVE account."
                        )
                    account_login = verified_account["login"]
                    account_server = verified_account["server"]
                save_live_readiness_item(
                    engine.config.live_readiness_file,
                    requirement,
                    verified=confirmed,
                    evidence=evidence,
                    account_login=account_login,
                    account_server=account_server,
                )
                st.success("Readiness record updated.")
                st.rerun()
            except (
                OperatingModeError,
                CredentialStoreError,
                MT5ConnectionError,
                TradeRejected,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.error(f"Readiness record was not saved: {error}")
        st.caption("Verified" if readiness[requirement] else "Not verified")

    current_policy = configuration["risk_policy"]
    st.write(
        "Configured risk: "
        f"{current_policy['risk_per_trade_percent']:.2f}% per trade, "
        f"{current_policy['max_daily_loss_percent']:.2f}% daily loss, "
        f"{current_policy['max_total_open_risk_percent']:.2f}% aggregate "
        "open risk."
    )
    real_money_ack = st.checkbox(
        "I understand LIVE trading can lose real money.",
        key="live_real_money_ack",
    )
    risk_limits_ack = st.checkbox(
        "I reviewed and accept the risk limits shown above.",
        key="live_risk_limits_ack",
    )
    if st.button(
        "Activate LIVE mode",
        type="primary",
        key="activate_live_mode",
        disabled=not (real_money_ack and risk_limits_ack),
    ):
        try:
            engine.activate_live(
                real_money_acknowledged=real_money_ack,
                risk_limits_acknowledged=risk_limits_ack,
            )
            st.success(
                "LIVE activation recorded. Execution remains disarmed; the "
                "first-order authorization and execution arming are separate."
            )
            st.rerun()
        except (
            CredentialStoreError,
            MT5ConnectionError,
            TradeRejected,
            ValueError,
            sqlite3.Error,
        ) as error:
            st.error(f"LIVE activation was denied: {error}")

    first_order_ack = st.checkbox(
        "I separately authorize the first LIVE order after reviewing its plan.",
        key="live_first_order_ack",
    )
    if st.button(
        "Authorize first LIVE order",
        key="authorize_first_live_order",
        disabled=not first_order_ack,
    ):
        try:
            engine.authorize_first_live_order(
                operator_confirmed=first_order_ack,
            )
            st.success(
                "First LIVE order authorization recorded. No order was submitted."
            )
            st.rerun()
        except (
            CredentialStoreError,
            MT5ConnectionError,
            TradeRejected,
            ValueError,
            sqlite3.Error,
        ) as error:
            st.error(f"First-order authorization was denied: {error}")


# ==================================================
# EXECUTION SAFETY PAGE
# ==================================================

elif page == "Execution Safety":

    st.subheader("Execution Safety & Operating Mode")
    st.caption(
        "The selected mode and risk limits are persisted by the backend. "
        "Execution remains subject to MT5 identity, account type, risk "
        "baselines, emergency lockout, and explicit operator confirmation."
    )

    try:
        engine, state = _execution_snapshot()
    except (OperatingModeError, sqlite3.Error) as error:
        st.error(f"Execution safety configuration is unavailable: {error}")
        st.stop()

    mode_columns = st.columns(4)
    mode_columns[0].metric("Operating mode", state["mode"], border=True)
    mode_columns[1].metric(
        "Account identity",
        "VERIFIED" if state["account_verified"] else "UNVERIFIED",
        border=True,
    )
    mode_columns[2].metric(
        "Entry permission",
        "ARMED" if state["entry_permission"] else "LOCKED",
        border=True,
    )
    mode_columns[3].metric(
        "Emergency stop",
        "ACTIVE" if state["emergency_stop"] else "CLEAR",
        border=True,
    )
    if state["account"]:
        st.caption(
            "Connected MT5 identity: "
            f"{mask_account_login(state['account']['login'])} @ "
            f"{state['account']['server']} "
            f"(trade mode {state['account']['trade_mode']})."
        )

    st.write(
        "Risk policy: "
        f"{state['risk_percent']:.2f}% maximum per trade; "
        f"{state['max_daily_loss_percent']:.2f}% daily-loss lockout; "
        f"{state['max_aggregate_stop_risk_percent']:.2f}% aggregate "
        f"stop risk; at most {state['max_open_positions']} positions."
    )
    stored_config = engine.store.get_operational_config()
    stored_policy = stored_config["risk_policy"]
    st.caption("Select operating mode in Account Management. Mode changes disarm execution.")
    with st.form("operating_risk_policy"):
        policy_columns = st.columns(3)
        risk_per_trade = policy_columns[0].number_input(
            "Risk per trade (%)",
            min_value=0.01,
            max_value=0.25,
            value=float(stored_policy["risk_per_trade_percent"]),
            step=0.01,
            format="%.2f",
        )
        daily_loss = policy_columns[1].number_input(
            "Daily loss limit (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_daily_loss_percent"]),
            step=0.25,
        )
        weekly_loss = policy_columns[2].number_input(
            "Weekly loss limit (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_weekly_loss_percent"]),
            step=0.25,
        )
        monthly_loss = policy_columns[0].number_input(
            "Monthly loss limit (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_monthly_loss_percent"]),
            step=0.25,
        )
        aggregate_risk = policy_columns[1].number_input(
            "Aggregate open risk (%)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_total_open_risk_percent"]),
            step=0.05,
        )
        positions_max = policy_columns[2].number_input(
            "Maximum open positions",
            min_value=1,
            max_value=100,
            value=int(stored_policy["max_simultaneous_positions"]),
            step=1,
        )
        positions_per_symbol = policy_columns[0].number_input(
            "Maximum positions per symbol",
            min_value=1,
            max_value=100,
            value=int(stored_policy["max_positions_per_symbol"]),
            step=1,
        )
        trades_per_day = policy_columns[1].number_input(
            "Maximum trades per UTC day",
            min_value=1,
            max_value=100,
            value=int(stored_policy["max_trades_per_day"]),
            step=1,
        )
        consecutive_losses = policy_columns[2].number_input(
            "Consecutive-loss limit",
            min_value=1,
            max_value=100,
            value=int(stored_policy["max_consecutive_losses"]),
            step=1,
        )
        max_spread = policy_columns[0].number_input(
            "Maximum spread (pips)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_spread_pips"]),
            step=0.1,
        )
        max_slippage = policy_columns[1].number_input(
            "Maximum fill slippage (pips)",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["max_slippage_pips"]),
            step=0.1,
        )
        min_reward_risk = policy_columns[2].number_input(
            "Minimum reward-to-risk",
            min_value=0.01,
            max_value=100.0,
            value=float(stored_policy["minimum_reward_risk"]),
            step=0.1,
        )
        session_columns = st.columns(2)
        session_start = session_columns[0].text_input(
            "UTC session start (HH:MM; optional)",
            value=stored_policy["session_start_utc"] or "",
        )
        session_end = session_columns[1].text_input(
            "UTC session end (HH:MM; optional)",
            value=stored_policy["session_end_utc"] or "",
        )
        save_policy = st.form_submit_button(
            "Save Risk Limits",
            type="primary",
        )
    if save_policy:
        policy = {
            "risk_per_trade_percent": risk_per_trade,
            "max_daily_loss_percent": daily_loss,
            "max_weekly_loss_percent": weekly_loss,
            "max_monthly_loss_percent": monthly_loss,
            "max_total_open_risk_percent": aggregate_risk,
            "max_simultaneous_positions": int(positions_max),
            "max_positions_per_symbol": int(positions_per_symbol),
            "max_trades_per_day": int(trades_per_day),
            "max_consecutive_losses": int(consecutive_losses),
            "max_spread_pips": max_spread,
            "max_slippage_pips": max_slippage,
            "minimum_reward_risk": min_reward_risk,
            "session_start_utc": session_start.strip() or None,
            "session_end_utc": session_end.strip() or None,
        }
        try:
            engine.store.update_operational_config(
                risk_policy=policy,
            )
            st.success(
                "Risk policy saved. Any previous execution permission and "
                "LIVE activation were invalidated."
            )
            st.rerun()
        except (ValueError, sqlite3.Error) as error:
            st.error(f"Settings were not saved: {error}")

    saved_account = engine.store.get_account_connection()
    if saved_account:
        st.caption(
            "Saved account profile: "
            f"{mask_account_login(saved_account['login'])} @ "
            f"{saved_account['server']} ({saved_account['account_type']}); "
            f"identity verification "
            f"{'current' if saved_account['verified'] else 'required'}."
        )
    else:
        st.info("Save and verify an account profile in Account Management.")
    if state["reasons"]:
        st.warning("Entry permission is locked:")
        for reason in state["reasons"]:
            st.write(f"- {reason}")
    else:
        st.success(
            "General backend entry gates are clear. Every candidate still "
            "must pass signal, spread, reward/risk, exposure, loss, sizing, "
            "margin, and broker checks."
        )
    if state.get("open_positions") is not None:
        st.caption(
            f"Current MT5 exposure: {state['open_positions']} position(s), "
            f"{state['pending_orders']} pending order(s)."
        )

    st.divider()
    st.subheader("UTC Equity-Loss Baselines")
    baselines = engine.store.get_active_period_baselines()
    baseline_columns = st.columns(3)
    for column, period in zip(
        baseline_columns,
        ("daily", "weekly", "monthly"),
    ):
        baseline = baselines[period]
        with column:
            if baseline:
                st.metric(
                    f"{period.capitalize()} starting equity",
                    f"{baseline['starting_equity']:,.2f}",
                    border=True,
                )
                st.caption(
                    f"UTC period "
                    f"{baseline.get('period_start', baseline.get('trading_date'))}; "
                    "captured "
                    f"{baseline['confirmed_at']}."
                )
            else:
                st.warning(f"{period.capitalize()} baseline missing.")
                if not engine.store.period_baseline_window_open(period):
                    st.caption(
                        "UTC baseline window closed; entries stay locked "
                        "until the next period."
                    )
    if any(baseline is None for baseline in baselines.values()):
        if st.button(
            "Capture Available MT5 Equity Baselines",
            disabled=not state["account_verified"],
            key="capture_period_equity_baselines",
        ):
            try:
                captured = engine.establish_available_baselines()
                if captured:
                    st.success(
                        f"Captured {len(captured)} eligible UTC baseline(s)."
                    )
                else:
                    st.info(
                        "No baseline window is currently open. Missing "
                        "periods remain locked until their next UTC start."
                    )
                st.rerun()
            except (
                MT5ConnectionError,
                TradeRejected,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.error(f"Baselines were not captured: {error}")

    st.divider()
    st.subheader("Backend Execution Permission")
    if state["mode"] == "DEMO":
        st.warning(
            "DEMO mode can submit orders to the connected broker only after "
            "the configured login/server, demo account type, all UTC loss "
            "baselines, and persistent risk gates are verified."
        )
        if state["execution_enabled"]:
            if st.button(
                "Disable DEMO Order Execution",
                key="disable_demo_execution",
            ):
                engine.disable_execution(
                    "DEMO execution disabled from the dashboard."
                )
                st.success("DEMO order execution is disarmed.")
                st.rerun()
        else:
            with st.form("arm_demo_execution_form"):
                confirmed_demo = st.checkbox(
                    "I verified that MT5 is connected to my allowlisted DEMO "
                    "account and authorize order submission in DEMO mode.",
                    key="confirm_demo_execution",
                )
                arm_demo = st.form_submit_button(
                    "Arm DEMO Order Execution",
                    type="primary",
                    disabled=not confirmed_demo,
                )
            if arm_demo:
                try:
                    engine.enable_execution(operator_confirmed=True)
                    st.success(
                        "DEMO execution is armed. No order has been submitted "
                        "by this action."
                    )
                    st.rerun()
                except (
                    MT5ConnectionError,
                    TradeRejected,
                    OperatingModeError,
                    ValueError,
                    sqlite3.Error,
                ) as error:
                    st.error(f"DEMO execution remains locked: {error}")
    elif state["mode"] == "LIVE":
        st.warning(
            "LIVE mode can submit real-money orders only after account-bound "
            "operator authorization, readiness evidence, fresh risk checks, "
            "and the separate first-order confirmation in Account Management."
        )
        if state["execution_enabled"]:
            if st.button(
                "Disarm LIVE Order Execution",
                type="primary",
                key="disable_live_execution",
            ):
                engine.disable_execution(
                    "LIVE execution disarmed from the dashboard."
                )
                st.success("LIVE order execution is disarmed.")
                st.rerun()
        else:
            with st.form("arm_live_execution_form"):
                confirmed_live = st.checkbox(
                    "I verified that MT5 is connected to my intended LIVE "
                    "account and explicitly authorize autonomous LIVE order "
                    "submission under the displayed risk limits.",
                    key="confirm_live_execution",
                )
                arm_live = st.form_submit_button(
                    "Arm LIVE Order Execution",
                    type="primary",
                    disabled=not confirmed_live,
                )
            if arm_live:
                try:
                    engine.enable_execution(operator_confirmed=True)
                    st.success(
                        "LIVE execution is armed. No order has been submitted "
                        "by this action."
                    )
                    st.rerun()
                except (
                    MT5ConnectionError,
                    TradeRejected,
                    OperatingModeError,
                    ValueError,
                    sqlite3.Error,
                ) as error:
                    st.error(f"LIVE execution remains locked: {error}")
    else:
        st.info(
            "RESEARCH mode is signals-only. Select DEMO mode and complete "
            "the verified-account and baseline gates before any demo orders."
        )

    st.divider()
    st.subheader("Emergency Stop")
    st.caption(
        "Emergency lockout blocks new entries and persists across restarts. "
        "The separate runner continues protecting already-managed positions."
    )
    if st.button(
        "Activate Emergency Lockout",
        type="primary",
        key="activate_execution_lockout",
    ):
        engine.emergency_stop("Emergency lockout activated from dashboard.")
        st.warning("New automated entries are locked.")
        st.rerun()

    if state["emergency_stop"]:
        with st.form("clear_execution_lockout_form"):
            confirmed = st.checkbox(
                "I verified the account, open positions, and cause of lockout.",
                key="confirm_lockout_clear",
            )
            reason = st.text_input(
                "Reason for clearing this lockout",
                key="lockout_clear_reason",
            )
            clear_requested = st.form_submit_button(
                "Clear Lockout",
                disabled=not confirmed or not reason.strip(),
            )
        if clear_requested:
            try:
                engine.clear_emergency_stop(
                    reason,
                    operator_confirmed=confirmed,
                )
                st.success("Emergency lockout cleared.")
                st.rerun()
            except (
                MT5ConnectionError,
                TradeRejected,
                ValueError,
                sqlite3.Error,
            ) as error:
                st.error(f"Lockout remains active: {error}")

    st.divider()
    st.subheader("LIVE Readiness")
    readiness = read_live_readiness(
        engine.config.live_readiness_file,
        account_login=engine.config.expected_login,
        account_server=engine.config.expected_server,
    )
    for requirement, verified in readiness.items():
        st.write(
            f"{'✅' if verified else '⛔'} "
            f"{requirement.replace('_', ' ').capitalize()}: "
            f"{'verified' if verified else 'not verified'}"
        )
    st.info(
        "LIVE entries require account-bound GitHub authorization, reviewed "
        "readiness evidence, healthy supervision, account-bound baselines, "
        "risk checks, separate activation and first-order authorization. "
        "Execution is currently disarmed."
    )

    heartbeat = engine.store.runner_heartbeat()
    st.write(
        "Autonomous runner heartbeat: "
        f"{heartbeat.get('heartbeat_at') or 'not recorded'} "
        f"(PID {heartbeat.get('process_id') or 'unknown'}; "
        f"{'recent' if _runner_is_recent(engine.store) else 'stale'})"
    )
    st.code(
        "& '.\\venv\\Scripts\\python.exe' -m execution.runner",
        language="powershell",
    )
    st.caption(
        "The runner is a separate process. RESEARCH scans without execution; "
        "DEMO requires a verified saved DEMO profile and every independent "
        "backend risk and supervisor gate."
    )

    trades = engine.store.list_trades(limit=50)
    st.subheader("Persistent Trade Ledger")
    if trades:
        st.dataframe(
            [
                {
                    "Created (UTC)": row["created_at"],
                    "Symbol": row["symbol"],
                    "Direction": row["direction"],
                    "Mode": row["mode"],
                    "Volume": row["actual_volume"] or row["requested_volume"],
                    "Entry": row["entry_price"],
                    "Stop": row["stop_loss"],
                    "Target": row["take_profit"],
                    "Status": row["status"],
                    "Error": row["error"],
                }
                for row in trades
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No execution records are stored.")

    st.subheader("Managed Open Positions")
    open_trades = engine.store.list_trades(
        statuses=("OPEN", "UNPROTECTED"),
        limit=100,
    )
    if not open_trades:
        st.info("No NEXORA-managed positions are recorded as open.")
    for trade in open_trades:
        with st.container(border=True):
            st.write(
                f"**{trade['symbol']} {trade['direction']}** — "
                f"ticket {trade['position_ticket']} — "
                f"{trade['status']} — volume "
                f"{trade['actual_volume'] or trade['requested_volume']}"
            )
            with st.form(f"close_managed_{trade['trade_id']}"):
                confirmed_close = st.checkbox(
                    "I confirm I want to close this managed position.",
                    key=f"confirm_close_{trade['trade_id']}",
                )
                close_requested = st.form_submit_button(
                    "Close Managed Position",
                    disabled=(
                        not confirmed_close
                        or trade.get("position_ticket") is None
                    ),
                )
            if close_requested:
                try:
                    engine.close_managed_position(
                        int(trade["position_ticket"]),
                        operator_confirmed=confirmed_close,
                    )
                    st.success(
                        "Position closure was confirmed and broker history "
                        "was recorded."
                    )
                    st.rerun()
                except (
                    MT5ConnectionError,
                    TradeRejected,
                    ValueError,
                    sqlite3.Error,
                ) as error:
                    st.error(f"Position was not fully reconciled: {error}")

# ==================================================
# SYSTEM STATUS PAGE
# ==================================================

elif page == "System Status":

    st.subheader("NEXORA FX System Health")

    st.success(
        "Dashboard application is running"
    )

    mt5_status = check_mt5_connection()

    if mt5_status:

        st.success(
            "MetaTrader 5 connection successful"
        )

    else:

        st.error(
            "MetaTrader 5 connection unavailable"
        )

    st.success("Market data module loaded")
    st.success("Technical indicators module loaded")
    st.success("Market regime module loaded")
    st.success("Market structure module loaded")
    st.success("Multi-timeframe SMC module loaded")
    st.success("SMC zone validator module loaded")
    st.success("SMC setup evaluator module loaded")
    st.success("Setup diagnostics module loaded")
    st.success("Signal intelligence service imported")

    if _agent_enabled:

        st.success("Manual signal scan: ENABLED")

    else:

        st.warning("Manual signal scan: PAUSED")

    try:
        _, execution_status = _execution_snapshot()
        st.write(f"Operating mode: {execution_status['mode']}")
        st.write(
            "Backend order-entry permission: "
            f"{'ARMED' if execution_status['entry_permission'] else 'LOCKED'}"
        )
        if execution_status["reasons"]:
            st.caption("Entry lock reasons: " + "; ".join(execution_status["reasons"]))
    except (OperatingModeError, sqlite3.Error, MT5ConnectionError) as error:
        st.error(f"Execution status unavailable; entries are locked: {error}")

    st.info(
        "The dashboard switch controls manual scanning only. Continuous "
        "scanning and backend-gated execution run in the separate "
        "execution.runner process."
    )

    st.divider()

    st.subheader("Platform Information")

    st.write("Platform: NEXORA FX")
    st.write("Environment: Local Windows PC")
    st.write("Broker connection: MetaTrader 5")
    st.write("Market data: MT5 market feed")
    st.write("Analysis: Technical indicators and SMC")
    st.write("Signal intelligence: Multi-timeframe signal engine")
    st.write("Execution permissions: Backend-gated by operating mode and safety checks")


# ==================================================
# FOOTER
# ==================================================

st.divider()

st.markdown("### NEXORA :blue[FX]")

st.caption(
    "INTELLIGENCE. PRECISION. DISCIPLINE."
)

st.caption(
    f"{_display_mode} mode | Signal analysis and backend-gated execution"
)
