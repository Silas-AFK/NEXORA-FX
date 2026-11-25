
import MetaTrader5 as mt5

from execution.mt5_connection import (
    connect,
    get_account_info,
    disconnect,
)

from core.market_data import MarketDataError, get_historical_data
from core.indicators import calculate_indicators


def main():
    print("\nFOREX AI TRADING SYSTEM")
    print("=" * 45)

    if not connect():
        return

    try:
        account = get_account_info()

        if account:
            print("\nACCOUNT CONNECTED")
            print("Currency:", account["currency"])
            print("Balance:", account["balance"])
            print("Equity:", account["equity"])

        symbols = ["EURUSD", "GBPUSD", "USDJPY"]

        for symbol in symbols:
            print(f"\n{'=' * 45}")
            print(f"TECHNICAL ANALYSIS: {symbol}")
            print("=" * 45)

            # Retrieve 500 hourly candles
            try:
                data = get_historical_data(
                    symbol,
                    mt5.TIMEFRAME_H1,
                    bars=500,
                )
            except MarketDataError as error:
                print(f"Historical data unavailable: {error}")
                continue

            # Calculate indicators
            analysis = calculate_indicators(data)

            if analysis is None:
                print("Indicator calculation failed.")
                continue

            latest = analysis.iloc[-1]

            print("\nLATEST COMPLETED CANDLE")
            print("Time:", latest["time"])
            print("Close:", latest["close"])

            print("\nMOVING AVERAGES")
            print("EMA 20:", round(latest["EMA_20"], 5))
            print("EMA 50:", round(latest["EMA_50"], 5))
            print("EMA 200:", round(latest["EMA_200"], 5))

            print("\nMOMENTUM")
            print("RSI 14:", round(latest["RSI_14"], 2))
            print("MACD:", round(latest["MACD"], 6))
            print("MACD Signal:", round(latest["MACD_SIGNAL"], 6))

            print("\nVOLATILITY")
            print("ATR 14:", round(latest["ATR_14"], 6))
            print("ADX 14:", round(latest["ADX_14"], 2))

            print("\nBOLLINGER BANDS")
            print("Upper:", round(latest["BB_UPPER"], 5))
            print("Middle:", round(latest["BB_MIDDLE"], 5))
            print("Lower:", round(latest["BB_LOWER"], 5))

    finally:
        disconnect()


if __name__ == "__main__":
    main()
