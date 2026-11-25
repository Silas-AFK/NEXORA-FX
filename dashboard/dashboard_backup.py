
import streamlit as st
import MetaTrader5 as mt5
import pandas as pd
import plotly.graph_objects as go

from config.market_settings import (
    AVAILABLE_SYMBOLS,
    load_symbols,
    save_symbols,
)

from execution.mt5_connection import (
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


# ==================================================
# PAGE CONFIGURATION
# ==================================================

st.set_page_config(
    page_title="NEXORA FX | Trading Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ==================================================
# CUSTOM DESIGN
# ==================================================

st.markdown("""
<style>
    .stApp {
        background-color: #0B1120;
        color: #F8FAFC;
    }

    h1, h2, h3 {
        color: #F8FAFC !important;
        font-weight: 700;
    }

    [data-testid="stMetric"] {
        background-color: #172033;
        padding: 20px;
        border-radius: 12px;
        border: 1px solid #263449;
    }

    [data-testid="stMetricLabel"] {
        color: #94A3B8 !important;
    }

    [data-testid="stMetricValue"] {
        color: #F8FAFC !important;
    }

    [data-testid="stSidebar"] {
        background-color: #111827;
    }

    [data-testid="stSidebar"] p {
        color: #CBD5E1;
    }

    [data-testid="stDataFrame"] {
        border: 1px solid #263449;
        border-radius: 10px;
    }

    [data-baseweb="select"] {
        background-color: #172033;
    }

    hr {
        border-color: #263449;
    }
</style>
""", unsafe_allow_html=True)


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
        "System Status",
    ],
)

st.sidebar.divider()

st.sidebar.success("RESEARCH MODE")

st.sidebar.error(
    "AUTOMATED TRADING: DISABLED"
)

st.sidebar.caption(
    "Monitoring only. No orders will be placed."
)


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
            return None, "Historical data unavailable"

        analysis = calculate_indicators(data)

        if analysis is None:
            return None, "Indicator calculation failed"

        if analysis.empty:
            return None, "No completed analysis data available"

        return analysis, None

    except (MarketDataError, ValueError) as error:
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
# MAIN HEADER
# ==================================================

st.title("NEXORA :blue[FX]")

st.caption(
    "INTELLIGENCE. PRECISION. DISCIPLINE."
)

