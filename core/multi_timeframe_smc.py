
import MetaTrader5 as mt5

from core.data_validation import MarketDataValidationError, validate_ohlc_data
from core.market_data import MarketDataError, get_historical_data
from core.smc_engine import analyze_smc


TIMEFRAMES = {
    "H4": mt5.TIMEFRAME_H4,
    "H1": mt5.TIMEFRAME_H1,
    "M15": mt5.TIMEFRAME_M15,
}


def _get_component(analysis, name):
    return analysis.get(name, {}) if isinstance(analysis, dict) else {}


def _get_bias(structure):
    trend = structure.get("trend", "UNKNOWN")

    if trend == "BULLISH":
        return "BULLISH"

    if trend == "BEARISH":
        return "BEARISH"

    return "NEUTRAL"


def _summarize_timeframe(result):
    analysis = result.get("analysis", {})

    structure = _get_component(analysis, "market_structure")
    liquidity = _get_component(analysis, "liquidity")
    order_blocks = _get_component(analysis, "order_blocks")
    fvgs = _get_component(analysis, "fair_value_gaps")

    return {
        "valid": result.get("valid", False),
        "components_valid": result.get("components_valid", 0),
        "components_total": result.get("components_total", 0),

        "trend": structure.get("trend", "UNKNOWN"),

        "event": structure.get("event", "NONE"),
        "event_direction": structure.get(
            "event_direction", "UNKNOWN"
        ),
        "event_age_bars": structure.get("event_age_bars"),
        "event_bias_before_break": structure.get(
            "event_bias_before_break"
        ),
        "event_count": structure.get("event_count", 0),

        "buy_side_pools": len(
            liquidity.get("buy_side_liquidity", [])
        ),
        "sell_side_pools": len(
            liquidity.get("sell_side_liquidity", [])
        ),
        "liquidity_sweeps": liquidity.get("sweeps", []),

        "order_blocks_total": order_blocks.get("count", 0),
        "bullish_order_blocks": order_blocks.get(
            "bullish_count", 0
        ),
        "bearish_order_blocks": order_blocks.get(
            "bearish_count", 0
        ),

        "fair_value_gaps_total": fvgs.get("count", 0),
        "open_fair_value_gaps": fvgs.get("open_count", 0),

        "analysis": analysis,
    }


def _get_event_direction(timeframe_result):
    direction = timeframe_result.get(
        "event_direction"
    )

    if direction in ("BULLISH", "BEARISH"):
        return direction

    return "NEUTRAL"


def _get_event_alignment(higher_bias, h1, m15):
    h1_event = _get_event_direction(h1)
    m15_event = _get_event_direction(m15)

    if (
        h1_event == higher_bias
        and m15_event == higher_bias
    ):
        return "EVENTS_ALIGNED_WITH_H4"

    if (
        h1_event == "NEUTRAL"
        and m15_event == "NEUTRAL"
    ):
        return "NO_RECENT_DIRECTIONAL_EVENTS"

    if (
        h1_event == higher_bias
        or m15_event == higher_bias
    ):
        return "PARTIALLY_ALIGNED"

    if (
        h1_event != "NEUTRAL"
        and m15_event != "NEUTRAL"
        and h1_event == m15_event
    ):
        return "EVENTS_OPPOSE_H4"

    return "MIXED_EVENTS"


def analyze_multi_timeframe_smc(symbol, bars=500):
    """
    Analyze H4, H1 and M15 using completed candles only.

    This is a research module.
    It does not generate orders or permit automated execution.
    """

    if not symbol or not isinstance(symbol, str):
        return {
            "valid": False,
            "reason": "A valid broker symbol is required.",
            "execution_permitted": False,
        }

    if not isinstance(bars, int) or isinstance(bars, bool) or bars < 100:
        return {
            "valid": False,
            "reason": "Use at least 100 historical candles.",
            "execution_permitted": False,
        }

    timeframe_data = {}
    timeframe_errors = {}

    for timeframe_name, timeframe_value in TIMEFRAMES.items():
        try:
            timeframe_data[timeframe_name] = get_historical_data(
                symbol,
                timeframe_value,
                bars,
            )
        except MarketDataError as error:
            timeframe_errors[timeframe_name] = str(error)

    return analyze_multi_timeframe_smc_from_data(
        symbol,
        timeframe_data,
        timeframe_errors=timeframe_errors,
    )


