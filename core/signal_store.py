from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import sqlite3
import uuid

from core.data_validation import MarketDataValidationError, validate_ohlc_data


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATABASE = _PROJECT_ROOT / "data" / "nexora_fx.sqlite3"


def _database_path(db_path=None):
    configured = db_path or os.environ.get("NEXORA_DB_PATH")
    return Path(configured) if configured else _DEFAULT_DATABASE


def _connect(db_path=None):
    path = _database_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                signal_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                classification TEXT NOT NULL,
                signal_timestamp TEXT NOT NULL,
                entry_reference REAL,
                stop_reference REAL,
                target_reference REAL,
                quality_score REAL,
                outcome TEXT NOT NULL DEFAULT 'PENDING',
                outcome_timestamp TEXT,
                outcome_r REAL,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, signal_timestamp)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signals_symbol_time "
            "ON signals(symbol, signal_timestamp DESC)"
        )
    except Exception:
        connection.close()
        raise
    return connection


@contextmanager
def _database(db_path=None):
    connection = _connect(db_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _timestamp(value, name):
    if value is None:
        raise ValueError(f"{name} is required.")
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(
                str(value).replace("Z", "+00:00")
            )
        except ValueError as error:
            raise ValueError(f"{name} must be a valid ISO timestamp.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_number(value, name, positive=False):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric.")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{name} must be numeric.") from error
    if not math.isfinite(numeric) or (positive and numeric <= 0):
        raise ValueError(f"{name} must be a finite positive value.")
    return numeric


def record_signal(
    symbol,
    signal_result,
    signal_timestamp=None,
    db_path=None,
):
    """Persist one signal observation and ignore duplicate symbol/bar pairs."""

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("A valid symbol is required.")
    if not isinstance(signal_result, dict):
        raise ValueError("Signal result must be a mapping.")

    classification = str(
        signal_result.get(
            "classification",
            signal_result.get("signal", ""),
        )
    ).upper()
    if classification not in {"CONFIRMED", "WAIT", "REJECTED"}:
        raise ValueError("Signal classification is invalid.")

    direction = str(signal_result.get("direction", "UNKNOWN")).upper()
    timestamp = _timestamp(
        signal_timestamp or signal_result.get("signal_timestamp"),
        "Signal timestamp",
    )
    entry = _optional_number(
        signal_result.get("entry"),
        "Entry reference",
        positive=True,
    )
    stop = _optional_number(
        signal_result.get("stop_loss"),
        "Stop reference",
        positive=True,
    )
    target = _optional_number(
        signal_result.get("take_profit"),
        "Target reference",
        positive=True,
    )
    quality = _optional_number(
        signal_result.get("quality_score"),
        "Quality score",
    )

    candidate_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _database(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO signals (
                signal_id, symbol, direction, classification,
                signal_timestamp, entry_reference, stop_reference,
                target_reference, quality_score, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, signal_timestamp) DO NOTHING
            """,
            (
                candidate_id,
                symbol.strip(),
                direction,
                classification,
                timestamp,
                entry,
                stop,
                target,
                quality,
                created_at,
            ),
        )
        inserted = cursor.rowcount == 1
        if inserted:
            signal_id = candidate_id
        else:
            row = connection.execute(
                "SELECT signal_id FROM signals "
                "WHERE symbol = ? AND signal_timestamp = ?",
                (symbol.strip(), timestamp),
            ).fetchone()
            if row is None:
                raise RuntimeError("Signal insert conflicted without a matching record.")
            signal_id = row["signal_id"]

    return {"signal_id": signal_id, "inserted": inserted}


def get_recent_signals(limit=500, symbol=None, db_path=None):
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError("Signal history limit must be a positive integer.")

    with _database(db_path) as connection:
        if symbol:
            rows = connection.execute(
                "SELECT * FROM signals WHERE symbol = ? "
                "ORDER BY signal_timestamp DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM signals "
                "ORDER BY signal_timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(row) for row in rows]


def resolve_pending_outcomes(
    symbol,
    historical_data,
    timeframe_minutes=15,
    db_path=None,
):
    """Resolve reference-level outcomes from candles after each signal bar."""

    if (
        isinstance(timeframe_minutes, bool)
        or not isinstance(timeframe_minutes, int)
        or timeframe_minutes <= 0
    ):
        raise ValueError("Timeframe minutes must be a positive integer.")
    try:
        candles = validate_ohlc_data(historical_data, require_time=True)
    except MarketDataValidationError:
        raise
    now = datetime.now(timezone.utc)
    if (candles["time"] > now).any():
        raise MarketDataValidationError(
            "Outcome candles contain future-dated timestamps."
        )
    completed = candles["time"] + timedelta(minutes=timeframe_minutes)
    candles = candles.loc[completed <= now].reset_index(drop=True)

    with _database(db_path) as connection:
        rows = connection.execute(
            """
            SELECT signal_id, direction, signal_timestamp,
                   entry_reference, stop_reference, target_reference
            FROM signals
            WHERE symbol = ? AND outcome = 'PENDING'
              AND entry_reference IS NOT NULL
              AND stop_reference IS NOT NULL
              AND target_reference IS NOT NULL
            """,
            (symbol,),
        ).fetchall()

        updated = []
        for row in rows:
            signal_time = datetime.fromisoformat(row["signal_timestamp"])
            entry = float(row["entry_reference"])
            stop = float(row["stop_reference"])
            target = float(row["target_reference"])
            direction = row["direction"].upper()

            for candle in candles.itertuples(index=False):
                candle_time = candle.time.to_pydatetime()
                if candle_time.tzinfo is None:
                    candle_time = candle_time.replace(tzinfo=timezone.utc)
                if candle_time < signal_time:
                    continue

                if direction in {"BUY", "BULLISH"}:
                    stop_hit = candle.low <= stop
                    target_hit = candle.high >= target
                    reward_risk = (target - entry) / abs(entry - stop)
                elif direction in {"SELL", "BEARISH"}:
                    stop_hit = candle.high >= stop
                    target_hit = candle.low <= target
                    reward_risk = (entry - target) / abs(stop - entry)
                else:
                    break

                if not stop_hit and not target_hit:
                    continue
                if stop_hit and target_hit:
                    outcome = "AMBIGUOUS_STOP_FIRST"
                    outcome_r = -1.0
                elif stop_hit:
                    outcome = "STOP_LOSS"
                    outcome_r = -1.0
                else:
                    outcome = "TAKE_PROFIT"
                    outcome_r = reward_risk

                outcome_time = (
                    candle_time.timestamp() + timeframe_minutes * 60
                )
                resolved_at = datetime.fromtimestamp(
                    outcome_time,
                    tz=timezone.utc,
                ).isoformat()
                cursor = connection.execute(
                    """
                    UPDATE signals
                    SET outcome = ?, outcome_timestamp = ?, outcome_r = ?
                    WHERE signal_id = ? AND outcome = 'PENDING'
                    """,
                    (outcome, resolved_at, outcome_r, row["signal_id"]),
                )
                if cursor.rowcount == 1:
                    updated.append(row["signal_id"])
                break

    return updated
