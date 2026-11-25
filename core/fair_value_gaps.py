import pandas as pd


def detect_fair_value_gaps(
    data,
    atr_period=14,
    min_gap_atr=0.05,
    max_gaps=20
):
    """
    Detect three-candle Fair Value Gaps.

    Bullish FVG:
        Candle 3 low > Candle 1 high

    Bearish FVG:
        Candle 3 high < Candle 1 low

    Input must contain completed candles in chronological order.
    """

    required = ["open", "high", "low", "close"]

    if data is None or data.empty:
        return {"valid": False, "reason": "No market data available."}

    missing = [col for col in required if col not in data.columns]
    if missing:
        return {"valid": False, "reason": f"Missing columns: {missing}"}

    if atr_period < 2 or min_gap_atr < 0 or max_gaps < 1:
        return {"valid": False, "reason": "Invalid detection parameters."}

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

    if len(df) < atr_period + 2:
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

    gaps = []

    # i is the third candle of the three-candle pattern.
    for i in range(2, len(df)):

        current_atr = atr.iloc[i]

        if pd.isna(current_atr) or current_atr <= 0:
            continue

        first = df.iloc[i - 2]
        third = df.iloc[i]

        direction = None
        zone_high = None
        zone_low = None

        # Bullish imbalance.
        if third["low"] > first["high"]:
            direction = "BULLISH"
            zone_high = float(third["low"])
            zone_low = float(first["high"])

        # Bearish imbalance.
        elif third["high"] < first["low"]:
            direction = "BEARISH"
            zone_high = float(first["low"])
            zone_low = float(third["high"])

        if direction is None:
            continue

        gap_size = zone_high - zone_low
        gap_strength = gap_size / float(current_atr)

        if gap_strength < min_gap_atr:
            continue

        first_touch_index = None
        full_fill_index = None
        invalidation_index = None

        # Evaluate only candles after the FVG has formed.
        later = df.iloc[i + 1:]

        for row_index, candle in later.iterrows():

            if direction == "BULLISH":
                touched = candle["low"] <= zone_high
                fully_filled = candle["low"] <= zone_low
                invalidated = candle["close"] < zone_low

            else:
                touched = candle["high"] >= zone_low
                fully_filled = candle["high"] >= zone_high
                invalidated = candle["close"] > zone_high

            if touched and first_touch_index is None:
                first_touch_index = int(row_index)

            if fully_filled and full_fill_index is None:
                full_fill_index = int(row_index)

            if invalidated and invalidation_index is None:
                invalidation_index = int(row_index)

        if invalidation_index is not None:
            status = "INVALIDATED"
        elif full_fill_index is not None:
            status = "FULLY_FILLED"
        elif first_touch_index is not None:
            status = "PARTIALLY_FILLED"
        else:
            status = "OPEN"

        formation_time = None
        first_touch_time = None
        full_fill_time = None
        invalidation_time = None

        if "time" in df.columns:
            formation_time = str(df.iloc[i]["time"])

            if first_touch_index is not None:
                first_touch_time = str(df.iloc[first_touch_index]["time"])

            if full_fill_index is not None:
                full_fill_time = str(df.iloc[full_fill_index]["time"])

            if invalidation_index is not None:
                invalidation_time = str(df.iloc[invalidation_index]["time"])

        gaps.append({
            "direction": direction,
            "zone_high": round(zone_high, 8),
            "zone_low": round(zone_low, 8),
            "gap_size": round(gap_size, 8),
            "atr_at_formation": round(float(current_atr), 8),
            "gap_strength": round(gap_strength, 2),
            "formation_index": i,
            "formation_time": formation_time,
            "age_bars": len(df) - 1 - i,
            "status": status,
            "first_touch_index": first_touch_index,
            "first_touch_time": first_touch_time,
            "full_fill_index": full_fill_index,
            "full_fill_time": full_fill_time,
            "invalidation_index": invalidation_index,
            "invalidation_time": invalidation_time
        })

    # Newest gaps first.
    gaps.sort(
        key=lambda item: item["formation_index"],
        reverse=True
    )

    gaps = gaps[:max_gaps]

    return {
        "valid": True,
        "fair_value_gaps": gaps,
        "count": len(gaps),
        "bullish_count": sum(
            item["direction"] == "BULLISH" for item in gaps
        ),
        "bearish_count": sum(
            item["direction"] == "BEARISH" for item in gaps
        ),
        "open_count": sum(item["status"] == "OPEN" for item in gaps),
        "reason": (
            "Fair Value Gaps detected."
            if gaps else "No qualifying Fair Value Gaps detected."
        )
    }
