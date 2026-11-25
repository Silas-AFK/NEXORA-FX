from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from risk.risk_policy import RiskPolicy
from execution.account_connections import normalize_account_profile


RECONCILE_STATUSES = (
    "OPEN",
    "UNPROTECTED",
    "PENDING",
    "PENDING_CONFIRMATION",
    "PROTECTION_FAILED_CLOSED",
    "RISK_LIMIT_BREACH_CLOSED",
    "SLIPPAGE_LIMIT_BREACH_CLOSED",
    "POSITION_MISMATCH_CLOSED",
)
ENTRY_BLOCKING_STATUSES = (
    "UNPROTECTED",
    "PENDING",
    "PENDING_CONFIRMATION",
    "PROTECTION_FAILED",
    "PROTECTION_FAILED_CLOSED",
    "RISK_LIMIT_BREACH_CLOSED",
    "SLIPPAGE_LIMIT_BREACH_CLOSED",
    "POSITION_MISMATCH_CLOSED",
)


class TradingStore:
    """Durable execution ledger and fail-closed operational state."""

    BASELINE_WINDOW_SECONDS = 300

    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS safety_state (
                    state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                    emergency_stop INTEGER NOT NULL DEFAULT 0,
                    lock_reason TEXT,
                    lockout_updated_at TEXT,
                    runner_heartbeat_at TEXT,
                    runner_pid INTEGER,
                    runner_start_until REAL
                );
                INSERT OR IGNORE INTO safety_state (state_id) VALUES (1);

                CREATE TABLE IF NOT EXISTS operational_config (
                    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
                    mode TEXT NOT NULL DEFAULT 'DEMO',
                    agent_enabled INTEGER NOT NULL DEFAULT 0,
                    execution_enabled INTEGER NOT NULL DEFAULT 0,
                    runner_enabled INTEGER NOT NULL DEFAULT 0,
                    risk_policy_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS account_connections (
                    config_id INTEGER PRIMARY KEY CHECK (config_id = 1),
                    expected_login INTEGER,
                    broker_server TEXT,
                    account_type TEXT,
                    verified_login INTEGER,
                    verified_server TEXT,
                    verified_account_type TEXT,
                    verified_at TEXT,
                    updated_at TEXT NOT NULL,
                    CHECK (expected_login IS NULL OR expected_login > 0),
                    CHECK (
                        broker_server IS NULL
                        OR length(trim(broker_server)) > 0
                    ),
                    CHECK (
                        account_type IS NULL
                        OR account_type IN ('DEMO', 'LIVE')
                    )
                );

                CREATE TABLE IF NOT EXISTS daily_baselines (
                    trading_date TEXT PRIMARY KEY,
                    starting_equity REAL NOT NULL CHECK (starting_equity > 0),
                    account_login INTEGER NOT NULL,
                    account_server TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS period_baselines (
                    period TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    starting_equity REAL NOT NULL CHECK (starting_equity > 0),
                    account_login INTEGER NOT NULL,
                    account_server TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL,
                    PRIMARY KEY (period, period_start)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    signal_id TEXT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    account_login INTEGER NOT NULL,
                    account_server TEXT NOT NULL,
                    requested_volume REAL NOT NULL,
                    actual_volume REAL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL NOT NULL,
                    initial_risk REAL NOT NULL,
                    order_ticket INTEGER,
                    position_ticket INTEGER,
                    position_identifier INTEGER,
                    entry_time TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    realized_profit REAL,
                    risk_percent REAL NOT NULL DEFAULT 0.25,
                    strategy TEXT NOT NULL DEFAULT 'SMC_MTF',
                    currency TEXT NOT NULL DEFAULT 'UNKNOWN',
                    final_stop_loss REAL,
                    final_take_profit REAL,
                    exit_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_trades_status
                    ON trades(status, symbol);

                CREATE TABLE IF NOT EXISTS trade_actions (
                    action_id TEXT PRIMARY KEY,
                    position_ticket INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(position_ticket, action)
                );

                CREATE TABLE IF NOT EXISTS execution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    severity TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS execution_leases (
                    lease_name TEXT PRIMARY KEY,
                    token TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS closed_trade_history (
                    trade_id TEXT PRIMARY KEY,
                    position_identifier INTEGER NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_time TEXT,
                    entry_price REAL NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_price REAL,
                    volume REAL NOT NULL,
                    initial_stop_loss REAL NOT NULL,
                    initial_take_profit REAL NOT NULL,
                    final_stop_loss REAL,
                    final_take_profit REAL,
                    risk_percent REAL NOT NULL,
                    planned_risk_amount REAL NOT NULL,
                    realized_profit REAL NOT NULL,
                    commission REAL NOT NULL,
                    swap REAL NOT NULL,
                    fees REAL NOT NULL DEFAULT 0,
                    net_profit REAL NOT NULL,
                    strategy TEXT NOT NULL,
                    signal_id TEXT,
                    exit_reason TEXT,
                    duration_seconds REAL,
                    mode TEXT NOT NULL,
                    account_reference TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    deal_tickets_json TEXT NOT NULL,
                    reconciled_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_closed_trade_history_exit
                    ON closed_trade_history(exit_time);

                CREATE TABLE IF NOT EXISTS closed_trade_deals (
                    deal_ticket INTEGER PRIMARY KEY,
                    trade_id TEXT NOT NULL REFERENCES closed_trade_history(trade_id),
                    position_identifier INTEGER NOT NULL,
                    deal_time TEXT NOT NULL,
                    deal_type INTEGER,
                    deal_entry INTEGER,
                    volume REAL NOT NULL,
                    price REAL NOT NULL,
                    profit REAL NOT NULL,
                    commission REAL NOT NULL,
                    swap REAL NOT NULL,
                    fee REAL NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO account_connections
                    (config_id, updated_at)
                VALUES (1, ?)
                """,
                (self._now(),),
            )
            config = connection.execute(
                "SELECT config_id FROM operational_config WHERE config_id = 1"
            ).fetchone()
            if config is None:
                connection.execute(
                    """
                    INSERT INTO operational_config
                        (config_id, mode, agent_enabled, execution_enabled,
                         risk_policy_json, updated_at)
                    VALUES (1, 'DEMO', 0, 0, ?, ?)
                    """,
                    (
                        json.dumps(RiskPolicy().to_dict(), sort_keys=True),
                        self._now(),
                    ),
                )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(safety_state)")
            }
            if "runner_pid" not in columns:
                connection.execute(
                    "ALTER TABLE safety_state ADD COLUMN runner_pid INTEGER"
                )
            if "runner_start_until" not in columns:
                connection.execute(
                    "ALTER TABLE safety_state ADD COLUMN runner_start_until REAL"
                )
            config_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(operational_config)"
                )
            }
            if "runner_enabled" not in config_columns:
                connection.execute(
                    "ALTER TABLE operational_config ADD COLUMN "
                    "runner_enabled INTEGER NOT NULL DEFAULT 0"
                )
            trade_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(trades)")
            }
            if "position_identifier" not in trade_columns:
                connection.execute(
                    "ALTER TABLE trades ADD COLUMN position_identifier INTEGER"
                )
            for name, declaration in (
                ("entry_time", "TEXT"),
                ("risk_percent", "REAL NOT NULL DEFAULT 0.25"),
                ("strategy", "TEXT NOT NULL DEFAULT 'SMC_MTF'"),
                ("currency", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
                ("final_stop_loss", "REAL"),
                ("final_take_profit", "REAL"),
                ("exit_reason", "TEXT"),
            ):
                if name not in trade_columns:
                    connection.execute(
                        f"ALTER TABLE trades ADD COLUMN {name} {declaration}"
                    )
            history_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(closed_trade_history)"
                )
            }
            if "fees" not in history_columns:
                connection.execute(
                    "ALTER TABLE closed_trade_history "
                    "ADD COLUMN fees REAL NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def current_trading_date():
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def period_start(period, now=None):
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("Period baselines require timezone-aware UTC time.")
        current = current.astimezone(timezone.utc)
        if period == "daily":
            return current.date().isoformat()
        if period == "weekly":
            monday = current.date().fromordinal(
                current.date().toordinal() - current.weekday()
            )
            return monday.isoformat()
        if period == "monthly":
            return current.date().replace(day=1).isoformat()
        raise ValueError("Period must be daily, weekly, or monthly.")

    @classmethod
    def baseline_window_open(cls):
        now = datetime.now(timezone.utc)
        seconds_since_midnight = (
            now.hour * 3600 + now.minute * 60 + now.second
        )
        return seconds_since_midnight <= cls.BASELINE_WINDOW_SECONDS

    @classmethod
    def period_baseline_window_open(cls, period, now=None):
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            return False
        current = current.astimezone(timezone.utc)
        if period == "daily" and now is None:
            return cls.baseline_window_open()
        if period == "daily":
            is_start = True
        elif period == "weekly":
            is_start = current.weekday() == 0
        elif period == "monthly":
            is_start = current.day == 1
        else:
            raise ValueError("Period must be daily, weekly, or monthly.")
        seconds = current.hour * 3600 + current.minute * 60 + current.second
        return is_start and seconds <= cls.BASELINE_WINDOW_SECONDS

    def get_operational_config(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT mode, agent_enabled, execution_enabled, "
                "runner_enabled, risk_policy_json, updated_at "
                "FROM operational_config "
                "WHERE config_id = 1"
            ).fetchone()
            account_row = connection.execute(
                "SELECT * FROM account_connections WHERE config_id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("Trading configuration has not been initialized.")
        if account_row is None:
            raise RuntimeError("Account connection configuration is unavailable.")
        try:
            risk_values = json.loads(row["risk_policy_json"])
        except json.JSONDecodeError as error:
            raise ValueError("Stored risk configuration is invalid JSON.") from error
        if not isinstance(risk_values, dict):
            raise ValueError("Stored risk configuration must be an object.")
        policy = RiskPolicy.from_mapping(risk_values)
        return {
            "mode": row["mode"],
            "agent_enabled": bool(row["agent_enabled"]),
            "execution_enabled": bool(row["execution_enabled"]),
            "runner_enabled": bool(row["runner_enabled"]),
            "risk_policy": policy.to_dict(),
            "updated_at": row["updated_at"],
            "account_connection": self._account_connection_record(account_row),
        }

    @staticmethod
    def _account_connection_record(row):
        if (
            row["expected_login"] is None
            and row["broker_server"] is None
            and row["account_type"] is None
        ):
            return None
        if (
            row["expected_login"] is None
            or row["broker_server"] is None
            or row["account_type"] is None
        ):
            raise ValueError("Stored account connection configuration is incomplete.")
        profile = normalize_account_profile(
            row["expected_login"],
            row["broker_server"],
            row["account_type"],
        )
        verified = (
            row["verified_at"] is not None
            and row["verified_login"] == profile["login"]
            and row["verified_server"] == profile["server"]
            and row["verified_account_type"] == profile["account_type"]
        )
        return {
            **profile,
            "verified": verified,
            "verified_at": row["verified_at"] if verified else None,
        }

    def get_account_connection(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM account_connections WHERE config_id = 1"
            ).fetchone()
        if row is None:
            return None
        return self._account_connection_record(row)

    def _set_account_connection(self, login, broker_server, account_type, *, switch):
        profile = normalize_account_profile(login, broker_server, account_type)
        if not isinstance(switch, bool):
            raise ValueError("Account switch state must be a boolean.")

        with self.entry_lease():
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current_row = connection.execute(
                    "SELECT * FROM account_connections WHERE config_id = 1"
                ).fetchone()
                if current_row is None:
                    raise RuntimeError(
                        "Account connection configuration is unavailable."
                    )
                current = self._account_connection_record(current_row)
                changed = current is None or any(
                    current[key] != profile[value]
                    for key, value in (
                        ("login", "login"),
                        ("server", "server"),
                        ("account_type", "account_type"),
                    )
                )
                if switch and current is None:
                    raise ValueError(
                        "Save an account profile before switching accounts."
                    )
                if switch and not changed:
                    raise ValueError(
                        "The entered profile is the currently saved account."
                    )
                if not switch and current is not None and changed:
                    raise ValueError(
                        "Use Switch Account to change the saved account."
                    )
                if not changed:
                    return current

                placeholders = ",".join("?" for _ in RECONCILE_STATUSES)
                unresolved = connection.execute(
                    "SELECT COUNT(*) FROM trades WHERE status IN "
                    f"({placeholders})",
                    RECONCILE_STATUSES,
                ).fetchone()[0]
                if unresolved:
                    raise ValueError(
                        "Close or reconcile existing trades before changing "
                        "the account connection."
                    )

                now = self._now()
                connection.execute(
                    """
                    UPDATE account_connections
                    SET expected_login = ?, broker_server = ?, account_type = ?,
                        verified_login = NULL, verified_server = NULL,
                        verified_account_type = NULL, verified_at = NULL,
                        updated_at = ?
                    WHERE config_id = 1
                    """,
                    (
                        profile["login"],
                        profile["server"],
                        profile["account_type"],
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE operational_config
                    SET agent_enabled = 0, execution_enabled = 0,
                        runner_enabled = 0, updated_at = ?
                    WHERE config_id = 1
                    """,
                    (now,),
                )
        return self.get_account_connection()

    def save_account_connection(self, login, broker_server, account_type):
        return self._set_account_connection(
            login,
            broker_server,
            account_type,
            switch=False,
        )

    def switch_account_connection(self, login, broker_server, account_type):
        return self._set_account_connection(
            login,
            broker_server,
            account_type,
            switch=True,
        )

    def mark_account_connection_verified(self, login, broker_server, account_type):
        actual = normalize_account_profile(login, broker_server, account_type)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM account_connections WHERE config_id = 1"
            ).fetchone()
            current = self._account_connection_record(row) if row else None
            if current is None or any(
                current[key] != actual[key]
                for key in ("login", "server", "account_type")
            ):
                raise ValueError(
                    "Connected MT5 identity does not match the saved profile."
                )
            connection.execute(
                """
                UPDATE account_connections
                SET verified_login = ?, verified_server = ?,
                    verified_account_type = ?, verified_at = ?, updated_at = ?
                WHERE config_id = 1
                """,
                (
                    actual["login"],
                    actual["server"],
                    actual["account_type"],
                    self._now(),
                    self._now(),
                ),
            )

    def invalidate_account_connection_verification(self):
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE account_connections
                SET verified_login = NULL, verified_server = NULL,
                    verified_account_type = NULL, verified_at = NULL,
                    updated_at = ?
                WHERE config_id = 1
                """,
                (now,),
            )
            connection.execute(
                """
                UPDATE operational_config
                SET execution_enabled = 0, updated_at = ?
                WHERE config_id = 1
                """,
                (now,),
            )

    def clear_account_connection(self):
        with self.entry_lease():
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                placeholders = ",".join("?" for _ in RECONCILE_STATUSES)
                unresolved = connection.execute(
                    "SELECT COUNT(*) FROM trades WHERE status IN "
                    f"({placeholders})",
                    RECONCILE_STATUSES,
                ).fetchone()[0]
                if unresolved:
                    raise ValueError(
                        "Close or reconcile existing trades before disconnecting "
                        "the account."
                    )
                now = self._now()
                connection.execute(
                    """
                    UPDATE account_connections
                    SET expected_login = NULL, broker_server = NULL,
                        account_type = NULL, verified_login = NULL,
                        verified_server = NULL, verified_account_type = NULL,
                        verified_at = NULL, updated_at = ?
                    WHERE config_id = 1
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    UPDATE operational_config
                    SET agent_enabled = 0, execution_enabled = 0,
                        runner_enabled = 0, updated_at = ?
                    WHERE config_id = 1
                    """,
                    (now,),
                )

    def update_operational_config(
        self,
        *,
        mode=None,
        agent_enabled=None,
        execution_enabled=None,
        runner_enabled=None,
        risk_policy=None,
    ):
        current = self.get_operational_config()
        new_mode = current["mode"] if mode is None else str(mode).upper()
        if new_mode not in {"RESEARCH", "DEMO", "LIVE"}:
            raise ValueError("Mode must be RESEARCH, DEMO, or LIVE.")
        if agent_enabled is not None and not isinstance(agent_enabled, bool):
            raise ValueError("Agent state must be a boolean.")
        if execution_enabled is not None and not isinstance(execution_enabled, bool):
            raise ValueError("Execution state must be a boolean.")
        if runner_enabled is not None and not isinstance(runner_enabled, bool):
            raise ValueError("Runner state must be a boolean.")
        policy = RiskPolicy.from_mapping(
            risk_policy if risk_policy is not None else current["risk_policy"]
        )
        mode_changed = new_mode != current["mode"]
        if mode_changed and self.list_trades(statuses=RECONCILE_STATUSES):
            raise ValueError(
                "Close or reconcile existing trades before changing operating mode."
            )
        new_execution = (
            False
            if mode_changed or risk_policy is not None
            else current["execution_enabled"]
            if execution_enabled is None
            else execution_enabled
        )
        new_agent = (
            current["agent_enabled"]
            if agent_enabled is None
            else agent_enabled
        )
        new_runner = (
            current["runner_enabled"]
            if runner_enabled is None
            else runner_enabled
        )
        if new_mode == "RESEARCH":
            new_execution = False
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE operational_config
                SET mode = ?, agent_enabled = ?, execution_enabled = ?,
                    runner_enabled = ?, risk_policy_json = ?, updated_at = ?
                WHERE config_id = 1
                """,
                (
                    new_mode,
                    int(new_agent),
                    int(new_execution),
                    int(new_runner),
                    json.dumps(policy.to_dict(), sort_keys=True),
                    now,
                ),
            )
        return self.get_operational_config()

    def disarm_after_restart(self):
        """Stop scanning and clear execution permission on process startup."""

        current = self.get_operational_config()
        open_trades = self.list_trades(statuses=RECONCILE_STATUSES)
        mode = (
            "RESEARCH"
            if current["mode"] == "LIVE" and not open_trades
            else current["mode"]
        )
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE operational_config
                SET mode = ?, agent_enabled = 0, execution_enabled = 0,
                    updated_at = ?
                WHERE config_id = 1
                """,
                (mode, self._now()),
            )

    def set_agent_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Agent state must be a boolean.")
        return self.update_operational_config(agent_enabled=enabled)

    def set_execution_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Execution state must be a boolean.")
        return self.update_operational_config(execution_enabled=enabled)

    def set_runner_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Runner state must be a boolean.")
        return self.update_operational_config(runner_enabled=enabled)

    def disarm_supervisor(self):
        """Stop scanning and entries while allowing management to finish."""

        with self._connection() as connection:
            connection.execute(
                """
                UPDATE operational_config
                SET runner_enabled = 0, agent_enabled = 0,
                    execution_enabled = 0, updated_at = ?
                WHERE config_id = 1
                """,
                (self._now(),),
            )

    def get_lockout(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT emergency_stop, lock_reason, lockout_updated_at "
                "FROM safety_state WHERE state_id = 1"
            ).fetchone()
        return {
            "active": bool(row["emergency_stop"]),
            "reason": row["lock_reason"],
            "updated_at": row["lockout_updated_at"],
        }

    def activate_lockout(self, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A lockout reason is required.")
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE safety_state
                SET emergency_stop = 1, lock_reason = ?,
                    lockout_updated_at = ?
                WHERE state_id = 1
                """,
                (reason.strip(), now),
            )
            connection.execute(
                """
                INSERT INTO execution_events
                    (severity, event_type, detail, created_at)
                VALUES ('CRITICAL', 'LOCKOUT', ?, ?)
                """,
                (reason.strip(), now),
            )

    def clear_lockout(self, reason, *, operator_confirmed):
        if operator_confirmed is not True:
            raise PermissionError("Explicit operator confirmation is required.")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("A reason for clearing the lockout is required.")
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE safety_state
                SET emergency_stop = 0, lock_reason = ?,
                    lockout_updated_at = ?
                WHERE state_id = 1
                """,
                (f"Cleared: {reason.strip()}", now),
            )
            connection.execute(
                """
                INSERT INTO execution_events
                    (severity, event_type, detail, created_at)
                VALUES ('WARNING', 'LOCKOUT_CLEARED', ?, ?)
                """,
                (reason.strip(), now),
            )

    def set_daily_baseline(self, equity, account_login, account_server):
        return self.set_period_baseline(
            "daily",
            equity,
            account_login,
            account_server,
        )

    def set_period_baseline(
        self,
        period,
        equity,
        account_login,
        account_server,
        *,
        now=None,
    ):
        if (
            isinstance(equity, bool)
            or not isinstance(equity, (int, float))
            or not math.isfinite(equity)
            or equity <= 0
        ):
            raise ValueError("Daily starting equity must be positive.")
        if (
            isinstance(account_login, bool)
            or not isinstance(account_login, int)
            or account_login <= 0
        ):
            raise ValueError("A verified account login is required.")
        if not isinstance(account_server, str) or not account_server.strip():
            raise ValueError("A verified broker server is required.")
        if not self.period_baseline_window_open(period, now=now):
            raise ValueError(
                f"The UTC {period} baseline window is closed; entries remain "
                "locked until the next period."
            )
        period_start = self.period_start(period, now=now)
        confirmed_at = (now or datetime.now(timezone.utc)).astimezone(
            timezone.utc
        ).isoformat()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO period_baselines
                        (period, period_start, starting_equity, account_login,
                         account_server, confirmed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        period,
                        period_start,
                        float(equity),
                        account_login,
                        account_server.strip(),
                        confirmed_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                f"The {period} equity baseline already exists and cannot be overwritten."
            ) from error
        if period == "daily":
            return self.get_daily_baseline(period_start)
        return self.get_period_baseline(period, period_start)

    def get_daily_baseline(self, trading_date=None):
        date = trading_date or self.current_trading_date()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM period_baselines "
                "WHERE period = 'daily' AND period_start = ?",
                (date,),
            ).fetchone()
        if row:
            result = dict(row)
            result["trading_date"] = result.pop("period_start")
            return result
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM daily_baselines WHERE trading_date = ?",
                (date,),
            ).fetchone()
        return dict(row) if row else None

    def get_period_baseline(self, period, period_start=None):
        if period == "daily":
            return self.get_daily_baseline(period_start)
        start = period_start or self.period_start(period)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM period_baselines WHERE period = ? "
                "AND period_start = ?",
                (period, start),
            ).fetchone()
        return dict(row) if row else None

    def get_active_period_baselines(self):
        return {
            period: self.get_period_baseline(period)
            for period in ("daily", "weekly", "monthly")
        }

    @contextmanager
    def entry_lease(self, lease_seconds=30):
        """Serialize portfolio checks and order reservations across processes."""

        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive.")
        token = str(uuid.uuid4())
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT token, expires_at FROM execution_leases "
                "WHERE lease_name = 'entries'"
            ).fetchone()
            if row is not None and row["expires_at"] > now:
                connection.rollback()
                raise RuntimeError("Another process is evaluating a new entry.")
            connection.execute(
                """
                INSERT INTO execution_leases (lease_name, token, expires_at)
                VALUES ('entries', ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    token = excluded.token, expires_at = excluded.expires_at
                """,
                (token, now + lease_seconds),
            )
            connection.commit()
        finally:
            connection.close()

        try:
            yield
        finally:
            with self._connection() as release:
                release.execute(
                    "DELETE FROM execution_leases "
                    "WHERE lease_name = 'entries' AND token = ?",
                    (token,),
                )

    def reserve_trade(self, record):
        now = self._now()
        trade_id = str(uuid.uuid4())
        values = {
            **record,
            "trade_id": trade_id,
            "status": "PENDING",
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO trades (
                        trade_id, idempotency_key, signal_id, symbol, direction,
                        mode, account_login, account_server, requested_volume,
                        entry_price, stop_loss, take_profit, initial_risk,
                        status, created_at, updated_at, risk_percent, strategy,
                        currency
                    ) VALUES (
                        :trade_id, :idempotency_key, :signal_id, :symbol,
                        :direction, :mode, :account_login, :account_server,
                        :requested_volume, :entry_price, :stop_loss,
                        :take_profit, :initial_risk, :status, :created_at,
                        :updated_at, :risk_percent, :strategy, :currency
                    )
                    """,
                    {
                        **values,
                        "risk_percent": record.get("risk_percent", 0.25),
                        "strategy": record.get("strategy", "SMC_MTF"),
                        "currency": record.get("currency", "UNKNOWN"),
                    },
                )
        except sqlite3.IntegrityError as error:
            existing = self.get_trade_by_key(record["idempotency_key"])
            if existing is not None:
                return {"inserted": False, "trade": existing}
            raise ValueError(f"Trade reservation rejected: {error}") from error
        return {"inserted": True, "trade": self.get_trade(trade_id)}

    def get_trade_by_key(self, idempotency_key):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trades WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return dict(row) if row else None

    def get_trade(self, trade_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM trades WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_trade(self, trade_id, **fields):
        allowed = {
            "actual_volume",
            "entry_price",
            "order_ticket",
            "position_ticket",
            "position_identifier",
            "entry_time",
            "status",
            "error",
            "realized_profit",
            "closed_at",
            "risk_percent",
            "strategy",
            "currency",
            "final_stop_loss",
            "final_take_profit",
            "exit_reason",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = :{key}" for key in updates)
        updates["trade_id"] = trade_id
        with self._connection() as connection:
            connection.execute(
                f"UPDATE trades SET {assignments} WHERE trade_id = :trade_id",
                updates,
            )

    def list_trades(self, statuses=None, limit=200):
        with self._connection() as connection:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM trades WHERE status IN ({placeholders}) "
                    "ORDER BY created_at DESC LIMIT ?",
                    (*statuses, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def count_trades_since(self, timestamp):
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("A UTC timestamp is required.")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS trade_count FROM trades WHERE created_at >= ?",
                (timestamp,),
            ).fetchone()
        return int(row["trade_count"])

    def consecutive_closed_losses(self):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT net_profit FROM closed_trade_history "
                "ORDER BY exit_time DESC, rowid DESC"
            ).fetchall()
        losses = 0
        for row in rows:
            if row["net_profit"] >= 0:
                break
            losses += 1
        return losses

    def latest_closed_trade(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM closed_trade_history "
                "ORDER BY exit_time DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def record_closed_trade(self, record, deals):
        required = {
            "trade_id",
            "position_identifier",
            "symbol",
            "direction",
            "entry_price",
            "exit_time",
            "volume",
            "initial_stop_loss",
            "initial_take_profit",
            "risk_percent",
            "planned_risk_amount",
            "realized_profit",
            "commission",
            "swap",
            "fees",
            "net_profit",
            "strategy",
            "mode",
            "account_reference",
            "currency",
            "deal_tickets",
        }
        missing = required - set(record)
        if missing:
            raise ValueError(
                "Closed-trade record is missing: " + ", ".join(sorted(missing))
            )
        deal_rows = list(deals)
        if not deal_rows or not record["deal_tickets"]:
            raise ValueError("Verified broker deal history is required.")
        now = self._now()
        defaults = {
            "entry_time": None,
            "exit_price": None,
            "final_stop_loss": None,
            "final_take_profit": None,
            "signal_id": None,
            "exit_reason": None,
            "duration_seconds": None,
            "fees": 0.0,
        }
        payload = {
            **defaults,
            **record,
            "deal_tickets_json": json.dumps(
                sorted(set(int(ticket) for ticket in record["deal_tickets"]))
            ),
            "reconciled_at": now,
        }
        columns = (
            "trade_id, position_identifier, symbol, direction, entry_time, "
            "entry_price, exit_time, exit_price, volume, initial_stop_loss, "
            "initial_take_profit, final_stop_loss, final_take_profit, "
            "risk_percent, planned_risk_amount, realized_profit, commission, "
            "swap, fees, net_profit, strategy, signal_id, exit_reason, "
            "duration_seconds, mode, account_reference, currency, "
            "deal_tickets_json, reconciled_at"
        )
        values = (
            ":trade_id, :position_identifier, :symbol, :direction, :entry_time, "
            ":entry_price, :exit_time, :exit_price, :volume, :initial_stop_loss, "
            ":initial_take_profit, :final_stop_loss, :final_take_profit, "
            ":risk_percent, :planned_risk_amount, :realized_profit, :commission, "
            ":swap, :fees, :net_profit, :strategy, :signal_id, :exit_reason, "
            ":duration_seconds, :mode, :account_reference, :currency, "
            ":deal_tickets_json, :reconciled_at"
        )
        with self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO closed_trade_history ({columns})
                VALUES ({values})
                ON CONFLICT(trade_id) DO UPDATE SET
                    position_identifier = excluded.position_identifier,
                    entry_time = excluded.entry_time,
                    entry_price = excluded.entry_price,
                    exit_time = excluded.exit_time,
                    exit_price = excluded.exit_price,
                    volume = excluded.volume,
                    final_stop_loss = excluded.final_stop_loss,
                    final_take_profit = excluded.final_take_profit,
                    realized_profit = excluded.realized_profit,
                    commission = excluded.commission,
                    swap = excluded.swap,
                    fees = excluded.fees,
                    net_profit = excluded.net_profit,
                    exit_reason = excluded.exit_reason,
                    duration_seconds = excluded.duration_seconds,
                    currency = excluded.currency,
                    deal_tickets_json = excluded.deal_tickets_json,
                    reconciled_at = excluded.reconciled_at
                """,
                payload,
            )
            for deal in deal_rows:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO closed_trade_deals (
                        deal_ticket, trade_id, position_identifier, deal_time,
                        deal_type, deal_entry, volume, price, profit,
                        commission, swap, fee
                    ) VALUES (
                        :deal_ticket, :trade_id, :position_identifier, :deal_time,
                        :deal_type, :deal_entry, :volume, :price, :profit,
                        :commission, :swap, :fee
                    )
                    """,
                    {**deal, "trade_id": record["trade_id"]},
                )
        return self.get_closed_trade(record["trade_id"])

    def get_closed_trade(self, trade_id):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM closed_trade_history WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["deal_tickets"] = json.loads(record.pop("deal_tickets_json"))
        return record

    def list_closed_trades(self, *, start_time=None, end_time=None, limit=10000):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 100000
        ):
            raise ValueError("History limit must be from 1 to 100000.")
        clauses = []
        parameters = []
        if start_time is not None:
            clauses.append("exit_time >= ?")
            parameters.append(start_time)
        if end_time is not None:
            clauses.append("exit_time < ?")
            parameters.append(end_time)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM closed_trade_history "
                f"{where} ORDER BY exit_time DESC LIMIT ?",
                parameters,
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["deal_tickets"] = json.loads(item.pop("deal_tickets_json"))
            results.append(item)
        return results

    def count_closed_deals(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS deal_count FROM closed_trade_deals"
            ).fetchone()
        return int(row["deal_count"])

    def claim_action(self, position_ticket, action, detail=None):
        now = self._now()
        action_id = str(uuid.uuid4())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO trade_actions (
                        action_id, position_ticket, action, status,
                            detail, created_at, updated_at
                        ) VALUES (?, ?, ?, 'PENDING', ?, ?, ?)
                    """,
                        (
                            action_id,
                            position_ticket,
                            action,
                            detail,
                            now,
                            now,
                        ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def finish_action(self, position_ticket, action, status, detail=None):
        if status not in {"DONE", "FAILED", "RECONCILE", "SKIPPED"}:
            raise ValueError("Invalid trade action status.")
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE trade_actions
                SET status = ?, detail = ?, updated_at = ?
                WHERE position_ticket = ? AND action = ?
                """,
                (
                    status,
                    detail,
                    self._now(),
                    position_ticket,
                    action,
                ),
            )

    def action_status(self, position_ticket, action):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT status, detail FROM trade_actions "
                "WHERE position_ticket = ? AND action = ?",
                (position_ticket, action),
            ).fetchone()
        return dict(row) if row else None

    def record_event(self, severity, event_type, detail):
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO execution_events
                    (severity, event_type, detail, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (severity, event_type, str(detail), self._now()),
            )

    def events(self, limit=100):
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_events "
                "ORDER BY event_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def heartbeat(self, process_id=None):
        with self._connection() as connection:
            connection.execute(
                "UPDATE safety_state SET runner_heartbeat_at = ?, runner_pid = ? "
                "WHERE state_id = 1",
                (self._now(), process_id),
            )

    def runner_heartbeat(self):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT runner_heartbeat_at, runner_pid FROM safety_state "
                "WHERE state_id = 1"
            ).fetchone()
        return {
            "heartbeat_at": row["runner_heartbeat_at"],
            "process_id": row["runner_pid"],
        }

    def claim_runner_process(self, process_id, *, stale_after_seconds=60):
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            raise ValueError("A valid runner process ID is required.")
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT runner_pid, runner_heartbeat_at FROM safety_state "
                "WHERE state_id = 1"
            ).fetchone()
            existing_pid = row["runner_pid"]
            heartbeat = row["runner_heartbeat_at"]
            if heartbeat and existing_pid and existing_pid != process_id:
                try:
                    prior = datetime.fromisoformat(
                        heartbeat.replace("Z", "+00:00")
                    )
                except (AttributeError, TypeError, ValueError):
                    prior = None
                if prior is not None and prior.tzinfo is not None:
                    age = (now - prior.astimezone(timezone.utc)).total_seconds()
                    if 0 <= age < stale_after_seconds:
                        raise RuntimeError(
                            f"Runner process {existing_pid} already has a current heartbeat."
                        )
            connection.execute(
                "UPDATE safety_state SET runner_pid = ?, "
                "runner_heartbeat_at = ? WHERE state_id = 1",
                (process_id, now.isoformat()),
            )

    def clear_runner_status(self, process_id=None):
        with self._connection() as connection:
            if process_id is None:
                connection.execute(
                    "UPDATE safety_state SET runner_pid = NULL, "
                    "runner_heartbeat_at = NULL WHERE state_id = 1"
                )
            else:
                connection.execute(
                    "UPDATE safety_state SET runner_pid = NULL, "
                    "runner_heartbeat_at = NULL WHERE state_id = 1 "
                    "AND runner_pid = ?",
                    (process_id,),
                )

    @contextmanager
    def runner_start_lease(self, lease_seconds=60):
        if lease_seconds <= 0:
            raise ValueError("Lease duration must be positive.")
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT runner_start_until FROM safety_state WHERE state_id = 1"
            ).fetchone()
            if row["runner_start_until"] and row["runner_start_until"] > now:
                connection.rollback()
                raise RuntimeError("A runner launch is already in progress.")
            connection.execute(
                "UPDATE safety_state SET runner_start_until = ? "
                "WHERE state_id = 1",
                (now + lease_seconds,),
            )
            connection.commit()
        finally:
            connection.close()
        try:
            succeeded = False
            yield
            succeeded = True
        finally:
            if not succeeded:
                with self._connection() as release:
                    release.execute(
                        "UPDATE safety_state SET runner_start_until = NULL "
                        "WHERE state_id = 1 AND runner_start_until = ?",
                        (now + lease_seconds,),
                    )
