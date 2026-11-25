from risk.risk_manager import (
    calculate_position_size,
    check_daily_loss,
)

TRADING_ENABLED = False


def evaluate_trade(
    symbol,
    direction,
    entry_price,
    stop_loss,
    risk_percent,
    equity,
    starting_daily_equity,
    current_equity,
):
    """Evaluate a proposed trade without executing it."""

    reasons = []

    # 1. Validate inputs

    if direction not in ("BUY", "SELL"):
        reasons.append("Invalid trade direction.")

    if entry_price <= 0 or stop_loss <= 0:
        reasons.append("Invalid entry or stop-loss price.")

    elif direction == "BUY" and stop_loss >= entry_price:
        reasons.append("BUY stop loss must be below entry price.")

    elif direction == "SELL" and stop_loss <= entry_price:
        reasons.append("SELL stop loss must be above entry price.")

    if equity <= 0:
        reasons.append("Account equity must be positive.")

    if risk_percent <= 0:
        reasons.append("Risk percentage must be positive.")

    if starting_daily_equity <= 0 or current_equity < 0:
        reasons.append("Invalid daily equity values.")

    if reasons:
        return {
            "approved": False,
            "symbol": symbol,
            "direction": direction,
            "reasons": reasons,
            "execution_permitted": False,
        }

    # 2. Check daily loss

    daily_check = check_daily_loss(
        starting_daily_equity,
        current_equity,
        max_daily_loss_percent=1.0,
    )

    if not daily_check["allowed"]:
        reasons.append(daily_check["reason"])

    # 3. Check position size

    position = calculate_position_size(
        symbol=symbol,
        entry_price=entry_price,
        stop_loss=stop_loss,
        risk_percent=risk_percent,
        equity=equity,
    )

    if not position["valid"]:
        reasons.append(position["reason"])

    # 4. Enforce execution lock

    if not TRADING_ENABLED:
        reasons.append("Automated trading is disabled.")

    # 5. Final decision

    return {
        "approved": False,
        "symbol": symbol,
        "direction": direction,
        "position_check": position,
        "daily_loss_check": daily_check,
        "reasons": reasons,
        "execution_permitted": False,
    }
