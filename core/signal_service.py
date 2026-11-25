import MetaTrader5 as mt5
import pandas as pd

from core.market_data import (
    MarketDataError,
    get_historical_data,
    get_live_price,
)
from core.multi_timeframe_smc import analyze_multi_timeframe_smc
from core.zone_validator import validate_smc_zones
from core.signal_engine import evaluate_signal
from execution.mt5_connection import MT5ConnectionError, mt5_connection


def _failure(symbol, reason, **details):
    return {
        "valid": False,
        "symbol": symbol,
        "signal": "WAIT",
        "classification": "WAIT",
        "reason": reason,
        "execution_permitted": False,
        **details,
    }


def _get_completed_candles(symbol, timeframe, bars):
    """Retrieve validated completed candles from the market-data layer."""

    data = get_historical_data(symbol, timeframe, bars)
    if len(data) < 2:
        raise MarketDataError(
            f"Not enough completed candles for {symbol}."
        )
    return data


def analyze_signal_for_symbol(symbol, bars=500):
    """
    NEXORA FX read-only signal analysis service.

    No MT5 order is placed, modified, or closed.
    """

    if not isinstance(symbol, str) or not symbol.strip():
        return _failure(symbol, "A valid trading symbol is required.")
    symbol = symbol.strip()

    if (
        not isinstance(bars, int)
        or isinstance(bars, bool)
        or bars < 250
    ):
        return _failure(
            symbol,
            "At least 250 historical candles are required.",
        )

    try:
        with mt5_connection():
            return _analyze_signal_for_symbol(symbol, bars)
    except MT5ConnectionError as error:
        return _failure(symbol, str(error))
    except MarketDataError as error:
        return _failure(symbol, str(error))


def _analyze_signal_for_symbol(symbol, bars):
    live_price = get_live_price(symbol)

    mtf_result = analyze_multi_timeframe_smc(
        symbol,
        bars=bars,
    )
    if not isinstance(mtf_result, dict) or not mtf_result.get("valid"):
        return _failure(
            symbol,
            (
                mtf_result.get("reason", "SMC analysis is invalid.")
                if isinstance(mtf_result, dict)
                else "SMC analysis returned an invalid result."
            ),
            mtf_result=mtf_result,
        )

    zone_result = validate_smc_zones(symbol, mtf_result)
    if not isinstance(zone_result, dict) or not zone_result.get("valid"):
        return _failure(
            symbol,
            (
                zone_result.get("reason", "Zone validation is invalid.")
                if isinstance(zone_result, dict)
                else "Zone validation returned an invalid result."
            ),
            mtf_result=mtf_result,
            zone_result=zone_result,
        )

    h1_data = _get_completed_candles(
        symbol,
        mt5.TIMEFRAME_H1,
        bars,
    )
    m15_data = _get_completed_candles(
        symbol,
        mt5.TIMEFRAME_M15,
        bars,
    )

    if len(h1_data) < 250:
        return _failure(
            symbol,
            "Insufficient completed H1 candles.",
            mtf_result=mtf_result,
            zone_result=zone_result,
        )
    if len(m15_data) < 50:
        return _failure(
            symbol,
            "Insufficient completed M15 candles.",
            mtf_result=mtf_result,
            zone_result=zone_result,
        )

    signal_result = evaluate_signal(
        symbol=symbol,
        mtf_result=mtf_result,
        zone_result=zone_result,
        m15_data=m15_data,
        h1_data=h1_data,
        live_price=live_price,
    )
    signal_timestamp = (
        m15_data["time"].iloc[-1]
        + pd.Timedelta(minutes=15)
    ).isoformat()
    if isinstance(signal_result, dict):
        signal_result["signal_timestamp"] = signal_timestamp

    return {
        "valid": bool(
            isinstance(signal_result, dict)
            and signal_result.get("valid", False)
        ),
        "symbol": symbol,
        "signal_result": signal_result,
        "mtf_result": mtf_result,
        "zone_result": zone_result,
        "live_price": live_price,
        "signal_timestamp": signal_timestamp,
        "execution_permitted": False,
        "reason": "Read-only signal analysis completed.",
    }
