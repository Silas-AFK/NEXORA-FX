import math

import pandas as pd


class MarketDataValidationError(ValueError):
    """Raised when market data cannot be safely used for analysis."""


OHLC_COLUMNS = ("open", "high", "low", "close")


def validate_ohlc_data(data, require_time=True):
    """Return a normalized candle frame or raise a clear validation error."""

    if not isinstance(data, pd.DataFrame) or data.empty:
        raise MarketDataValidationError("Candle data is empty.")

    missing = [column for column in OHLC_COLUMNS if column not in data.columns]
    if missing:
        raise MarketDataValidationError(
            "Candle data is missing required OHLC columns: "
            + ", ".join(missing)
        )

    result = data.copy()
    if require_time and "time" not in result.columns:
        raise MarketDataValidationError(
            "Candle data is missing the required time column."
        )

    for column in OHLC_COLUMNS:
        result[column] = pd.to_numeric(result[column], errors="coerce")

    for column in OHLC_COLUMNS:
        if result[column].isna().any():
            raise MarketDataValidationError(
                f"Candle column '{column}' contains missing or non-numeric values."
            )
        if not result[column].map(
            lambda value: math.isfinite(float(value))
        ).all():
            raise MarketDataValidationError(
                f"Candle column '{column}' contains non-finite prices."
            )
        if (result[column] <= 0).any():
            raise MarketDataValidationError(
                f"Candle column '{column}' contains non-positive prices."
            )

    invalid_candles = (
        (result["high"] < result["low"])
        | (result["high"] < result["open"])
        | (result["high"] < result["close"])
        | (result["low"] > result["open"])
        | (result["low"] > result["close"])
    )
    if invalid_candles.any():
        raise MarketDataValidationError(
            "Candle data contains invalid OHLC relationships."
        )

    if "spread" in result.columns:
        result["spread"] = pd.to_numeric(result["spread"], errors="coerce")
        if (
            result["spread"].isna().any()
            or not result["spread"].map(
                lambda value: math.isfinite(float(value))
            ).all()
            or (result["spread"] < 0).any()
        ):
            raise MarketDataValidationError(
                "Candle spread must contain finite, non-negative values."
            )

    if require_time:
        raw_time = result["time"]
        if pd.api.types.is_numeric_dtype(raw_time):
            parsed_time = pd.to_datetime(
                raw_time,
                unit="s",
                utc=True,
                errors="coerce",
            )
        else:
            parsed_time = pd.to_datetime(
                raw_time,
                utc=True,
                errors="coerce",
            )

        if parsed_time.isna().any():
            raise MarketDataValidationError(
                "Candle timestamps contain missing or invalid values."
            )
        result["time"] = parsed_time

        if result["time"].duplicated().any():
            raise MarketDataValidationError(
                "Candle data contains duplicate timestamps."
            )
        if not result["time"].is_monotonic_increasing:
            raise MarketDataValidationError(
                "Candle timestamps must be in chronological order."
            )

    return result.reset_index(drop=True)
