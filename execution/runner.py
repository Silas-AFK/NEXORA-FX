"""Separate-process scanner and trade supervisor."""

import argparse
import logging
import os
import sqlite3
import time

from config.market_settings import load_symbols
from core.signal_service import analyze_signal_for_symbol
from core.signal_store import record_signal
from core.market_data import MarketDataError
from execution.mt5_connection import MT5ConnectionError
from execution.operating_mode import OperatingMode
from execution.trading_engine import TradeRejected, TradingEngine
from execution.trading_store import RECONCILE_STATUSES


LOGGER = logging.getLogger("nexora.runner")


def scan_cycle(
    engine,
    symbols,
    scanner=analyze_signal_for_symbol,
    *,
    allow_execution=True,
):
    """Scan selected markets, persist observations, and gate confirmed entries."""

    engine.refresh_configuration()
    results = []
    if engine.config.mode != OperatingMode.RESEARCH:
        if engine.store.list_trades(statuses=RECONCILE_STATUSES):
            engine.reconcile_open_trades()
        results.extend(
            {
                "symbol": None,
                "classification": "POSITION_MANAGEMENT",
                **item,
            }
            for item in engine.manage_open_positions()
        )

    if engine.config.agent_enabled:
        if not symbols:
            raise ValueError("Select at least one instrument before scanning.")
        if (
            engine.config.mode != OperatingMode.RESEARCH
            and engine.config.execution_enabled
        ):
            engine.establish_available_baselines()
        for symbol in symbols:
            with engine.connection_factory():
                analysis = scanner(symbol)
            if not isinstance(analysis, dict):
                raise ValueError(
                    f"Signal service returned invalid data for {symbol}."
                )

            signal = analysis.get("signal_result")
            if not isinstance(signal, dict):
                results.append({
                    "symbol": symbol,
                    "classification": "WAIT",
                    "reason": analysis.get(
                        "reason",
                        "Signal analysis unavailable.",
                    ),
                })
                continue

            signal["signal_timestamp"] = analysis.get(
                "signal_timestamp",
                signal.get("signal_timestamp"),
            )
            observation = None
            if analysis.get("valid") and signal.get("signal_timestamp"):
                observation = record_signal(
                    symbol,
                    signal,
                    signal_timestamp=signal["signal_timestamp"],
                )
                signal["signal_id"] = observation["signal_id"]

            result = {
                "symbol": symbol,
                "classification": signal.get("classification", "WAIT"),
                "signal_id": (
                    observation["signal_id"] if observation else None
                ),
                "reason": analysis.get("reason"),
                "execution": None,
            }
            if (
                analysis.get("valid")
                and allow_execution
                and engine.config.execution_enabled
                and engine.config.mode != OperatingMode.RESEARCH
                and str(signal.get("classification", "")).upper()
                == "CONFIRMED"
            ):
                try:
                    result["execution"] = engine.submit_signal(
                        symbol,
                        signal,
                        signal_id=result["signal_id"],
                    )
                except TradeRejected as error:
                    result["execution"] = {
                        "submitted": False,
                        "reason": str(error),
                    }
                    engine.store.record_event(
                        "WARNING",
                        "ENTRY_BLOCKED",
                        f"{symbol}: {error}",
                    )
            results.append(result)

    engine.store.heartbeat(os.getpid())
    return results


def _managed_trades_remain(engine):
    return bool(engine.store.list_trades(statuses=RECONCILE_STATUSES))


def _stop_requested(engine):
    engine.refresh_configuration()
    return not engine.config.runner_enabled


def run_forever(
    engine,
    symbols_provider=load_symbols,
    scanner=analyze_signal_for_symbol,
):
    LOGGER.info("NEXORA runner started in %s mode", engine.config.mode.value)
    interval = _interval_seconds()
    while True:
        try:
            engine.refresh_configuration()
            symbols = symbols_provider() if engine.config.agent_enabled else []
            results = scan_cycle(engine, symbols, scanner)
            for result in results:
                LOGGER.info("Runner result: %s", result)
            if _stop_requested(engine) and not _managed_trades_remain(engine):
                LOGGER.info("Runner stopped after controls were disarmed.")
                engine.store.clear_runner_status(os.getpid())
                return
        except (
            TradeRejected,
            MT5ConnectionError,
            MarketDataError,
            sqlite3.Error,
            ValueError,
        ) as error:
            LOGGER.error("Runner cycle failed closed: %s", error)
            engine.store.record_event("ERROR", "RUNNER_CYCLE_FAILED", str(error))
            engine.store.heartbeat(os.getpid())
        time.sleep(interval)


def _interval_seconds():
    raw = os.environ.get("NEXORA_SCAN_INTERVAL_SECONDS", "15")
    try:
        interval = int(raw)
    except ValueError as error:
        raise ValueError(
            "NEXORA_SCAN_INTERVAL_SECONDS must be a positive integer."
        ) from error
    if interval < 1:
        raise ValueError("NEXORA_SCAN_INTERVAL_SECONDS must be positive.")
    return interval


def main():
    parser = argparse.ArgumentParser(
        description="Run NEXORA's separate-process signal and trade supervisor."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scan cycle and exit.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    engine = TradingEngine()
    symbols = load_symbols()

    if args.once:
        for result in scan_cycle(engine, symbols, allow_execution=False):
            LOGGER.info("Runner result: %s", result)
        return
    engine.store.claim_runner_process(os.getpid())
    engine.store.disarm_after_restart()
    engine.store.set_runner_enabled(True)
    engine.refresh_configuration()
    try:
        run_forever(engine)
    except KeyboardInterrupt:
        engine.store.disarm_supervisor()
        if _managed_trades_remain(engine):
            LOGGER.warning(
                "Scanning is disarmed; continuing supervision until managed "
                "positions are reconciled."
            )
            run_forever(engine)
        else:
            engine.store.clear_runner_status(os.getpid())


if __name__ == "__main__":
    main()
