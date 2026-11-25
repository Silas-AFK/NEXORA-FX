from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
from datetime import datetime, timezone

from execution.account_connections import (
    account_type_from_trade_mode,
    normalize_account_profile,
)
from risk.risk_policy import RiskPolicy
from risk.risk_manager import check_period_loss


class OperatingMode(str, Enum):
    RESEARCH = "RESEARCH"
    DEMO = "DEMO"
    LIVE = "LIVE"


class OperatingModeError(ValueError):
    """Raised when an execution mode or safety configuration is invalid."""


LIVE_VERIFICATIONS = (
    "account_identity_verified",
    "order_execution_verified",
    "stop_loss_verified",
    "position_sizing_verified",
    "daily_loss_protection_verified",
    "emergency_stop_verified",
    "duplicate_prevention_verified",
    "demo_forward_test_verified",
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIVE_READINESS_FILE = _PROJECT_ROOT / "config" / "live_readiness.json"


def _env_percent(name, default, *, maximum=100.0):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise OperatingModeError(f"{name} must be a finite percentage.") from error
    if not math.isfinite(value) or value <= 0 or value > maximum:
        raise OperatingModeError(
            f"{name} must be greater than 0 and no greater than {maximum}."
        )
    return value


@dataclass(frozen=True)
class OperatingConfig:
    mode: OperatingMode
    expected_login: int | None
    expected_server: str | None
    demo_armed: bool
    live_authorized: bool
    risk_percent: float
    max_daily_loss_percent: float
    max_aggregate_stop_risk_percent: float
    max_open_positions: int
    live_readiness_file: Path
    expected_account_type: str | None = None
    account_verified: bool = False
    max_weekly_loss_percent: float = 2.0
    max_monthly_loss_percent: float = 4.0
    max_positions_per_symbol: int = 1
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    max_spread_pips: float = 2.0
    max_slippage_pips: float = 1.0
    minimum_reward_risk: float = 1.5
    session_start_utc: str | None = None
    session_end_utc: str | None = None
    execution_enabled: bool = False
    agent_enabled: bool = False
    runner_enabled: bool = False

    @classmethod
    def from_environment(cls, settings=None):
        settings = settings or {}
        raw_mode = str(
            settings.get(
                "mode",
                os.environ.get("NEXORA_OPERATING_MODE", "DEMO"),
            )
        ).strip().upper()
        try:
            mode = OperatingMode(raw_mode)
        except ValueError as error:
            raise OperatingModeError(
                "NEXORA_OPERATING_MODE must be RESEARCH, DEMO, or LIVE."
            ) from error

        account_settings = settings.get("account_connection")
        if account_settings is None:
            expected_login = None
            expected_server = None
            expected_account_type = None
            account_verified = False
        elif isinstance(account_settings, dict):
            try:
                profile = normalize_account_profile(
                    account_settings.get("login"),
                    account_settings.get("server"),
                    account_settings.get("account_type"),
                )
            except ValueError as error:
                raise OperatingModeError(
                    "Stored account connection configuration is invalid."
                ) from error
            expected_login = profile["login"]
            expected_server = profile["server"]
            expected_account_type = profile["account_type"]
            account_verified = account_settings.get("verified") is True
        else:
            raise OperatingModeError(
                "Stored account connection configuration must be an object."
            )
        raw_positions = (
            "2"
            if settings
            else os.environ.get("NEXORA_MAX_OPEN_POSITIONS", "2")
        )
        try:
            max_open_positions = int(raw_positions)
        except ValueError as error:
            raise OperatingModeError(
                "NEXORA_MAX_OPEN_POSITIONS must be a positive integer."
            ) from error
        if max_open_positions < 1:
            raise OperatingModeError("NEXORA_MAX_OPEN_POSITIONS must be positive.")

        risk_defaults = RiskPolicy().to_dict()
        stored_risk = settings.get("risk_policy", {})
        if not isinstance(stored_risk, dict):
            raise OperatingModeError("Stored risk policy must be an object.")
        if not settings:
            risk_defaults.update({
                "risk_per_trade_percent": _env_percent(
                    "NEXORA_RISK_PERCENT",
                    risk_defaults["risk_per_trade_percent"],
                    maximum=0.25,
                ),
                "max_daily_loss_percent": _env_percent(
                    "NEXORA_MAX_DAILY_LOSS_PERCENT",
                    risk_defaults["max_daily_loss_percent"],
                ),
                "max_total_open_risk_percent": _env_percent(
                    "NEXORA_MAX_AGGREGATE_RISK_PERCENT",
                    risk_defaults["max_total_open_risk_percent"],
                ),
                "max_simultaneous_positions": max_open_positions,
            })
        risk_defaults.update(stored_risk)
        try:
            risk_policy = RiskPolicy.from_mapping(risk_defaults)
        except ValueError as error:
            raise OperatingModeError(str(error)) from error
        max_open_positions = risk_policy.max_simultaneous_positions

        if settings:
            execution_enabled = settings.get("execution_enabled", False)
            agent_enabled = settings.get("agent_enabled", False)
            runner_enabled = settings.get("runner_enabled", False)
            if (
                not isinstance(execution_enabled, bool)
                or not isinstance(agent_enabled, bool)
                or not isinstance(runner_enabled, bool)
            ):
                raise OperatingModeError(
                    "Stored agent, runner, and execution states must be boolean."
                )
            demo_armed = (
                execution_enabled
                and mode == OperatingMode.DEMO
            )
        else:
            demo_armed = os.environ.get("NEXORA_DEMO_ARMED", "") == (
                "I_UNDERSTAND_DEMO_ORDERS"
            )
            execution_enabled = demo_armed or mode == OperatingMode.RESEARCH
            agent_enabled = False
            runner_enabled = False
        if mode == OperatingMode.RESEARCH:
            execution_enabled = False

        readiness_path = Path(
            os.environ.get(
                "NEXORA_LIVE_READINESS_FILE",
                str(_LIVE_READINESS_FILE),
            )
        )
        return cls(
            mode=mode,
            expected_login=expected_login,
            expected_server=expected_server,
            demo_armed=os.environ.get("NEXORA_DEMO_ARMED", "")
            == "I_UNDERSTAND_DEMO_ORDERS"
            if not settings
            else demo_armed,
            live_authorized=os.environ.get("NEXORA_LIVE_AUTHORIZATION", "")
            == "I_EXPLICITLY_AUTHORIZE_LIVE_TRADING",
            risk_percent=risk_policy.risk_per_trade_percent,
            max_daily_loss_percent=risk_policy.max_daily_loss_percent,
            max_aggregate_stop_risk_percent=(
                risk_policy.max_total_open_risk_percent
            ),
            max_open_positions=max_open_positions,
            live_readiness_file=readiness_path,
            expected_account_type=expected_account_type,
            account_verified=account_verified,
            max_weekly_loss_percent=risk_policy.max_weekly_loss_percent,
            max_monthly_loss_percent=risk_policy.max_monthly_loss_percent,
            max_positions_per_symbol=risk_policy.max_positions_per_symbol,
            max_trades_per_day=risk_policy.max_trades_per_day,
            max_consecutive_losses=risk_policy.max_consecutive_losses,
            max_spread_pips=risk_policy.max_spread_pips,
            max_slippage_pips=risk_policy.max_slippage_pips,
            minimum_reward_risk=risk_policy.minimum_reward_risk,
            session_start_utc=risk_policy.session_start_utc,
            session_end_utc=risk_policy.session_end_utc,
            execution_enabled=execution_enabled,
            agent_enabled=agent_enabled,
            runner_enabled=runner_enabled,
        )


def read_live_readiness(path=None):
    readiness_path = Path(path) if path is not None else _LIVE_READINESS_FILE
    try:
        content = json.loads(readiness_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        content = {}
    except (OSError, json.JSONDecodeError):
        content = {}
    if not isinstance(content, dict):
        content = {}
    readiness = {}
    for item in LIVE_VERIFICATIONS:
        record = content.get(item)
        if not isinstance(record, dict):
            readiness[item] = False
            continue
        evidence = record.get("evidence")
        verified_at = record.get("verified_at")
        try:
            parsed_time = datetime.fromisoformat(
                str(verified_at).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            parsed_time = None
        readiness[item] = (
            record.get("verified") is True
            and isinstance(evidence, str)
            and bool(evidence.strip())
            and parsed_time is not None
            and parsed_time.tzinfo is not None
            and parsed_time <= datetime.now(timezone.utc)
        )
    return readiness


def verify_account_identity(api, config, *, require_trading_permissions=True):
    """Require an explicitly allowlisted login, server, and terminal mode."""

    identity_reasons = []
    permission_reasons = []
    try:
        terminal = api.terminal_info()
        account = api.account_info()
    except Exception as error:
        return {
            "verified": False,
            "identity_verified": False,
            "trading_permissions_verified": False,
            "reasons": [f"Could not inspect MT5 account state: {error}"],
            "account": None,
        }

    if terminal is None or getattr(terminal, "connected", None) is not True:
        identity_reasons.append("MT5 terminal is not connected.")
    if require_trading_permissions and (
        getattr(terminal, "trade_allowed", None) is not True
        or getattr(terminal, "tradeapi_disabled", None) is True
    ):
        permission_reasons.append("MT5 terminal trading permissions are disabled.")
    if account is None:
        identity_reasons.append("MT5 account information is unavailable.")
        reasons = identity_reasons + permission_reasons
        return {
            "verified": False,
            "identity_verified": False,
            "trading_permissions_verified": (
                not permission_reasons
                if require_trading_permissions
                else None
            ),
            "reasons": reasons,
            "account": None,
        }

    login = getattr(account, "login", None)
    server = getattr(account, "server", None)
    if require_trading_permissions and (
        getattr(account, "trade_allowed", None) is not True
        or getattr(account, "trade_expert", None) is not True
    ):
        permission_reasons.append("MT5 account does not permit expert trading.")
    if config.expected_login is None or login != config.expected_login:
        identity_reasons.append(
            "MT5 account login does not match the saved profile."
        )
    if (
        not config.expected_server
        or not isinstance(server, str)
        or server != config.expected_server
    ):
        identity_reasons.append("MT5 broker server does not match the saved profile.")

    trade_mode = getattr(account, "trade_mode", None)
    actual_account_type = account_type_from_trade_mode(api, trade_mode)
    if config.expected_account_type is None:
        identity_reasons.append("No expected MT5 account type is configured.")
    elif actual_account_type != config.expected_account_type:
        identity_reasons.append(
            "The connected MT5 account type does not match the saved profile."
        )

    reasons = identity_reasons + permission_reasons
    return {
        "verified": not reasons,
        "identity_verified": not identity_reasons,
        "trading_permissions_verified": (
            not permission_reasons
            if require_trading_permissions
            else None
        ),
        "reasons": reasons,
        "account": {
            "login": login,
            "server": server,
            "trade_mode": trade_mode,
            "currency": getattr(account, "currency", None),
        },
    }


def permission_reasons(config, account_check, emergency_stopped=False):
    """Return all reasons entry permission is denied; never grant by UI state."""

    reasons = []
    if config.mode == OperatingMode.RESEARCH:
        reasons.append("RESEARCH mode never permits order submission.")
    elif not config.execution_enabled:
        reasons.append("Order execution is disabled by the operator.")
    if config.mode != OperatingMode.RESEARCH:
        if not config.runner_enabled:
            reasons.append("The persistent trading supervisor is not running.")
        if not config.agent_enabled:
            reasons.append("Automated signal scanning is disabled.")
        if not config.account_verified:
            reasons.append("The saved MT5 account profile has not been verified.")
    if not account_check.get("verified"):
        reasons.extend(account_check.get("reasons", ["Account identity is unverified."]))
    if config.mode == OperatingMode.DEMO and config.expected_account_type != "DEMO":
        reasons.append("DEMO execution requires a saved DEMO account profile.")
    if emergency_stopped:
        reasons.append("Persistent emergency lockout is active.")

    if config.mode == OperatingMode.DEMO:
        if not config.demo_armed:
            reasons.append("Demo order permission has not been explicitly armed.")
    elif config.mode == OperatingMode.LIVE:
        reasons.append("LIVE execution is disabled and unavailable.")
        if not config.live_authorized:
            reasons.append("Explicit backend live authorization is absent.")
        missing = [
            item
            for item, verified in read_live_readiness(
                config.live_readiness_file
            ).items()
            if not verified
        ]
        if missing:
            reasons.append(
                "Live readiness checks are incomplete: " + ", ".join(missing)
            )

    return reasons


def execution_state(config=None, api=None, store=None):
    """Return a display-safe snapshot of the backend's effective permissions."""

    if config is None:
        settings = store.get_operational_config() if store is not None else None
        config = OperatingConfig.from_environment(settings=settings)
    if api is None:
        import MetaTrader5 as api

    emergency = False
    lock_reason = None
    open_position_count = None
    pending_order_count = None
    if store is not None:
        lockout = store.get_lockout()
        emergency = lockout["active"]
        lock_reason = lockout["reason"]

    account_check = verify_account_identity(api, config)
    reasons = permission_reasons(config, account_check, emergency)
    if store is not None and config.mode != OperatingMode.RESEARCH:
        baselines = store.get_active_period_baselines()
        for period in ("daily", "weekly", "monthly"):
            baseline = baselines[period]
            if baseline is None:
                reasons.append(f"The {period} UTC equity baseline is missing.")
                if not store.period_baseline_window_open(period):
                    reasons.append(
                        f"The {period} baseline window is closed; entries "
                        "remain locked until the next period."
                    )
            elif account_check.get("account") and (
                baseline["account_login"] != account_check["account"]["login"]
                or baseline["account_server"] != account_check["account"]["server"]
            ):
                reasons.append(
                    f"The {period} equity baseline belongs to another account."
                )
        try:
            account_info = api.account_info()
        except Exception as error:
            account_info = None
            reasons.append(f"Current MT5 equity is unavailable: {error}")
        equity = getattr(account_info, "equity", None)
        if (
            isinstance(equity, bool)
            or not isinstance(equity, (int, float))
            or not math.isfinite(equity)
            or equity <= 0
        ):
            reasons.append("Current MT5 equity is unavailable or invalid.")
        else:
            loss_limits = {
                "daily": config.max_daily_loss_percent,
                "weekly": config.max_weekly_loss_percent,
                "monthly": config.max_monthly_loss_percent,
            }
            for period, limit in loss_limits.items():
                baseline = baselines[period]
                if baseline is None:
                    continue
                result = check_period_loss(
                    baseline.get("starting_equity"),
                    equity,
                    limit,
                    period=period,
                )
                if not result["allowed"]:
                    reasons.append(result["reason"])
        if account_check.get("verified"):
            try:
                positions = api.positions_get()
                pending = api.orders_get()
            except Exception as error:
                positions = None
                pending = None
                reasons.append(f"MT5 exposure is unavailable: {error}")
            if positions is None or pending is None:
                reasons.append("MT5 open-position or pending-order data is unavailable.")
            else:
                open_position_count = len(positions)
                pending_order_count = len(pending)
                if (
                    open_position_count + pending_order_count
                    >= config.max_open_positions
                ):
                    reasons.append("Maximum simultaneous positions reached.")
    return {
        "mode": config.mode.value,
        "agent_enabled": config.agent_enabled,
        "runner_enabled": config.runner_enabled,
        "execution_enabled": config.execution_enabled,
        "entry_permission": not reasons,
        "account_verified": (
            account_check["identity_verified"] and config.account_verified
        ),
        "account": account_check["account"],
        "open_positions": open_position_count,
        "pending_orders": pending_order_count,
        "emergency_stop": emergency,
        "lock_reason": lock_reason,
        "reasons": reasons,
        "risk_percent": config.risk_percent,
        "max_daily_loss_percent": config.max_daily_loss_percent,
        "max_aggregate_stop_risk_percent": (
            config.max_aggregate_stop_risk_percent
        ),
        "max_open_positions": config.max_open_positions,
        "max_weekly_loss_percent": config.max_weekly_loss_percent,
        "max_monthly_loss_percent": config.max_monthly_loss_percent,
        "max_positions_per_symbol": config.max_positions_per_symbol,
        "max_trades_per_day": config.max_trades_per_day,
        "max_consecutive_losses": config.max_consecutive_losses,
        "max_spread_pips": config.max_spread_pips,
        "max_slippage_pips": config.max_slippage_pips,
        "minimum_reward_risk": config.minimum_reward_risk,
        "session_start_utc": config.session_start_utc,
        "session_end_utc": config.session_end_utc,
        "live_readiness": read_live_readiness(config.live_readiness_file),
    }


