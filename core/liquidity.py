
import math
import pandas as pd


def _confirmed_pivots(data, swing_length):
    """
    Identify confirmed swing highs and swing lows.

    A pivot is confirmed only after the required
    candles to its right have closed.
    """
    highs = []
    lows = []

    for i in range(swing_length, len(data) - swing_length):
        left = data.iloc[i - swing_length:i]
        right = data.iloc[i + 1:i + swing_length + 1]

        high = float(data.iloc[i]["high"])
        low = float(data.iloc[i]["low"])

        if high > left["high"].max() and high > right["high"].max():
            highs.append({
                "index": i,
                "price": high
            })

        if low < left["low"].min() and low < right["low"].min():
            lows.append({
                "index": i,
                "price": low
            })

    return highs, lows


def _cluster_levels(pivots, tolerance, min_touches):
    """
    Group nearby confirmed pivots into potential
    buy-side and sell-side liquidity pools.
    """
    if not pivots:
        return []

    ordered = sorted(pivots, key=lambda item: item["price"])

    clusters = []
    current = [ordered[0]]

    for pivot in ordered[1:]:
        if pivot["price"] - current[0]["price"] <= tolerance:
            current.append(pivot)
        else:
            clusters.append(current)
            current = [pivot]

    clusters.append(current)

    levels = []

    for cluster in clusters:
        if len(cluster) < min_touches:
            continue

        prices = [item["price"] for item in cluster]
        indices = [item["index"] for item in cluster]

        levels.append({
            "price": round(
                float(pd.Series(prices).median()), 8
            ),
            "touches": len(cluster),
            "first_index": min(indices),
            "last_index": max(indices),
        })

    return sorted(
        levels,
        key=lambda item: item["last_index"],
        reverse=True
    )


def _get_candle_timestamp(data, index):
    """Return a candle timestamp in ISO format."""

    if "time" not in data.columns:
        return None

    value = data.iloc[index]["time"]

    if pd.isna(value):
        return None

    try:
        return pd.Timestamp(value).isoformat()
    except (TypeError, ValueError):
        return str(value)


def _detect_sweeps_for_candle(
    candle,
    buy_side_levels,
    sell_side_levels,
    sweep_buffer
):
    """
    Detect potential liquidity sweeps on one candle.

    Buy-side sweep:
        Price moves above a buy-side liquidity level
        and closes below that level.

    Sell-side sweep:
        Price moves below a sell-side liquidity level
        and closes above that level.
    """

    candle_high = float(candle["high"])
    candle_low = float(candle["low"])
    candle_close = float(candle["close"])

    sweeps = []

    for level in buy_side_levels:
        if (
            candle_high > level["price"] + sweep_buffer
            and candle_close < level["price"]
        ):
            sweeps.append({
                "type": "BUY_SIDE_SWEEP",
                "level": level["price"],
                "candle_close": candle_close,
                "pool_touches": level["touches"],
            })

    for level in sell_side_levels:
        if (
            candle_low < level["price"] - sweep_buffer
            and candle_close > level["price"]
        ):
            sweeps.append({
                "type": "SELL_SIDE_SWEEP",
                "level": level["price"],
                "candle_close": candle_close,
                "pool_touches": level["touches"],
            })

    return sweeps


def _classify_repeated_sweeps(historical_sweeps, tolerance):
    """
    Group historical sweeps by direction and approximate price.

    FIRST_OBSERVED_SWEEP:
        First interaction with a grouped level in the
        available historical scan.

    REPEATED_SWEEP:
        Another interaction with an already observed level.

    Events are never deleted.

    Important:
        FIRST_OBSERVED_SWEEP does not guarantee that the
        level has never been swept before the scan began.
    """

    unique_levels = []

    for event in historical_sweeps:

        event_type = event["type"]
        event_level = float(event["level"])

        candidates = [
            level
            for level in unique_levels
            if (
                level["type"] == event_type
                and abs(
                    level["anchor_price"] - event_level
                ) <= tolerance
            )
        ]

        if candidates:

            # Match the closest previously identified level.
            matched_level = min(
                candidates,
                key=lambda item: abs(
                    item["anchor_price"] - event_level
                )
            )

            matched_level["event_count"] += 1
            matched_level["last_sweep_timestamp"] = (
                event.get("timestamp")
            )
            matched_level["last_candle_index"] = (
                event.get("candle_index")
            )
            matched_level["last_data_index"] = (
                event.get("data_index")
            )

            event["sweep_classification"] = "REPEATED_SWEEP"
            event["unique_level_id"] = matched_level["id"]
            event["repeat_count"] = matched_level["event_count"]

            event["first_sweep_timestamp"] = (
                matched_level["first_sweep_timestamp"]
            )

        else:

            level_id = (
                f"{event_type}_{len(unique_levels) + 1}"
            )

            new_level = {
                "id": level_id,
                "type": event_type,
                "anchor_price": event_level,
                "first_sweep_timestamp": event.get("timestamp"),
                "last_sweep_timestamp": event.get("timestamp"),
                "first_candle_index": event.get("candle_index"),
                "last_candle_index": event.get("candle_index"),
                "first_data_index": event.get("data_index"),
                "last_data_index": event.get("data_index"),
                "event_count": 1,
            }

            unique_levels.append(new_level)

            event["sweep_classification"] = (
                "FIRST_OBSERVED_SWEEP"
            )
            event["unique_level_id"] = level_id
            event["repeat_count"] = 1

            event["first_sweep_timestamp"] = (
                new_level["first_sweep_timestamp"]
            )

    return unique_levels


