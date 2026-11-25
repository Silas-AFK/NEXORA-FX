import math

import MetaTrader5 as mt5

from execution.mt5_connection import MT5ConnectionError, mt5_connection
from risk.risk_manager import calculate_position_size_from_spec


DEFAULT_RISK_PERCENT = 0.25


def build_advisory_trade_plan(
    symbol,
    direction,
    entry,
    stop_loss,
    take_profit,
    equity,
    broker_spec,
    risk_percent=DEFAULT_RISK_PERCENT,
    margin_estimate=None,
    free_margin=None,
):
    """Build a pure advisory plan; this function has no broker side effects."""

    direction = str(direction).strip().upper()
    if direction in {"BULLISH", "BUY"}:
        direction = "BUY"
    elif direction in {"BEARISH", "SELL"}:
        direction = "SELL"
    else:
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Direction must be BUY/BULLISH or SELL/BEARISH.",
            "execution_permitted": False,
        }

    numeric = {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "equity": equity,
        "risk_percent": risk_percent,
    }
    for name, value in numeric.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            return {
                "valid": False,
                "symbol": symbol,
                "reason": f"{name.replace('_', ' ').capitalize()} must be finite.",
                "execution_permitted": False,
            }

    if not symbol or entry <= 0 or stop_loss <= 0 or take_profit <= 0:
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Symbol and trade prices must be positive.",
            "execution_permitted": False,
        }
    if equity <= 0 or risk_percent <= 0:
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Equity and risk percentage must be positive.",
            "execution_permitted": False,
        }
    if risk_percent > DEFAULT_RISK_PERCENT:
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Advisory risk cannot exceed 0.25% per trade.",
            "execution_permitted": False,
        }
    if direction == "BUY" and not (stop_loss < entry < take_profit):
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "BUY plan requires stop < entry < target.",
            "execution_permitted": False,
        }
    if direction == "SELL" and not (take_profit < entry < stop_loss):
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "SELL plan requires target < entry < stop.",
            "execution_permitted": False,
        }

    stop_distance = abs(entry - stop_loss)
    target_distance = abs(take_profit - entry)
    risk_reward = target_distance / stop_distance
    position = calculate_position_size_from_spec(
        symbol=symbol,
        entry_price=entry,
        stop_loss=stop_loss,
        risk_percent=risk_percent,
        equity=equity,
        broker_spec=broker_spec,
    )
    if not position.get("valid"):
        return {
            "valid": False,
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "stop_distance": stop_distance,
            "risk_reward": risk_reward,
            "risk_percent": risk_percent,
            "equity": equity,
            "broker_spec": broker_spec,
            "position": position,
            "reason": position.get("reason", "Position sizing failed."),
            "execution_permitted": False,
        }

    margin_reliable = (
        isinstance(margin_estimate, (int, float))
        and not isinstance(margin_estimate, bool)
        and math.isfinite(margin_estimate)
        and margin_estimate >= 0
    )
    margin_warning = None
    if margin_reliable and free_margin is not None:
        if (
            not isinstance(free_margin, (int, float))
            or isinstance(free_margin, bool)
            or not math.isfinite(free_margin)
            or free_margin < 0
        ):
            margin_warning = "Free margin is unavailable or invalid."
        elif margin_estimate > free_margin:
            return {
                "valid": False,
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "risk_reward": risk_reward,
                "position": position,
                "margin_estimate": margin_estimate,
                "free_margin": free_margin,
                "reason": "Estimated margin exceeds available free margin.",
                "execution_permitted": False,
            }
    elif not margin_reliable:
        margin_warning = "Margin estimate is unavailable from the broker."

    return {
        "valid": True,
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "stop_distance": stop_distance,
        "risk_reward": risk_reward,
        "estimated_monetary_risk": position["estimated_risk"],
        "risk_budget": position["risk_budget"],
        "estimated_volume": position["volume"],
        "tick_size": position["tick_size"],
        "tick_value": position["tick_value"],
        "volume_min": position["volume_min"],
        "volume_max": position["volume_max"],
        "volume_step": position["volume_step"],
        "margin_estimate": margin_estimate if margin_reliable else None,
        "free_margin": free_margin,
        "margin_warning": margin_warning,
        "risk_percent": risk_percent,
        "equity": equity,
        "advisory_only": True,
        "execution_permitted": False,
        "reason": "Advisory plan calculated; no order was sent.",
    }