def analyze_multi_timeframe_smc_from_data(
    symbol,
    timeframe_data,
    timeframe_errors=None,
):
    """Analyze supplied completed H4/H1/M15 candles without broker access."""

    if not isinstance(symbol, str) or not symbol.strip():
        return {
            "valid": False,
            "reason": "A valid broker symbol is required.",
            "execution_permitted": False,
        }
    if not isinstance(timeframe_data, dict):
        return {
            "valid": False,
            "symbol": symbol,
            "reason": "Timeframe candle data must be a mapping.",
            "execution_permitted": False,
        }

    timeframe_errors = dict(timeframe_errors or {})
    timeframe_results = {}
    for timeframe_name in TIMEFRAMES:
        data = timeframe_data.get(timeframe_name)
        if data is None:
            timeframe_results[timeframe_name] = {
                "valid": False,
                "reason": timeframe_errors.get(
                    timeframe_name,
                    f"{timeframe_name} candle data is missing.",
                ),
            }
            continue
        try:
            completed_data = validate_ohlc_data(data, require_time=True)
            result = analyze_smc(completed_data)
            timeframe_results[timeframe_name] = _summarize_timeframe(result)
        except (MarketDataValidationError, TypeError, ValueError) as error:
            timeframe_results[timeframe_name] = {
                "valid": False,
                "reason": f"{timeframe_name} candle data is invalid: {error}",
            }
        except Exception as error:
            timeframe_results[timeframe_name] = {
                "valid": False,
                "reason": f"{timeframe_name} analysis error: {error}",
            }

    required_timeframes = ["H4", "H1", "M15"]

    all_valid = all(
        timeframe_results.get(name, {}).get("valid", False)
        for name in required_timeframes
    )

    if not all_valid:
        return {
            "valid": False,
            "symbol": symbol,
            "timeframes": timeframe_results,
            "market_bias": "UNAVAILABLE",
            "alignment": "INCOMPLETE_DATA",
            "execution_permitted": False,
            "reason": "One or more timeframe analyses failed: "
            + "; ".join(
                f"{name}: {result.get('reason', 'invalid analysis')}"
                for name, result in timeframe_results.items()
                if not result.get("valid", False)
            ),
        }

    h4 = timeframe_results["H4"]
    h1 = timeframe_results["H1"]
    m15 = timeframe_results["M15"]

    higher_bias = _get_bias(h4)

    h1_direction = _get_bias(h1)
    m15_direction = _get_bias(m15)

    h1_event_direction = _get_event_direction(h1)
    m15_event_direction = _get_event_direction(m15)

    # Trend alignment remains separate from event alignment.
    if higher_bias == "NEUTRAL":
        trend_alignment = "NO_CLEAR_H4_BIAS"

    elif h1_direction == "NEUTRAL":
        trend_alignment = "H1_NEUTRAL"

    elif h1_direction != higher_bias:
        trend_alignment = "H1_OPPOSES_H4"

    elif m15_direction == "NEUTRAL":
        trend_alignment = "M15_NEUTRAL"

    elif m15_direction != higher_bias:
        trend_alignment = "M15_OPPOSES_H4"

    else:
        trend_alignment = "TREND_ALIGNED"

    event_alignment = _get_event_alignment(
        higher_bias,
        h1,
        m15
    )

    if (
        trend_alignment == "TREND_ALIGNED"
        and event_alignment == "EVENTS_ALIGNED_WITH_H4"
    ):
        overall_alignment = "FULLY_ALIGNED"

    elif event_alignment == "EVENTS_ALIGNED_WITH_H4":
        overall_alignment = "EVENT_ALIGNED_TREND_MIXED"

    elif trend_alignment == "TREND_ALIGNED":
        overall_alignment = "TREND_ALIGNED"

    else:
        overall_alignment = "MIXED"

    return {
        "valid": True,
        "symbol": symbol,

        "market_bias": higher_bias,

        "h1_direction": h1_direction,
        "m15_direction": m15_direction,

        "h1_event_direction": h1_event_direction,
        "m15_event_direction": m15_event_direction,

        "trend_alignment": trend_alignment,
        "event_alignment": event_alignment,
        "alignment": overall_alignment,

        "timeframes": timeframe_results,

        "execution_permitted": False,

        "reason": "Multi-timeframe SMC research completed.",
    }