st.markdown(
    "Professional Forex Intelligence & Market Analysis"
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
            "Please open MT5 and log into your demo account."
        )

        st.stop()

    # ACCOUNT OVERVIEW

    st.subheader("Account Overview")

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Balance",
        f"{account['balance']:,.2f} {account['currency']}",
    )

    col2.metric(
        "Equity",
        f"{account['equity']:,.2f} {account['currency']}",
    )

    col3.metric(
        "Currency",
        account["currency"],
    )

    col4.metric(
        "Leverage",
        f"1:{account['leverage']}",
    )

    st.divider()

    # LIVE MARKET WATCH

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
            use_container_width=True,
            hide_index=True,
        )

    else:

        st.warning(
            "No live market prices are currently available."
        )

    st.divider()

    # MARKET SUMMARY

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
                )

                col2.metric(
                    "RSI 14",
                    f"{latest['RSI_14']:.2f}",
                )

                col3.metric(
                    "ADX 14",
                    f"{latest['ADX_14']:.2f}",
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

    # MARKET SELECTION

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

    # LOAD MARKET DATA

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

        # ==========================================
        # CANDLESTICK CHART
        # ==========================================

        fig = go.Figure()

        fig.add_trace(
            go.Candlestick(
                x=data["time"],
                open=data["open"],
                high=data["high"],
                low=data["low"],
                close=data["close"],
                name="Candlesticks",
            )
        )

        # ==========================================
        # EMA 20
        # ==========================================

        fig.add_trace(
            go.Scatter(
                x=data["time"],
                y=data["EMA_20"],
                name="EMA 20",
                line=dict(width=1.5),
            )
        )

        # ==========================================
        # EMA 50
        # ==========================================

        fig.add_trace(
            go.Scatter(
                x=data["time"],
                y=data["EMA_50"],
                name="EMA 50",
                line=dict(width=1.5),
            )
        )

        # ==========================================
        # EMA 200
        # ==========================================

        fig.add_trace(
            go.Scatter(
                x=data["time"],
                y=data["EMA_200"],
                name="EMA 200",
                line=dict(width=2),
            )
        )

        # ==========================================
        # CHART LAYOUT
        # ==========================================

        fig.update_layout(
            template="plotly_dark",
            height=650,
            xaxis_rangeslider_visible=False,
            title=(
                f"{selected_symbol} - "
                f"{selected_timeframe}"
            ),
            paper_bgcolor="#0B1120",
            plot_bgcolor="#111827",
            font=dict(color="#CBD5E1"),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
            ),
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )

        # ==========================================
        # LATEST COMPLETED CANDLE
        # ==========================================

        latest = data.iloc[-1]

        st.divider()

        st.subheader("Latest Completed Candle")

        col1, col2, col3, col4 = st.columns(4)

        col1.metric(
            "Open",
            f"{latest['open']:.5f}",
        )

        col2.metric(
            "High",
            f"{latest['high']:.5f}",
        )

        col3.metric(
            "Low",
            f"{latest['low']:.5f}",
        )

        col4.metric(
            "Close",
            f"{latest['close']:.5f}",
        )

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

    # MARKET SELECTION

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

    # LOAD MARKET DATA

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

        # ==========================================
        # MOMENTUM AND VOLATILITY
        # ==========================================

        st.subheader("Momentum & Volatility")

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "RSI 14",
            f"{latest['RSI_14']:.2f}",
        )

        c2.metric(
            "ADX 14",
            f"{latest['ADX_14']:.2f}",
        )

        c3.metric(
            "ATR 14",
            f"{latest['ATR_14']:.5f}",
        )

        st.divider()

        # ==========================================
        # MOVING AVERAGES
        # ==========================================

        st.subheader("Moving Averages")

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "EMA 20",
            f"{latest['EMA_20']:.5f}",
        )

        c2.metric(
            "EMA 50",
            f"{latest['EMA_50']:.5f}",
        )

        c3.metric(
            "EMA 200",
            f"{latest['EMA_200']:.5f}",
        )

        st.divider()

        # ==========================================
        # MACD
        # ==========================================

        st.subheader("MACD")

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "MACD",
            f"{latest['MACD']:.6f}",
        )

        c2.metric(
            "Signal",
            f"{latest['MACD_SIGNAL']:.6f}",
        )

        c3.metric(
            "Histogram",
            f"{latest['MACD_HIST']:.6f}",
        )

        st.divider()

        # ==========================================
        # BOLLINGER BANDS
        # ==========================================

        st.subheader("Bollinger Bands")

        c1, c2, c3 = st.columns(3)

        c1.metric(
            "Upper Band",
            f"{latest['BB_UPPER']:.5f}",
        )

        c2.metric(
            "Middle Band",
            f"{latest['BB_MIDDLE']:.5f}",
        )

        c3.metric(
            "Lower Band",
            f"{latest['BB_LOWER']:.5f}",
        )

        st.divider()

        # ==========================================
        # HISTORICAL DATA
        # ==========================================

        with st.expander(
            "View Historical Indicator Data"
        ):

            st.dataframe(
                data.tail(50),
                use_container_width=True,
                hide_index=True,
            )


# ==================================================
# SYSTEM STATUS PAGE
# ==================================================

elif page == "System Status":

    st.subheader("NEXORA FX System Health")

    st.success(
        "Dashboard application is running"
    )

    # CHECK ACTUAL MT5 CONNECTION

    mt5_status = check_mt5_connection()

    if mt5_status:

        st.success(
            "MetaTrader 5 connection successful"
        )

    else:

        st.error(
            "MetaTrader 5 connection unavailable"
        )

    st.success(
        "Market data module loaded"
    )

    st.success(
        "Technical indicators module loaded"
    )

    st.warning(
        "AI prediction engine: Not built yet"
    )

    st.warning(
        "Automated execution: Disabled"
    )

    st.info(
        "NEXORA FX is currently operating in "
        "read-only research mode. It does not "
        "place or modify trades."
    )

    st.divider()

    st.subheader("Platform Information")

    st.write("Platform: NEXORA FX")

    st.write("Environment: Local Windows PC")

    st.write("Broker connection: MetaTrader 5")

    st.write("Market data: MT5 market feed")

    st.write("Analysis: Technical indicators")

    st.write("Execution permissions: Disabled")


# ==================================================
# FOOTER
# ==================================================

st.divider()

st.markdown("### NEXORA :blue[FX]")

st.caption(
    "INTELLIGENCE. PRECISION. DISCIPLINE."
)

st.caption(
    "Research Mode | Automated Trading Disabled"
)
