import pandas as pd
from unittest.mock import Mock

from core import backtesting


def _candles(times, spread=0.1):
    frame = pd.DataFrame({
        "time": times,
        "open": [100.0] * len(times),
        "high": [100.5] * len(times),
        "low": [99.5] * len(times),
        "close": [100.0] * len(times),
        "spread": [spread] * len(times),
    })
    return frame


def test_backtest_enters_after_signal_bar_and_stops_first_on_ambiguity():
    times = pd.date_range(
        "2025-01-01T00:00:00Z",
        periods=5,
        freq="15min",
    )
    candles = _candles(times)
    candles.loc[2, "high"] = 102.5
    candles.loc[2, "low"] = 98.5
    event = {
        "symbol": "EURUSD",
        "direction": "BUY",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "signal_index": 0,
        "signal_timestamp": times[0].isoformat(),
    }

    result = backtesting.run_bar_backtest(
        candles,
        [event],
        config=backtesting.BacktestConfig(
            initial_equity=10000,
            risk_percent=0.25,
            slippage_price=0.01,
            commission_bps_per_side=0.35,
            minimum_sample_trades=2,
        ),
        split_index=2,
    )
    trade = result["trades"][0]

    assert trade["entry_index"] == 1
    assert trade["entry_timestamp"] == times[1].isoformat()
    assert trade["exit_index"] == 2
    assert trade["outcome"] == "AMBIGUOUS_STOP_FIRST"
    assert trade["net_r"] < -1
    assert trade["commission_r"] > 0
    assert result["statistics"]["maximum_drawdown_percent"] > 0
    assert result["statistics"]["sample_status"] == "INSUFFICIENT_SAMPLE"
    assert result["execution_permitted"] is False


def test_backtest_records_take_profit_and_out_of_sample_statistics():
    times = pd.date_range(
        "2025-01-01T00:00:00Z",
        periods=6,
        freq="15min",
    )
    candles = _candles(times)
    candles.loc[2, "high"] = 102.5
    event = {
        "direction": "BUY",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "signal_index": 1,
    }

    result = backtesting.run_bar_backtest(
        candles,
        [event],
        config=backtesting.BacktestConfig(
            fixed_spread_price=0.1,
            commission_bps_per_side=0.0,
            minimum_sample_trades=1,
        ),
        split_index=2,
    )

    assert result["trades"][0]["outcome"] == "TAKE_PROFIT"
    assert result["statistics"]["trades"] == 1
    assert result["out_of_sample"]["trades"] == 0
    assert result["in_sample"]["trades"] == 1


def test_backtest_requires_a_spread_model():
    times = pd.date_range(
        "2025-01-01T00:00:00Z",
        periods=3,
        freq="15min",
    )
    candles = _candles(times).drop(columns="spread")

    try:
        backtesting.run_bar_backtest(
            candles,
            [],
            config=backtesting.BacktestConfig(fixed_spread_price=None),
        )
    except ValueError as error:
        assert "Spread data is required" in str(error)
    else:
        raise AssertionError("A backtest without spread input must fail.")


def test_entry_cost_uses_only_spread_known_at_signal_close():
    times = pd.date_range(
        "2025-01-01T00:00:00Z",
        periods=4,
        freq="15min",
    )
    candles = _candles(times, spread=0.1)
    candles.loc[1, "spread"] = 0.8
    result = backtesting.run_bar_backtest(
        candles,
        [{
            "direction": "BUY",
            "entry": 100.0,
            "stop_loss": 99.0,
            "take_profit": 102.0,
            "signal_index": 0,
        }],
        config=backtesting.BacktestConfig(
            slippage_price=0.0,
            max_holding_bars=1,
        ),
    )

    assert result["trades"][0]["entry_price"] == 100.05
    assert result["trades"][0]["spread_price"] == 0.1


def test_smc_replay_uses_only_completed_higher_timeframe_data(monkeypatch):
    h1_times = pd.date_range(
        "2025-01-01T00:00:00Z",
        periods=270,
        freq="1h",
    )
    h4_times = pd.date_range(
        "2024-12-20T00:00:00Z",
        periods=60,
        freq="4h",
    )
    m15_times = pd.date_range(
        h1_times[-1] - pd.Timedelta(minutes=13 * 15),
        periods=54,
        freq="15min",
    )
    observed = []

    def fake_mtf(symbol, frames):
        decision_end = frames["M15"]["time"].iloc[-1] + pd.Timedelta(minutes=15)
        observed.append({
            "decision_end": decision_end,
            "m15_last": frames["M15"]["time"].iloc[-1],
            "h1_last_close": (
                frames["H1"]["time"].iloc[-1] + pd.Timedelta(hours=1)
            ),
            "h4_last_close": (
                frames["H4"]["time"].iloc[-1] + pd.Timedelta(hours=4)
            ),
        })
        return {
            "valid": True,
            "symbol": symbol,
            "market_bias": "NEUTRAL",
            "timeframes": {
                name: {"valid": True}
                for name in ("H4", "H1", "M15")
            },
        }

    monkeypatch.setattr(
        backtesting,
        "analyze_multi_timeframe_smc_from_data",
        fake_mtf,
    )
    monkeypatch.setattr(
        backtesting,
        "validate_smc_zones",
        Mock(return_value={"valid": True, "symbol": "EURUSD"}),
    )
    monkeypatch.setattr(
        backtesting,
        "evaluate_signal",
        Mock(return_value={"valid": True, "classification": "WAIT"}),
    )

    result = backtesting.run_smc_backtest(
        "EURUSD",
        m15_data=_candles(m15_times),
        h1_data=_candles(h1_times, spread=0.1).drop(columns="spread"),
        h4_data=_candles(h4_times, spread=0.1).drop(columns="spread"),
        config=backtesting.BacktestConfig(
            fixed_spread_price=0.1,
            minimum_sample_trades=1,
        ),
    )

    assert result["valid"] is True
    assert result["signals_evaluated"] == len(observed)
    assert observed
    assert all(item["m15_last"] < item["decision_end"] for item in observed)
    assert all(item["h1_last_close"] <= item["decision_end"] for item in observed)
    assert all(item["h4_last_close"] <= item["decision_end"] for item in observed)
