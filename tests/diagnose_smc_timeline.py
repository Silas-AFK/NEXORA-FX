
import sys
from pathlib import Path
from pprint import pprint

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.mt5_connection import connect, disconnect
from core.multi_timeframe_smc import analyze_multi_timeframe_smc
from core.zone_validator import validate_smc_zones


SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY"]


def print_section(title):
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)


def show_keys(label, obj):
    if isinstance(obj, dict):
        print(f"{label} keys: {list(obj.keys())}")

    elif isinstance(obj, list):
        print(f"{label}: list length = {len(obj)}")

        if obj and isinstance(obj[0], dict):
            print(f"{label} first item keys: {list(obj[0].keys())}")

    else:
        print(f"{label}: {type(obj).__name__}")


def show_samples(label, items, limit=5):
    print(f"\n--- {label} ---")

    if not isinstance(items, list):
        print("Not a list:", type(items).__name__)
        return

    print("Total count:", len(items))

    if not items:
        print("No records found.")
        return

    for index, item in enumerate(items[:limit]):
        print(f"\nSample #{index + 1}")
        pprint(item, sort_dicts=False, width=120)


def inspect_structure(analysis):
    structure = analysis.get("market_structure")

    print("\n--- MARKET STRUCTURE ---")
    show_keys("Market structure", structure)

    if not isinstance(structure, dict):
        print("Market structure data unavailable.")
        return

    for key, value in structure.items():
        if isinstance(value, list):
            show_samples(
                f"Market structure: {key}",
                value,
                limit=5
            )

    # Also inspect nested event history if present.
    for key in ("event_history", "events", "historical_events"):
        value = structure.get(key)

        if isinstance(value, list):
            show_samples(
                f"Structure event history: {key}",
                value,
                limit=10
            )


def inspect_liquidity(analysis):
    liquidity = analysis.get("liquidity")

    print("\n--- LIQUIDITY ---")
    show_keys("Liquidity", liquidity)

    if not isinstance(liquidity, dict):
        return

    for key in (
        "recent_sweeps",
        "sweeps",
        "historical_sweeps",
        "recent_unique_swept_levels",
        "unique_swept_levels",
    ):
        value = liquidity.get(key)

        if isinstance(value, list):
            show_samples(
                f"Liquidity: {key}",
                value,
                limit=5 if key == "recent_sweeps" else 3
            )


def inspect_order_blocks(analysis):
    order_blocks = analysis.get("order_blocks")

    print("\n--- ORDER BLOCKS ---")
    show_keys("Order Blocks", order_blocks)

    if not isinstance(order_blocks, dict):
        return

    show_samples(
        "Order Block candidates",
        order_blocks.get("order_blocks", []),
        limit=5
    )


def inspect_fair_value_gaps(analysis):
    fair_value_gaps = analysis.get("fair_value_gaps")

    print("\n--- FAIR VALUE GAPS ---")
    show_keys("Fair Value Gaps", fair_value_gaps)

    if not isinstance(fair_value_gaps, dict):
        return

    show_samples(
        "Fair Value Gap candidates",
        fair_value_gaps.get("fair_value_gaps", []),
        limit=5
    )


def inspect_timeframe(name, tf_result):
    print_section(f"{name} TIMEFRAME")

    if not isinstance(tf_result, dict):
        print("Invalid timeframe result.")
        return

    show_keys("Timeframe result", tf_result)

    print("Valid:", tf_result.get("valid"))
    print("Trend:", tf_result.get("trend"))
    print("Latest event:", tf_result.get("event"))
    print("Event direction:", tf_result.get("event_direction"))

    analysis = tf_result.get("analysis", {})

    if not isinstance(analysis, dict):
        print("Analysis data unavailable.")
        return

    show_keys("Analysis", analysis)

    inspect_structure(analysis)
    inspect_liquidity(analysis)
    inspect_order_blocks(analysis)
    inspect_fair_value_gaps(analysis)


def inspect_zones(zone_result):
    print_section("ZONE VALIDATION RESULT")

    if not isinstance(zone_result, dict):
        print("Invalid zone validation result.")
        return

    show_keys("Zone result", zone_result)

    print("Valid:", zone_result.get("valid"))
    print("Market bias:", zone_result.get("market_bias"))
    print("Current price:", zone_result.get("current_price"))
    print("Zones examined:", zone_result.get("zones_examined"))
    print("Eligible zones:", zone_result.get("eligible_count"))
    print("Approaching zones:", zone_result.get("approaching_count"))
    print("Retest zones:", zone_result.get("retest_count"))
    print("Execution permitted:", zone_result.get("execution_permitted"))

    print("\nZone state counts:")
    pprint(zone_result.get("state_counts", {}), sort_dicts=False)

    print("\nZone rejection counts:")
    pprint(zone_result.get("rejection_counts", {}), sort_dicts=False)

    print("\nTimeframe errors:")
    pprint(zone_result.get("timeframe_errors", []), sort_dicts=False)

    show_samples(
        "Eligible zones",
        zone_result.get("eligible_zones", []),
        limit=10
    )

    show_samples(
        "Approaching zones",
        zone_result.get("approaching_zones", []),
        limit=5
    )

    show_samples(
        "Retest zones",
        zone_result.get("retest_zones", []),
        limit=5
    )

    show_samples(
        "All validated zones",
        zone_result.get("all_zones", []),
        limit=5
    )


def inspect_symbol(symbol):
    print_section(f"SYMBOL: {symbol}")

    mtf_result = analyze_multi_timeframe_smc(
        symbol,
        bars=500
    )

    if not isinstance(mtf_result, dict):
        print("MTF analysis returned an invalid result.")
        return

    show_keys("MTF result", mtf_result)

    print("MTF valid:", mtf_result.get("valid"))
    print("Market bias:", mtf_result.get("market_bias"))
    print("H1 direction:", mtf_result.get("h1_direction"))
    print("M15 direction:", mtf_result.get("m15_direction"))
    print("Alignment:", mtf_result.get("alignment"))
    print("Execution permitted:", mtf_result.get("execution_permitted"))

    if not mtf_result.get("valid", False):
        print("\nMTF analysis invalid:")
        pprint(mtf_result, sort_dicts=False)
        return

    timeframes = mtf_result.get("timeframes", {})

    if not isinstance(timeframes, dict):
        print("Timeframe collection unavailable.")
        return

    for timeframe_name in ("H4", "H1", "M15"):
        tf_result = timeframes.get(timeframe_name, {})
        inspect_timeframe(timeframe_name, tf_result)

    zone_result = validate_smc_zones(
        symbol,
        mtf_result
    )

    inspect_zones(zone_result)


def main():
    print_section("NEXORA FX - EVENT TIMELINE DIAGNOSTIC")

    connected = connect()

    if not connected:
        print(
            "Could not connect to MT5. "
            "Ensure MT5 is open and logged in."
        )
        return

    try:
        for symbol in SYMBOLS:
            try:
                inspect_symbol(symbol)

            except Exception as error:
                print_section(f"ERROR: {symbol}")
                print(type(error).__name__, str(error))

    finally:
        disconnect()

    print_section("DIAGNOSTIC FINISHED")
    print("No orders were sent.")
    print("Automated trading execution remains disabled.")


if __name__ == "__main__":
    main()
