import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.signal_engine import evaluate_signal


def _candles(count):
    close = [1.1 + index * 0.0001 for index in range(count)]
    return pd.DataFrame({
        "open": close,
        "high": [value + 0.0002 for value in close],
        "low": [value - 0.0002 for value in close],
        "close": close,
    })


def test_wait_result_includes_partial_quality_score():
    mtf_result = {
        "valid": True,
        "symbol": "EURUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            "H4": {"valid": True, "trend": "BULLISH"},
            "H1": {"valid": True, "trend": "BULLISH"},
            "M15": {"valid": True, "trend": "BEARISH"},
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "EURUSD"},
        m15_data=_candles(50),
        h1_data=_candles(250),
    )

    assert result["classification"] == "WAIT"
    assert result["missing_conditions"] == ["MULTI_TIMEFRAME_ALIGNMENT"]
    assert result["quality_score"] == 16.67
    assert result["quality_breakdown"]["trend_alignment"] == 16.67
    assert result["quality_basis"].startswith("Rule-based")
    assert result["execution_permitted"] is False


def test_rejected_result_includes_score_and_rejection_reason():
    mtf_result = {
        "valid": True,
        "symbol": "EURUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            "H4": {"valid": True, "trend": "BULLISH"},
            "H1": {
                "valid": True,
                "trend": "BULLISH",
                "event_direction": "BEARISH",
                "event_age_bars": 1,
            },
            "M15": {"valid": True, "trend": "BULLISH"},
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "EURUSD"},
        m15_data=_candles(50),
        h1_data=_candles(250),
    )

    assert result["classification"] == "REJECTED"
    assert result["quality_score"] == 25
    assert result["quality_breakdown"]["trend_alignment"] == 25
    assert result["warnings"] == ["OPPOSING_H1_STRUCTURE_EVENT"]
    assert result["execution_permitted"] is False


def test_symbol_mismatch_returns_wait():
    mtf_result = {
        "valid": True,
        "symbol": "GBPUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            name: {"valid": True, "trend": "BULLISH"}
            for name in ("H4", "H1", "M15")
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "EURUSD"},
        m15_data=_candles(50),
        h1_data=_candles(250),
    )

    assert result["valid"] is False
    assert result["classification"] == "WAIT"
    assert result["missing_conditions"] == ["MTF_SYMBOL_MATCH"]
    assert "does not match" in result["reason"]
    assert result["execution_permitted"] is False


def test_incomplete_required_timeframe_returns_wait():
    mtf_result = {
        "valid": True,
        "symbol": "EURUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            "H4": {"valid": True, "trend": "BULLISH"},
            "H1": {"valid": True, "trend": "BULLISH"},
            "M15": {"valid": False, "trend": "BULLISH"},
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "EURUSD"},
        m15_data=_candles(50),
        h1_data=_candles(250),
    )

    assert result["valid"] is False
    assert result["classification"] == "WAIT"
    assert result["missing_conditions"] == ["VALID_M15_DATA"]


def test_zone_symbol_mismatch_returns_wait():
    mtf_result = {
        "valid": True,
        "symbol": "EURUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            name: {"valid": True, "trend": "BULLISH"}
            for name in ("H4", "H1", "M15")
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "GBPUSD"},
        m15_data=_candles(50),
        h1_data=_candles(250),
    )

    assert result["classification"] == "WAIT"
    assert result["missing_conditions"] == ["ZONE_SYMBOL_MATCH"]


def test_invalid_m15_candles_return_wait_with_reason():
    m15 = _candles(50)
    m15.loc[10, "close"] = float("nan")
    mtf_result = {
        "valid": True,
        "symbol": "EURUSD",
        "market_bias": "BULLISH",
        "timeframes": {
            name: {"valid": True, "trend": "BULLISH"}
            for name in ("H4", "H1", "M15")
        },
    }

    result = evaluate_signal(
        symbol="EURUSD",
        mtf_result=mtf_result,
        zone_result={"valid": True, "symbol": "EURUSD"},
        m15_data=m15,
        h1_data=_candles(250),
    )

    assert result["classification"] == "WAIT"
    assert result["missing_conditions"] == ["VALID_M15_CANDLES"]
    assert "missing or non-numeric" in result["reason"]


if __name__ == "__main__":
    test_wait_result_includes_partial_quality_score()
    test_rejected_result_includes_score_and_rejection_reason()
    print("Signal-quality tests passed.")
