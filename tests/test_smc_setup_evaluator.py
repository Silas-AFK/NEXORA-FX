import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from core.smc_setup_evaluator import (
    _summarize_unique_levels,
    _get_liquidity_sweep,
)


def test_unique_liquidity_levels():
    sweeps = [
        {
            "unique_level_id": "SELL_SIDE_SWEEP_1",
            "type": "SELL_SIDE_SWEEP",
            "level": 1.1000,
            "repeat_count": 1,
            "sweep_classification": "FIRST_OBSERVED_SWEEP",
            "bars_ago": 5,
        },
        {
            "unique_level_id": "SELL_SIDE_SWEEP_1",
            "type": "SELL_SIDE_SWEEP",
            "level": 1.1000,
            "repeat_count": 2,
            "sweep_classification": "REPEATED_SWEEP",
            "bars_ago": 2,
        },
        {
            "unique_level_id": "BUY_SIDE_SWEEP_2",
            "type": "BUY_SIDE_SWEEP",
            "level": 1.1050,
            "repeat_count": 1,
            "sweep_classification": "FIRST_OBSERVED_SWEEP",
            "bars_ago": 1,
        },
    ]

    levels = _summarize_unique_levels(sweeps)

    assert len(levels) == 2
    assert levels[0]["unique_level_id"] == "BUY_SIDE_SWEEP_2"

    sell_level = next(
        level for level in levels
        if level["unique_level_id"] == "SELL_SIDE_SWEEP_1"
    )

    assert sell_level["event_count"] == 2
    assert sell_level["repeat_count"] == 2
    assert sell_level["repeated"] is True

    print("PASS: Repeated events are grouped into unique levels.")


def test_bullish_liquidity_sweep():
    timeframe_result = {
        "analysis": {
            "liquidity": {
                "valid": True,
                "recent_sweeps": [
                    {
                        "unique_level_id": "SELL_SIDE_SWEEP_1",
                        "type": "SELL_SIDE_SWEEP",
                        "level": 1.1000,
                        "repeat_count": 2,
                        "sweep_classification": "REPEATED_SWEEP",
                        "fresh": True,
                        "bars_ago": 1,
                    },
                    {
                        "unique_level_id": "BUY_SIDE_SWEEP_2",
                        "type": "BUY_SIDE_SWEEP",
                        "level": 1.1050,
                        "repeat_count": 1,
                        "sweep_classification": "FIRST_OBSERVED_SWEEP",
                        "fresh": True,
                        "bars_ago": 0,
                    },
                ],
            }
        }
    }

    result = _get_liquidity_sweep(
        timeframe_result,
        bias="BULLISH"
    )

    assert result["valid"] is True
    assert result["supports_bias"] is True
    assert result["matching_unique_level_count"] == 1
    assert result["opposing_unique_level_count"] == 1
    assert result["matching_repeated_level_count"] == 1
    assert result["opposing_repeated_level_count"] == 0

    print("PASS: Bullish liquidity classification works.")


if __name__ == "__main__":
    test_unique_liquidity_levels()
    test_bullish_liquidity_sweep()

    print("\nAll SMC liquidity tests passed.")
