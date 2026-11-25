from datetime import datetime, timezone
import math
import time

import MetaTrader5 as mt5
import pandas as pd

from core.data_validation import (
    MarketDataValidationError,
    validate_ohlc_data,
)
from execution.mt5_connection import (
    MT5ConnectionError,
    mt5_connection,
)


class MarketDataError(RuntimeError):
    """Raised when broker market data is unavailable or unsafe to use."""


_TIMEFRAME_SECONDS = {
    mt5.TIMEFRAME_M1: 60,
    mt5.TIMEFRAME_M5: 5 * 60,
    mt5.TIMEFRAME_M15: 15 * 60,
    mt5.TIMEFRAME_M30: 30 * 60,
    mt5.TIMEFRAME_H1: 60 * 60,
    mt5.TIMEFRAME_H4: 4 * 60 * 60,
    mt5.TIMEFRAME_D1: 24 * 60 * 60,
    mt5.TIMEFRAME_W1: 7 * 24 * 60 * 60,
}


def _broker_utc_offset_seconds(timestamp, now_epoch):
    """Infer a clear positive broker-server offset from its tick timestamp."""

    if not math.isfinite(timestamp) or timestamp <= now_epoch + 5:
        return 0
    skew = timestamp - now_epoch
    offset = round(skew / 900) * 900
    if 900 <= offset <= 14 * 60 * 60 and abs(skew - offset) <= 300:
        return int(offset)
    return 0


def _tick_timestamp(tick):
    time_msc = getattr(tick, "time_msc", None)
    timestamp = (
        float(time_msc) / 1000.0
        if time_msc
        else float(getattr(tick, "time", 0))
    )
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _ensure_symbol_available(symbol):
    if not isinstance(symbol, str) or not symbol.strip():
        raise MarketDataError("A valid broker symbol is required.")

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        raise MarketDataError(
            f"Broker symbol '{symbol}' is unavailable: {mt5.last_error()}"
        )

    if not symbol_info.visible and not mt5.symbol_select(symbol, True):
        raise MarketDataError(
            f"Could not enable '{symbol}' in Market Watch: {mt5.last_error()}"
        )


def ensure_mt5_connection():
    """Return whether a terminal session can be safely leased."""

    try:
        with mt5_connection():
            return True
    except MT5ConnectionError:
        return False


def ensure_symbol_available(symbol):
    """Return whether a symbol is available, without retaining a connection."""

    try:
        with mt5_connection():
            _ensure_symbol_available(symbol)
        return True
    except (MT5ConnectionError, MarketDataError):
        return False


def get_live_price(symbol, max_age_seconds=120):
    """Return a fresh bid/ask quote, rejecting stale or malformed ticks."""

    if (
        isinstance(max_age_seconds, bool)
        or not isinstance(max_age_seconds, (int, float))
        or not math.isfinite(max_age_seconds)
        or max_age_seconds <= 0
    ):
        raise MarketDataError("Quote freshness limit must be positive.")

    try:
        with mt5_connection():
            _ensure_symbol_available(symbol)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise MarketDataError(
                    f"Could not retrieve a quote for '{symbol}': {mt5.last_error()}"
                )

            bid = float(tick.bid)
            ask = float(tick.ask)
            if (
                not math.isfinite(bid)
                or not math.isfinite(ask)
                or bid <= 0
                or ask < bid
            ):
                raise MarketDataError(
                    f"Broker returned invalid bid/ask values for '{symbol}'."
                )

            tick_time = _tick_timestamp(tick)
            if tick_time is None:
                raise MarketDataError(
                    f"Quote timestamp is unavailable for '{symbol}'."
                )

            now_epoch = time.time()
            server_offset = _broker_utc_offset_seconds(
                tick_time,
                now_epoch,
            )
            normalized_tick_time = tick_time - server_offset
            age_seconds = now_epoch - normalized_tick_time
            if age_seconds < -5:
                raise MarketDataError(
                    f"Quote timestamp for '{symbol}' is in the future."
                )
            if age_seconds > max_age_seconds:
                raise MarketDataError(
                    f"Quote for '{symbol}' is stale "
                    f"({age_seconds:.1f}s old; limit {max_age_seconds:.0f}s)."
                )

            quote_time = datetime.fromtimestamp(
                normalized_tick_time,
                tz=timezone.utc,
            ).isoformat()
            return {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "time": quote_time,
                "age_seconds": max(age_seconds, 0.0),
                "server_time_offset_seconds": server_offset,
            }
    except MT5ConnectionError as error:
        raise MarketDataError(str(error)) from error


def get_historical_data(
    symbol,
    timeframe,
    bars=1000,
    include_forming=False,
):
    """Fetch validated OHLC candles, excluding any still-forming candle."""

    if (
        isinstance(bars, bool)
        or not isinstance(bars, int)
        or bars <= 0
    ):
        raise MarketDataError("Bars must be a positive integer.")
    if not isinstance(include_forming, bool):
        raise MarketDataError("include_forming must be a boolean.")
    if timeframe not in _TIMEFRAME_SECONDS:
        raise MarketDataError(
            f"Unsupported timeframe for completion checks: {timeframe}."
        )

    try:
        with mt5_connection():
            _ensure_symbol_available(symbol)
            rates = mt5.copy_rates_from_pos(
                symbol,
                timeframe,
                0,
                bars,
            )
            tick = mt5.symbol_info_tick(symbol)
            if rates is None or len(rates) == 0:
                raise MarketDataError(
                    f"No historical data returned for '{symbol}': "
                    f"{mt5.last_error()}"
                )

        frame = pd.DataFrame(rates)
        try:
            frame = validate_ohlc_data(frame, require_time=True)
        except MarketDataValidationError as error:
            raise MarketDataError(
                f"Invalid historical data for '{symbol}': {error}"
            ) from error

        now_epoch = datetime.now(timezone.utc).timestamp()
        tick_time = _tick_timestamp(tick) if tick is not None else None
        server_offset = (
            _broker_utc_offset_seconds(tick_time, now_epoch)
            if tick_time is not None
            else 0
        )
        if server_offset:
            frame["time"] = (
                frame["time"] - pd.Timedelta(seconds=server_offset)
            )

        if frame["time"].iloc[-1].timestamp() > now_epoch + 5:
            raise MarketDataError(
                f"Broker candle timestamps for '{symbol}' are future-dated; "
                "the server time offset could not be determined safely."
            )

        if not include_forming:
            duration = _TIMEFRAME_SECONDS[timeframe]
            last_open = frame["time"].iloc[-1].timestamp()
            if last_open + duration > datetime.now(timezone.utc).timestamp():
                frame = frame.iloc[:-1].copy()

        if frame.empty:
            raise MarketDataError(
                f"No completed candles are available for '{symbol}'."
            )
        frame.attrs["server_time_offset_seconds"] = server_offset
        return frame.reset_index(drop=True)
    except MT5ConnectionError as error:
        raise MarketDataError(str(error)) from error
