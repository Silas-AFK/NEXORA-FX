import pandas as pd


def detect_order_blocks(
    data,
    atr_period=14,
    displacement_multiplier=1.5,
    structure_lookback=10,
    search_candles=5,
    max_blocks=10
):
    """
    Detect candidate SMC order blocks using displacement and
    a close beyond a preceding local high or low.

    Input must contain completed candles in chronological order.
    This is a research heuristic, not a definitive institutional
    order-flow detector.
    """

    required = ["open", "high", "low", "close"]

    if data is None or data.empty:
        return {"valid": False, "reason": "No market data available."}

    missing = [col for col in required if col not in data.columns]
    if missing:
        return {"valid": False, "reason": f"Missing columns: {missing}"}

    if atr_period < 2 or structure_lookback < 2 or search_candles < 1:
        return {"valid": False, "reason": "Invalid detection parameters."}

    if displacement_multiplier <= 0 or max_blocks < 1:
        return {"valid": False, "reason": "Invalid displacement or block settings."}

    df = data.copy().reset_index(drop=True)

    for column in required:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    if df[required].isna().any().any():
        return {"valid": False, "reason": "OHLC data contains invalid values."}

    if (
        (df["high"] < df["low"]).any()
        or (df["open"] > df["high"]).any()
        or (df["open"] < df["low"]).any()
        or (df["close"] > df["high"]).any()
        or (df["close"] < df["low"]).any()
    ):
        return {"valid": False, "reason": "Invalid OHLC candle values."}

    minimum = max(
        atr_period + 2,
        structure_lookback + 2,
        search_candles + 2
    )

    if len(df) < minimum:
        return {"valid": False, "reason": "Not enough candles for detection."}

    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = true_range.rolling(
        window=atr_period,
        min_periods=atr_period
    ).mean()

    # Store one strongest displacement per formation candle and direction.
    candidates = {}

    for i in range(max(atr_period, structure_lookback), len(df)):
        current = df.iloc[i]
        current_atr = atr.iloc[i]

        if pd.isna(current_atr) or current_atr <= 0:
            continue

        body = abs(float(current["close"]) - float(current["open"]))
        strength = body / float(current_atr)

        if strength < displacement_multiplier:
            continue

        previous = df.iloc[i - structure_lookback:i]

        bullish_break = (
            current["close"] > previous["high"].max()
            and current["close"] > current["open"]
        )

        bearish_break = (
            current["close"] < previous["low"].min()
            and current["close"] < current["open"]
        )

        if not bullish_break and not bearish_break:
            continue

        direction = "BULLISH" if bullish_break else "BEARISH"

        search_start = max(0, i - search_candles)
        block_index = None

        for j in range(i - 1, search_start - 1, -1):
            candle = df.iloc[j]

            if direction == "BULLISH" and candle["close"] < candle["open"]:
                block_index = j
                break

            if direction == "BEARISH" and candle["close"] > candle["open"]:
                block_index = j
                break

        if block_index is None:
            continue

        key = (direction, block_index)

        candidate = {
            "direction": direction,
            "block_index": block_index,
            "displacement_index": i,
            "displacement_body": body,
            "atr_at_displacement": float(current_atr),
            "strength": strength
        }

        existing = candidates.get(key)

        if existing is None or strength > existing["strength"]:
            candidates[key] = candidate

    blocks = []

    for candidate in candidates.values():
        direction = candidate["direction"]
        block_index = candidate["block_index"]
        displacement_index = candidate["displacement_index"]

        block = df.iloc[block_index]
        zone_high = float(block["high"])
        zone_low = float(block["low"])

        # Only candles after displacement can mitigate or invalidate the zone.
        later = df.iloc[displacement_index + 1:]

        mitigation_index = None
        invalidation_index = None

        for row_index, candle in later.iterrows():

            touched = (
                candle["low"] <= zone_high
                and candle["high"] >= zone_low
            )

            if touched and mitigation_index is None:
                mitigation_index = int(row_index)

            if direction == "BULLISH":
                invalidated = candle["close"] < zone_low
            else:
                invalidated = candle["close"] > zone_high

            if invalidated and invalidation_index is None:
                invalidation_index = int(row_index)

        if invalidation_index is not None:
            status = "INVALIDATED"
        elif mitigation_index is not None:
            status = "MITIGATED"
        else:
            status = "UNMITIGATED"

        formation_time = None
        displacement_time = None
        mitigation_time = None
        invalidation_time = None

        if "time" in df.columns:
            formation_time = str(df.iloc[block_index]["time"])
            displacement_time = str(df.iloc[displacement_index]["time"])

            if mitigation_index is not None:
                mitigation_time = str(df.iloc[mitigation_index]["time"])

            if invalidation_index is not None:
                invalidation_time = str(df.iloc[invalidation_index]["time"])

        blocks.append({
            "direction": direction,
            "zone_high": round(zone_high, 8),
            "zone_low": round(zone_low, 8),
            "formation_index": block_index,
            "displacement_index": displacement_index,
            "formation_time": formation_time,
            "displacement_time": displacement_time,
            "displacement_body": round(candidate["displacement_body"], 8),
            "atr_at_displacement": round(candidate["atr_at_displacement"], 8),
            "displacement_strength": round(candidate["strength"], 2),
            "age_bars": len(df) - 1 - displacement_index,
            "status": status,
            "mitigation_index": mitigation_index,
            "mitigation_time": mitigation_time,
            "invalidation_index": invalidation_index,
            "invalidation_time": invalidation_time
        })

    # Most recent displacement candidates first.
    blocks.sort(
        key=lambda item: item["displacement_index"],
        reverse=True
    )

    blocks = blocks[:max_blocks]

    return {
        "valid": True,
        "order_blocks": blocks,
        "count": len(blocks),
        "bullish_count": sum(
            item["direction"] == "BULLISH" for item in blocks
        ),
        "bearish_count": sum(
            item["direction"] == "BEARISH" for item in blocks
        ),
        "reason": (
            "Unique candidate order blocks detected."
            if blocks else "No qualifying order blocks detected."
        )
    }
