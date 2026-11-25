
import math
import pandas as pd

from core.indicators import calculate_indicators
from core.market_regime import detect_market_regime
from core.liquidity import detect_liquidity
from core.data_validation import (
    MarketDataValidationError,
    validate_ohlc_data,
)


# ============================================================
# NEXORA FX — SIGNAL INTELLIGENCE ENGINE
# Version 3.0
# Rule-based quality scoring for all outcomes
# Research mode — MT5 execution disabled
# ============================================================

DEFAULT_CONFIG = {
    "liquidity_freshness_bars": 8,
    "maximum_zone_age_bars": 100,
    "maximum_event_age_h1": 12,
    "maximum_event_age_m15": 8,
    "minimum_risk_reward": 1.5,
    "target_risk_reward": 2.0,
    "stop_atr_buffer": 0.20,
    "maximum_spread_atr": 0.15,
    "adx_trend_threshold": 25,
    "minimum_candles_m15": 50,
    "minimum_candles_h1": 250,
}


# ============================================================
# BASIC HELPERS
# ============================================================

def _number(value):
    """Return a finite float or None."""

    try:
        result = float(value)

        if math.isfinite(result):
            return result

    except (TypeError, ValueError):
        pass

    return None


def _direction(value):
    """Normalize market direction without losing ranging information."""

    text = str(value or "").strip().upper()

    if "BULL" in text or "UPTREND" in text:
        return "BULLISH"

    if "BEAR" in text or "DOWNTREND" in text:
        return "BEARISH"

    if "RANGING" in text or "SIDEWAYS" in text:
        return "RANGING"

    if "NEUTRAL" in text:
        return "NEUTRAL"

    return "UNKNOWN"


def _timeframe(mtf_result, name):
    """Get a timeframe result safely."""

    timeframes = mtf_result.get("timeframes", {})

    if not isinstance(timeframes, dict):
        return {}

    result = timeframes.get(name, {})

    return result if isinstance(result, dict) else {}


def _event_is_fresh(timeframe_data, maximum_age):
    """Check whether a structure event is recent."""

    age = _number(timeframe_data.get("event_age_bars"))

    return (
        age is not None
        and 0 <= age <= maximum_age
    )


def _validate_config(settings):
    """Validate important numerical configuration settings."""

    integer_settings = [
        "liquidity_freshness_bars",
        "maximum_zone_age_bars",
        "maximum_event_age_h1",
        "maximum_event_age_m15",
        "minimum_candles_m15",
        "minimum_candles_h1",
    ]

    for key in integer_settings:
        value = settings.get(key)

        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            return False, f"Invalid configuration value: {key}"

    positive_settings = [
        "minimum_risk_reward",
        "target_risk_reward",
        "maximum_spread_atr",
        "adx_trend_threshold",
    ]

    for key in positive_settings:
        value = _number(settings.get(key))

        if value is None or value <= 0:
            return False, f"Invalid configuration value: {key}"

        settings[key] = value

    buffer = _number(settings.get("stop_atr_buffer"))

    if buffer is None or buffer < 0:
        return False, "Invalid configuration value: stop_atr_buffer"

    settings["stop_atr_buffer"] = buffer

    if settings["target_risk_reward"] < settings["minimum_risk_reward"]:
        return False, "Target risk/reward must meet the minimum."

    return True, None


# ============================================================
# ATR AND CANDLE VALIDATION
# ============================================================

def _atr(data, period=14):
    """Calculate ATR from completed OHLC candles."""

    if data is None or len(data) < period + 1:
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

    value = true_range.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean().iloc[-1]

    return _number(value)


def _validate_candles(data, minimum, name):
    """Validate completed candle data."""

    if not isinstance(data, pd.DataFrame) or data.empty:
        return False, f"{name} candle data is missing."

    if len(data) < minimum:
        return False, (
            f"{name} needs at least {minimum} completed candles."
        )

    try:
        validate_ohlc_data(data, require_time="time" in data.columns)
    except MarketDataValidationError as error:
        return False, f"{name} candle data is invalid: {error}"

    return True, None


# ============================================================
# LIQUIDITY ANALYSIS
# ============================================================

