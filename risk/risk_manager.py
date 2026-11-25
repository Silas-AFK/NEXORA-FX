from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math

import MetaTrader5 as mt5

from execution.mt5_connection import (
    MT5ConnectionError,
    mt5_connection,
)


def _decimal(value, name):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number.")
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite number.")
    return result


def calculate_risk_amount(equity, risk_percent):
    """Calculate the maximum permitted monetary risk."""

    try:
        equity_value = _decimal(equity, "Equity")
        risk_value = _decimal(risk_percent, "Risk percentage")
    except ValueError:
        return 0.0
    if equity_value <= 0 or risk_value <= 0:
        return 0.0
    return float(equity_value * risk_value / Decimal("100"))


def calculate_position_size_from_spec(
    symbol,
    entry_price,
    stop_loss,
    risk_percent,
    equity,
    broker_spec,
):
    """Pure position-size calculation using broker symbol specifications."""

    try:
        entry = _decimal(entry_price, "Entry price")
        stop = _decimal(stop_loss, "Stop loss")
        risk_rate = _decimal(risk_percent, "Risk percentage")
        account_equity = _decimal(equity, "Equity")
        tick_size = _decimal(broker_spec["tick_size"], "Tick size")
        tick_value = _decimal(broker_spec["tick_value"], "Tick value")
        volume_min = _decimal(broker_spec["volume_min"], "Minimum volume")
        volume_max = _decimal(broker_spec["volume_max"], "Maximum volume")
        volume_step = _decimal(broker_spec["volume_step"], "Volume step")
    except (KeyError, TypeError, ValueError) as error:
        return {"valid": False, "reason": str(error)}

    if (
        entry <= 0
        or stop <= 0
        or risk_rate <= 0
        or account_equity <= 0
        or tick_size <= 0
        or tick_value <= 0
        or volume_min <= 0
        or volume_max < volume_min
        or volume_step <= 0
    ):
        return {
            "valid": False,
            "reason": "Prices, broker specifications, equity and risk must be valid.",
        }

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return {"valid": False, "reason": "Stop-loss distance cannot be zero."}

    risk_budget = account_equity * risk_rate / Decimal("100")
    loss_per_lot = stop_distance / tick_size * tick_value
    if loss_per_lot <= 0:
        return {"valid": False, "reason": "Could not calculate loss per lot."}

    raw_volume = risk_budget / loss_per_lot
    if raw_volume < volume_min:
        minimum_risk = volume_min * loss_per_lot
        return {
            "valid": False,
            "reason": "Minimum broker lot exceeds the selected risk budget.",
            "risk_budget": float(risk_budget),
            "minimum_lot": float(volume_min),
            "minimum_lot_risk": float(minimum_risk),
        }

    maximum_steps = (
        (volume_max - volume_min) / volume_step
    ).to_integral_value(rounding=ROUND_DOWN)
    requested_steps = (
        (raw_volume - volume_min) / volume_step
    ).to_integral_value(rounding=ROUND_DOWN)
    volume = volume_min + min(requested_steps, maximum_steps) * volume_step
    estimated_risk = volume * loss_per_lot

    if estimated_risk > risk_budget:
        return {
            "valid": False,
            "reason": "Rounded position volume would exceed the risk budget.",
        }

    return {
        "valid": True,
        "symbol": symbol,
        "volume": float(volume),
        "risk_percent": float(risk_rate),
        "risk_budget": float(risk_budget),
        "estimated_risk": float(estimated_risk),
        "stop_distance": float(stop_distance),
        "loss_per_lot": float(loss_per_lot),
        "tick_size": float(tick_size),
        "tick_value": float(tick_value),
        "volume_min": float(volume_min),
        "volume_max": float(volume_max),
        "volume_step": float(volume_step),
        "reason": "Position size calculated within the risk budget.",
    }


def calculate_position_size(
    symbol,
    entry_price,
    stop_loss,
    risk_percent,
    equity,
):
    """Calculate position size without changing connection ownership."""

    try:
        with mt5_connection():
            info = mt5.symbol_info(symbol)
            if info is None:
                return {
                    "valid": False,
                    "reason": f"Unknown broker symbol: {symbol}",
                }

            tick_value = getattr(info, "trade_tick_value_loss", 0) or 0
            if not math.isfinite(float(tick_value)) or tick_value <= 0:
                tick_value = getattr(info, "trade_tick_value", 0)

            spec = {
                "tick_size": getattr(info, "trade_tick_size", None),
                "tick_value": tick_value,
                "volume_min": getattr(info, "volume_min", None),
                "volume_max": getattr(info, "volume_max", None),
                "volume_step": getattr(info, "volume_step", None),
            }
            return calculate_position_size_from_spec(
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                risk_percent=risk_percent,
                equity=equity,
                broker_spec=spec,
            )
    except MT5ConnectionError as error:
        return {"valid": False, "reason": str(error)}


def check_daily_loss(
    starting_equity,
    current_equity,
    max_daily_loss_percent=1.0,
):
    """Compatibility wrapper for the daily loss policy."""

    return check_period_loss(
        starting_equity,
        current_equity,
        max_daily_loss_percent,
        period="daily",
    )


def check_period_loss(
    starting_equity,
    current_equity,
    maximum_loss_percent,
    *,
    period="period",
):
    """Fail closed when period equity inputs or the configured limit are invalid."""

    try:
        starting = _decimal(starting_equity, "Starting equity")
        current = _decimal(current_equity, "Current equity")
        maximum = _decimal(maximum_loss_percent, f"{period.capitalize()} loss limit")
    except ValueError as error:
        return {"allowed": False, "reason": str(error)}

    if starting <= 0 or current < 0 or maximum <= 0:
        return {"allowed": False, "reason": "Invalid equity values or loss limit."}

    loss_amount = max(starting - current, Decimal("0"))
    loss_percent = loss_amount / starting * Decimal("100")
    allowed = loss_percent < maximum
    return {
        "allowed": allowed,
        "loss_amount": round(float(loss_amount), 2),
        "loss_percent": round(float(loss_percent), 2),
        "maximum_loss_percent": float(maximum),
        "reason": (
            f"{period.capitalize()} loss limit not reached."
            if allowed
            else f"Maximum {period} loss reached."
        ),
    }