def get_advisory_trade_plan(
    symbol,
    direction,
    entry,
    stop_loss,
    take_profit,
    equity=None,
    risk_percent=DEFAULT_RISK_PERCENT,
):
    """Read broker specs and calculate an advisory-only trade plan."""

    if not isinstance(symbol, str) or not symbol.strip():
        return {
            "valid": False,
            "reason": "A valid broker symbol is required.",
            "execution_permitted": False,
        }
    for name, value in (
        ("entry", entry),
        ("stop loss", stop_loss),
        ("take profit", take_profit),
        ("risk percentage", risk_percent),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            return {
                "valid": False,
                "symbol": symbol,
                "reason": f"{name.capitalize()} must be a finite positive number.",
                "execution_permitted": False,
            }
    if equity is not None and (
        isinstance(equity, bool)
        or not isinstance(equity, (int, float))
        or not math.isfinite(equity)
        or equity <= 0
    ):
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Equity must be a finite positive number.",
            "execution_permitted": False,
        }

    if risk_percent > DEFAULT_RISK_PERCENT:
        return {
        "valid": False,
        "symbol": symbol,
        "reason": "Advisory risk cannot exceed 0.25% per trade.",
        "execution_permitted": False,
        }

    try:
        with mt5_connection():
            info = mt5.symbol_info(symbol)
            if info is None:
                return {
                    "valid": False,
                    "symbol": symbol,
                    "reason": f"Unknown broker symbol: {symbol}",
                    "execution_permitted": False,
                }

            tick_value = getattr(info, "trade_tick_value_loss", 0) or 0
            if not math.isfinite(float(tick_value)) or tick_value <= 0:
                tick_value = getattr(info, "trade_tick_value", 0)
            broker_spec = {
                "tick_size": getattr(info, "trade_tick_size", None),
                "tick_value": tick_value,
                "volume_min": getattr(info, "volume_min", None),
                "volume_max": getattr(info, "volume_max", None),
                "volume_step": getattr(info, "volume_step", None),
            }

            account = mt5.account_info()
            account_equity = equity
            free_margin = None
            if account is not None:
                if account_equity is None:
                    account_equity = account.equity
                raw_free_margin = getattr(account, "margin_free", None)
                if raw_free_margin is not None:
                    free_margin = float(raw_free_margin)
            if account_equity is None:
                return {
                    "valid": False,
                    "symbol": symbol,
                    "reason": "Account equity is unavailable; enter equity manually.",
                    "execution_permitted": False,
                }

            order_type = (
                mt5.ORDER_TYPE_BUY
                if str(direction).upper() in {"BUY", "BULLISH"}
                else mt5.ORDER_TYPE_SELL
            )
            margin_estimate = None
            try:
                preliminary_position = calculate_position_size_from_spec(
                    symbol=symbol,
                    entry_price=entry,
                    stop_loss=stop_loss,
                    risk_percent=risk_percent,
                    equity=account_equity,
                    broker_spec=broker_spec,
                )
                if preliminary_position.get("valid"):
                    calculated_margin = mt5.order_calc_margin(
                        order_type,
                        symbol,
                        preliminary_position["volume"],
                        float(entry),
                    )
                    if calculated_margin is not None:
                        margin_estimate = float(calculated_margin)
            except Exception:
                margin_estimate = None

            return build_advisory_trade_plan(
                symbol=symbol,
                direction=direction,
                entry=float(entry),
                stop_loss=float(stop_loss),
                take_profit=float(take_profit),
                equity=float(account_equity),
                broker_spec=broker_spec,
                risk_percent=risk_percent,
                margin_estimate=margin_estimate,
                free_margin=free_margin,
            )
    except MT5ConnectionError as error:
        return {
            "valid": False,
            "symbol": symbol,
            "reason": str(error),
            "execution_permitted": False,
        }