def _get_recent_sweep(data, direction, freshness_bars):
    """
    BUY requires a sell-side sweep.
    SELL requires a buy-side sweep.
    """

    result = detect_liquidity(
        data,
        freshness_bars=freshness_bars,
    )

    if not isinstance(result, dict) or not result.get("valid"):
        reason = (
            result.get("reason", "Liquidity analysis failed.")
            if isinstance(result, dict)
            else "Liquidity analysis returned an invalid result."
        )

        return None, reason

    required_type = (
        "SELL_SIDE_SWEEP"
        if direction == "BULLISH"
        else "BUY_SIDE_SWEEP"
    )

    events = result.get("recent_sweeps", [])

    if not isinstance(events, list):
        events = []

    matching = []

    for event in events:

        if not isinstance(event, dict):
            continue

        event_type = str(
            event.get("type", event.get("sweep_type", ""))
        ).upper()

        if (
            event_type == required_type
            and event.get("fresh") is True
        ):
            matching.append(event)

    if not matching:
        return None, (
            f"No fresh {required_type.replace('_', ' ').lower()} "
            "was detected."
        )

    matching.sort(
        key=lambda item: _number(item.get("candle_index")) or -1,
        reverse=True,
    )

    return matching[0], None


# ============================================================
# ORDER BLOCK / FAIR VALUE GAP REJECTION
# ============================================================

def _bullish_rejection(candle, zone_high, zone_low):
    """Detect bullish rejection of a price zone."""

    open_price = float(candle["open"])
    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])

    body = abs(close - open_price)
    lower_wick = min(open_price, close) - low
    midpoint = (zone_high + zone_low) / 2

    interacted = low <= zone_high and high >= zone_low

    rejected = (
        close > open_price
        and close > midpoint
        and lower_wick >= body * 0.5
    )

    return interacted and rejected


def _bearish_rejection(candle, zone_high, zone_low):
    """Detect bearish rejection of a price zone."""

    open_price = float(candle["open"])
    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])

    body = abs(close - open_price)
    upper_wick = high - max(open_price, close)
    midpoint = (zone_high + zone_low) / 2

    interacted = high >= zone_low and low <= zone_high

    rejected = (
        close < open_price
        and close < midpoint
        and upper_wick >= body * 0.5
    )

    return interacted and rejected


def _find_retested_zone(
    zone_result,
    m15_data,
    direction,
    maximum_age,
):
    """
    Find a directional active M15 OB or FVG that the latest
    completed candle has interacted with and rejected.
    """

    zones = zone_result.get("all_zones", [])

    if not isinstance(zones, list):
        return None, "Zone data is unavailable."

    latest = m15_data.iloc[-1]
    candidates = []

    for zone in zones:

        if not isinstance(zone, dict):
            continue

        if str(zone.get("timeframe", "")).upper() != "M15":
            continue

        if _direction(zone.get("direction")) != direction:
            continue

        zone_type = str(zone.get("zone_type", "")).upper()
        status = str(zone.get("status", "")).upper()

        if zone_type in {"FVG", "FAIR_VALUE_GAP"}:

            if status != "OPEN":
                continue

        elif zone_type in {"OB", "ORDER_BLOCK"}:

            if status != "UNMITIGATED":
                continue

        else:
            continue

        if zone.get("invalidated") is True:
            continue

        if zone.get("filled") is True:
            continue

        age = _number(zone.get("age_bars"))

        if age is None or age < 0 or age > maximum_age:
            continue

        zone_high = _number(zone.get("zone_high"))
        zone_low = _number(zone.get("zone_low"))

        if (
            zone_high is None
            or zone_low is None
            or zone_high <= zone_low
        ):
            continue

        if direction == "BULLISH":

            rejected = _bullish_rejection(
                latest,
                zone_high,
                zone_low,
            )

        else:

            rejected = _bearish_rejection(
                latest,
                zone_high,
                zone_low,
            )

        if rejected:
            candidates.append(zone)

    if not candidates:
        return None, (
            "No active M15 order block or fair value gap "
            "has a rejection on the latest completed candle."
        )

    candidates.sort(
        key=lambda item: _number(item.get("age_bars")) or 0
    )

    return candidates[0], None


# ============================================================
# LIVE PRICE
# ============================================================

def _get_live_quote(live_price, direction):
    """Read and validate a live bid/ask quote."""

    if not isinstance(live_price, dict):
        return None, None, False

    bid = _number(live_price.get("bid"))
    ask = _number(live_price.get("ask"))

    if bid is None or ask is None:
        return None, None, False

    if bid <= 0 or ask <= 0 or ask < bid:
        return None, None, False

    entry = ask if direction == "BULLISH" else bid

    return entry, ask - bid, True


# ============================================================
# RESULT BUILDER
# ============================================================

