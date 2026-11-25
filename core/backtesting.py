from dataclasses import dataclass
import math

import pandas as pd

from core.data_validation import MarketDataValidationError, validate_ohlc_data
from core.multi_timeframe_smc import analyze_multi_timeframe_smc_from_data
from core.signal_engine import evaluate_signal
from core.zone_validator import validate_smc_zones


_TIMEFRAME_MINUTES = {"H4": 240, "H1": 60, "M15": 15}


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10000.0
    risk_percent: float = 0.25
    fixed_spread_price: float = None
    slippage_price: float = 0.0
    commission_bps_per_side: float = 0.0
    max_holding_bars: int = 96
    out_of_sample_fraction: float = 0.30
    minimum_sample_trades: int = 30


def _validate_config(config):
    if not isinstance(config, BacktestConfig):
        raise ValueError("Backtest configuration is invalid.")
    for name in (
        "initial_equity",
        "risk_percent",
        "slippage_price",
        "commission_bps_per_side",
        "out_of_sample_fraction",
    ):
        value = getattr(config, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"{name} must be finite.")
    if config.initial_equity <= 0 or config.risk_percent <= 0:
        raise ValueError("Initial equity and risk must be positive.")
    if config.risk_percent > 100:
        raise ValueError("Risk percentage cannot exceed 100.")
    if config.slippage_price < 0 or config.commission_bps_per_side < 0:
        raise ValueError("Slippage and commission cannot be negative.")
    if not 0 < config.out_of_sample_fraction < 1:
        raise ValueError("Out-of-sample fraction must be between 0 and 1.")
    if (
        isinstance(config.max_holding_bars, bool)
        or not isinstance(config.max_holding_bars, int)
        or config.max_holding_bars < 1
    ):
        raise ValueError("Maximum holding bars must be a positive integer.")
    if (
        isinstance(config.minimum_sample_trades, bool)
        or not isinstance(config.minimum_sample_trades, int)
        or config.minimum_sample_trades < 1
    ):
        raise ValueError("Minimum sample trades must be a positive integer.")
    if config.fixed_spread_price is not None and (
        isinstance(config.fixed_spread_price, bool)
        or not isinstance(config.fixed_spread_price, (int, float))
        or not math.isfinite(config.fixed_spread_price)
        or config.fixed_spread_price <= 0
    ):
        raise ValueError("Fixed spread must be a finite positive price value.")


def _spread_for(candles, index, config):
    if "spread" in candles.columns:
        return float(candles.iloc[index]["spread"])
    if config.fixed_spread_price is not None:
        return float(config.fixed_spread_price)
    raise ValueError(
        "Spread data is required: upload an M15 'spread' column or set a "
        "fixed spread in price units."
    )


def _completed_candles(candles, timeframe_minutes, now=None):
    """Drop a forming last candle and reject future-dated histories."""

    if (
        isinstance(timeframe_minutes, bool)
        or not isinstance(timeframe_minutes, int)
        or timeframe_minutes <= 0
    ):
        raise ValueError("Timeframe minutes must be a positive integer.")
    frame = validate_ohlc_data(candles, require_time=True)
    current_time = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if current_time.tzinfo is None:
        current_time = current_time.tz_localize("UTC")
    else:
        current_time = current_time.tz_convert("UTC")
    if (frame["time"] > current_time).any():
        raise ValueError("Candle data contains future-dated timestamps.")

    candle_close_times = frame["time"] + pd.Timedelta(minutes=timeframe_minutes)
    completed = frame.loc[candle_close_times <= current_time].reset_index(drop=True)
    if completed.empty:
        raise ValueError("No completed candles are available for replay.")
    return completed


def _summarize_trades(trades, initial_equity, minimum_sample_trades):
    equity = initial_equity
    peak = initial_equity
    max_drawdown = 0.0
    wins = 0
    gross_profit = 0.0
    gross_loss = 0.0
    consecutive = 0
    max_consecutive = 0
    expectancy_values = []
    planned_rr = []

    for trade in trades:
        net_r = float(trade["net_r"])
        pnl = equity * float(trade["risk_percent"]) / 100.0 * net_r
        equity += pnl
        trade["pnl"] = pnl
        trade["equity_after"] = equity
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

        expectancy_values.append(net_r)
        planned_rr.append(float(trade["planned_risk_reward"]))
        if net_r > 0:
            wins += 1
            gross_profit += net_r
            consecutive = 0
        else:
            gross_loss += abs(net_r)
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)

    count = len(trades)
    return {
        "trades": count,
        "win_rate_percent": (wins / count * 100.0) if count else 0.0,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (None if gross_profit == 0 else "unbounded")
        ),
        "net_return_percent": (
            (equity / initial_equity - 1) * 100.0
            if initial_equity
            else 0.0
        ),
        "maximum_drawdown_percent": max_drawdown * 100.0,
        "average_reward_to_risk": (
            sum(planned_rr) / len(planned_rr)
            if planned_rr
            else None
        ),
        "expectancy_r": (
            sum(expectancy_values) / count if count else None
        ),
        "maximum_consecutive_losses": max_consecutive,
        "final_equity": equity,
        "sample_status": (
            "INSUFFICIENT_SAMPLE"
            if count < minimum_sample_trades
            else "SAMPLE_THRESHOLD_MET"
        ),
        "sample_threshold": minimum_sample_trades,
    }


