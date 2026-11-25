from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
import math

import pandas as pd


def _utc_midnight(value):
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def utc_period_bounds(period, now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Performance periods require timezone-aware datetimes.")
    current = current.astimezone(timezone.utc)
    today = current.date()
    if period == "daily":
        start = today
        end = today + timedelta(days=1)
    elif period == "weekly":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=7)
    elif period == "monthly":
        start = today.replace(day=1)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    else:
        raise ValueError("Performance period must be daily, weekly, or monthly.")
    return _utc_midnight(start), _utc_midnight(end)


def summarize_trades(trades):
    """Calculate performance from broker-verified closed-trade records."""

    values = []
    for trade in trades:
        profit = trade.get("net_profit")
        if (
            isinstance(profit, bool)
            or not isinstance(profit, (int, float))
            or not math.isfinite(profit)
        ):
            continue
        values.append(float(profit))

    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    win_rate = len(wins) / len(values) * 100.0 if values else None
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None
    expectancy = sum(values) / len(values) if values else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    max_consecutive_losses = 0
    max_consecutive_wins = 0
    losses_in_row = 0
    wins_in_row = 0
    for value in values:
        if value < 0:
            losses_in_row += 1
            wins_in_row = 0
        elif value > 0:
            wins_in_row += 1
            losses_in_row = 0
        else:
            wins_in_row = 0
            losses_in_row = 0
        max_consecutive_losses = max(max_consecutive_losses, losses_in_row)
        max_consecutive_wins = max(max_consecutive_wins, wins_in_row)

    durations = [
        float(trade["duration_seconds"])
        for trade in trades
        if isinstance(trade.get("duration_seconds"), (int, float))
        and math.isfinite(trade["duration_seconds"])
        and trade["duration_seconds"] >= 0
    ]
    reward_risk_values = [
        float(trade["net_profit"]) / float(trade["planned_risk_amount"])
        for trade in trades
        if isinstance(trade.get("net_profit"), (int, float))
        and math.isfinite(trade["net_profit"])
        and isinstance(trade.get("planned_risk_amount"), (int, float))
        and math.isfinite(trade["planned_risk_amount"])
        and trade["planned_risk_amount"] > 0
    ]
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": sum(values),
        "profit_factor": profit_factor,
        "average_profit": expectancy,
        "average_win": average_win,
        "average_loss": average_loss,
        "expectancy": expectancy,
        "average_reward_to_risk": (
            sum(reward_risk_values) / len(reward_risk_values)
            if reward_risk_values
            else None
        ),
        "max_drawdown": max_drawdown,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "average_duration_seconds": (
            sum(durations) / len(durations) if durations else None
        ),
    }


def group_performance(trades, group_key):
    groups = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(group_key) or "UNKNOWN")].append(trade)
    return {
        key: summarize_trades(records)
        for key, records in sorted(groups.items())
    }


def daily_realized_series(trades):
    """Return daily closed-trade P/L, not an account-equity series."""

    totals = defaultdict(float)
    for trade in trades:
        timestamp = trade.get("exit_time")
        profit = trade.get("net_profit")
        if not isinstance(timestamp, str) or not isinstance(profit, (int, float)):
            continue
        try:
            moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None or not math.isfinite(profit):
            continue
        totals[moment.astimezone(timezone.utc).date().isoformat()] += float(profit)
    frame = pd.DataFrame(
        [{"date": date, "net_profit": amount} for date, amount in sorted(totals.items())]
    )
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"], utc=True)
        frame["cumulative_realized_profit"] = frame["net_profit"].cumsum()
    return frame


def performance_report(trades):
    """Build verified-history metrics and safe, clearly labelled chart data."""

    ordered = sorted(
        trades,
        key=lambda trade: str(trade.get("exit_time") or ""),
    )
    daily = daily_realized_series(ordered)
    return {
        "summary": summarize_trades(ordered),
        "by_symbol": group_performance(ordered, "symbol"),
        "by_strategy": group_performance(ordered, "strategy"),
        "by_direction": group_performance(ordered, "direction"),
        "daily_realized": daily,
    }