def _quality_breakdown(
    direction,
    conditions,
    config,
    market_regime=None,
    liquidity_sweep=None,
    selected_zone=None,
    spread=None,
    atr=None,
    risk_reward=None,
):
    """Score observed setup criteria without treating the score as probability."""

    settings = dict(DEFAULT_CONFIG)

    if isinstance(config, dict):
        settings.update(config)

    matched_timeframes = sum(
        _direction(conditions.get(name)) == direction
        for name in ("h4", "h1", "m15")
    )
    trend_alignment = (
        25 * matched_timeframes / 3
        if direction in {"BULLISH", "BEARISH"}
        else 0
    )

    regime_name = ""
    if isinstance(market_regime, dict):
        regime_name = str(
            market_regime.get("regime", "")
        ).upper()

    if (
        regime_name == "BULLISH_TREND" and direction == "BULLISH"
    ) or (
        regime_name == "BEARISH_TREND" and direction == "BEARISH"
    ):
        regime_points = 15
    elif regime_name in {"SIDEWAYS", "RANGING"}:
        regime_points = 8
    elif regime_name == "MIXED_TREND":
        regime_points = 5
    elif regime_name in {"BULLISH_TREND", "BEARISH_TREND"}:
        regime_points = 0
    else:
        regime_points = 0

    liquidity_points = (
        20
        if isinstance(liquidity_sweep, dict) and liquidity_sweep
        else 0
    )

    zone_age = (
        _number(selected_zone.get("age_bars"))
        if isinstance(selected_zone, dict)
        else None
    )
    if zone_age is None or zone_age < 0:
        zone_points = 0
    else:
        maximum_zone_age = settings["maximum_zone_age_bars"]
        zone_points = max(0, 25 * (1 - zone_age / maximum_zone_age))

    spread_value = _number(spread)
    atr_value = _number(atr)
    maximum_spread_atr = _number(settings["maximum_spread_atr"])
    if (
        spread_value is not None
        and atr_value is not None
        and atr_value > 0
        and maximum_spread_atr is not None
        and maximum_spread_atr > 0
    ):
        spread_points = max(
            0,
            5 * (
                1
                - (spread_value / atr_value) / maximum_spread_atr
            ),
        )
    else:
        spread_points = 0

    rr_value = _number(risk_reward)
    target_risk_reward = _number(settings["target_risk_reward"])
    if (
        rr_value is not None
        and target_risk_reward is not None
        and target_risk_reward > 0
    ):
        rr_points = min(10, 10 * rr_value / target_risk_reward)
    else:
        rr_points = 0

    return {
        "trend_alignment": round(trend_alignment, 2),
        "liquidity_sweep": round(liquidity_points, 2),
        "zone_freshness": round(zone_points, 2),
        "market_regime": round(regime_points, 2),
        "spread_quality": round(spread_points, 2),
        "risk_reward": round(rr_points, 2),
    }


def _result(
    symbol,
    classification,
    direction,
    reason,
    conditions=None,
    warnings=None,
    **extra,
):
    """Build a consistent signal response."""

    quality_config = extra.pop("quality_config", None)
    quality_breakdown = extra.get("quality_breakdown")

    if not isinstance(quality_breakdown, dict):
        quality_breakdown = _quality_breakdown(
            direction=direction,
            conditions=conditions or {},
            config=quality_config,
            market_regime=extra.get("market_regime"),
            liquidity_sweep=extra.get("liquidity_sweep"),
            selected_zone=extra.get("selected_zone"),
            spread=extra.get("spread"),
            atr=extra.get("atr"),
            risk_reward=extra.get("risk_reward"),
        )
        extra["quality_breakdown"] = quality_breakdown

    quality_score = extra.get("quality_score")
    if _number(quality_score) is None:
        quality_score = round(sum(quality_breakdown.values()), 2)
        extra["quality_score"] = quality_score

    extra.setdefault(
        "quality_label",
        "HIGH" if quality_score >= 80
        else "MODERATE" if quality_score >= 60
        else "LOW",
    )

    return {
        "valid": True,
        "symbol": symbol,
        "classification": classification,
        "direction": direction,
        "reason": reason,
        "conditions": conditions or {},
        "warnings": warnings or [],
        "quality_basis": "Rule-based setup completeness; not a probability.",
        "execution_permitted": False,
        **extra,
    }


# ============================================================
# MAIN SIGNAL ENGINE
# ============================================================