def detect_liquidity(
    data,
    swing_length=3,
    tolerance=None,
    min_touches=2,
    lookback=300,
    history_bars=100,
    freshness_bars=8
):
    """
    NEXORA FX historical liquidity detection engine.

    Features:
        - Confirmed swing highs and swing lows.
        - Buy-side and sell-side liquidity pools.
        - Historical liquidity sweep detection.
        - First and repeated sweep classification.
        - Unique swept-level tracking.
        - Recent sweep freshness tracking.
        - Protection against historical look-ahead bias.
        - Original DataFrame index mapping through data_index.

    Only completed candles should be supplied.

    candle_index:
        Index inside the internally trimmed DataFrame.

    data_index:
        Corresponding positional index in the original input DataFrame.

    The latest-candle 'sweeps' field is retained for
    backward compatibility.
    """

    required = ["high", "low", "close"]

    # --------------------------------------------------
    # 1. INPUT VALIDATION
    # --------------------------------------------------

    if data is None or not isinstance(data, pd.DataFrame) or data.empty:
        return {
            "valid": False,
            "reason": "No market data available."
        }

    missing = [
        column for column in required
        if column not in data.columns
    ]

    if missing:
        return {
            "valid": False,
            "reason": f"Missing columns: {missing}"
        }

    if not isinstance(swing_length, int) or isinstance(swing_length, bool) or swing_length < 1:
        return {
            "valid": False,
            "reason": "Invalid swing length."
        }

    if not isinstance(min_touches, int) or isinstance(min_touches, bool) or min_touches < 2:
        return {
            "valid": False,
            "reason": "Minimum touches must be at least 2."
        }

    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < (2 * swing_length + 5):
        return {
            "valid": False,
            "reason": "Invalid lookback period."
        }

    if not isinstance(history_bars, int) or isinstance(history_bars, bool) or history_bars < 1:
        return {
            "valid": False,
            "reason": "Invalid history_bars value."
        }

    if not isinstance(freshness_bars, int) or isinstance(freshness_bars, bool) or freshness_bars < 0:
        return {
            "valid": False,
            "reason": "Invalid freshness_bars value."
        }

    # --------------------------------------------------
    # 2. TRIM DATA WHILE PRESERVING ORIGINAL INDEX OFFSET
    # --------------------------------------------------

    source_index_offset = len(data) - min(len(data), lookback)

    df = data.copy().tail(lookback).reset_index(drop=True)

    for column in required:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    if df[required].isna().any().any():
        return {
            "valid": False,
            "reason": "OHLC data contains invalid values."
        }

    if (
        (df["high"] < df["low"]).any()
        or (df["high"] < df["close"]).any()
        or (df["low"] > df["close"]).any()
    ):
        return {
            "valid": False,
            "reason": "Invalid OHLC data."
        }

    if len(df) < (2 * swing_length + 5):
        return {
            "valid": False,
            "reason": "Not enough candles."
        }

    # --------------------------------------------------
    # 3. LIQUIDITY TOLERANCE
    # --------------------------------------------------

    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    median_range = float(
        true_range.dropna().tail(100).median()
    )

    if not math.isfinite(median_range) or median_range <= 0:
        return {
            "valid": False,
            "reason": "Could not calculate valid candle range."
        }

    if tolerance is None:
        tolerance = median_range * 0.15

    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError):
        return {
            "valid": False,
            "reason": "Invalid liquidity tolerance."
        }

    if not math.isfinite(tolerance) or tolerance <= 0:
        return {
            "valid": False,
            "reason": "Liquidity tolerance must be positive."
        }

    sweep_buffer = tolerance * 0.1

    # --------------------------------------------------
    # 4. CURRENT LIQUIDITY POOLS
    # --------------------------------------------------

    swing_highs, swing_lows = _confirmed_pivots(
        df,
        swing_length
    )

    buy_side = _cluster_levels(
        swing_highs,
        tolerance,
        min_touches
    )

    sell_side = _cluster_levels(
        swing_lows,
        tolerance,
        min_touches
    )

    # --------------------------------------------------
    # 5. HISTORICAL SWEEP DETECTION
    # --------------------------------------------------

    historical_sweeps = []

    scan_start = max(
        2 * swing_length + 5,
        len(df) - history_bars
    )

    # Calculate confirmed pivots once.
    # A pivot at index p becomes available only after
    # its right-side confirmation candles have closed.
    all_highs, all_lows = _confirmed_pivots(
        df,
        swing_length
    )

    # Group pivots by the candle at which they become confirmed.
    confirmed_highs = {}
    confirmed_lows = {}

    for pivot in all_highs:
        confirmation_index = pivot["index"] + swing_length
        confirmed_highs.setdefault(
            confirmation_index, []
        ).append(pivot)

    for pivot in all_lows:
        confirmation_index = pivot["index"] + swing_length
        confirmed_lows.setdefault(
            confirmation_index, []
        ).append(pivot)

    available_highs = []
    available_lows = []

    for candle_index in range(len(df)):

        # Add pivots only when their confirmation candle
        # has already closed before the current candle.
        available_highs.extend(
            confirmed_highs.get(candle_index - 1, [])
        )

        available_lows.extend(
            confirmed_lows.get(candle_index - 1, [])
        )

        if candle_index < scan_start:
            continue

        prior_buy_side = _cluster_levels(
            available_highs,
            tolerance,
            min_touches
        )

        prior_sell_side = _cluster_levels(
            available_lows,
            tolerance,
            min_touches
        )

        candle = df.iloc[candle_index]

        candle_sweeps = _detect_sweeps_for_candle(
            candle,
            prior_buy_side,
            prior_sell_side,
            sweep_buffer
        )

        for sweep in candle_sweeps:

            event = dict(sweep)

            # Local index inside the trimmed DataFrame.
            event["candle_index"] = candle_index

            # Original positional index in the supplied DataFrame.
            event["data_index"] = (
                source_index_offset + candle_index
            )

            event["timestamp"] = _get_candle_timestamp(
                df,
                candle_index
            )

            event["bars_ago"] = (
                len(df) - 1 - candle_index
            )

            event["fresh"] = (
                event["bars_ago"] <= freshness_bars
            )

            historical_sweeps.append(event)

    # --------------------------------------------------
    # 6. FIRST AND REPEATED SWEEP CLASSIFICATION
    # --------------------------------------------------

    unique_swept_levels = _classify_repeated_sweeps(
        historical_sweeps,
        tolerance
    )

    first_sweep_count = sum(
        1
        for event in historical_sweeps
        if event["sweep_classification"] == "FIRST_OBSERVED_SWEEP"
    )

    repeated_sweep_count = sum(
        1
        for event in historical_sweeps
        if event["sweep_classification"] == "REPEATED_SWEEP"
    )

    # --------------------------------------------------
    # 7. LATEST AND RECENT SWEEPS
    # --------------------------------------------------

    latest_index = len(df) - 1

    sweeps = [
        event
        for event in historical_sweeps
        if event["candle_index"] == latest_index
    ]

    recent_sweeps = [
        event
        for event in historical_sweeps
        if event["fresh"]
    ]

    recent_unique_level_ids = {
        event["unique_level_id"]
        for event in recent_sweeps
    }

    recent_unique_swept_levels = [
        level
        for level in unique_swept_levels
        if level["id"] in recent_unique_level_ids
    ]

    # --------------------------------------------------
    # 8. FINAL RESULTS
    # --------------------------------------------------

    return {
        "valid": True,

        "tolerance": round(tolerance, 8),
        "sweep_buffer": round(sweep_buffer, 8),

        "source_index_offset": source_index_offset,
        "source_data_length": len(data),
        "analysed_data_length": len(df),

        "buy_side_liquidity": buy_side,
        "sell_side_liquidity": sell_side,

        "sweeps": sweeps,
        "historical_sweeps": historical_sweeps,
        "recent_sweeps": recent_sweeps,

        "unique_swept_levels": unique_swept_levels,
        "recent_unique_swept_levels": recent_unique_swept_levels,

        "historical_sweep_count": len(historical_sweeps),
        "recent_sweep_count": len(recent_sweeps),

        "unique_swept_level_count": len(unique_swept_levels),
        "recent_unique_swept_level_count": len(recent_unique_swept_levels),

        "first_sweep_count": first_sweep_count,
        "repeated_sweep_count": repeated_sweep_count,

        "history_bars": history_bars,
        "freshness_bars": freshness_bars,

        "buy_side_count": len(buy_side),
        "sell_side_count": len(sell_side),

        "reason": (
            "Potential liquidity sweep detected on latest candle."
            if sweeps
            else
            "No sweep on latest candle; historical events were analysed."
            if historical_sweeps
            else
            "No liquidity sweep detected in the historical scan."
        )
    }
