
"""
NEXORA FX — Setup Diagnostics
Reads the actual multi-timeframe SMC result structure.

Research and diagnostics only.
Trade execution is permanently disabled.
"""


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _timeframe_data(mtf_result, timeframe):
    mtf_result = _as_dict(mtf_result)
    timeframes = _as_dict(mtf_result.get("timeframes"))
    return _as_dict(timeframes.get(timeframe))


def _zone_data(zone_result, setup_result):
    zone_result = _as_dict(zone_result)
    setup_result = _as_dict(setup_result)

    eligible = _as_list(zone_result.get("eligible_zones"))

    if not eligible:
        eligible = _as_list(
            zone_result.get("eligible")
        )

    if not eligible:
        eligible = _as_list(
            setup_result.get("eligible_m15_zones")
        )

    rejected = _as_list(zone_result.get("rejected_zones"))

    return {
        "eligible": eligible,
        "rejected": rejected,
        "examined": zone_result.get(
            "examined_count",
            zone_result.get("total_examined")
        ),
        "valid": zone_result.get("valid"),
        "reason": zone_result.get("reason"),
    }


def _get_sweep_summary(setup_result, mtf_result):
    setup_result = _as_dict(setup_result)
    mtf_result = _as_dict(mtf_result)

    # Prefer the liquidity summary already created by the evaluator.
    evaluator_sweep = _as_dict(
        setup_result.get("liquidity_sweep")
    )

    if evaluator_sweep:
        return evaluator_sweep

    # Otherwise inspect the M15 liquidity analysis.
    m15 = _timeframe_data(mtf_result, "M15")
    analysis = _as_dict(m15.get("analysis"))
    liquidity = _as_dict(analysis.get("liquidity"))

    return liquidity


def _get_zone_reasons(zone):
    zone = _as_dict(zone)

    reasons = zone.get("rejection_reasons")

    if isinstance(reasons, list):
        return reasons

    reasons = zone.get("reasons")

    if isinstance(reasons, list):
        return reasons

    return []


