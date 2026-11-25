
import pandas as pd


def calculate_indicators(df):
    """
    Calculate technical indicators using historical OHLC data.
    Returns a new DataFrame with indicator columns.
    """

    if df is None or df.empty:
        return None

    df = df.copy()

    # Exponential Moving Averages
    df["EMA_20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["EMA_200"] = df["close"].ewm(span=200, adjust=False).mean()

    # Relative Strength Index (RSI 14)
    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss
    df["RSI_14"] = 100 - (100 / (1 + rs))

    # MACD
    ema_12 = df["close"].ewm(span=12, adjust=False).mean()
    ema_26 = df["close"].ewm(span=26, adjust=False).mean()

    df["MACD"] = ema_12 - ema_26
    df["MACD_SIGNAL"] = df["MACD"].ewm(
        span=9, adjust=False
    ).mean()
    df["MACD_HIST"] = df["MACD"] - df["MACD_SIGNAL"]

    # Average True Range (ATR 14)
    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["ATR_14"] = true_range.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    # Average Directional Index (ADX 14)
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = up_move.where(
        (up_move > down_move) & (up_move > 0), 0.0
    )

    minus_dm = down_move.where(
        (down_move > up_move) & (down_move > 0), 0.0
    )

    atr = df["ATR_14"]

    plus_di = 100 * plus_dm.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean() / atr

    minus_di = 100 * minus_dm.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean() / atr

    dx = (
        100 * (plus_di - minus_di).abs()
        / (plus_di + minus_di)
    )

    df["ADX_14"] = dx.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    # Bollinger Bands (20-period, 2 standard deviations)
    middle_band = df["close"].rolling(20).mean()
    std_dev = df["close"].rolling(20).std()

    df["BB_MIDDLE"] = middle_band
    df["BB_UPPER"] = middle_band + (2 * std_dev)
    df["BB_LOWER"] = middle_band - (2 * std_dev)

    return df
