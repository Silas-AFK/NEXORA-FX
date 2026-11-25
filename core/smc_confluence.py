def _direction(value):
    if value in ("BULLISH", "BEARISH"):
        return value
    return "NEUTRAL"


def _count_direction(items, direction, status=None):
    if not isinstance(items, list):
        return 0

    count = 0

    for item in items:
        if not isinstance(item, dict):
            continue

        if item.get("direction") != direction:
            continue

        if status is not None and item.get("status") != status:
            continue

        count += 1

    return count


def _component(timeframe, name):
    return timeframe.get("analysis", {}).get(name, {})


def analyze_smc_confluence(mtf_result):
    """
    Combine multi-timeframe SMC research findings.

    This module does not generate trade orders.
    """

    if not isinstance(mtf_result, dict) or not mtf_result.get("valid"):
        return {
            "valid": False,
            "reason": "Valid multi-timeframe analysis is required.",
            "execution_permitted": False,
        }

    timeframes = mtf_result.get("timeframes", {})

    required = ("H4", "H1", "M15")

    if not all(
        isinstance(timeframes.get(name), dict)
        and timeframes[name].get("valid")
        for name in required
    ):
        return {
            "valid": False,
            "reason": "One or more timeframe results are missing.",
            "execution_permitted": False,
        }

    h4 = timeframes["H4"]
    h1 = timeframes["H1"]
    m15 = timeframes["M15"]

    bias = _direction(mtf_result.get("market_bias"))
    h1_trend = _direction(h1.get("trend"))
    m15_trend = _direction(m15.get("trend"))

    if bias == "NEUTRAL":
        return {
            "valid": True,
            "symbol": mtf_result.get("symbol"),
            "market_bias": bias,
            "alignment": mtf_result.get("alignment"),
            "assessment": "NO_CLEAR_HIGHER_TIMEFRAME_BIAS",
            "evidence": [],
            "warnings": [
                "H4 does not currently establish a directional bias."
            ],
            "execution_permitted": False,
            "reason": "Confluence analysis completed without a directional bias.",
        }

    evidence = []
    warnings = []

    if h1_trend == bias:
        evidence.append("H1 structural trend agrees with H4.")
    elif h1_trend == "NEUTRAL":
        warnings.append("H1 structure is neutral.")
    else:
        warnings.append("H1 structure opposes H4.")

    if m15_trend == bias:
        evidence.append("M15 structural trend agrees with H4.")
    elif m15_trend == "NEUTRAL":
        warnings.append("M15 structure is neutral.")
    else:
        warnings.append("M15 structure opposes H4.")

    for timeframe_name, timeframe in (
        ("H4", h4),
        ("H1", h1),
        ("M15", m15),
    ):
        structure = _component(timeframe, "market_structure")
        liquidity = _component(timeframe, "liquidity")
        order_blocks = _component(timeframe, "order_blocks")
        fvgs = _component(timeframe, "fair_value_gaps")

        event = structure.get("event", "NONE")
        event_direction = structure.get("event_direction", "UNKNOWN")

        if event in ("BOS", "CHoCH") and event_direction == bias:
            evidence.append(
                f"{timeframe_name} has a {bias} {event}."
            )
        elif event in ("BOS", "CHoCH") and event_direction in (
            "BULLISH", "BEARISH"
        ):
            warnings.append(
                f"{timeframe_name} latest {event} opposes H4 bias."
            )

        sweeps = liquidity.get("sweeps", [])

        if isinstance(sweeps, list):
            matching_sweeps = [
                sweep for sweep in sweeps
                if isinstance(sweep, dict)
                and sweep.get("direction") == bias
            ]

            if matching_sweeps:
                evidence.append(
                    f"{timeframe_name} has a liquidity sweep "
                    "in the H4 direction."
                )

        ob_count = _count_direction(
            order_blocks.get("order_blocks", []),
            bias,
            "UNMITIGATED",
        )

        fvg_count = _count_direction(
            fvgs.get("fair_value_gaps", []),
            bias,
            "OPEN",
        )

        if ob_count:
            evidence.append(
                f"{timeframe_name} has {ob_count} unmitigated "
                f"{bias.lower()} order block(s)."
            )

        if fvg_count:
            evidence.append(
                f"{timeframe_name} has {fvg_count} open "
                f"{bias.lower()} FVG(s)."
            )

    if h1_trend == bias and m15_trend == bias:
        assessment = "STRUCTURAL_ALIGNMENT"
    elif h1_trend == bias or m15_trend == bias:
        assessment = "PARTIAL_STRUCTURAL_ALIGNMENT"
    else:
        assessment = "STRUCTURAL_CONFLICT"

    return {
        "valid": True,
        "symbol": mtf_result.get("symbol"),
        "market_bias": bias,
        "alignment": mtf_result.get("alignment"),
        "assessment": assessment,
        "evidence": evidence,
        "warnings": warnings,
        "evidence_count": len(evidence),
        "warning_count": len(warnings),
        "execution_permitted": False,
        "reason": "SMC confluence research completed.",
    }