def generate_setup_diagnostics(
    setup_result,
    mtf_result=None,
    zone_result=None
):
    """
    Generate a readable explanation using:
    - evaluate_smc_setup() output
    - analyze_multi_timeframe_smc() output
    - validate_smc_zones() output
    """

    setup_result = _as_dict(setup_result)
    mtf_result = _as_dict(mtf_result)
    zone_result = _as_dict(zone_result)

    classification = setup_result.get(
        "classification", "UNKNOWN"
    )

    market_bias = setup_result.get(
        "market_bias",
        mtf_result.get("market_bias", "UNKNOWN")
    )

    missing = _as_list(
        setup_result.get("missing_conditions")
    )

    warnings = _as_list(
        setup_result.get("warnings")
    )

    findings = []
    actions = []

    # Read the actual timeframe structure.
    h4 = _timeframe_data(mtf_result, "H4")
    h1 = _timeframe_data(mtf_result, "H1")
    m15 = _timeframe_data(mtf_result, "M15")

    # H4 details
    h4_trend = h4.get("trend", "UNKNOWN")
    h4_event = h4.get("event")
    h4_event_direction = h4.get("event_direction")
    h4_event_age = h4.get("event_age_bars")

    # H1 details
    h1_trend = h1.get("trend", "UNKNOWN")
    h1_event = h1.get("event")
    h1_event_direction = h1.get("event_direction")
    h1_event_age = h1.get("event_age_bars")

    # M15 details
    m15_trend = m15.get("trend", "UNKNOWN")
    m15_event = m15.get("event")
    m15_event_direction = m15.get("event_direction")
    m15_event_age = m15.get("event_age_bars")

    # Overall classification
    classification_messages = {
        "STRUCTURAL_CONFLICT": (
            "Structural conflict detected. At least one "
            "lower timeframe opposes the higher-timeframe bias."
        ),
        "WAITING_FOR_STRUCTURE": (
            "Required lower-timeframe structure is not confirmed."
        ),
        "WAITING_FOR_LIQUIDITY": (
            "Qualifying supporting liquidity evidence is missing."
        ),
        "WAITING_FOR_VALID_ZONE": (
            "No eligible entry zone currently meets the validation rules."
        ),
        "WAITING_FOR_RETEST": (
            "An eligible zone exists, but a recorded retest is not confirmed."
        ),
        "RETEST_OBSERVED_WAITING_FOR_CONFIRMATION": (
            "A zone retest was observed. Confirmation is still required."
        ),
        "NO_SETUP": (
            "No directional setup is currently established."
        ),
    }

    findings.append(
        classification_messages.get(
            classification,
            "Evaluator classification: " + str(classification)
        )
    )

    # Timeframe summaries
    findings.append(
        "H4: trend="
        + str(h4_trend)
        + ", event="
        + str(h4_event)
        + ", event direction="
        + str(h4_event_direction)
        + ", event age="
        + str(h4_event_age)
        + " bars."
    )

    findings.append(
        "H1: trend="
        + str(h1_trend)
        + ", event="
        + str(h1_event)
        + ", event direction="
        + str(h1_event_direction)
        + ", event age="
        + str(h1_event_age)
        + " bars."
    )

    findings.append(
        "M15: trend="
        + str(m15_trend)
        + ", event="
        + str(m15_event)
        + ", event direction="
        + str(m15_event_direction)
        + ", event age="
        + str(m15_event_age)
        + " bars."
    )

    # Structural comparison
    if market_bias in ("BULLISH", "BEARISH"):
        if m15_trend not in (market_bias, "RANGING", "UNKNOWN"):
            findings.append(
                "M15 trend conflicts with the higher-timeframe bias."
            )

        if m15_event in ("BOS", "CHoCH"):
            if m15_event_direction != market_bias:
                findings.append(
                    "M15 structural event direction conflicts "
                    "with the higher-timeframe bias."
                )

        if h1_event in ("BOS", "CHoCH"):
            if h1_event_direction == market_bias:
                findings.append(
                    "H1 structural event agrees with the higher-timeframe bias."
                )
            else:
                findings.append(
                    "H1 structural event does not agree with the higher-timeframe bias."
                )

    # Liquidity details
    sweep = _get_sweep_summary(setup_result, mtf_result)

    supports_bias = sweep.get("supports_bias")

    if supports_bias is not None:
        findings.append(
            "Liquidity supports market bias: "
            + str(supports_bias)
        )

    if "recent_sweep_count" in sweep:
        findings.append(
            "Recent liquidity sweeps: "
            + str(sweep.get("recent_sweep_count"))
        )

    if "historical_sweep_count" in sweep:
        findings.append(
            "Historical liquidity sweeps: "
            + str(sweep.get("historical_sweep_count"))
        )

    if sweep.get("reason"):
        findings.append(
            "Liquidity explanation: "
            + str(sweep.get("reason"))
        )

    # Zone details
    zones = _zone_data(zone_result, setup_result)

    findings.append(
        "Eligible zones: " + str(len(zones["eligible"]))
    )

    if zones["examined"] is not None:
        findings.append(
            "Zones examined: " + str(zones["examined"])
        )

    if zones["valid"] is not None:
        findings.append(
            "Zone validation valid: " + str(zones["valid"])
        )

    if zones["reason"]:
        findings.append(
            "Zone validation explanation: "
            + str(zones["reason"])
        )

    # Explain rejected zones when the validator provides them.
    findings.append(
        "Rejected zones with detailed records: "
        + str(len(zones["rejected"]))
    )

    for index, zone in enumerate(zones["rejected"], start=1):
        zone = _as_dict(zone)
        reasons = _get_zone_reasons(zone)

        if reasons:
            findings.append(
                "Rejected zone "
                + str(index)
                + " ("
                + str(zone.get("timeframe", "unknown timeframe"))
                + ", "
                + str(zone.get("zone_type", "unknown type"))
                + "): "
                + ", ".join(map(str, reasons))
            )

    # Evaluator conditions
    for condition in missing:
        findings.append(
            "Missing condition: " + str(condition)
        )

    for warning in warnings:
        findings.append(
            "Warning: " + str(warning)
        )

    reason = setup_result.get("reason")

    if reason:
        findings.append(
            "Evaluator explanation: " + str(reason)
        )

    # Suggested waiting conditions. These are not trade instructions.
    if classification == "STRUCTURAL_CONFLICT":
        actions.append(
            "Wait for lower-timeframe structure to align with the market bias."
        )

    elif classification == "WAITING_FOR_STRUCTURE":
        actions.append(
            "Wait for a qualifying BOS or CHoCH aligned with the market bias."
        )

    elif classification == "WAITING_FOR_LIQUIDITY":
        actions.append(
            "Wait for a qualifying liquidity event that supports the market bias."
        )

    elif classification == "WAITING_FOR_VALID_ZONE":
        actions.append(
            "Wait for a zone to pass the existing validation rules."
        )

    elif classification == "WAITING_FOR_RETEST":
        actions.append(
            "Wait for a recorded retest of an eligible zone."
        )

    elif classification == "RETEST_OBSERVED_WAITING_FOR_CONFIRMATION":
        actions.append(
            "Wait until explicit confirmation logic is implemented and validated."
        )

    elif classification == "NO_SETUP":
        actions.append(
            "Continue monitoring; do not force a directional classification."
        )

    findings = list(dict.fromkeys(findings))
    actions = list(dict.fromkeys(actions))

    return {
        "valid": True,
        "symbol": setup_result.get(
            "symbol",
            mtf_result.get("symbol")
        ),
        "classification": classification,
        "market_bias": market_bias,
        "timeframes": {
            "H4": {
                "trend": h4_trend,
                "event": h4_event,
                "event_direction": h4_event_direction,
                "event_age_bars": h4_event_age,
            },
            "H1": {
                "trend": h1_trend,
                "event": h1_event,
                "event_direction": h1_event_direction,
                "event_age_bars": h1_event_age,
            },
            "M15": {
                "trend": m15_trend,
                "event": m15_event,
                "event_direction": m15_event_direction,
                "event_age_bars": m15_event_age,
            },
        },
        "findings": findings,
        "recommended_actions": actions,
        "missing_conditions": missing,
        "warnings": warnings,
        "eligible_zone_count": len(zones["eligible"]),
        "rejected_zone_count": len(zones["rejected"]),
        "liquidity_supports_bias": supports_bias,
        "execution_permitted": False,
    }


def print_setup_diagnostics(diagnostics):
    """Print a readable report in the terminal."""

    diagnostics = _as_dict(diagnostics)

    print("\n" + "=" * 60)
    print("NEXORA FX — SETUP DIAGNOSTICS")
    print("=" * 60)

    print("Symbol:", diagnostics.get("symbol", "UNKNOWN"))
    print("Classification:", diagnostics.get("classification", "UNKNOWN"))
    print("Market Bias:", diagnostics.get("market_bias", "UNKNOWN"))

    print("\nTIMEFRAME DETAILS:")

    timeframes = _as_dict(diagnostics.get("timeframes"))

    for name in ("H4", "H1", "M15"):
        data = _as_dict(timeframes.get(name))

        print(
            name,
            "| Trend:", data.get("trend"),
            "| Event:", data.get("event"),
            "| Direction:", data.get("event_direction"),
            "| Age:", data.get("event_age_bars")
        )

    print("\nFINDINGS:")

    for finding in _as_list(diagnostics.get("findings")):
        print(" -", finding)

    print("\nRECOMMENDED WAITING CONDITIONS:")

    for action in _as_list(diagnostics.get("recommended_actions")):
        print(" -", action)

    print("\nExecution permitted:", False)
    print("=" * 60)
