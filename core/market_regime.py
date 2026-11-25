import pandas as pd


def detect_market_regime(
    data,
    adx_trend_threshold=25,
    volatility_lookback=50,
):
    """
    Classify market conditions using technical indicators.

    Expected columns:
    EMA_20, EMA_50, EMA_200, ADX_14, ATR_14
    """

    required_columns = [
        "EMA_20",
        "EMA_50",
        "EMA_200",
        "ADX_14",
        "ATR_14",
    ]

    if data is None or data.empty:
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": "No market data available.",
        }

    missing = [
        column for column in required_columns
        if column not in data.columns
    ]

    if missing:
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": f"Missing indicators: {missing}",
        }

    # Use the latest completed candle.
    latest = data.iloc[-1]

    values = latest[required_columns]

    if values.isna().any():
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": "Latest candle has incomplete indicators.",
        }

    ema20 = float(latest["EMA_20"])
    ema50 = float(latest["EMA_50"])
    ema200 = float(latest["EMA_200"])
    adx = float(latest["ADX_14"])
    atr = float(latest["ATR_14"])

    if atr <= 0 or adx < 0:
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": "Invalid indicator values.",
        }

    # Calculate the recent ATR baseline.
    atr_history = (
        data["ATR_14"]
        .dropna()
        .tail(volatility_lookback)
    )

    if len(atr_history) < volatility_lookback:
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": "Not enough ATR history.",
        }

    average_atr = float(atr_history.mean())

    if average_atr <= 0:
        return {
            "regime": "INSUFFICIENT_DATA",
            "reason": "Invalid average ATR.",
        }

    volatility_ratio = atr / average_atr

    # Identify unusually high volatility.
    if volatility_ratio >= 1.5:
        regime = "HIGH_VOLATILITY"
        reason = "Current ATR is significantly above its baseline."

    # Identify bullish trend.
    elif (
        adx >= adx_trend_threshold
        and ema20 > ema50 > ema200
    ):
        regime = "BULLISH_TREND"
        reason = "Strong trend with bullish EMA alignment."

    # Identify bearish trend.
    elif (
        adx >= adx_trend_threshold
        and ema20 < ema50 < ema200
    ):
        regime = "BEARISH_TREND"
        reason = "Strong trend with bearish EMA alignment."

    # Otherwise classify as ranging or sideways.
    elif adx >= adx_trend_threshold:
        regime = "MIXED_TREND"
        reason = "Trend strength is elevated, but EMA alignment is mixed."

    else:
        regime = "SIDEWAYS"
        reason = "Trend strength is below the directional threshold."

    return {
        "regime": regime,
        "reason": reason,
        "adx": round(adx, 2),
        "atr": round(atr, 6),
        "average_atr": round(average_atr, 6),
        "volatility_ratio": round(volatility_ratio, 2),
        "ema_alignment": (
            "BULLISH"
            if ema20 > ema50 > ema200
            else "BEARISH"
            if ema20 < ema50 < ema200
            else "MIXED"
        ),
    }

