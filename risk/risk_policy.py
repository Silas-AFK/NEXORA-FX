from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import math

from risk.risk_manager import check_period_loss


@dataclass(frozen=True)
class RiskPolicy:
    risk_per_trade_percent: float = 0.25
    max_daily_loss_percent: float = 1.0
    max_weekly_loss_percent: float = 2.0
    max_monthly_loss_percent: float = 4.0
    max_total_open_risk_percent: float = 0.5
    max_simultaneous_positions: int = 2
    max_positions_per_symbol: int = 1
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    max_spread_pips: float = 2.0
    max_slippage_pips: float = 1.0
    minimum_reward_risk: float = 1.5
    session_start_utc: str | None = None
    session_end_utc: str | None = None

    @classmethod
    def from_mapping(cls, values=None):
        values = values or {}
        defaults = asdict(cls())
        unknown = set(values) - set(defaults)
        if unknown:
            raise ValueError(
                "Unknown risk setting(s): " + ", ".join(sorted(unknown))
            )
        defaults.update(values)

        for name in (
            "risk_per_trade_percent",
            "max_daily_loss_percent",
            "max_weekly_loss_percent",
            "max_monthly_loss_percent",
            "max_total_open_risk_percent",
            "max_spread_pips",
            "max_slippage_pips",
            "minimum_reward_risk",
        ):
            value = defaults[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number.")
            if name == "risk_per_trade_percent" and value > 0.25:
                raise ValueError("Per-trade risk cannot exceed 0.25%.")
            if name.endswith("_loss_percent") or name == "max_total_open_risk_percent":
                if value > 100:
                    raise ValueError(f"{name} cannot exceed 100%.")
            defaults[name] = float(value)

        for name in (
            "max_simultaneous_positions",
            "max_positions_per_symbol",
            "max_trades_per_day",
            "max_consecutive_losses",
        ):
            value = defaults[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or value > 100
            ):
                raise ValueError(f"{name} must be an integer from 1 to 100.")

        if defaults["max_positions_per_symbol"] > defaults["max_simultaneous_positions"]:
            raise ValueError(
                "Per-symbol positions cannot exceed simultaneous positions."
            )

        for name in ("session_start_utc", "session_end_utc"):
            value = defaults[name]
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{name} must be HH:MM or unset.")
                try:
                    datetime.strptime(value, "%H:%M")
                except ValueError as error:
                    raise ValueError(f"{name} must be a valid HH:MM time.") from error
        if (defaults["session_start_utc"] is None) != (
            defaults["session_end_utc"] is None
        ):
            raise ValueError("Both UTC session endpoints must be set together.")
        if defaults["session_start_utc"] == defaults["session_end_utc"] and (
            defaults["session_start_utc"] is not None
        ):
            raise ValueError("UTC session start and end cannot be identical.")
        return cls(**defaults)

    def to_dict(self):
        return asdict(self)


def evaluate_entry_limits(
    policy,
    *,
    equity,
    baselines,
    total_open_risk,
    current_positions,
    positions_by_symbol,
    trades_today,
    consecutive_losses,
    spread_pips,
    reward_risk,
    now=None,
):
    """Return backend entry-denial reasons for validated account context."""

    if not isinstance(policy, RiskPolicy):
        policy = RiskPolicy.from_mapping(policy)
    if (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(equity)
        or equity <= 0
    ):
        return ["Current account equity is unavailable or invalid."]

    reasons = []
    if not isinstance(baselines, dict):
        baselines = {}
        reasons.append("Equity baseline data is invalid.")
    thresholds = {
        "daily": policy.max_daily_loss_percent,
        "weekly": policy.max_weekly_loss_percent,
        "monthly": policy.max_monthly_loss_percent,
    }
    for period, limit in thresholds.items():
        baseline = (baselines or {}).get(period)
        if not isinstance(baseline, dict):
            reasons.append(f"The {period} equity baseline is missing.")
            continue
        loss_check = check_period_loss(
            baseline.get("starting_equity"),
            equity,
            limit,
            period=period,
        )
        if not loss_check["allowed"]:
            reasons.append(loss_check["reason"])

    if (
        isinstance(total_open_risk, bool)
        or not isinstance(total_open_risk, (int, float))
        or not math.isfinite(total_open_risk)
        or total_open_risk < 0
    ):
        reasons.append("Aggregate open risk is unavailable or invalid.")
    elif total_open_risk >= equity * policy.max_total_open_risk_percent / 100.0:
        reasons.append("Maximum aggregate open stop-risk reached.")

    if (
        isinstance(current_positions, bool)
        or not isinstance(current_positions, int)
        or current_positions < 0
    ):
        reasons.append("Open position count is unavailable or invalid.")
    elif current_positions >= policy.max_simultaneous_positions:
        reasons.append("Maximum simultaneous positions reached.")
    if (
        isinstance(trades_today, bool)
        or not isinstance(trades_today, int)
        or trades_today < 0
    ):
        reasons.append("Daily trade count is unavailable or invalid.")
    elif trades_today >= policy.max_trades_per_day:
        reasons.append("Maximum trades per UTC day reached.")
    if (
        isinstance(consecutive_losses, bool)
        or not isinstance(consecutive_losses, int)
        or consecutive_losses < 0
    ):
        reasons.append("Consecutive-loss count is unavailable or invalid.")
    elif consecutive_losses >= policy.max_consecutive_losses:
        reasons.append("Maximum consecutive-loss limit reached.")
    if (
        isinstance(spread_pips, bool)
        or not isinstance(spread_pips, (int, float))
        or not math.isfinite(spread_pips)
        or spread_pips < 0
        or spread_pips > policy.max_spread_pips
    ):
        reasons.append(
            f"Spread exceeds the {policy.max_spread_pips:.2f}-pip limit."
        )
    if (
        isinstance(reward_risk, bool)
        or not isinstance(reward_risk, (int, float))
        or not math.isfinite(reward_risk)
        or reward_risk < 0
        or reward_risk < policy.minimum_reward_risk
    ):
        reasons.append(
            f"Reward-to-risk is below the {policy.minimum_reward_risk:.2f}:1 minimum."
        )

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        reasons.append("Trading-session check requires a timezone-aware time.")
    elif policy.session_start_utc is not None:
        current_utc = current_time.astimezone(timezone.utc).strftime("%H:%M")
        start = policy.session_start_utc
        end = policy.session_end_utc
        inside = (
            start <= current_utc < end
            if start < end
            else current_utc >= start or current_utc < end
        )
        if not inside:
            reasons.append("Current UTC time is outside the configured session.")

    if not isinstance(positions_by_symbol, dict):
        reasons.append("Per-symbol position counts are unavailable or invalid.")
        positions_by_symbol = {}
    for symbol, count in positions_by_symbol.items():
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
        ):
            reasons.append(f"Position count is invalid for {symbol}.")
        elif count > policy.max_positions_per_symbol:
            reasons.append(
                f"Maximum positions per symbol reached for {symbol}."
            )
    return reasons
