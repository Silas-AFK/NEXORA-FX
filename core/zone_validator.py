
import MetaTrader5 as mt5
import pandas as pd
import math

from core.data_validation import MarketDataValidationError, validate_ohlc_data
from core.market_data import MarketDataError, get_historical_data, get_live_price


TIMEFRAMES = {
    "H4": mt5.TIMEFRAME_H4,
    "H1": mt5.TIMEFRAME_H1,
    "M15": mt5.TIMEFRAME_M15,
}

ACTIVE_STATUSES = {
    "ORDER_BLOCK": {"UNMITIGATED"},
    "FVG": {"OPEN"},
}

TOUCHED_STATUSES = {
    "ORDER_BLOCK": {"MITIGATED"},
    "FVG": {"PARTIALLY_FILLED"},
}

FULLY_FILLED_STATUSES = {
    "FVG": {"FULLY_FILLED"},
}

INVALID_STATUSES = {"INVALIDATED"}


def calculate_atr(data, period=14):
    """Calculate ATR using completed candles."""

    if data is None or len(data) < period + 1:
        return None

    required = ["high", "low", "close"]

    if any(column not in data.columns for column in required):
        return None

    previous_close = data["close"].shift(1)

    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    value = true_range.rolling(period).mean().iloc[-1]

    if pd.isna(value) or value <= 0:
        return None

    return float(value)


def _get_zone_touch_information(zone, zone_type):
    """Read recorded touch or mitigation information."""

    if zone_type == "ORDER_BLOCK":
        touch_index = zone.get("mitigation_index")
        touch_time = zone.get("mitigation_time")

    elif zone_type == "FVG":
        touch_index = zone.get("first_touch_index")
        touch_time = zone.get("first_touch_time")

    else:
        touch_index = None
        touch_time = None

    return {
        "first_touch_index": touch_index,
        "first_touch_time": touch_time,
        "has_recorded_touch": (
            touch_index is not None or touch_time is not None
        ),
    }


def _classify_zone_state(
    direction,
    current_price,
    zone_high,
    zone_low,
    active,
    fresh,
    nearby,
    direction_matches,
    touched,
    fully_filled,
    invalidated,
    bias_available,
):
    """Classify a zone without treating it as an entry signal."""

    if invalidated:
        return "INVALIDATED"

    if fully_filled:
        return "FULLY_FILLED"

    if not fresh:
        return "EXPIRED"

    if not bias_available:
        return "NO_DIRECTIONAL_BIAS"

    if not direction_matches:
        return "DIRECTION_MISMATCH"

    if not nearby:
        return "TOO_FAR"

    inside_zone = zone_low <= current_price <= zone_high

    if inside_zone:
        if active:
            return "IN_RETEST"

        if touched:
            return "PREVIOUSLY_TOUCHED_IN_ZONE"

        return "ZONE_STATUS_UNCONFIRMED"

    if touched:
        return "PREVIOUSLY_TOUCHED_OUTSIDE"

    if not active:
        return "ZONE_NOT_ACTIVE"

    if direction == "BULLISH" and current_price > zone_high:
        return "APPROACHING"

    if direction == "BEARISH" and current_price < zone_low:
        return "APPROACHING"

    return "WRONG_PRICE_POSITION"


def _rejection_reasons(
    bias_available,
    direction_matches,
    invalidated,
    fully_filled,
    fresh,
    nearby,
    active,
    touched,
    correct_side,
    inside_zone,
    status,
):
    """Build explicit reasons why a zone is not eligible."""

    reasons = []

    if invalidated:
        reasons.append("ZONE_INVALIDATED")

    if fully_filled:
        reasons.append("FVG_FULLY_FILLED")

    if not bias_available:
        reasons.append("NO_DIRECTIONAL_BIAS")
    elif not direction_matches:
        reasons.append("DIRECTION_MISMATCH")

    if not fresh:
        reasons.append("ZONE_TOO_OLD")

    if not nearby:
        reasons.append("ZONE_TOO_FAR")

    if touched and not active:
        reasons.append("ZONE_PREVIOUSLY_TOUCHED")

    if not active and not touched and not invalidated and not fully_filled:
        reasons.append("ZONE_NOT_ACTIVE")

    if status == "UNKNOWN":
        reasons.append("UNKNOWN_ZONE_STATUS")

    if not correct_side and not inside_zone:
        reasons.append("WRONG_PRICE_POSITION")

    return reasons


