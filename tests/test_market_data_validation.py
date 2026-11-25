from contextlib import nullcontext
from types import SimpleNamespace
import time
from unittest.mock import Mock

import pandas as pd
import pytest

from core.data_validation import (
    MarketDataValidationError,
    validate_ohlc_data,
)
from core import market_data


def _candles(times=None):
    times = times or [
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:15:00Z",
    ]
    return pd.DataFrame({
        "time": times,
        "open": [1.1] * len(times),
        "high": [1.2] * len(times),
        "low": [1.0] * len(times),
        "close": [1.15] * len(times),
    })


def test_validate_ohlc_normalizes_timestamps_to_utc():
    result = validate_ohlc_data(_candles())

    assert str(result["time"].dt.tz) == "UTC"
    assert result["time"].is_monotonic_increasing


@pytest.mark.parametrize(
    "frame, message",
    [
        (pd.DataFrame({"open": [1], "high": [1], "low": [1]}), "missing"),
        (
            pd.DataFrame({
                "time": ["2025-01-01T00:00:00Z"],
                "open": ["x"],
                "high": [1.2],
                "low": [1.0],
                "close": [1.1],
            }),
            "non-numeric",
        ),
        (
            pd.DataFrame({
                "time": ["2025-01-01T00:00:00Z"],
                "open": [None],
                "high": [1.2],
                "low": [1.0],
                "close": [1.1],
            }),
            "missing",
        ),
        (
            pd.DataFrame({
                "time": ["2025-01-01T00:00:00Z"],
                "open": [1.1],
                "high": [float("inf")],
                "low": [1.0],
                "close": [1.1],
            }),
            "non-finite",
        ),
        (
            pd.DataFrame({
                "time": ["2025-01-01T00:00:00Z"],
                "open": [1.1],
                "high": [1.0],
                "low": [1.05],
                "close": [1.1],
            }),
            "relationships",
        ),
    ],
)
def test_validate_ohlc_rejects_malformed_frames(frame, message):
    with pytest.raises(MarketDataValidationError, match=message):
        validate_ohlc_data(frame)


def test_validate_ohlc_rejects_duplicate_and_unordered_timestamps():
    duplicate = _candles([
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:00:00Z",
    ])
    unordered = _candles([
        "2025-01-01T00:15:00Z",
        "2025-01-01T00:00:00Z",
    ])

    with pytest.raises(MarketDataValidationError, match="duplicate"):
        validate_ohlc_data(duplicate)
    with pytest.raises(MarketDataValidationError, match="chronological"):
        validate_ohlc_data(unordered)


def test_get_historical_data_excludes_only_forming_candle(monkeypatch):
    now = int(time.time())
    current_open = now - now % 900
    rates = [
        {
            "time": current_open - offset,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
        }
        for offset in (1800, 900, 0)
    ]
    monkeypatch.setattr(market_data, "mt5_connection", nullcontext)
    monkeypatch.setattr(
        market_data,
        "_ensure_symbol_available",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "copy_rates_from_pos",
        Mock(return_value=rates),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "symbol_info_tick",
        Mock(return_value=None),
    )

    result = market_data.get_historical_data(
        "EURUSD",
        market_data.mt5.TIMEFRAME_M15,
        bars=3,
    )

    assert len(result) == 2
    assert result["time"].is_monotonic_increasing


def test_historical_candles_are_normalized_from_broker_server_time(monkeypatch):
    now = int(time.time())
    current_open = now - now % 900
    rates = [
        {
            "time": current_open + 7200 - offset,
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
        }
        for offset in (1800, 900, 0)
    ]
    monkeypatch.setattr(market_data, "mt5_connection", nullcontext)
    monkeypatch.setattr(
        market_data,
        "_ensure_symbol_available",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "copy_rates_from_pos",
        Mock(return_value=rates),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "symbol_info_tick",
        Mock(return_value=SimpleNamespace(
            time_msc=(now + 7200) * 1000,
            time=0,
        )),
    )

    result = market_data.get_historical_data(
        "EURUSD",
        market_data.mt5.TIMEFRAME_M15,
        bars=3,
    )

    assert len(result) == 2
    assert result["time"].iloc[-1].timestamp() == current_open - 900
    assert result.attrs["server_time_offset_seconds"] == 7200


def test_get_live_price_rejects_stale_quotes(monkeypatch):
    monkeypatch.setattr(market_data, "mt5_connection", nullcontext)
    monkeypatch.setattr(
        market_data,
        "_ensure_symbol_available",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "symbol_info_tick",
        Mock(return_value=SimpleNamespace(
            bid=1.1,
            ask=1.1002,
            time_msc=int((time.time() - 300) * 1000),
            time=0,
        )),
    )

    with pytest.raises(market_data.MarketDataError, match="stale"):
        market_data.get_live_price("EURUSD", max_age_seconds=120)


def test_get_live_price_accepts_normalized_broker_server_offset(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(
        market_data,
        "time",
        SimpleNamespace(time=lambda: now),
    )
    monkeypatch.setattr(market_data, "mt5_connection", nullcontext)
    monkeypatch.setattr(
        market_data,
        "_ensure_symbol_available",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        market_data.mt5,
        "symbol_info_tick",
        Mock(return_value=SimpleNamespace(
            bid=1.1,
            ask=1.1002,
            time_msc=int((now + 7200) * 1000),
            time=0,
        )),
    )

    quote = market_data.get_live_price("EURUSD")

    assert quote["server_time_offset_seconds"] == 7200
    assert quote["age_seconds"] < 1