def evaluate_signal(
    symbol,
    mtf_result,
    zone_result,
    m15_data,
    h1_data,
    live_price=None,
    config=None,
):
    """
    Evaluate a potential Forex trading setup.

    Output classifications:
        CONFIRMED
        WAIT
        REJECTED

    CONFIRMED means the configured research rules passed.
    It does not mean a profitable trade is guaranteed.

    No MT5 order is placed, modified or closed.
    """

    settings = dict(DEFAULT_CONFIG)

    if isinstance(config, dict):
        settings.update(config)

    def build_result(*args, **kwargs):
        kwargs["quality_config"] = settings
        return _result(*args, **kwargs)

    def invalid_result(reason, condition):
        return {
            "valid": False,
            "symbol": symbol,
            "classification": "WAIT",
            "signal": "WAIT",
            "direction": "UNKNOWN",
            "reason": reason,
            "missing_conditions": [condition] if condition else [],
            "execution_permitted": False,
        }

    # --------------------------------------------------------
    # 1. INPUT VALIDATION
    # --------------------------------------------------------

    if not isinstance(symbol, str) or not symbol.strip():

        return invalid_result("A valid symbol is required.", "VALID_SYMBOL")

    if (
        not isinstance(mtf_result, dict)
        or not mtf_result.get("valid", False)
    ):

        return invalid_result(
            "Valid multi-timeframe analysis is required.",
            "VALID_MTF_ANALYSIS",
        )

    if (
        not isinstance(zone_result, dict)
        or not zone_result.get("valid", False)
    ):

        return invalid_result(
            "Valid zone validation is required.",
            "VALID_ZONE_ANALYSIS",
        )

    symbol = symbol.strip()
    if mtf_result.get("symbol") != symbol:
        return invalid_result(
            "Requested symbol does not match multi-timeframe analysis.",
            "MTF_SYMBOL_MATCH",
        )
    if zone_result.get("symbol") != symbol:
        return invalid_result(
            "Requested symbol does not match zone analysis.",
            "ZONE_SYMBOL_MATCH",
        )

    timeframes = mtf_result.get("timeframes")
    if not isinstance(timeframes, dict):
        return invalid_result(
            "Multi-timeframe result is missing its timeframe map.",
            "REQUIRED_TIMEFRAMES",
        )
    for timeframe_name in ("H4", "H1", "M15"):
        timeframe_result = timeframes.get(timeframe_name)
        if (
            not isinstance(timeframe_result, dict)
            or timeframe_result.get("valid") is not True
        ):
            return invalid_result(
                f"{timeframe_name} timeframe analysis is invalid or incomplete.",
                f"VALID_{timeframe_name}_DATA",
            )

    config_valid, config_error = _validate_config(settings)

    if not config_valid:

        return invalid_result(config_error, "VALID_SIGNAL_CONFIGURATION")

    valid, error = _validate_candles(
        m15_data,
        settings["minimum_candles_m15"],
        "M15",
    )

    if not valid:

        return invalid_result(error, "VALID_M15_CANDLES")

    valid, error = _validate_candles(
        h1_data,
        settings["minimum_candles_h1"],
        "H1",
    )

    if not valid:

        return invalid_result(error, "VALID_H1_CANDLES")

    # --------------------------------------------------------
    # 2. MULTI-TIMEFRAME DIRECTION
    # --------------------------------------------------------

    h4 = _timeframe(mtf_result, "H4")
    h1 = _timeframe(mtf_result, "H1")
    m15 = _timeframe(mtf_result, "M15")

    bias = _direction(
        mtf_result.get("market_bias") or h4.get("trend")
    )

    h4_direction = _direction(h4.get("trend"))
    h1_direction = _direction(h1.get("trend"))
    m15_direction = _direction(m15.get("trend"))

    conditions = {
        "h4": h4_direction,
        "h1": h1_direction,
        "m15": m15_direction,
    }

    if bias not in {"BULLISH", "BEARISH"}:

        return build_result(
            symbol,
            "WAIT",
            "UNKNOWN",
            "H4 has not established a directional market bias.",
            conditions=conditions,
            missing_conditions=["H4_DIRECTIONAL_BIAS"],
        )

    if h4_direction == "UNKNOWN":
        h4_direction = bias

    conditions["h4"] = h4_direction

    direction = bias

    opposite = (
        "BEARISH"
        if direction == "BULLISH"
        else "BULLISH"
    )

    if not (
        h4_direction == direction
        and h1_direction == direction
        and m15_direction == direction
    ):

        return build_result(
            symbol,
            "WAIT",
            direction,
            "H4, H1 and M15 trends are not fully aligned.",
            conditions=conditions,
            missing_conditions=["MULTI_TIMEFRAME_ALIGNMENT"],
        )

    # Reject a recent opposing structure event.

    for name, timeframe_data, maximum_age in [
        ("H1", h1, settings["maximum_event_age_h1"]),
        ("M15", m15, settings["maximum_event_age_m15"]),
    ]:

        event_direction = _direction(
            timeframe_data.get("event_direction")
        )

        if (
            event_direction == opposite
            and _event_is_fresh(timeframe_data, maximum_age)
        ):

            return build_result(
                symbol,
                "REJECTED",
                direction,
                f"A recent opposing {name} structure event "
                "conflicts with the proposed direction.",
                conditions=conditions,
                warnings=[f"OPPOSING_{name}_STRUCTURE_EVENT"],
            )

    # --------------------------------------------------------
    # 3. MARKET REGIME
    # --------------------------------------------------------

    h1_indicators = calculate_indicators(h1_data)

    if h1_indicators is None or h1_indicators.empty:

        return build_result(
            symbol,
            "WAIT",
            direction,
            "H1 indicators could not be calculated.",
            conditions=conditions,
            missing_conditions=["H1_INDICATORS"],
        )

    regime = detect_market_regime(
        h1_indicators,
        adx_trend_threshold=settings["adx_trend_threshold"],
    )

    if not isinstance(regime, dict):

        return build_result(
            symbol,
            "WAIT",
            direction,
            "Market regime analysis returned invalid data.",
            conditions=conditions,
            missing_conditions=["VALID_MARKET_REGIME"],
        )

    regime_name = str(
        regime.get("regime", "INSUFFICIENT_DATA")
    ).upper()

    if regime_name == "INSUFFICIENT_DATA":

        return build_result(
            symbol,
            "WAIT",
            direction,
            regime.get(
                "reason",
                "Market regime data is insufficient.",
            ),
            conditions=conditions,
            market_regime=regime,
            missing_conditions=["VALID_MARKET_REGIME"],
        )

    if regime_name == "HIGH_VOLATILITY":

        return build_result(
            symbol,
            "REJECTED",
            direction,
            "High volatility makes this setup unsuitable "
            "for the current rule set.",
            conditions=conditions,
            market_regime=regime,
            warnings=["HIGH_VOLATILITY"],
        )

    if (
        regime_name == "BULLISH_TREND"
        and direction != "BULLISH"
    ) or (
        regime_name == "BEARISH_TREND"
        and direction != "BEARISH"
    ):

        return build_result(
            symbol,
            "REJECTED",
            direction,
            "The H1 market regime conflicts with "
            "the proposed direction.",
            conditions=conditions,
            market_regime=regime,
            warnings=["MARKET_REGIME_CONFLICT"],
        )

    # --------------------------------------------------------
    # 4. RECENT LIQUIDITY SWEEP
    # --------------------------------------------------------

    sweep, sweep_error = _get_recent_sweep(
        m15_data,
        direction,
        settings["liquidity_freshness_bars"],
    )

    if sweep is None:

        return build_result(
            symbol,
            "WAIT",
            direction,
            sweep_error,
            conditions=conditions,
            market_regime=regime,
            missing_conditions=[
                "FRESH_DIRECTIONAL_LIQUIDITY_SWEEP"
            ],
        )

    # --------------------------------------------------------
    # 5. ZONE RETEST AND REJECTION
    # --------------------------------------------------------

    zone, zone_error = _find_retested_zone(
        zone_result,
        m15_data,
        direction,
        settings["maximum_zone_age_bars"],
    )

    if zone is None:

        return build_result(
            symbol,
            "WAIT",
            direction,
            zone_error,
            conditions=conditions,
            market_regime=regime,
            liquidity_sweep=sweep,
            missing_conditions=[
                "M15_ZONE_RETEST_AND_REJECTION"
            ],
        )

    # --------------------------------------------------------
    # 6. ENTRY AND RISK LEVELS
    # --------------------------------------------------------

    atr = _atr(m15_data)

    if atr is None or atr <= 0:

        return build_result(
            symbol,
            "WAIT",
            direction,
            "M15 ATR could not be calculated.",
            conditions=conditions,
            market_regime=regime,
            liquidity_sweep=sweep,
            selected_zone=zone,
            missing_conditions=["VALID_M15_ATR"],
        )

    latest = m15_data.iloc[-1]

    live_entry, spread, quote_available = _get_live_quote(
        live_price,
        direction,
    )

    warnings = []

    if quote_available:

        entry = live_entry

        if spread > atr * settings["maximum_spread_atr"]:

            return build_result(
                symbol,
                "REJECTED",
                direction,
                "Current spread is too large relative to M15 ATR.",
                conditions=conditions,
                market_regime=regime,
                liquidity_sweep=sweep,
                selected_zone=zone,
                spread=spread,
                atr=atr,
                warnings=["SPREAD_TOO_HIGH"],
            )

    else:

        entry = _number(latest["close"])

        warnings.append(
            "LIVE_QUOTE_UNAVAILABLE: entry uses the last "
            "completed M15 close and is not a live executable quote."
        )

    if entry is None or entry <= 0:

        return build_result(
            symbol,
            "WAIT",
            direction,
            "A valid entry reference could not be calculated.",
            conditions=conditions,
            market_regime=regime,
            liquidity_sweep=sweep,
            selected_zone=zone,
            spread=spread,
            atr=atr,
            warnings=warnings,
            missing_conditions=["VALID_ENTRY_REFERENCE"],
        )

    buffer = atr * settings["stop_atr_buffer"]

    if direction == "BULLISH":

        stop_loss = float(latest["low"]) - buffer
        risk = entry - stop_loss

    else:

        stop_loss = float(latest["high"]) + buffer
        risk = stop_loss - entry

    if risk <= 0:

        return build_result(
            symbol,
            "REJECTED",
            direction,
            "Calculated stop-loss is not on the correct side "
            "of the entry price.",
            conditions=conditions,
            market_regime=regime,
            liquidity_sweep=sweep,
            selected_zone=zone,
            spread=spread,
            atr=atr,
            warnings=warnings,
        )

    minimum_rr = settings["minimum_risk_reward"]
    target_rr = settings["target_risk_reward"]

    if direction == "BULLISH":

        take_profit = entry + risk * target_rr

    else:

        take_profit = entry - risk * target_rr

    risk_reward = abs(take_profit - entry) / risk

    if risk_reward < minimum_rr:

        return build_result(
            symbol,
            "REJECTED",
            direction,
            "Calculated risk/reward is below "
            "the configured minimum.",
            conditions=conditions,
            market_regime=regime,
            liquidity_sweep=sweep,
            selected_zone=zone,
            spread=spread,
            atr=atr,
            risk_reward=risk_reward,
            warnings=warnings,
            missing_conditions=["MINIMUM_RISK_REWARD"],
        )

    # --------------------------------------------------------
    # 7. DYNAMIC QUALITY SCORE
    # --------------------------------------------------------

    quality_breakdown = _quality_breakdown(
        direction=direction,
        conditions=conditions,
        config=settings,
        market_regime=regime,
        liquidity_sweep=sweep,
        selected_zone=zone,
        spread=spread,
        atr=atr,
        risk_reward=risk_reward,
    )
    score = round(
        sum(quality_breakdown.values()),
        2,
    )

    conditions.update({
        "multi_timeframe_alignment": True,
        "recent_liquidity_sweep": True,
        "active_zone_retest": True,
        "rejection_confirmation": True,
        "market_regime": regime_name,
        "live_quote_available": quote_available,
        "spread_check_passed": quote_available,
        "risk_reward_passed": risk_reward >= minimum_rr,
    })

    # --------------------------------------------------------
    # 8. FINAL CLASSIFICATION
    # --------------------------------------------------------

    if not quote_available:

        classification = "WAIT"

        reason = (
            "Technical conditions are present, but a live "
            "bid/ask quote is required before confirmation."
        )
        missing_conditions = ["LIVE_BID_ASK_QUOTE"]

    else:

        classification = "CONFIRMED"

        reason = (
            "Directional structure, liquidity sweep, "
            "zone rejection, market regime and risk checks passed."
        )
        missing_conditions = []

    # --------------------------------------------------------
    # 9. RETURN SIGNAL
    # --------------------------------------------------------

    return build_result(
        classification=classification,
        direction=direction,
        reason=reason,
        conditions=conditions,
        warnings=warnings,
        entry=round(entry, 8),
        stop_loss=round(stop_loss, 8),
        take_profit=round(take_profit, 8),
        risk_distance=round(risk, 8),
        risk_reward=round(risk_reward, 2),
        atr=round(atr, 8),
        spread=round(spread, 8) if spread is not None else None,
        quality_score=score,
        quality_breakdown=quality_breakdown,
        market_regime=regime,
        liquidity_sweep=sweep,
        selected_zone=zone,
        execution_permitted=False,
        missing_conditions=missing_conditions,
    )
