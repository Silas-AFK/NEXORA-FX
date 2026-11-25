
import pandas as pd


def detect_market_structure(data, swing_length=3):
    """
    Detect confirmed swing points and close-confirmed BOS / CHoCH events.

    Important:
    - Only completed candles should be supplied.
    - A pivot becomes available only after its right-side confirmation candles.
    - Event indices refer to the supplied DataFrame after resetting its index.
    - Event timestamps are included when a time column or DatetimeIndex exists.
    """

    required = ["high", "low", "close"]

    # ---------------------------------------------------------
    # 1. INPUT VALIDATION
    # ---------------------------------------------------------

    if data is None or not isinstance(data, pd.DataFrame) or data.empty:
        return {
            "valid": False,
            "reason": "No market data available."
        }

    missing = [col for col in required if col not in data.columns]

    if missing:
        return {
            "valid": False,
            "reason": f"Missing columns: {missing}"
        }

    if (
        not isinstance(swing_length, int)
        or isinstance(swing_length, bool)
        or swing_length < 1
    ):
        return {
            "valid": False,
            "reason": "Swing length must be a positive integer."
        }

    # Preserve timestamps before resetting the index.
    if "time" in data.columns:
        raw_timestamps = data["time"].tolist()
    elif "timestamp" in data.columns:
        raw_timestamps = data["timestamp"].tolist()
    elif isinstance(data.index, pd.DatetimeIndex):
        raw_timestamps = data.index.tolist()
    else:
        raw_timestamps = [None] * len(data)

    df = data.copy().reset_index(drop=True)

    for column in required:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    if df[required].isna().any().any():
        return {
            "valid": False,
            "reason": "OHLC data contains missing or invalid values."
        }

    # Reject infinite values.
    for column in required:
        if not df[column].map(lambda value: abs(value) != float("inf")).all():
            return {
                "valid": False,
                "reason": "OHLC data contains infinite values."
            }

    invalid_ohlc = (
        (df["high"] < df["low"])
        | (df["high"] < df["close"])
        | (df["low"] > df["close"])
    )

    if invalid_ohlc.any():
        return {
            "valid": False,
            "reason": "Invalid OHLC data."
        }

    if len(df) < (2 * swing_length + 5):
        return {
            "valid": False,
            "reason": "Not enough candles to detect market structure."
        }

    def get_timestamp(index):
        """Return a timestamp as a readable string when available."""

        if index < 0 or index >= len(raw_timestamps):
            return None

        value = raw_timestamps[index]

        if value is None:
            return None

        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass

        try:
            return str(pd.Timestamp(value))
        except (TypeError, ValueError):
            return str(value)

    # ---------------------------------------------------------
    # 2. DETECT CONFIRMED SWING POINTS
    # ---------------------------------------------------------

    confirmed_pivots = []

    for i in range(swing_length, len(df) - swing_length):

        left = df.iloc[i - swing_length:i]
        right = df.iloc[i + 1:i + swing_length + 1]

        high = float(df.iloc[i]["high"])
        low = float(df.iloc[i]["low"])

        # Confirmed swing high.
        if (
            high > left["high"].max()
            and high > right["high"].max()
        ):
            confirmed_pivots.append({
                "type": "HIGH",
                "index": i,
                "confirmed_at": i + swing_length,
                "price": high,
                "timestamp": get_timestamp(i),
                "confirmation_timestamp": get_timestamp(i + swing_length)
            })

        # Confirmed swing low.
        if (
            low < left["low"].min()
            and low < right["low"].min()
        ):
            confirmed_pivots.append({
                "type": "LOW",
                "index": i,
                "confirmed_at": i + swing_length,
                "price": low,
                "timestamp": get_timestamp(i),
                "confirmation_timestamp": get_timestamp(i + swing_length)
            })

    confirmed_pivots.sort(
        key=lambda item: (
            item["confirmed_at"],
            item["index"]
        )
    )

    # ---------------------------------------------------------
    # 3. STRUCTURE TRACKING
    # ---------------------------------------------------------

    swing_highs = []
    swing_lows = []

    active_high = None
    active_low = None

    event_history = []

    def get_bias():

        if len(swing_highs) < 2 or len(swing_lows) < 2:
            return "UNCONFIRMED"

        hh = (
            swing_highs[-1]["price"]
            > swing_highs[-2]["price"]
        )

        hl = (
            swing_lows[-1]["price"]
            > swing_lows[-2]["price"]
        )

        lh = (
            swing_highs[-1]["price"]
            < swing_highs[-2]["price"]
        )

        ll = (
            swing_lows[-1]["price"]
            < swing_lows[-2]["price"]
        )

        if hh and hl:
            return "BULLISH"

        if lh and ll:
            return "BEARISH"

        return "RANGING"

    pivot_cursor = 0

    # ---------------------------------------------------------
    # 4. PROCESS CANDLES CHRONOLOGICALLY
    # ---------------------------------------------------------

    for candle_index in range(len(df)):

        close = float(df.iloc[candle_index]["close"])

        previous_close = (
            float(df.iloc[candle_index - 1]["close"])
            if candle_index > 0
            else None
        )

        # ---------------------------------------------
        # Check bullish structural break.
        # ---------------------------------------------

        if previous_close is not None:

            if (
                active_high is not None
                and not active_high["broken"]
                and previous_close <= active_high["price"]
                and close > active_high["price"]
            ):

                bias_before_break = get_bias()

                event_type = (
                    "CHoCH"
                    if bias_before_break == "BEARISH"
                    else "BOS"
                )

                event_history.append({
                    "event": event_type,
                    "direction": "BULLISH",
                    "level": active_high["price"],
                    "index": candle_index,
                    "timestamp": get_timestamp(candle_index),
                    "bias_before_break": bias_before_break,
                    "broken_pivot_index": active_high["index"],
                    "pivot_confirmed_at": active_high["confirmed_at"],
                    "pivot_timestamp": active_high["timestamp"],
                    "pivot_confirmation_timestamp": (
                        active_high["confirmation_timestamp"]
                    )
                })

                active_high["broken"] = True

            # -----------------------------------------
            # Check bearish structural break.
            # -----------------------------------------

            if (
                active_low is not None
                and not active_low["broken"]
                and previous_close >= active_low["price"]
                and close < active_low["price"]
            ):

                bias_before_break = get_bias()

                event_type = (
                    "CHoCH"
                    if bias_before_break == "BULLISH"
                    else "BOS"
                )

                event_history.append({
                    "event": event_type,
                    "direction": "BEARISH",
                    "level": active_low["price"],
                    "index": candle_index,
                    "timestamp": get_timestamp(candle_index),
                    "bias_before_break": bias_before_break,
                    "broken_pivot_index": active_low["index"],
                    "pivot_confirmed_at": active_low["confirmed_at"],
                    "pivot_timestamp": active_low["timestamp"],
                    "pivot_confirmation_timestamp": (
                        active_low["confirmation_timestamp"]
                    )
                })

                active_low["broken"] = True

        # ---------------------------------------------
        # Activate newly confirmed pivots.
        #
        # Pivots are NOT active before confirmed_at.
        # ---------------------------------------------

        while (
            pivot_cursor < len(confirmed_pivots)
            and confirmed_pivots[pivot_cursor]["confirmed_at"]
            == candle_index
        ):

            pivot = confirmed_pivots[pivot_cursor]

            if pivot["type"] == "HIGH":

                swing_highs.append(pivot)

                active_high = {
                    "price": pivot["price"],
                    "index": pivot["index"],
                    "confirmed_at": pivot["confirmed_at"],
                    "timestamp": pivot["timestamp"],
                    "confirmation_timestamp": (
                        pivot["confirmation_timestamp"]
                    ),
                    "broken": close > pivot["price"]
                }

            else:

                swing_lows.append(pivot)

                active_low = {
                    "price": pivot["price"],
                    "index": pivot["index"],
                    "confirmed_at": pivot["confirmed_at"],
                    "timestamp": pivot["timestamp"],
                    "confirmation_timestamp": (
                        pivot["confirmation_timestamp"]
                    ),
                    "broken": close < pivot["price"]
                }

            pivot_cursor += 1

    # ---------------------------------------------------------
    # 5. VALIDATE SWING COUNTS
    # ---------------------------------------------------------

    if len(swing_highs) < 2 or len(swing_lows) < 2:

        return {
            "valid": False,
            "reason": "Not enough confirmed swing highs and lows.",
            "swing_highs": len(swing_highs),
            "swing_lows": len(swing_lows),
            "confirmed_pivots": confirmed_pivots,
            "event_history": event_history
        }

    # ---------------------------------------------------------
    # 6. CALCULATE STRUCTURAL DIRECTION
    # ---------------------------------------------------------

    previous_high, latest_high = swing_highs[-2:]
    previous_low, latest_low = swing_lows[-2:]

    higher_high = latest_high["price"] > previous_high["price"]
    higher_low = latest_low["price"] > previous_low["price"]

    lower_high = latest_high["price"] < previous_high["price"]
    lower_low = latest_low["price"] < previous_low["price"]

    trend = get_bias()

    # ---------------------------------------------------------
    # 7. GET LATEST STRUCTURAL EVENT
    # ---------------------------------------------------------

    latest_event = (
        event_history[-1]
        if event_history
        else None
    )

    if latest_event:

        event = latest_event["event"]
        event_direction = latest_event["direction"]
        broken_level = latest_event["level"]

        event_age_bars = (
            len(df) - 1 - latest_event["index"]
        )

        event_bias = latest_event["bias_before_break"]

        reason = (
            f"{event} confirmed by candle close in "
            f"{event_direction} direction."
        )

    else:

        event = "NONE"
        event_direction = None
        broken_level = None
        event_age_bars = None
        event_bias = None

        reason = (
            "No confirmed structural break detected "
            "in the available data."
        )

    # ---------------------------------------------------------
    # 8. RETURN RESULTS
    # ---------------------------------------------------------

    return {
        "valid": True,

        "trend": trend,

        "event": event,
        "event_direction": event_direction,
        "broken_level": broken_level,
        "event_age_bars": event_age_bars,
        "event_bias_before_break": event_bias,

        "event_history": event_history,

        "confirmed_pivots": confirmed_pivots,

        "higher_high": higher_high,
        "higher_low": higher_low,
        "lower_high": lower_high,
        "lower_low": lower_low,

        "latest_swing_high": latest_high["price"],
        "latest_swing_low": latest_low["price"],

        "previous_swing_high": previous_high["price"],
        "previous_swing_low": previous_low["price"],

        "latest_swing_high_index": latest_high["index"],
        "latest_swing_low_index": latest_low["index"],

        "latest_swing_high_confirmed_at": (
            latest_high["confirmed_at"]
        ),
        "latest_swing_low_confirmed_at": (
            latest_low["confirmed_at"]
        ),

        "swing_high_count": len(swing_highs),
        "swing_low_count": len(swing_lows),

        "event_count": len(event_history),

        "reason": reason
    }