def validate_smc_zones(
    symbol,
    mtf_result,
    max_distance_atr=1.0,
    max_age_bars=100,
    historical_data=None,
    current_price=None,
):
    """
    NEXORA FX SMC zone validation engine.

    Research-only classification:
    - Validates order blocks and fair value gaps.
    - Checks zone age and ATR distance.
    - Checks H4 directional agreement.
    - Classifies approaching and retest zones.
    - Separates touched, filled and invalidated zones.
    - Records malformed zones instead of silently skipping them.

    No orders are sent or modified.
    Supplied historical data and current_price keep this function offline-capable.
    """

    # 1. Validate inputs

    if not isinstance(mtf_result, dict) or not mtf_result.get("valid"):
        return {
            "valid": False,
            "reason": "Valid multi-timeframe analysis is required.",
            "execution_permitted": False,
        }

    if (
        not isinstance(max_distance_atr, (int, float))
        or isinstance(max_distance_atr, bool)
        or not math.isfinite(max_distance_atr)
        or max_distance_atr <= 0
        or not isinstance(max_age_bars, int)
        or isinstance(max_age_bars, bool)
        or max_age_bars < 1
    ):
        return {
            "valid": False,
            "reason": "Invalid zone validation settings.",
            "execution_permitted": False,
        }

    if not isinstance(symbol, str) or not symbol.strip():
        return {
            "valid": False,
            "reason": "Invalid trading symbol.",
            "execution_permitted": False,
        }

    if mtf_result.get("symbol") != symbol:
        return {
            "valid": False,
            "reason": "Symbol does not match the multi-timeframe analysis.",
            "execution_permitted": False,
        }

    # 2. Get current market price

    if current_price is None:
        try:
            quote = get_live_price(symbol)
            current_price = (quote["bid"] + quote["ask"]) / 2.0
        except MarketDataError as error:
            return {
                "valid": False,
                "reason": str(error),
                "execution_permitted": False,
            }
    else:
        try:
            current_price = float(current_price)
        except (TypeError, ValueError, OverflowError):
            return {
                "valid": False,
                "reason": "Current price must be a finite positive number.",
                "execution_permitted": False,
            }
        if not math.isfinite(current_price) or current_price <= 0:
            return {
                "valid": False,
                "reason": "Current price must be a finite positive number.",
                "execution_permitted": False,
            }

    market_bias = str(
        mtf_result.get("market_bias", "NEUTRAL")
    ).upper()

    bias_available = market_bias in {"BULLISH", "BEARISH"}

    timeframes = mtf_result.get("timeframes", {})

    if not isinstance(timeframes, dict):
        return {
            "valid": False,
            "reason": "Invalid timeframe analysis structure.",
            "execution_permitted": False,
        }
    if historical_data is not None and not isinstance(historical_data, dict):
        return {
            "valid": False,
            "reason": "Historical candle data must be provided per timeframe.",
            "execution_permitted": False,
        }

    validated_zones = []
    skipped_zones = []
    timeframe_errors = []

    detected_counts = {
        "ORDER_BLOCK": 0,
        "FVG": 0,
    }

    examined_counts = {
        "ORDER_BLOCK": 0,
        "FVG": 0,
    }

    # 3. Process each timeframe

    for timeframe_name, timeframe_value in TIMEFRAMES.items():

        timeframe_result = timeframes.get(timeframe_name, {})

        if (
            not isinstance(timeframe_result, dict)
            or not timeframe_result.get("valid")
        ):
            timeframe_errors.append(
                f"{timeframe_name} analysis is unavailable."
            )
            continue

        try:
            if historical_data is None:
                data = get_historical_data(
                    symbol,
                    timeframe_value,
                    200,
                )
            else:
                data = historical_data.get(timeframe_name)
                if data is None:
                    raise MarketDataError(
                        f"{timeframe_name} candle data is missing."
                    )
            data = validate_ohlc_data(data, require_time=True)
        except (MarketDataError, MarketDataValidationError) as error:
            timeframe_errors.append(
                f"{timeframe_name} price history unavailable: {error}"
            )
            continue

        if len(data) < 16:
            timeframe_errors.append(
                f"{timeframe_name} price history has fewer than 16 completed candles."
            )
            continue

        atr = calculate_atr(data)

        if atr is None:
            timeframe_errors.append(
                f"{timeframe_name} ATR could not be calculated."
            )
            continue

        analysis = timeframe_result.get("analysis", {})

        if not isinstance(analysis, dict):
            timeframe_errors.append(
                f"{timeframe_name} analysis data is invalid."
            )
            continue

        order_block_data = analysis.get("order_blocks", {})
        fvg_data = analysis.get("fair_value_gaps", {})

        if not isinstance(order_block_data, dict):
            order_block_data = {}

        if not isinstance(fvg_data, dict):
            fvg_data = {}

        zone_sources = [
            (
                "ORDER_BLOCK",
                order_block_data.get("order_blocks", []),
            ),
            (
                "FVG",
                fvg_data.get("fair_value_gaps", []),
            ),
        ]

        # 4. Process individual detected zones

        for zone_type, zones in zone_sources:

            if not isinstance(zones, list):
                timeframe_errors.append(
                    f"{timeframe_name} {zone_type} data is not a list."
                )
                continue

            detected_counts[zone_type] += len(zones)

            for zone_number, zone in enumerate(zones):

                zone_reference = {
                    "timeframe": timeframe_name,
                    "zone_type": zone_type,
                    "zone_number": zone_number,
                }

                if not isinstance(zone, dict):
                    skipped_zones.append({
                        **zone_reference,
                        "skip_reason": "ZONE_DATA_NOT_A_DICTIONARY",
                    })
                    continue

                # Required fields

                try:
                    zone_high = float(zone["zone_high"])
                    zone_low = float(zone["zone_low"])
                    raw_age = zone["age_bars"]

                    if isinstance(raw_age, bool):
                        raise ValueError("Boolean age is invalid.")

                    age = int(raw_age)

                except (KeyError, TypeError, ValueError, OverflowError):
                    skipped_zones.append({
                        **zone_reference,
                        "skip_reason": "MISSING_OR_INVALID_REQUIRED_FIELDS",
                    })
                    continue

                if (
                    not math.isfinite(zone_high)
                    or not math.isfinite(zone_low)
                    or zone_low <= 0
                    or zone_high < zone_low
                    or age < 0
                ):
                    skipped_zones.append({
                        **zone_reference,
                        "skip_reason": "INVALID_ZONE_PRICE_OR_AGE",
                    })
                    continue

                examined_counts[zone_type] += 1

                direction = str(
                    zone.get("direction", "UNKNOWN")
                ).upper()

                status = str(
                    zone.get("status", "UNKNOWN")
                ).upper()

                # 5. Calculate distance and price position

                if current_price < zone_low:
                    distance = zone_low - current_price
                    price_position = "BELOW_ZONE"

                elif current_price > zone_high:
                    distance = current_price - zone_high
                    price_position = "ABOVE_ZONE"

                else:
                    distance = 0.0
                    price_position = "INSIDE_ZONE"

                distance_atr = distance / atr

                inside_zone = (
                    zone_low <= current_price <= zone_high
                )

                # 6. Determine zone status

                active = (
                    status in ACTIVE_STATUSES[zone_type]
                )

                touched = (
                    status in TOUCHED_STATUSES[zone_type]
                )

                fully_filled = (
                    status in FULLY_FILLED_STATUSES.get(
                        zone_type, set()
                    )
                )

                invalidated = status in INVALID_STATUSES

                fresh = age <= max_age_bars
                nearby = distance_atr <= max_distance_atr

                direction_matches = (
                    bias_available
                    and direction == market_bias
                )

                touch_information = _get_zone_touch_information(
                    zone,
                    zone_type,
                )

                if direction == "BULLISH":
                    correct_side = current_price > zone_high

                elif direction == "BEARISH":
                    correct_side = current_price < zone_low

                else:
                    correct_side = False

                # 7. Classify the zone

                zone_state = _classify_zone_state(
                    direction=direction,
                    current_price=current_price,
                    zone_high=zone_high,
                    zone_low=zone_low,
                    active=active,
                    fresh=fresh,
                    nearby=nearby,
                    direction_matches=direction_matches,
                    touched=touched,
                    fully_filled=fully_filled,
                    invalidated=invalidated,
                    bias_available=bias_available,
                )

                approaching = zone_state == "APPROACHING"

                # An active zone inside its boundaries is a
                # preliminary retest observation, not confirmation.
                in_retest = (
                    zone_state == "IN_RETEST"
                    and active
                )

                # 8. Build rejection reasons

                rejection_reasons = _rejection_reasons(
                    bias_available=bias_available,
                    direction_matches=direction_matches,
                    invalidated=invalidated,
                    fully_filled=fully_filled,
                    fresh=fresh,
                    nearby=nearby,
                    active=active,
                    touched=touched,
                    correct_side=correct_side,
                    inside_zone=inside_zone,
                    status=status,
                )

                eligible = (
                    not rejection_reasons
                    and (approaching or in_retest)
                )

                # 9. Record every examined zone

                validated_zones.append({
                    **zone_reference,

                    "direction": direction,
                    "status": status,
                    "zone_state": zone_state,

                    "zone_high": zone_high,
                    "zone_low": zone_low,

                    "age_bars": age,

                    "distance_from_price": round(
                        distance, 8
                    ),

                    "distance_atr": round(
                        distance_atr, 3
                    ),

                    "price_position": price_position,

                    "active": active,
                    "touched": touched,
                    "fully_filled": fully_filled,
                    "invalidated": invalidated,
                    "fresh": fresh,
                    "nearby": nearby,
                    "inside_zone": inside_zone,

                    "direction_matches_bias": direction_matches,
                    "correct_side": correct_side,

                    "approaching": approaching,
                    "in_retest": in_retest,
                    "eligible": eligible,

                    "first_touch_index": (
                        touch_information["first_touch_index"]
                    ),

                    "first_touch_time": (
                        touch_information["first_touch_time"]
                    ),

                    "has_recorded_touch": (
                        touch_information["has_recorded_touch"]
                    ),

                    "formation_index": zone.get(
                        "formation_index"
                    ),

                    "formation_time": zone.get(
                        "formation_time"
                    ),

                    "displacement_index": zone.get(
                        "displacement_index"
                    ),

                    "displacement_strength": zone.get(
                        "displacement_strength"
                    ),

                    "full_fill_index": zone.get(
                        "full_fill_index"
                    ),

                    "invalidation_index": zone.get(
                        "invalidation_index"
                    ),

                    "rejection_reasons": rejection_reasons,
                })

    # 10. Build result lists

    eligible_zones = [
        zone for zone in validated_zones
        if zone["eligible"]
    ]

    rejected_zones = [
        zone for zone in validated_zones
        if not zone["eligible"]
    ]

    approaching_zones = [
        zone for zone in eligible_zones
        if zone["approaching"]
    ]

    retest_zones = [
        zone for zone in eligible_zones
        if zone["in_retest"]
    ]

    # Sort nearest eligible zones first.

    eligible_zones.sort(
        key=lambda zone: (
            zone["distance_atr"],
            zone["age_bars"],
        )
    )

    approaching_zones.sort(
        key=lambda zone: (
            zone["distance_atr"],
            zone["age_bars"],
        )
    )

    retest_zones.sort(
        key=lambda zone: (
            zone["distance_atr"],
            zone["age_bars"],
        )
    )

    # 11. Build state and rejection summaries

    state_counts = {}
    rejection_counts = {}

    timeframe_counts = {
        name: {
            "ORDER_BLOCK": 0,
            "FVG": 0,
            "eligible": 0,
            "rejected": 0,
        }
        for name in TIMEFRAMES
    }

    for zone in validated_zones:

        state = zone["zone_state"]

        state_counts[state] = (
            state_counts.get(state, 0) + 1
        )

        timeframe_name = zone["timeframe"]
        zone_type = zone["zone_type"]

        timeframe_counts[timeframe_name][zone_type] += 1

        if zone["eligible"]:
            timeframe_counts[timeframe_name]["eligible"] += 1
        else:
            timeframe_counts[timeframe_name]["rejected"] += 1

        for reason in zone["rejection_reasons"]:
            rejection_counts[reason] = (
                rejection_counts.get(reason, 0) + 1
            )

    # 12. Final output

    return {
        "valid": len(timeframe_errors) == 0,

        "symbol": symbol,
        "market_bias": market_bias,
        "current_price": current_price,

        "max_distance_atr": max_distance_atr,
        "max_age_bars": max_age_bars,

        "zones_detected": sum(detected_counts.values()),
        "zones_examined": len(validated_zones),
        "zones_skipped": len(skipped_zones),

        "detected_counts": detected_counts,
        "examined_counts": examined_counts,

        "eligible_count": len(eligible_zones),
        "rejected_count": len(rejected_zones),
        "approaching_count": len(approaching_zones),
        "retest_count": len(retest_zones),

        "eligible_zones": eligible_zones,
        "rejected_zones": rejected_zones,
        "approaching_zones": approaching_zones,
        "retest_zones": retest_zones,

        "all_zones": validated_zones,
        "skipped_zones": skipped_zones,

        "state_counts": state_counts,
        "rejection_counts": rejection_counts,
        "timeframe_counts": timeframe_counts,

        "timeframe_errors": timeframe_errors,

        "execution_permitted": False,

        "reason": (
            "Zone validation completed."
            if not timeframe_errors
            else "Zone validation completed with timeframe errors."
        ),
    }
