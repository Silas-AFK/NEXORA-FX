
import sys
from pathlib import Path

# Add the ForexAI project root to Python's import path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.multi_timeframe_smc import analyze_multi_timeframe_smc
from core.zone_validator import validate_smc_zones
from execution.mt5_connection import MT5ConnectionError, mt5_connection


def print_zone_breakdown(result):
    """Print zone states and rejection reasons."""

    print("\nZONE STATE BREAKDOWN:")

    states = {}

    for zone in result.get("all_zones", []):
        state = zone.get("zone_state", "UNKNOWN")
        states[state] = states.get(state, 0) + 1

    if states:
        for state, count in sorted(states.items()):
            print(f"{state}: {count}")
    else:
        print("No zone states available.")

    print("\nREJECTION REASONS:")

    reasons = {}

    for zone in result.get("all_zones", []):
        for reason in zone.get("rejection_reasons", []):
            reasons[reason] = reasons.get(reason, 0) + 1

    if reasons:
        for reason, count in sorted(reasons.items()):
            print(f"{reason}: {count}")
    else:
        print("No rejection reasons recorded.")


def print_retest_zones(result):
    """Print zones currently classified as retests."""

    retest_zones = result.get("retest_zones", [])

    if not retest_zones:
        print("\nNo retest zones found.")
        return

    print("\nRETEST ZONES:")

    for zone in retest_zones:
        print({
            "timeframe": zone.get("timeframe"),
            "type": zone.get("zone_type"),
            "direction": zone.get("direction"),
            "status": zone.get("status"),
            "zone_state": zone.get("zone_state"),
            "distance_atr": zone.get("distance_atr"),
            "age_bars": zone.get("age_bars"),
            "rejection_reasons": zone.get("rejection_reasons", []),
        })


def _run_analysis():
    with mt5_connection():
        pairs = ["EURUSD", "GBPUSD", "USDJPY"]

        for symbol in pairs:
            print("\n" + "=" * 50)
            print("SYMBOL:", symbol)
            print("=" * 50)

            mtf = analyze_multi_timeframe_smc(symbol)

            if not mtf.get("valid"):
                print("Multi-timeframe analysis failed.")
                print("Reason:", mtf.get("reason"))
                continue

            result = validate_smc_zones(symbol, mtf)

            print("\nVALIDATION SUMMARY:")
            print("Valid:", result.get("valid"))
            print("Market bias:", result.get("market_bias"))
            print("Zones examined:", result.get("zones_examined"))
            print("Eligible:", result.get("eligible_count"))
            print("Approaching:", result.get("approaching_count"))
            print("In retest:", result.get("retest_count"))
            print("Timeframe errors:", result.get("timeframe_errors"))
            print("Execution permitted:", result.get("execution_permitted"))

            print_zone_breakdown(result)
            print_retest_zones(result)


def main():
    try:
        _run_analysis()
    except MT5ConnectionError as error:
        print(f"MT5 connection unavailable: {error}")


if __name__ == "__main__":
    main()
