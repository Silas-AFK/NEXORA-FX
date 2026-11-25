from datetime import datetime, timezone

import pandas as pd

from core.signal_store import (
    get_recent_signals,
    record_signal,
    resolve_pending_outcomes,
)


def test_signal_deduplication_does_not_mark_confirmed_as_success(tmp_path):
    database = tmp_path / "signals.sqlite3"
    signal = {
        "classification": "CONFIRMED",
        "direction": "BULLISH",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "quality_score": 80.0,
    }
    timestamp = "2025-01-01T00:15:00Z"

    first = record_signal(
        "EURUSD",
        signal,
        signal_timestamp=timestamp,
        db_path=database,
    )
    duplicate = record_signal(
        "EURUSD",
        signal,
        signal_timestamp=timestamp,
        db_path=database,
    )
    stored = get_recent_signals(db_path=database)

    assert first["inserted"] is True
    assert duplicate["inserted"] is False
    assert duplicate["signal_id"] == first["signal_id"]
    assert len(stored) == 1
    assert stored[0]["classification"] == "CONFIRMED"
    assert stored[0]["outcome"] == "PENDING"
    assert stored[0]["outcome_timestamp"] is None


def test_outcome_resolution_uses_future_candles_and_conservative_ties(tmp_path):
    database = tmp_path / "signals.sqlite3"
    record_signal(
        "EURUSD",
        {
            "classification": "CONFIRMED",
            "direction": "BULLISH",
            "entry": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
        },
        signal_timestamp="2025-01-01T00:15:00Z",
        db_path=database,
    )
    candles = pd.DataFrame({
        "time": [
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:15:00Z",
        ],
        "open": [100.0, 100.0],
        "high": [101.0, 102.5],
        "low": [99.5, 98.5],
        "close": [100.0, 100.5],
    })

    updated = resolve_pending_outcomes(
        "EURUSD",
        candles,
        timeframe_minutes=15,
        db_path=database,
    )
    stored = get_recent_signals(db_path=database)

    assert len(updated) == 1
    assert stored[0]["outcome"] == "AMBIGUOUS_STOP_FIRST"
    assert stored[0]["outcome_r"] == -1.0
    assert stored[0]["outcome_timestamp"] == "2025-01-01T00:30:00+00:00"


def test_wait_signal_without_prices_remains_unresolved(tmp_path):
    database = tmp_path / "signals.sqlite3"
    record_signal(
        "EURUSD",
        {
            "classification": "WAIT",
            "direction": "UNKNOWN",
            "quality_score": 20,
        },
        signal_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        db_path=database,
    )

    assert get_recent_signals(db_path=database)[0]["outcome"] == "PENDING"


def test_outcome_resolver_ignores_a_forming_candle(tmp_path):
    database = tmp_path / "signals.sqlite3"
    now = pd.Timestamp.now(tz="UTC")
    current_open = now.floor("15min")
    record_signal(
        "EURUSD",
        {
            "classification": "CONFIRMED",
            "direction": "BULLISH",
            "entry": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
        },
        signal_timestamp=current_open.isoformat(),
        db_path=database,
    )
    forming = pd.DataFrame({
        "time": [current_open],
        "open": [100.0],
        "high": [103.0],
        "low": [98.0],
        "close": [100.0],
    })

    updated = resolve_pending_outcomes(
        "EURUSD",
        forming,
        db_path=database,
    )

    assert updated == []
    assert get_recent_signals(db_path=database)[0]["outcome"] == "PENDING"
