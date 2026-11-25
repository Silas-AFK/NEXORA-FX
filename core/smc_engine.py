from core.market_structure import detect_market_structure
from core.liquidity import detect_liquidity
from core.order_blocks import detect_order_blocks
from core.fair_value_gaps import detect_fair_value_gaps


def analyze_smc(data):
    """
    Run all Smart Money Concepts research detectors
    against the same completed-candle dataset.

    This module does not generate or execute trade orders.
    """

    if data is None or data.empty:
        return {
            "valid": False,
            "reason": "No market data available."
        }

    results = {}

    detectors = {
        "market_structure": detect_market_structure,
        "liquidity": detect_liquidity,
        "order_blocks": detect_order_blocks,
        "fair_value_gaps": detect_fair_value_gaps,
    }

    for name, detector in detectors.items():
        try:
            results[name] = detector(data)
        except Exception as error:
            results[name] = {
                "valid": False,
                "reason": f"Detector error: {error}"
            }

    valid_count = sum(
        result.get("valid", False)
        for result in results.values()
    )

    return {
        "valid": valid_count == len(detectors),
        "components_valid": valid_count,
        "components_total": len(detectors),
        "analysis": results,
        "execution_permitted": False,
        "reason": (
            "SMC analysis completed."
            if valid_count == len(detectors)
            else "One or more SMC components failed."
        )
    }