def run_bar_backtest(candles, signal_events, config=None, split_index=None):
    """Replay precomputed, point-in-time signals against candles."""

    config = config or BacktestConfig()
    _validate_config(config)
    bars = _completed_candles(candles, timeframe_minutes=15)
    if not isinstance(signal_events, list):
        raise ValueError("Signal events must be a list.")
    if len(bars) < 2:
        raise ValueError("At least two candles are required for replay.")
    if "spread" not in bars.columns and config.fixed_spread_price is None:
        raise ValueError(
            "Spread data is required: upload a spread column or set a fixed spread."
        )

    if split_index is None:
        split_index = int(
            len(bars) * (1 - config.out_of_sample_fraction)
        )
    split_index = max(1, min(int(split_index), len(bars) - 1))

    trades = []
    skipped_gap_entries = 0
    last_trade_exit_index = -1
    events = sorted(
        signal_events,
        key=lambda event: int(event.get("signal_index", -1)),
    )

    for event in events:
        signal_index = event.get("signal_index")
        if (
            isinstance(signal_index, bool)
            or not isinstance(signal_index, int)
            or signal_index < 0
            or signal_index >= len(bars) - 1
        ):
            raise ValueError("Signal event index is outside the candle range.")
        if signal_index <= last_trade_exit_index:
            continue

        direction = str(event.get("direction", "")).upper()
        if direction not in {"BUY", "SELL", "BULLISH", "BEARISH"}:
            raise ValueError("Signal event direction is invalid.")
        direction = "BUY" if direction in {"BUY", "BULLISH"} else "SELL"

        entry_reference = float(event["entry"])
        stop = float(event["stop_loss"])
        target = float(event["take_profit"])
        if not all(
            math.isfinite(value) and value > 0
            for value in (entry_reference, stop, target)
        ):
            raise ValueError("Signal event contains invalid trade levels.")
        if direction == "BUY" and not stop < entry_reference < target:
            raise ValueError("BUY signal levels are inconsistent.")
        if direction == "SELL" and not target < entry_reference < stop:
            raise ValueError("SELL signal levels are inconsistent.")

        spread = _spread_for(bars, signal_index, config)
        half_spread = spread / 2.0
        entry_bar = bars.iloc[signal_index + 1]
        if direction == "BUY":
            entry_fill = (
                float(entry_bar["open"])
                + half_spread
                + config.slippage_price
            )
        else:
            entry_fill = (
                float(entry_bar["open"])
                - half_spread
                - config.slippage_price
            )
        if (
            direction == "BUY"
            and not stop < entry_fill < target
        ) or (
            direction == "SELL"
            and not target < entry_fill < stop
        ):
            skipped_gap_entries += 1
            continue

        stop_distance = abs(entry_fill - stop)
        planned_rr = abs(target - entry_fill) / stop_distance
        last_index = min(
            len(bars) - 1,
            signal_index + 1 + config.max_holding_bars - 1,
        )
        outcome = "TIME_EXIT"
        exit_index = last_index
        exit_fill = None

        for candle_index in range(signal_index + 1, last_index + 1):
            candle = bars.iloc[candle_index]
            bar_spread = _spread_for(bars, candle_index - 1, config)
            half_bar_spread = bar_spread / 2.0
            mid_open = float(candle["open"])
            mid_high = float(candle["high"])
            mid_low = float(candle["low"])
            if direction == "BUY":
                stop_hit = mid_low - half_bar_spread <= stop
                target_hit = mid_high - half_bar_spread >= target
                opening_stop = mid_open - half_bar_spread <= stop
                opening_target = mid_open - half_bar_spread >= target
                if opening_stop:
                    outcome = "STOP_LOSS"
                    exit_fill = mid_open - half_bar_spread - config.slippage_price
                elif opening_target:
                    outcome = "TAKE_PROFIT"
                    exit_fill = target - config.slippage_price
                elif stop_hit and target_hit:
                    outcome = "AMBIGUOUS_STOP_FIRST"
                    exit_fill = stop - config.slippage_price
                elif stop_hit:
                    outcome = "STOP_LOSS"
                    exit_fill = stop - config.slippage_price
                elif target_hit:
                    outcome = "TAKE_PROFIT"
                    exit_fill = target - config.slippage_price
            else:
                stop_hit = mid_high + half_bar_spread >= stop
                target_hit = mid_low + half_bar_spread <= target
                opening_stop = mid_open + half_bar_spread >= stop
                opening_target = mid_open + half_bar_spread <= target
                if opening_stop:
                    outcome = "STOP_LOSS"
                    exit_fill = mid_open + half_bar_spread + config.slippage_price
                elif opening_target:
                    outcome = "TAKE_PROFIT"
                    exit_fill = target + config.slippage_price
                elif stop_hit and target_hit:
                    outcome = "AMBIGUOUS_STOP_FIRST"
                    exit_fill = stop + config.slippage_price
                elif stop_hit:
                    outcome = "STOP_LOSS"
                    exit_fill = stop + config.slippage_price
                elif target_hit:
                    outcome = "TAKE_PROFIT"
                    exit_fill = target + config.slippage_price
            if exit_fill is not None:
                exit_index = candle_index
                break

        if exit_fill is None:
            final_candle = bars.iloc[exit_index]
            exit_spread = _spread_for(
                bars,
                max(signal_index, exit_index - 1),
                config,
            ) / 2.0
            if direction == "BUY":
                exit_fill = (
                    float(final_candle["close"])
                    - exit_spread
                    - config.slippage_price
                )
            else:
                exit_fill = (
                    float(final_candle["close"])
                    + exit_spread
                    + config.slippage_price
                )

        direction_sign = 1.0 if direction == "BUY" else -1.0
        gross_r = direction_sign * (exit_fill - entry_fill) / stop_distance
        commission_r = (
            config.commission_bps_per_side
            * 2.0
            / 10000.0
            * entry_fill
            / stop_distance
        )
        net_r = gross_r - commission_r
        trades.append({
            "symbol": event.get("symbol"),
            "direction": direction,
            "signal_index": signal_index,
            "signal_timestamp": event.get("signal_timestamp"),
            "entry_index": signal_index + 1,
            "entry_timestamp": bars["time"].iloc[signal_index + 1].isoformat(),
            "entry_price": entry_fill,
            "stop_loss": stop,
            "take_profit": target,
            "exit_index": exit_index,
            "exit_timestamp": bars["time"].iloc[exit_index].isoformat(),
            "exit_price": exit_fill,
            "outcome": outcome,
            "gross_r": gross_r,
            "commission_r": commission_r,
            "net_r": net_r,
            "planned_risk_reward": planned_rr,
            "risk_percent": config.risk_percent,
            "spread_price": spread,
            "slippage_price_per_side": config.slippage_price,
            "commission_bps_per_side": config.commission_bps_per_side,
        })
        last_trade_exit_index = exit_index

    overall = _summarize_trades(
        trades,
        config.initial_equity,
        config.minimum_sample_trades,
    )
    in_sample_trades = [
        trade for trade in trades
        if trade["signal_index"] < split_index
    ]
    out_sample_trades = [
        trade for trade in trades
        if trade["signal_index"] >= split_index
    ]
    return {
        "valid": True,
        "trades": trades,
        "statistics": overall,
        "in_sample": _summarize_trades(
            in_sample_trades,
            config.initial_equity,
            config.minimum_sample_trades,
        ),
        "out_of_sample": _summarize_trades(
            out_sample_trades,
            config.initial_equity,
            config.minimum_sample_trades,
        ),
        "skipped_gap_entries": skipped_gap_entries,
        "execution_permitted": False,
        "research_only": True,
    }


