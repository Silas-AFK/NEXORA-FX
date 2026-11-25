def _get_timeframe(mtf_result, name):
    timeframes = mtf_result.get("timeframes", {})
    if not isinstance(timeframes, dict):
        return {}
    return timeframes.get(name, {})


def _safe_count(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _summarize_unique_levels(sweeps):
    levels = {}

    for sweep in sweeps:
        if not isinstance(sweep, dict):
            continue

        level_id = sweep.get("unique_level_id")
        sweep_type = sweep.get("type")

        if level_id is None:
            price = sweep.get("level")

            if price is None:
                continue

            level_id = f"{sweep_type}_{price}"

        if level_id not in levels:
            levels[level_id] = {
                "unique_level_id": level_id,
                "type": sweep_type,
                "sweep_type": sweep_type,
                "level": sweep.get("level"),
                "event_count": 0,
                "repeat_count": 0,
                "repeated": False,
                "latest_bars_ago": None,
                "latest_timestamp": None,
            }

        item = levels[level_id]
        item["event_count"] += 1

        item["repeat_count"] = max(
            item["repeat_count"],
            _safe_count(sweep.get("repeat_count", 1))
        )

        if (
            sweep.get("sweep_classification") == "REPEATED_SWEEP"
            or item["repeat_count"] > 1
        ):
            item["repeated"] = True

        bars_ago = sweep.get("bars_ago")

        if isinstance(bars_ago, (int, float)):
            if (
                item["latest_bars_ago"] is None
                or bars_ago < item["latest_bars_ago"]
            ):
                item["latest_bars_ago"] = bars_ago
                item["latest_timestamp"] = sweep.get("timestamp")

    summarized = list(levels.values())

    summarized.sort(
        key=lambda item: (
            item["latest_bars_ago"]
            if item["latest_bars_ago"] is not None
            else float("inf")
        )
    )

    return summarized


def _get_liquidity_sweep(timeframe_result, bias=None):
    analysis = timeframe_result.get("analysis", {})

    if not isinstance(analysis, dict):
        analysis = {}

    liquidity = analysis.get("liquidity", {})

    if not isinstance(liquidity, dict):
        liquidity = {}

    if not liquidity.get("valid", False):
        return {
            "valid": False,
            "status": "LIQUIDITY_DATA_UNAVAILABLE",
            "supports_bias": False,
            "sweeps": [],
            "matching_sweeps": [],
            "opposing_sweeps": [],
            "unique_levels": [],
            "matching_unique_levels": [],
            "opposing_unique_levels": [],
            "first_observed_count": 0,
            "repeated_sweep_count": 0,
            "matching_sweep_count": 0,
            "opposing_sweep_count": 0,
            "matching_repeated_count": 0,
            "opposing_repeated_count": 0,
            "unique_recent_level_count": 0,
            "matching_unique_level_count": 0,
            "opposing_unique_level_count": 0,
            "repeated_unique_level_count": 0,
            "matching_repeated_level_count": 0,
            "opposing_repeated_level_count": 0,
            "unique_swept_level_count": 0,
            "recent_unique_swept_level_count": 0,
            "reason": liquidity.get(
                "reason", "Liquidity data unavailable."
            )
        }

    # Prefer fresh historical sweeps.
    recent_sweeps = liquidity.get("recent_sweeps", [])

    if not isinstance(recent_sweeps, list):
        recent_sweeps = []

    fresh_sweeps = [
        sweep
        for sweep in recent_sweeps
        if isinstance(sweep, dict)
        and sweep.get("fresh") is True
    ]

    if fresh_sweeps:
        sweeps = fresh_sweeps
        source = "RECENT_HISTORICAL_SWEEPS"
    else:
        latest_sweeps = liquidity.get("sweeps", [])

        if not isinstance(latest_sweeps, list):
            latest_sweeps = []

        sweeps = latest_sweeps
        source = "LATEST_CANDLE_SWEEPS"

    # Directional sweep mapping.
    if bias == "BULLISH":
        matching_type = "SELL_SIDE_SWEEP"
        opposing_type = "BUY_SIDE_SWEEP"

    elif bias == "BEARISH":
        matching_type = "BUY_SIDE_SWEEP"
        opposing_type = "SELL_SIDE_SWEEP"

    else:
        matching_type = None
        opposing_type = None

    matching = [
        sweep for sweep in sweeps
        if isinstance(sweep, dict)
        and sweep.get("type") == matching_type
    ] if matching_type else []

    opposing = [
        sweep for sweep in sweeps
        if isinstance(sweep, dict)
        and sweep.get("type") == opposing_type
    ] if opposing_type else []

    first_observed = [
        sweep for sweep in sweeps
        if isinstance(sweep, dict)
        and sweep.get("sweep_classification")
        == "FIRST_OBSERVED_SWEEP"
    ]

    repeated = [
        sweep for sweep in sweeps
        if isinstance(sweep, dict)
        and sweep.get("sweep_classification")
        == "REPEATED_SWEEP"
    ]

    # Group sweeps by unique liquidity level.
    unique_levels = _summarize_unique_levels(sweeps)

    matching_unique = [
        level for level in unique_levels
        if level["sweep_type"] == matching_type
    ] if matching_type else []

    opposing_unique = [
        level for level in unique_levels
        if level["sweep_type"] == opposing_type
    ] if opposing_type else []

    repeated_levels = [
        level for level in unique_levels
        if level["repeated"]
    ]

    matching_repeated = [
        level for level in matching_unique
        if level["repeated"]
    ]

    opposing_repeated = [
        level for level in opposing_unique
        if level["repeated"]
    ]

    if not sweeps:
        status = "NO_RECENT_SWEEP"
    elif bias not in ("BULLISH", "BEARISH"):
        status = "SWEEP_DETECTED_BIAS_UNAVAILABLE"
    elif matching_unique:
        status = "SWEEP_SUPPORTS_BIAS"
    else:
        status = "SWEEP_NOT_SUPPORTING_BIAS"

    # Lowest bars_ago means most recent event.
    latest_sweep = (
        min(
            sweeps,
            key=lambda item: (
                item.get("bars_ago", float("inf"))
                if isinstance(item, dict)
                else float("inf")
            )
        )
        if sweeps else None
    )

    return {
        "valid": True,
        "status": status,
        "source": source,
        "supports_bias": bool(matching_unique),
        "latest_sweep": latest_sweep,

        # Individual sweep events.
        "sweeps": sweeps,
        "matching_sweeps": matching,
        "opposing_sweeps": opposing,
        "first_observed_sweeps": first_observed,
        "repeated_sweeps": repeated,

        # Unique liquidity levels.
        "unique_levels": unique_levels,
        "matching_unique_levels": matching_unique,
        "opposing_unique_levels": opposing_unique,
        "repeated_unique_levels": repeated_levels,
        "matching_repeated_unique_levels": matching_repeated,
        "opposing_repeated_unique_levels": opposing_repeated,

        # Event counts.
        "first_observed_count": len(first_observed),
        "repeated_sweep_count": len(repeated),
        "matching_sweep_count": len(matching),
        "opposing_sweep_count": len(opposing),

        # Unique level counts.
        "unique_recent_level_count": len(unique_levels),
        "matching_unique_level_count": len(matching_unique),
        "opposing_unique_level_count": len(opposing_unique),
        "repeated_unique_level_count": len(repeated_levels),
        "matching_repeated_level_count": len(matching_repeated),
        "opposing_repeated_level_count": len(opposing_repeated),

        # Backward-compatible fields.
        "matching_repeated_count": len(matching_repeated),
        "opposing_repeated_count": len(opposing_repeated),
        "unique_swept_level_count": _safe_count(
            liquidity.get("unique_swept_level_count", 0)
        ),
        "recent_unique_swept_level_count": _safe_count(
            liquidity.get("recent_unique_swept_level_count", 0)
        ),

        "reason": (
            "Recent unique liquidity level sweep supports market bias."
            if matching_unique
            else "No recent unique liquidity level sweep supports market bias."
        )
    }


def evaluate_smc_setup(symbol, mtf_result, zone_result):
    result = {
        "valid": False,
        "symbol": symbol,
        "market_bias": "NEUTRAL",
        "h1_trend": "UNKNOWN",
        "m15_trend": "UNKNOWN",
        "h1_event": None,
        "h1_event_direction": None,
        "m15_event": None,
        "m15_event_direction": None,
        "h1_event_age_bars": None,
        "m15_event_age_bars": None,
        "classification": "NO_SETUP",
        "liquidity_sweep": {},
        "liquidity_by_timeframe": {},
        "eligible_m15_zones": [],
        "touched_m15_zones": [],
        "evidence": [],
        "warnings": [],
        "missing_conditions": [],
        "execution_permitted": False,
        "reason": ""
    }

    # --------------------------------------------------
    # 1. INPUT VALIDATION
    # --------------------------------------------------

    if not isinstance(mtf_result, dict) or not mtf_result.get("valid"):
        result["reason"] = "Invalid multi-timeframe analysis."
        return result

    if not isinstance(zone_result, dict) or not zone_result.get("valid"):
        result["reason"] = "Invalid SMC zone validation."
        return result

    if mtf_result.get("symbol") != symbol:
        result["reason"] = "Multi-timeframe symbol mismatch."
        return result

    if zone_result.get("symbol") != symbol:
        result["reason"] = "Zone validation symbol mismatch."
        return result

    h4 = _get_timeframe(mtf_result, "H4")
    h1 = _get_timeframe(mtf_result, "H1")
    m15 = _get_timeframe(mtf_result, "M15")

    if not all(
        isinstance(tf, dict) and tf.get("valid", False)
        for tf in (h4, h1, m15)
    ):
        result["reason"] = (
            "One or more timeframe analyses are invalid."
        )
        return result

    bias = mtf_result.get("market_bias", "NEUTRAL")

    h1_trend = h1.get("trend", "UNKNOWN")
    m15_trend = m15.get("trend", "UNKNOWN")

    h1_event = h1.get("event")
    h1_event_direction = h1.get("event_direction")

    m15_event = m15.get("event")
    m15_event_direction = m15.get("event_direction")

    h1_event_age = h1.get("event_age_bars")
    m15_event_age = m15.get("event_age_bars")

    result["valid"] = True
    result["market_bias"] = bias
    result["h1_trend"] = h1_trend
    result["m15_trend"] = m15_trend

    result["h1_event"] = h1_event
    result["h1_event_direction"] = h1_event_direction
    result["m15_event"] = m15_event
    result["m15_event_direction"] = m15_event_direction

    result["h1_event_age_bars"] = h1_event_age
    result["m15_event_age_bars"] = m15_event_age

    # --------------------------------------------------
    # 2. LIQUIDITY ANALYSIS ACROSS ALL TIMEFRAMES
    # --------------------------------------------------

    for name, timeframe_result in (
        ("H4", h4),
        ("H1", h1),
        ("M15", m15)
    ):
        result["liquidity_by_timeframe"][name] = (
            _get_liquidity_sweep(timeframe_result, bias)
        )

    result["liquidity_sweep"] = (
        result["liquidity_by_timeframe"]["M15"]
    )

    # --------------------------------------------------
    # 3. H4 HIGHER-TIMEFRAME DIRECTIONAL BIAS
    # --------------------------------------------------

    if bias not in ("BULLISH", "BEARISH"):
        result["classification"] = "NO_SETUP"

        result["warnings"].append(
            "H4 has no clear directional bias."
        )

        result["missing_conditions"].append(
            "H4_DIRECTIONAL_BIAS"
        )

        result["reason"] = (
            "Waiting for a clear higher-timeframe bias."
        )

        return result

    result["evidence"].append(
        f"H4 directional bias is {bias}."
    )

    # --------------------------------------------------
    # 4. H1 STRUCTURAL EVENT CONFIRMATION
    # --------------------------------------------------

    h1_event_aligned = (
        h1_event in ("BOS", "CHoCH")
        and h1_event_direction == bias
    )

    if h1_event_aligned:
        result["evidence"].append(
            f"H1 {h1_event} event aligns with H4 {bias} bias."
        )
    else:
        result["missing_conditions"].append(
            "H1_CONFIRMATION_EVENT"
        )

        result["warnings"].append(
            "H1 has no BOS or CHoCH event aligned with H4 bias."
        )

    # RANGING is permitted when the structural event aligns.
    # An opposite directional trend is handled as conflict.

    if h1_trend in ("BULLISH", "BEARISH") and h1_trend != bias:
        result["warnings"].append(
            "H1 trend opposes H4 bias."
        )

        result["missing_conditions"].append(
            "H1_TREND_ALIGNMENT"
        )

    # --------------------------------------------------
    # 5. M15 STRUCTURAL EVENT CONFIRMATION
    # --------------------------------------------------

    m15_event_aligned = (
        m15_event in ("BOS", "CHoCH")
        and m15_event_direction == bias
    )

    if m15_event_aligned:
        result["evidence"].append(
            f"M15 {m15_event} event aligns with H4 {bias} bias."
        )
    else:
        result["missing_conditions"].append(
            "M15_CONFIRMATION_EVENT"
        )

        result["warnings"].append(
            "M15 has no BOS or CHoCH event aligned with H4 bias."
        )

    # RANGING M15 trend is not an automatic rejection.

    if m15_trend in ("BULLISH", "BEARISH") and m15_trend != bias:
        result["warnings"].append(
            "M15 trend opposes H4 bias."
        )

        result["missing_conditions"].append(
            "M15_TREND_ALIGNMENT"
        )

    # --------------------------------------------------
    # 6. DIRECTIONAL CONFLICT DETECTION
    # --------------------------------------------------

    directional_conflict = (
        (
            h1_trend in ("BULLISH", "BEARISH")
            and h1_trend != bias
        )
        or (
            m15_trend in ("BULLISH", "BEARISH")
            and m15_trend != bias
        )
        or (
            h1_event in ("BOS", "CHoCH")
            and h1_event_direction in ("BULLISH", "BEARISH")
            and h1_event_direction != bias
        )
        or (
            m15_event in ("BOS", "CHoCH")
            and m15_event_direction in ("BULLISH", "BEARISH")
            and m15_event_direction != bias
        )
    )

    if directional_conflict:
        result["classification"] = "STRUCTURAL_CONFLICT"

        result["reason"] = (
            "A directional timeframe trend or structural event "
            "opposes the H4 bias."
        )

        result["execution_permitted"] = False
        return result

    # --------------------------------------------------
    # 7. M15 LIQUIDITY SWEEP VALIDATION
    # --------------------------------------------------

    sweep = result["liquidity_sweep"]

    liquidity_supports_bias = sweep.get(
        "supports_bias", False
    )

    if liquidity_supports_bias:
        result["evidence"].append(
            "Recent M15 unique liquidity sweep supports H4 bias."
        )
    else:
        result["missing_conditions"].append(
            "M15_DIRECTIONAL_LIQUIDITY_SWEEP"
        )

        result["warnings"].append(
            "No recent M15 unique liquidity sweep supports H4 bias."
        )

    matching_repeated_count = sweep.get(
        "matching_repeated_level_count", 0
    )

    opposing_repeated_count = sweep.get(
        "opposing_repeated_level_count", 0
    )

    if matching_repeated_count > 0:
        result["evidence"].append(
            f"M15 has {matching_repeated_count} unique "
            "repeated liquidity level(s) supporting H4 bias."
        )

    if opposing_repeated_count > 0:
        result["warnings"].append(
            f"M15 has {opposing_repeated_count} unique "
            "repeated liquidity level(s) opposing H4 bias."
        )

    # Repeated sweeps are contextual information only.
    # They do not independently approve a setup.

    # --------------------------------------------------
    # 8. M15 ZONE VALIDATION
    # --------------------------------------------------

    all_zones = zone_result.get("eligible_zones", [])

    if not isinstance(all_zones, list):
        all_zones = []

    eligible_m15_zones = [
        zone
        for zone in all_zones
        if isinstance(zone, dict)
        and zone.get("timeframe") == "M15"
        and zone.get("direction") == bias
        and zone.get("eligible") is True
    ]

    result["eligible_m15_zones"] = eligible_m15_zones

    if eligible_m15_zones:
        result["evidence"].append(
            "Eligible M15 zone matches H4 bias."
        )
    else:
        result["missing_conditions"].append(
            "ELIGIBLE_M15_ZONE"
        )

        result["warnings"].append(
            "No eligible M15 zone matches H4 bias."
        )

    # A touch requires explicit touch evidence from validator.
    touched_m15_zones = [
        zone
        for zone in eligible_m15_zones
        if zone.get("has_recorded_touch") is True
        and zone.get("first_touch_index") is not None
    ]

    result["touched_m15_zones"] = touched_m15_zones

    if touched_m15_zones:
        result["evidence"].append(
            "M15 zone has a recorded price interaction."
        )
    else:
        result["missing_conditions"].append(
            "M15_ZONE_RETEST"
        )

        result["warnings"].append(
            "M15 zone has not recorded a confirmed touch."
        )

    # --------------------------------------------------
    # 9. FINAL RESEARCH CLASSIFICATION
    # --------------------------------------------------

    structural_alignment = (
        h1_event_aligned
        and m15_event_aligned
    )

    if not structural_alignment:
        result["classification"] = "WAITING_FOR_STRUCTURE"

    elif not liquidity_supports_bias:
        result["classification"] = "WAITING_FOR_LIQUIDITY"

    elif not eligible_m15_zones:
        result["classification"] = "WAITING_FOR_VALID_ZONE"

    elif not touched_m15_zones:
        result["classification"] = "WAITING_FOR_RETEST"

    else:
        result["classification"] = (
            "RETEST_OBSERVED_WAITING_FOR_CONFIRMATION"
        )

        result["warnings"].append(
            "A zone interaction is recorded, but a separate "
            "rejection/confirmation rule is not implemented."
        )

        result["missing_conditions"].append(
            "PRICE_REJECTION_CONFIRMATION"
        )

    result["reason"] = (
        "Research evaluation completed. "
        "No trading action is authorized."
    )

    # --------------------------------------------------
    # 10. SAFETY INVARIANT
    # --------------------------------------------------

    result["execution_permitted"] = False

    return result