def run_smc_backtest(
    symbol,
    m15_data,
    h1_data,
    h4_data,
    config=None,
):
    """Recompute NEXORA SMC signals from point-in-time uploaded candles."""

    config = config or BacktestConfig()
    _validate_config(config)
    if not isinstance(symbol, str) or not symbol.strip():
        return {"valid": False, "reason": "A valid symbol is required."}

    try:
        reference_time = pd.Timestamp.now(tz="UTC")
        m15 = _completed_candles(m15_data, 15, now=reference_time)
        h1 = _completed_candles(h1_data, 60, now=reference_time)
        h4 = _completed_candles(h4_data, 240, now=reference_time)
    except MarketDataValidationError as error:
        return {"valid": False, "reason": str(error)}

    if len(m15) < 50 or len(h1) < 250 or len(h4) < 50:
        return {
            "valid": False,
            "reason": "Replay requires at least 50 M15, 250 H1 and 50 H4 candles.",
        }
    if "spread" not in m15.columns and config.fixed_spread_price is None:
        return {
            "valid": False,
            "reason": (
                "Spread modelling is required. Upload an M15 'spread' "
                "column or configure a fixed spread in price units."
            ),
        }
    if len(m15) > 5000:
        return {
            "valid": False,
            "reason": (
                "The offline SMC replay limit is 5,000 M15 candles. "
                "Use a narrower date range for responsive replay."
            ),
        }

    h1_close = h1["time"] + pd.Timedelta(minutes=_TIMEFRAME_MINUTES["H1"])
    h4_close = h4["time"] + pd.Timedelta(minutes=_TIMEFRAME_MINUTES["H4"])
    signal_events = []
    signals_evaluated = 0
    no_spread_rows = 0

    for index in range(49, len(m15) - 1):
        signal_bar = m15.iloc[index]
        decision_time = signal_bar["time"] + pd.Timedelta(minutes=15)
        h1_completed = h1.loc[h1_close <= decision_time].tail(500)
        h4_completed = h4.loc[h4_close <= decision_time].tail(500)
        if len(h1_completed) < 250 or len(h4_completed) < 50:
            continue

        m15_completed = m15.iloc[max(0, index - 499):index + 1]
        frames = {
            "M15": m15_completed,
            "H1": h1_completed,
            "H4": h4_completed,
        }
        mtf_result = analyze_multi_timeframe_smc_from_data(
            symbol.strip(),
            frames,
        )
        if not mtf_result.get("valid"):
            continue

        spread = (
            float(signal_bar["spread"])
            if "spread" in m15.columns
            else config.fixed_spread_price
        )
        if spread is None or spread < 0:
            no_spread_rows += 1
            continue
        close = float(signal_bar["close"])
        bid = close - spread / 2.0
        ask = close + spread / 2.0
        if bid <= 0 or ask < bid:
            continue

        zone_result = validate_smc_zones(
            symbol.strip(),
            mtf_result,
            historical_data=frames,
            current_price=close,
        )
        if not zone_result.get("valid"):
            continue

        signals_evaluated += 1
        result = evaluate_signal(
            symbol=symbol.strip(),
            mtf_result=mtf_result,
            zone_result=zone_result,
            m15_data=m15_completed,
            h1_data=h1_completed,
            live_price={
                "symbol": symbol.strip(),
                "bid": bid,
                "ask": ask,
                "spread": spread,
            },
        )
        if result.get("classification") != "CONFIRMED":
            continue

        signal_events.append({
            "symbol": symbol.strip(),
            "direction": result["direction"],
            "entry": result["entry"],
            "stop_loss": result["stop_loss"],
            "take_profit": result["take_profit"],
            "signal_index": index,
            "signal_timestamp": decision_time.isoformat(),
        })

    split_index = int(
        len(m15) * (1 - config.out_of_sample_fraction)
    )
    replay = run_bar_backtest(
        m15,
        signal_events,
        config=config,
        split_index=split_index,
    )
    replay.update({
        "symbol": symbol.strip(),
        "source": "Uploaded historical candles; recomputed SMC signals",
        "signals_evaluated": signals_evaluated,
        "confirmed_signals": len(signal_events),
        "out_of_sample_split_index": split_index,
        "spread_model": (
            "Per-candle M15 spread column"
            if "spread" in m15.columns
            else "Fixed configured spread"
        ),
        "price_assumption": "Uploaded candle OHLC is treated as mid-price.",
        "warnings": [
            "Simulated results are not broker fills or evidence of profitability.",
            "Trade risk is theoretical and assumes exact risk-percent sizing.",
            "Costs depend on the supplied spread, slippage and commission inputs.",
        ],
    })
    if no_spread_rows:
        replay["warnings"].append(
            f"{no_spread_rows} replay rows were skipped for invalid spread values."
        )
    replay["per_symbol"] = {
        symbol.strip(): replay["statistics"],
    }
    return replay


def run_smc_backtests(datasets_by_symbol, config=None):
    """Run isolated offline SMC replays and expose statistics per symbol."""

    if not isinstance(datasets_by_symbol, dict) or not datasets_by_symbol:
        return {
            "valid": False,
            "reason": "At least one symbol dataset is required.",
            "execution_permitted": False,
        }

    results = {}
    for symbol, frames in datasets_by_symbol.items():
        if not isinstance(frames, dict):
            results[symbol] = {
                "valid": False,
                "reason": "Each symbol must map to M15, H1 and H4 data.",
            }
            continue
        results[symbol] = run_smc_backtest(
            symbol=symbol,
            m15_data=frames.get("M15"),
            h1_data=frames.get("H1"),
            h4_data=frames.get("H4"),
            config=config,
        )

    return {
        "valid": all(result.get("valid", False) for result in results.values()),
        "per_symbol": {
            symbol: result.get("statistics")
            for symbol, result in results.items()
            if result.get("valid")
        },
        "results": results,
        "execution_permitted": False,
        "research_only": True,
    }
