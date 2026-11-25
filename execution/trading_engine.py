from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
import hashlib
import math
import os
from pathlib import Path
import time

import MetaTrader5 as mt5

from execution.account_connections import (
    account_type_from_trade_mode,
    normalize_account_profile,
)
from execution.mt5_connection import mt5_connection
from execution.operating_mode import (
    OperatingConfig,
    OperatingMode,
    execution_state,
    permission_reasons,
    verify_account_identity,
)
from execution.trading_store import (
    ENTRY_BLOCKING_STATUSES,
    RECONCILE_STATUSES,
    TradingStore,
)
from core.market_data import get_live_price
from risk.risk_policy import RiskPolicy, evaluate_entry_limits


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LEDGER = _PROJECT_ROOT / "data" / "trading.sqlite3"
_MAGIC = 722025


class TradeRejected(RuntimeError):
    """Raised when an execution request fails a safety invariant."""


def _finite_positive(value, name):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise TradeRejected(f"{name} must be a finite positive number.")
    return float(value)


def _as_tuple(value, label):
    if value is None:
        raise TradeRejected(f"MT5 could not retrieve {label}.")
    return tuple(value)


def _close_enough(actual, expected, tolerance):
    return (
        isinstance(actual, (int, float))
        and math.isfinite(float(actual))
        and abs(float(actual) - expected) <= tolerance
    )


class TradingEngine:
    """Fail-closed, account-allowlisted MT5 execution and position manager."""

    def __init__(
        self,
        *,
        config=None,
        api=None,
        store=None,
        database_path=None,
        connection_factory=None,
        quote_provider=None,
        sleeper=None,
    ):
        self.api = api or mt5
        if store is not None:
            self.store = store
        else:
            path = database_path or os.environ.get(
                "NEXORA_TRADING_DB",
                str(_DEFAULT_LEDGER),
            )
            self.store = TradingStore(path)
        self._config_from_store = config is None
        self.config = (
            OperatingConfig.from_environment(
                settings=self.store.get_operational_config()
            )
            if config is None
            else config
        )
        self.connection_factory = connection_factory or mt5_connection
        self.quote_provider = quote_provider or get_live_price
        self.sleeper = sleeper or time.sleep

    def refresh_configuration(self):
        if self._config_from_store:
            self.config = OperatingConfig.from_environment(
                settings=self.store.get_operational_config()
            )
        return self.config

    def connected_account(self):
        with self.connection_factory():
            terminal = self.api.terminal_info()
            if terminal is None or getattr(terminal, "connected", None) is not True:
                raise TradeRejected("MT5 terminal is not connected.")
            account = self.api.account_info()
            if account is None:
                raise TradeRejected("MT5 account information is unavailable.")
            account_type = account_type_from_trade_mode(
                self.api,
                getattr(account, "trade_mode", None),
            )
            if account_type is None:
                raise TradeRejected("MT5 account type could not be determined.")
            try:
                identity = normalize_account_profile(
                    getattr(account, "login", None),
                    getattr(account, "server", None),
                    account_type,
                )
            except ValueError as error:
                raise TradeRejected(
                    "Connected MT5 account identity is invalid."
                ) from error
            return {
                **identity,
                "terminal_trade_allowed": (
                    getattr(terminal, "trade_allowed", None) is True
                    and getattr(terminal, "tradeapi_disabled", None) is not True
                ),
                "account_trade_allowed": (
                    getattr(account, "trade_allowed", None) is True
                    and getattr(account, "trade_expert", None) is True
                ),
            }

    def connect_account(self):
        try:
            actual = self.connected_account()
        except Exception:
            self.store.invalidate_account_connection_verification()
            self.refresh_configuration()
            raise

        profile = self.store.get_account_connection()
        actual["matches_saved_profile"] = (
            None
            if profile is None
            else all(
                profile[key] == actual[key]
                for key in ("login", "server", "account_type")
            )
        )
        if actual["matches_saved_profile"] is False:
            self.store.invalidate_account_connection_verification()
            self.refresh_configuration()
        return actual

    def verify_account_connection(self):
        with self.store.entry_lease():
            self.refresh_configuration()
            if self.store.get_account_connection() is None:
                raise TradeRejected("Save an account profile before verification.")
            with self.connection_factory():
                result = verify_account_identity(
                    self.api,
                    self.config,
                    require_trading_permissions=False,
                )
            if not result["verified"]:
                self.store.invalidate_account_connection_verification()
                self.refresh_configuration()
                raise TradeRejected("; ".join(result["reasons"]))
            account = result["account"]
            account_type = account_type_from_trade_mode(
                self.api,
                account.get("trade_mode"),
            )
            if account_type is None:
                self.store.invalidate_account_connection_verification()
                self.refresh_configuration()
                raise TradeRejected("MT5 account type could not be determined.")
            try:
                self.store.mark_account_connection_verified(
                    account.get("login"),
                    account.get("server"),
                    account_type,
                )
            except ValueError as error:
                self.store.invalidate_account_connection_verification()
                self.refresh_configuration()
                raise TradeRejected(str(error)) from error
            self.refresh_configuration()
            return {
                "login": account["login"],
                "server": account["server"],
                "account_type": account_type,
                "verified": True,
            }

    def save_account_connection(self, login, broker_server, account_type):
        result = self.store.save_account_connection(
            login,
            broker_server,
            account_type,
        )
        self.refresh_configuration()
        return result

    def switch_account_connection(self, login, broker_server, account_type):
        result = self.store.switch_account_connection(
            login,
            broker_server,
            account_type,
        )
        self.refresh_configuration()
        return result

    def disconnect_account(self):
        self.store.clear_account_connection()
        self.refresh_configuration()

    def _risk_policy(self):
        return RiskPolicy.from_mapping({
            "risk_per_trade_percent": self.config.risk_percent,
            "max_daily_loss_percent": self.config.max_daily_loss_percent,
            "max_weekly_loss_percent": self.config.max_weekly_loss_percent,
            "max_monthly_loss_percent": self.config.max_monthly_loss_percent,
            "max_total_open_risk_percent": (
                self.config.max_aggregate_stop_risk_percent
            ),
            "max_simultaneous_positions": self.config.max_open_positions,
            "max_positions_per_symbol": self.config.max_positions_per_symbol,
            "max_trades_per_day": self.config.max_trades_per_day,
            "max_consecutive_losses": self.config.max_consecutive_losses,
            "max_spread_pips": self.config.max_spread_pips,
            "max_slippage_pips": self.config.max_slippage_pips,
            "minimum_reward_risk": self.config.minimum_reward_risk,
            "session_start_utc": self.config.session_start_utc,
            "session_end_utc": self.config.session_end_utc,
        })

    def enable_execution(self, *, operator_confirmed=False):
        if operator_confirmed is not True:
            raise TradeRejected("Explicit operator confirmation is required.")
        with self.store.entry_lease():
            self.refresh_configuration()
            if self.config.mode == OperatingMode.RESEARCH:
                raise TradeRejected("Execution cannot be enabled in RESEARCH mode.")
            if self.config.mode == OperatingMode.LIVE:
                raise TradeRejected("LIVE execution is disabled and unavailable.")
            candidate = self.config
            candidate = candidate.__class__(
                **{
                    **candidate.__dict__,
                    "execution_enabled": True,
                    "demo_armed": candidate.mode == OperatingMode.DEMO,
                }
            )
            with self.connection_factory():
                state = execution_state(
                    config=candidate,
                    api=self.api,
                    store=self.store,
                )
                if state["reasons"]:
                    raise TradeRejected("; ".join(state["reasons"]))
            self.store.set_execution_enabled(True)
        self.refresh_configuration()
        self.store.record_event(
            "WARNING",
            "EXECUTION_ENABLED",
            f"Operator enabled {self.config.mode.value} order execution.",
        )
        return self.config.mode.value

    def disable_execution(self, reason="Execution disabled by operator."):
        self.store.set_execution_enabled(False)
        self.refresh_configuration()
        self.store.record_event("WARNING", "EXECUTION_DISABLED", reason)

    def establish_period_baseline(self, period):
        with self.connection_factory():
            account_identity = self._verified_account()
            account = self.api.account_info()
            equity = _finite_positive(
                getattr(account, "equity", None),
                "Account equity",
            )
            return self.store.set_period_baseline(
                period,
                equity,
                int(account_identity["login"]),
                account_identity["server"],
            )

    def establish_available_baselines(self):
        with self.connection_factory():
            results = []
            account_identity = self._verified_account()
            account = self.api.account_info()
            if account is None:
                raise TradeRejected("MT5 account equity is unavailable.")
            equity = _finite_positive(
                getattr(account, "equity", None),
                "Account equity",
            )
            for period in ("daily", "weekly", "monthly"):
                if (
                    self.store.get_period_baseline(period) is None
                    and self.store.period_baseline_window_open(period)
                ):
                    results.append(self.store.set_period_baseline(
                        period,
                        equity,
                        int(account_identity["login"]),
                        account_identity["server"],
                    ))
            return results

    def _verified_account(self):
        self.refresh_configuration()
        result = verify_account_identity(self.api, self.config)
        if not result["identity_verified"]:
            self.store.invalidate_account_connection_verification()
            self.refresh_configuration()
            raise TradeRejected("; ".join(result["reasons"]))
        if not result["verified"]:
            raise TradeRejected("; ".join(result["reasons"]))
        return result["account"]

    def _assert_entry_permission(self, account=None):
        account = account or self._verified_account()
        reasons = permission_reasons(
            self.config,
            {"verified": True, "reasons": [], "account": account},
            emergency_stopped=self.store.get_lockout()["active"],
        )
        if reasons:
            raise TradeRejected("; ".join(reasons))
        return account

    def establish_daily_baseline(self):
        """Persist a once-per-UTC-day equity baseline after identity checks."""

        with self.connection_factory():
            account_identity = self._verified_account()
            account = self.api.account_info()
            equity = _finite_positive(
                getattr(account, "equity", None),
                "Account equity",
            )
            return self.store.set_daily_baseline(
                equity,
                int(account_identity["login"]),
                account_identity["server"],
            )

    def emergency_stop(self, reason="Emergency stop activated by operator."):
        self.store.activate_lockout(reason)
        return self.store.get_lockout()

    def clear_emergency_stop(self, reason, *, operator_confirmed=False):
        if operator_confirmed is not True:
            raise TradeRejected("Explicit operator confirmation is required.")
        with self.connection_factory():
            self._verified_account()
            positions = _as_tuple(self.api.positions_get(), "open positions")
            orders = _as_tuple(self.api.orders_get(), "pending orders")
            if positions or orders:
                raise TradeRejected(
                    "Emergency lockout cannot be cleared while positions or "
                    "pending orders remain."
                )
            self.store.clear_lockout(
                reason,
                operator_confirmed=operator_confirmed,
            )
        return self.store.get_lockout()

    def _account_and_period_baselines(self, account_identity):
        account = self.api.account_info()
        if account is None:
            raise TradeRejected("MT5 account equity is unavailable.")
        equity = _finite_positive(getattr(account, "equity", None), "Account equity")
        baselines = self.store.get_active_period_baselines()
        for period, baseline in baselines.items():
            if baseline is None:
                raise TradeRejected(
                    f"The {period} equity baseline is missing; trading is locked."
                )
            if (
                baseline["account_login"] != account_identity["login"]
                or baseline["account_server"] != account_identity["server"]
            ):
                raise TradeRejected(
                    f"The {period} equity baseline belongs to a different "
                    "MT5 account."
                )
        return account, equity, baselines

    def _broker_spec(self, symbol):
        info = self.api.symbol_info(symbol)
        if info is None:
            raise TradeRejected(f"Broker symbol {symbol} is unavailable.")
        symbol_mode = getattr(info, "trade_mode", None)
        if symbol_mode == getattr(self.api, "SYMBOL_TRADE_MODE_DISABLED", object()):
            raise TradeRejected(f"Broker trading is disabled for {symbol}.")
        spec = {
            "tick_size": getattr(info, "trade_tick_size", None),
            "tick_value": (
                getattr(info, "trade_tick_value_loss", 0)
                or getattr(info, "trade_tick_value", None)
            ),
            "volume_min": getattr(info, "volume_min", None),
            "volume_max": getattr(info, "volume_max", None),
            "volume_step": getattr(info, "volume_step", None),
        }
        for key in spec:
            _finite_positive(spec[key], f"{symbol} {key}")
        return info, spec

    @staticmethod
    def _pip_size(info):
        point = _finite_positive(getattr(info, "point", None), "Broker point size")
        digits = getattr(info, "digits", None)
        if isinstance(digits, int) and not isinstance(digits, bool) and digits in (3, 5):
            return point * 10
        return point

    @staticmethod
    def _deal_time(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            moment = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if not math.isfinite(value):
                raise TradeRejected("Broker deal timestamp is invalid.")
            timestamp = float(value)
            if timestamp > 100000000000:
                timestamp /= 1000.0
            moment = datetime.fromtimestamp(timestamp, timezone.utc)
        elif isinstance(value, str):
            try:
                moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise TradeRejected("Broker deal timestamp is invalid.") from error
        else:
            raise TradeRejected("Broker deal timestamp is invalid.")
        if moment.tzinfo is None or moment.utcoffset() is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat()

    def _closed_trade_record(self, trade, position_identifier, history):
        deals = tuple(history)
        if not deals:
            raise TradeRejected("Broker returned no closed-position deal history.")

        entry_code = getattr(self.api, "DEAL_ENTRY_IN", 0)
        close_codes = {
            getattr(self.api, "DEAL_ENTRY_OUT", 1),
            getattr(self.api, "DEAL_ENTRY_OUT_BY", 3),
        }
        buy_code = getattr(self.api, "DEAL_TYPE_BUY", 0)
        sell_code = getattr(self.api, "DEAL_TYPE_SELL", 1)
        verified_deals = []
        entry_deals = []
        close_deals = []
        for deal in deals:
            deal_position = getattr(deal, "position_id", position_identifier)
            if deal_position != position_identifier:
                raise TradeRejected("Broker history contains another position.")
            ticket = getattr(deal, "ticket", None)
            if isinstance(ticket, bool) or not isinstance(ticket, int) or ticket <= 0:
                raise TradeRejected("Broker history contains an invalid deal ticket.")
            deal_time = self._deal_time(
                getattr(deal, "time_msc", None) or getattr(deal, "time", None)
            )
            if deal_time is None:
                raise TradeRejected("Broker deal history has no timestamp.")

            def finite_deal_value(name, *, positive=False):
                raw = getattr(deal, name, 0.0)
                if (
                    isinstance(raw, bool)
                    or not isinstance(raw, (int, float))
                    or not math.isfinite(raw)
                    or (positive and raw <= 0)
                    or (not positive and raw < 0 and name == "volume")
                ):
                    raise TradeRejected(
                        f"Broker deal history has an invalid {name}."
                    )
                return float(raw)

            volume = finite_deal_value("volume")
            price = finite_deal_value("price", positive=True)
            profit = finite_deal_value("profit")
            commission = finite_deal_value("commission")
            swap = finite_deal_value("swap")
            fee = finite_deal_value("fee")
            deal_type = getattr(deal, "type", None)
            deal_entry = getattr(deal, "entry", None)
            row = {
                "deal_ticket": ticket,
                "position_identifier": int(position_identifier),
                "deal_time": deal_time,
                "deal_type": deal_type,
                "deal_entry": deal_entry,
                "volume": volume,
                "price": price,
                "profit": profit,
                "commission": commission,
                "swap": swap,
                "fee": fee,
            }
            verified_deals.append(row)
            if deal_entry == entry_code:
                if deal_type not in {buy_code, sell_code} or volume <= 0:
                    raise TradeRejected("Broker entry deal is incomplete.")
                entry_deals.append((row, deal_type))
            elif deal_entry in close_codes:
                if volume <= 0:
                    raise TradeRejected("Broker exit deal has invalid volume.")
                close_deals.append((row, getattr(deal, "reason", None)))
            else:
                raise TradeRejected(
                    "Broker history contains an unsupported position reversal."
                )

        if not entry_deals or not close_deals:
            raise TradeRejected("Broker history does not prove a complete close.")
        entry_volume = sum(row["volume"] for row, _ in entry_deals)
        exit_volume = sum(row["volume"] for row, _ in close_deals)
        spec = self.api.symbol_info(trade["symbol"])
        if spec is None:
            raise TradeRejected(
                f"Broker specifications unavailable for {trade['symbol']}."
            )
        volume_step = _finite_positive(
            getattr(spec, "volume_step", None),
            "Broker volume step",
        )
        if exit_volume + volume_step * 1e-6 < entry_volume:
            raise TradeRejected("Broker history does not confirm the full close.")
        if len({deal["deal_ticket"] for deal in verified_deals}) != len(
            verified_deals
        ):
            raise TradeRejected("Broker history contains duplicate deal tickets.")

        first_type = entry_deals[0][1]
        if any(deal_type != first_type for _, deal_type in entry_deals):
            raise TradeRejected("Broker entry history has conflicting directions.")
        direction = "BUY" if first_type == buy_code else "SELL"
        average_entry = sum(
            row["price"] * row["volume"] for row, _ in entry_deals
        ) / entry_volume
        average_exit = sum(
            row["price"] * row["volume"] for row, _ in close_deals
        ) / exit_volume
        entry_time = min(row["deal_time"] for row, _ in entry_deals)
        exit_time = max(row["deal_time"] for row, _ in close_deals)
        entry_moment = datetime.fromisoformat(entry_time)
        exit_moment = datetime.fromisoformat(exit_time)
        if exit_moment < entry_moment:
            raise TradeRejected("Broker history has an exit before the entry.")

        account = self.api.account_info()
        if account is None:
            raise TradeRejected("MT5 account information is unavailable.")
        account_identity = (
            f"{trade['account_login']}@{trade['account_server']}"
        )
        realized_profit = sum(deal["profit"] for deal in verified_deals)
        commission = sum(deal["commission"] for deal in verified_deals)
        swap = sum(deal["swap"] for deal in verified_deals)
        fees = sum(deal["fee"] for deal in verified_deals)
        exit_reason = "BROKER_CLOSED"
        for _, reason in close_deals:
            if reason == getattr(self.api, "DEAL_REASON_SL", object()):
                exit_reason = "STOP_LOSS"
                break
            if reason == getattr(self.api, "DEAL_REASON_TP", object()):
                exit_reason = "TAKE_PROFIT"
        record = {
            "trade_id": trade["trade_id"],
            "position_identifier": int(position_identifier),
            "symbol": trade["symbol"],
            "direction": direction,
            "entry_time": entry_time,
            "entry_price": average_entry,
            "exit_time": exit_time,
            "exit_price": average_exit,
            "volume": entry_volume,
            "initial_stop_loss": float(trade["stop_loss"]),
            "initial_take_profit": float(trade["take_profit"]),
            "final_stop_loss": trade.get("final_stop_loss") or trade["stop_loss"],
            "final_take_profit": (
                trade.get("final_take_profit") or trade["take_profit"]
            ),
            "risk_percent": float(trade.get("risk_percent") or 0.25),
            "planned_risk_amount": float(trade["initial_risk"]),
            "realized_profit": realized_profit,
            "commission": commission,
            "swap": swap,
            "fees": fees,
            "net_profit": realized_profit + commission + swap + fees,
            "strategy": trade.get("strategy") or "SMC_MTF",
            "signal_id": trade.get("signal_id"),
            "exit_reason": exit_reason,
            "duration_seconds": (exit_moment - entry_moment).total_seconds(),
            "mode": trade["mode"],
            "account_reference": hashlib.sha256(
                account_identity.encode("utf-8")
            ).hexdigest(),
            "currency": getattr(account, "currency", "UNKNOWN"),
            "deal_tickets": [deal["deal_ticket"] for deal in verified_deals],
        }
        return record, verified_deals

    @staticmethod
    def _align_price(price, tick_size, rounding):
        price_value = Decimal(str(price))
        tick = Decimal(str(tick_size))
        aligned = (price_value / tick).to_integral_value(rounding=rounding) * tick
        return float(aligned)

    def _profit_at_stop(self, order_type, symbol, volume, entry, stop):
        volume = _finite_positive(volume, "Trade volume")
        entry = _finite_positive(entry, "Entry price")
        stop = _finite_positive(stop, "Stop loss")
        result = self.api.order_calc_profit(
            order_type,
            symbol,
            float(volume),
            float(entry),
            float(stop),
        )
        if (
            result is None
            or isinstance(result, bool)
            or not isinstance(result, (int, float))
            or not math.isfinite(result)
            or result >= 0
        ):
            raise TradeRejected(
                f"Broker could not verify stop risk for {symbol}."
            )
        return abs(float(result))

    def _open_position_risk(self, position):
        volume = _finite_positive(position.volume, "Open position volume")
        entry = _finite_positive(position.price_open, "Open position entry")
        stop = _finite_positive(position.sl, "Open position stop loss")
        order_type = (
            self.api.ORDER_TYPE_BUY
            if position.type == self.api.POSITION_TYPE_BUY
            else self.api.ORDER_TYPE_SELL
        )
        result = self.api.order_calc_profit(
            order_type,
            position.symbol,
            volume,
            entry,
            stop,
        )
        if (
            result is None
            or isinstance(result, bool)
            or not isinstance(result, (int, float))
            or not math.isfinite(result)
        ):
            raise TradeRejected(
                f"Broker could not verify open risk for {position.symbol}."
            )
        return max(-float(result), 0.0)

    def _size_order(
        self,
        symbol,
        direction,
        order_type,
        entry,
        stop,
        take_profit,
        equity,
        info,
        spec,
    ):
        if direction == "BUY" and not stop < entry < take_profit:
            raise TradeRejected("BUY requires stop < current entry < target.")
        if direction == "SELL" and not take_profit < entry < stop:
            raise TradeRejected("SELL requires target < current entry < stop.")

        symbol_mode = getattr(info, "trade_mode", None)
        allowed_modes = (
            {
                getattr(self.api, "SYMBOL_TRADE_MODE_FULL", object()),
                getattr(self.api, "SYMBOL_TRADE_MODE_LONGONLY", object()),
            }
            if direction == "BUY"
            else {
                getattr(self.api, "SYMBOL_TRADE_MODE_FULL", object()),
                getattr(self.api, "SYMBOL_TRADE_MODE_SHORTONLY", object()),
            }
        )
        if symbol_mode not in allowed_modes:
            raise TradeRejected(
                f"Broker symbol mode does not permit {direction} entries."
            )
        point = _finite_positive(getattr(info, "point", None), "Broker point size")
        stops_level = getattr(info, "trade_stops_level", 0) or 0
        minimum_distance = float(stops_level) * point
        if min(abs(entry - stop), abs(take_profit - entry)) < minimum_distance:
            raise TradeRejected("Stop-loss or target violates broker stop distance.")

        risk_budget = equity * self.config.risk_percent / 100.0
        minimum = Decimal(str(spec["volume_min"]))
        maximum = Decimal(str(spec["volume_max"]))
        step = Decimal(str(spec["volume_step"]))
        min_volume = float(minimum)
        min_risk = self._profit_at_stop(
            order_type,
            symbol,
            min_volume,
            entry,
            stop,
        )
        risk_per_lot = min_risk / min_volume
        if risk_per_lot <= 0 or risk_budget < min_risk:
            raise TradeRejected(
                "Broker minimum volume exceeds the 0.25% per-trade risk budget."
            )

        max_risk_volume = Decimal(str(risk_budget / risk_per_lot))
        if max_risk_volume < minimum:
            raise TradeRejected("Risk budget is below broker minimum volume.")
        step_count = (
            (min(maximum, max_risk_volume) - minimum) / step
        ).to_integral_value(rounding=ROUND_DOWN)
        volume = minimum + step_count * step
        estimated_risk = self._profit_at_stop(
            order_type,
            symbol,
            float(volume),
            entry,
            stop,
        )
        while estimated_risk > risk_budget and volume - step >= minimum:
            volume -= step
            estimated_risk = self._profit_at_stop(
                order_type,
                symbol,
                float(volume),
                entry,
                stop,
            )
        if estimated_risk > risk_budget:
            raise TradeRejected("Position size cannot satisfy the risk budget.")

        margin = self.api.order_calc_margin(
            order_type,
            symbol,
            float(volume),
            float(entry),
        )
        current_account = self.api.account_info()
        if current_account is None:
            raise TradeRejected("MT5 account information is unavailable.")
        free_margin = getattr(current_account, "margin_free", None)
        if (
            margin is None
            or not isinstance(margin, (int, float))
            or not math.isfinite(margin)
            or margin < 0
        ):
            raise TradeRejected("Broker could not verify required margin.")
        if (
            free_margin is None
            or not isinstance(free_margin, (int, float))
            or not math.isfinite(free_margin)
            or free_margin < margin
        ):
            raise TradeRejected("Required margin exceeds available free margin.")

        return float(volume), estimated_risk, float(margin)

    def _calculate_portfolio_risk(self):
        positions = _as_tuple(self.api.positions_get(), "open positions")
        pending_orders = _as_tuple(self.api.orders_get(), "pending orders")
        known_trades = self.store.list_trades(
            statuses=("OPEN", "UNPROTECTED")
        )
        if any(trade["mode"] != self.config.mode.value for trade in known_trades):
            self.store.activate_lockout(
                "Open trade mode does not match the configured operating mode."
            )
            raise TradeRejected(
                "Operating mode changed while a managed position remains open."
            )
        known_tickets = {
            int(trade["position_ticket"])
            for trade in known_trades
            if trade.get("position_ticket") is not None
        }
        open_tickets = {int(position.ticket) for position in positions}
        untracked = open_tickets - known_tickets
        missing = known_tickets - open_tickets
        if untracked or missing or pending_orders:
            reason = "Unreconciled broker positions or pending orders detected."
            self.store.activate_lockout(reason)
            raise TradeRejected(
                reason + " Reconcile the account before enabling entries."
            )
        total_risk = 0.0
        for position in positions:
            stop = getattr(position, "sl", 0)
            if not stop:
                self.store.activate_lockout(
                    f"Open position {getattr(position, 'ticket', '?')} has no stop loss."
                )
                raise TradeRejected(
                    "An open position has no protective stop; entries are locked."
                )
            _finite_positive(stop, "Open position stop loss")
            if getattr(position, "magic", None) != _MAGIC:
                self.store.activate_lockout(
                    f"Untracked position {position.ticket} detected."
                )
                raise TradeRejected(
                    "Untracked MT5 positions are not eligible for automatic execution."
                )
            total_risk += self._open_position_risk(position)
        for order in pending_orders:
            stop = getattr(order, "sl", 0)
            if not stop:
                self.store.activate_lockout(
                    f"Pending order {getattr(order, 'ticket', '?')} has no stop loss."
                )
                raise TradeRejected(
                    "A pending order has no protective stop; entries are locked."
                )
            _finite_positive(stop, "Pending order stop loss")
            order_type = self._pending_order_type(order.type)
            total_risk += self._profit_at_stop(
                order_type,
                order.symbol,
                order.volume_current,
                order.price_open,
                stop,
            )
        return positions, pending_orders, total_risk

    def _pending_order_type(self, pending_type):
        buy_types = {
            getattr(self.api, name)
            for name in (
                "ORDER_TYPE_BUY_LIMIT",
                "ORDER_TYPE_BUY_STOP",
                "ORDER_TYPE_BUY_STOP_LIMIT",
            )
            if hasattr(self.api, name)
        }
        return (
            self.api.ORDER_TYPE_BUY
            if pending_type in buy_types
            else self.api.ORDER_TYPE_SELL
        )

    def _filling_mode(self, info):
        allowed = getattr(info, "filling_mode", 0)
        if (
            allowed & getattr(self.api, "SYMBOL_FILLING_FOK", 1)
            and hasattr(self.api, "ORDER_FILLING_FOK")
        ):
            return self.api.ORDER_FILLING_FOK
        if (
            allowed & getattr(self.api, "SYMBOL_FILLING_IOC", 2)
            and hasattr(self.api, "ORDER_FILLING_IOC")
        ):
            return self.api.ORDER_FILLING_IOC
        raise TradeRejected("No supported MT5 order filling policy is available.")

    def submit_signal(self, symbol, signal_result, *, signal_id=None):
        """Submit one confirmed signal, gated and idempotent across processes."""

        if not isinstance(symbol, str) or not symbol.strip():
            raise TradeRejected("A valid symbol is required.")
        if not isinstance(signal_result, dict):
            raise TradeRejected("Signal result must be a mapping.")
        classification = str(signal_result.get("classification", "")).upper()
        if classification != "CONFIRMED":
            raise TradeRejected("Only CONFIRMED analytical signals can be planned.")
        direction = str(signal_result.get("direction", "")).upper()
        if direction in {"BULLISH", "BUY"}:
            direction = "BUY"
            order_type = self.api.ORDER_TYPE_BUY
        elif direction in {"BEARISH", "SELL"}:
            direction = "SELL"
            order_type = self.api.ORDER_TYPE_SELL
        else:
            raise TradeRejected("Signal direction must be bullish or bearish.")

        signal_timestamp = signal_result.get("signal_timestamp")
        if not signal_id and not signal_timestamp:
            raise TradeRejected("A stable signal ID or timestamp is required.")
        idempotency_key = f"{symbol.strip()}:{signal_id or signal_timestamp}"
        stop = _finite_positive(signal_result.get("stop_loss"), "Stop loss")
        target = _finite_positive(signal_result.get("take_profit"), "Take profit")

        with self.connection_factory():
            with self.store.entry_lease():
                self.refresh_configuration()
                account_identity = self._verified_account()
                self._assert_entry_permission(account_identity)
                previous = self.store.get_trade_by_key(idempotency_key)
                if previous is not None:
                    return {
                        "submitted": False,
                        "duplicate": True,
                        "reason": "This signal already has an execution record.",
                        "trade": previous,
                    }
                unresolved = self.store.list_trades(
                    statuses=ENTRY_BLOCKING_STATUSES,
                )
                if unresolved:
                    self.store.activate_lockout(
                        "Unresolved order record requires broker reconciliation."
                    )
                    raise TradeRejected(
                        "An earlier order is unresolved; entries are locked."
                    )
                account, equity, baselines = self._account_and_period_baselines(
                    account_identity
                )
                positions, pending, open_risk = self._calculate_portfolio_risk()
                aggregate_budget = (
                    equity
                    * self.config.max_aggregate_stop_risk_percent
                    / 100.0
                )
                symbol = symbol.strip()
                symbol_count = sum(
                    position.symbol == symbol for position in positions
                ) + sum(order.symbol == symbol for order in pending)
                if symbol_count >= self.config.max_positions_per_symbol:
                    raise TradeRejected(
                        f"Maximum positions per symbol reached for {symbol}."
                    )
                if symbol_count and getattr(
                    account,
                    "margin_mode",
                    None,
                ) != getattr(self.api, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", object()):
                    raise TradeRejected(
                        "Multiple positions per symbol require a verified MT5 "
                        "hedging account."
                    )

                info, spec = self._broker_spec(symbol)
                quote = self.quote_provider(symbol)
                pip_size = self._pip_size(info)
                bid = _finite_positive(quote.get("bid"), "Live bid")
                ask = _finite_positive(quote.get("ask"), "Live ask")
                if ask < bid:
                    raise TradeRejected("Live quote has an inverted spread.")
                spread_pips = (ask - bid) / pip_size
                entry_rounding = ROUND_CEILING if direction == "BUY" else ROUND_FLOOR
                entry = self._align_price(
                    ask if direction == "BUY" else bid,
                    spec["tick_size"],
                    entry_rounding,
                )
                entry = _finite_positive(entry, "Market entry price")
                stop = self._align_price(
                    stop,
                    spec["tick_size"],
                    ROUND_FLOOR if direction == "BUY" else ROUND_CEILING,
                )
                target = self._align_price(
                    target,
                    spec["tick_size"],
                    ROUND_CEILING if direction == "BUY" else ROUND_FLOOR,
                )
                stop_distance = abs(entry - stop)
                reward_risk = abs(target - entry) / stop_distance
                utc_day_start = datetime.now(timezone.utc).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                ).isoformat()
                risk_reasons = evaluate_entry_limits(
                    self._risk_policy(),
                    equity=equity,
                    baselines=baselines,
                    total_open_risk=open_risk,
                    current_positions=len(positions) + len(pending),
                    positions_by_symbol={symbol: symbol_count + 1},
                    trades_today=self.store.count_trades_since(utc_day_start),
                    consecutive_losses=self.store.consecutive_closed_losses(),
                    spread_pips=spread_pips,
                    reward_risk=reward_risk,
                )
                if risk_reasons:
                    if any(
                        "loss reached" in reason.lower()
                        or "consecutive-loss" in reason.lower()
                        for reason in risk_reasons
                    ):
                        self.store.activate_lockout("; ".join(risk_reasons))
                    raise TradeRejected("; ".join(risk_reasons))
                volume, estimated_risk, margin = self._size_order(
                    symbol,
                    direction,
                    order_type,
                    entry,
                    stop,
                    target,
                    equity,
                    info,
                    spec,
                )
                if open_risk + estimated_risk > aggregate_budget:
                    raise TradeRejected(
                        "This order would exceed maximum aggregate stop-risk."
                    )

                reservation = self.store.reserve_trade({
                    "idempotency_key": idempotency_key,
                    "signal_id": str(signal_id) if signal_id else None,
                    "symbol": symbol,
                    "direction": direction,
                    "mode": self.config.mode.value,
                    "account_login": int(account_identity["login"]),
                    "account_server": account_identity["server"],
                    "requested_volume": volume,
                    "entry_price": entry,
                    "stop_loss": stop,
                    "take_profit": target,
                    "initial_risk": estimated_risk,
                    "risk_percent": self.config.risk_percent,
                    "strategy": str(signal_result.get("strategy", "SMC_MTF")),
                    "currency": getattr(account, "currency", "UNKNOWN"),
                })
                if not reservation["inserted"]:
                    return {
                        "submitted": False,
                        "duplicate": True,
                        "reason": "This signal already has an execution record.",
                        "trade": reservation["trade"],
                    }

                request = {
                    "action": self.api.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": volume,
                    "type": order_type,
                    "price": entry,
                    "sl": stop,
                    "tp": target,
                    "deviation": max(
                        1,
                        math.ceil(
                            self.config.max_slippage_pips
                            * pip_size
                            / _finite_positive(
                                getattr(info, "point", None),
                                "Broker point size",
                            )
                        ),
                    ),
                    "magic": _MAGIC,
                    "comment": "NEXORA guarded entry",
                    "type_time": self.api.ORDER_TIME_GTC,
                    "type_filling": self._filling_mode(info),
                }
                check = self.api.order_check(request)
                if check is None or getattr(check, "retcode", None) != 0:
                    detail = (
                        getattr(check, "comment", "Broker order check failed.")
                        if check is not None
                        else "Broker order check returned no result."
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        status="REJECTED",
                        error=str(detail),
                    )
                    raise TradeRejected(str(detail))

                result = self.api.order_send(request)
                done_codes = {
                    getattr(self.api, "TRADE_RETCODE_DONE", -1),
                    getattr(self.api, "TRADE_RETCODE_DONE_PARTIAL", -2),
                }
                if result is None or getattr(result, "retcode", None) not in done_codes:
                    detail = (
                        getattr(result, "comment", "MT5 order was rejected.")
                        if result is not None
                        else "MT5 order submission returned no result."
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        status="REJECTED",
                        error=str(detail),
                    )
                    self.store.record_event("ERROR", "ORDER_REJECTED", detail)
                    raise TradeRejected(str(detail))

                ticket = getattr(result, "order", None) or getattr(result, "deal", None)
                position = self._find_position(symbol, direction, volume, ticket)
                if position is None:
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        status="PENDING_CONFIRMATION",
                        error="Order accepted; position confirmation is pending.",
                    )
                    self.store.activate_lockout(
                        "Broker accepted an order but the position could not be verified."
                    )
                    return {
                        "submitted": True,
                        "protected": False,
                        "reason": "Broker acceptance needs manual reconciliation.",
                        "trade": self.store.get_trade(
                            reservation["trade"]["trade_id"]
                        ),
                    }
                if (
                    position.symbol != symbol
                    or getattr(position, "magic", None) != _MAGIC
                    or float(position.volume) > volume + 1e-8
                    or (
                        position.type == self.api.POSITION_TYPE_BUY
                    ) != (direction == "BUY")
                    or getattr(position, "magic", None) != _MAGIC
                ):
                    detail = "Broker position does not match the reserved order."
                    position_info = self.api.symbol_info(position.symbol)
                    closed = (
                        position_info is not None
                        and self._close_position(position, position_info)
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        position_ticket=int(position.ticket),
                        position_identifier=int(
                            getattr(position, "identifier", position.ticket)
                        ),
                        actual_volume=float(position.volume),
                        status=(
                            "POSITION_MISMATCH_CLOSED"
                            if closed
                            else "UNPROTECTED"
                        ),
                        error=detail,
                        closed_at=(
                            datetime.now(timezone.utc).isoformat()
                            if closed
                            else None
                        ),
                    )
                    self.store.activate_lockout(detail)
                    raise TradeRejected(detail)

                protection_ok = self._ensure_protection(
                    position,
                    stop,
                    target,
                    info,
                )
                if not protection_ok:
                    closed = self._close_position(position, info)
                    detail = (
                        "Protective stop/target could not be verified."
                        if not closed
                        else "Protective levels failed verification; position was closed."
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        position_ticket=int(position.ticket),
                        position_identifier=int(
                            getattr(position, "identifier", position.ticket)
                        ),
                        actual_volume=float(position.volume),
                        status=(
                            "PROTECTION_FAILED_CLOSED"
                            if closed
                            else "UNPROTECTED"
                        ),
                        error=detail,
                        closed_at=(
                            datetime.now(timezone.utc).isoformat()
                            if closed
                            else None
                        ),
                    )
                    self.store.activate_lockout(detail)
                    raise TradeRejected(detail)

                verified_positions = self.api.positions_get(
                    ticket=position.ticket
                )
                if verified_positions is None or not verified_positions:
                    detail = "Protective position could not be re-read after verification."
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        position_ticket=int(position.ticket),
                        position_identifier=int(
                            getattr(position, "identifier", position.ticket)
                        ),
                        status="UNPROTECTED",
                        error=detail,
                    )
                    self.store.activate_lockout(detail)
                    raise TradeRejected(detail)
                position = verified_positions[0]
                slippage_pips = abs(float(position.price_open) - entry) / pip_size
                if slippage_pips > self.config.max_slippage_pips:
                    closed = self._close_position(position, info)
                    detail = (
                        f"Fill slippage {slippage_pips:.2f} pips exceeded "
                        f"the {self.config.max_slippage_pips:.2f}-pip limit; "
                        + ("position was closed." if closed else "position remains open.")
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        position_ticket=int(position.ticket),
                        position_identifier=int(
                            getattr(position, "identifier", position.ticket)
                        ),
                        actual_volume=float(position.volume),
                        status="SLIPPAGE_LIMIT_BREACH_CLOSED" if closed else "OPEN",
                        error=detail,
                        closed_at=(
                            datetime.now(timezone.utc).isoformat()
                            if closed
                            else None
                        ),
                    )
                    self.store.activate_lockout(detail)
                    raise TradeRejected(detail)
                actual_risk = self._profit_at_stop(
                    order_type,
                    symbol,
                    position.volume,
                    position.price_open,
                    _finite_positive(position.sl, "Verified stop loss"),
                )
                actual_risk_budget = equity * self.config.risk_percent / 100.0
                if (
                    actual_risk > actual_risk_budget
                    or open_risk + actual_risk > aggregate_budget
                ):
                    closed = self._close_position(position, info)
                    detail = (
                        "Filled position exceeded configured risk; "
                        + (
                            "position was closed."
                            if closed
                            else "position remains protected but requires review."
                        )
                    )
                    self.store.update_trade(
                        reservation["trade"]["trade_id"],
                        order_ticket=ticket,
                        position_ticket=int(position.ticket),
                        actual_volume=float(position.volume),
                        position_identifier=int(
                            getattr(position, "identifier", position.ticket)
                        ),
                        status=(
                            "RISK_LIMIT_BREACH_CLOSED"
                            if closed
                            else "OPEN"
                        ),
                        error=detail,
                        closed_at=(
                            datetime.now(timezone.utc).isoformat()
                            if closed
                            else None
                        ),
                    )
                    self.store.activate_lockout(detail)
                    raise TradeRejected(detail)

                self.store.update_trade(
                    reservation["trade"]["trade_id"],
                    order_ticket=ticket,
                    position_ticket=int(position.ticket),
                    position_identifier=int(
                        getattr(position, "identifier", position.ticket)
                    ),
                    actual_volume=float(position.volume),
                    entry_time=self._deal_time(getattr(position, "time", None)),
                    final_stop_loss=float(position.sl),
                    final_take_profit=float(position.tp),
                    status="OPEN",
                    error=None,
                )
                self.store.record_event(
                    "INFO",
                    "ORDER_OPENED",
                    f"{symbol} {direction} volume={position.volume} margin≈{margin}",
                )
                return {
                    "submitted": True,
                    "protected": True,
                    "reason": "Order accepted and protective levels verified.",
                    "trade": self.store.get_trade(reservation["trade"]["trade_id"]),
                }

    def _find_position(self, symbol, direction, requested_volume, ticket):
        for attempt in range(3):
            positions = _as_tuple(self.api.positions_get(symbol=symbol), "position")
            for position in positions:
                side = (
                    "BUY"
                    if position.type == self.api.POSITION_TYPE_BUY
                    else "SELL"
                )
                ticket_matches = ticket is not None and (
                    position.ticket == ticket
                    or getattr(position, "identifier", None) == ticket
                )
                matches_request = (
                    position.symbol == symbol
                    and side == direction
                    and getattr(position, "magic", _MAGIC) == _MAGIC
                    and float(position.volume) <= requested_volume + 1e-8
                )
                if side == direction and position.symbol == symbol and (
                    ticket_matches or matches_request
                ):
                    return position
            if attempt < 2:
                self.sleeper(0.1)
        return None

    def _ensure_protection(self, position, stop, target, info):
        point = _finite_positive(getattr(info, "point", None), "Broker point size")
        tolerance = point * 2
        if _close_enough(position.sl, stop, tolerance) and _close_enough(
            position.tp,
            target,
            tolerance,
        ):
            return True

        request = {
            "action": self.api.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": stop,
            "tp": target,
            "magic": _MAGIC,
        }
        result = self.api.order_send(request)
        done = getattr(self.api, "TRADE_RETCODE_DONE", -1)
        if result is None or getattr(result, "retcode", None) != done:
            return False
        refreshed = self.api.positions_get(ticket=position.ticket)
        if refreshed is None or not refreshed:
            return False
        verified = refreshed[0]
        return (
            _close_enough(verified.sl, stop, tolerance)
            and _close_enough(verified.tp, target, tolerance)
        )

    def _close_position(self, position, info):
        quote = self.quote_provider(position.symbol)
        is_buy = position.type == self.api.POSITION_TYPE_BUY
        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "position": position.ticket,
            "volume": float(position.volume),
            "type": (
                self.api.ORDER_TYPE_SELL if is_buy else self.api.ORDER_TYPE_BUY
            ),
            "price": quote["bid"] if is_buy else quote["ask"],
            "deviation": 10,
            "magic": _MAGIC,
            "comment": "NEXORA protection failure close",
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        result = self.api.order_send(request)
        accepted = (
            result is not None
            and getattr(result, "retcode", None)
            == getattr(self.api, "TRADE_RETCODE_DONE", -1)
        )
        if not accepted:
            return False
        refreshed = self.api.positions_get(ticket=position.ticket)
        return refreshed is not None and not refreshed

    def close_managed_position(self, ticket, *, operator_confirmed=False):
        if operator_confirmed is not True:
            raise TradeRejected("Explicit operator confirmation is required.")
        if isinstance(ticket, bool) or not isinstance(ticket, int) or ticket <= 0:
            raise TradeRejected("A valid managed position ticket is required.")
        with self.connection_factory():
            account = self._verified_account()
            matches = self.store.list_trades(
                statuses=("OPEN", "UNPROTECTED")
            )
            trade = next(
                (
                    row
                    for row in matches
                    if row.get("position_ticket") == ticket
                ),
                None,
            )
            if trade is None:
                raise TradeRejected("Position is not in the NEXORA trade ledger.")
            if (
                trade["account_login"] != account["login"]
                or trade["account_server"] != account["server"]
                or trade["mode"] != self.config.mode.value
            ):
                raise TradeRejected("Managed position identity does not match.")
            positions = self.api.positions_get(ticket=ticket)
            if positions is None or not positions:
                raise TradeRejected("Managed position is not available at MT5.")
            position = positions[0]
            if getattr(position, "magic", None) != _MAGIC:
                raise TradeRejected("Position magic number does not match NEXORA.")
            info = self.api.symbol_info(position.symbol)
            if info is None or not self._close_position(position, info):
                raise TradeRejected(
                    "Broker did not verify the managed position close."
                )
            position_identifier = int(
                trade.get("position_identifier") or position.ticket
            )
            history = self.api.history_deals_get(position=position_identifier)
            if history is None:
                self.store.activate_lockout(
                    f"Manual close history unavailable for position {ticket}."
                )
                raise TradeRejected(
                    "Position closed, but broker history is unavailable."
                )
            try:
                closed_record, deal_rows = self._closed_trade_record(
                    trade,
                    position_identifier,
                    history,
                )
                closed_record["exit_reason"] = "OPERATOR_CLOSE"
                self.store.record_closed_trade(closed_record, deal_rows)
            except (TradeRejected, ValueError, TypeError) as error:
                self.store.activate_lockout(
                    f"Manual close history invalid for position {ticket}: {error}"
                )
                raise TradeRejected(
                    "Position closed, but broker history needs reconciliation."
                ) from error
            self.store.update_trade(
                trade["trade_id"],
                status="CLOSED",
                closed_at=closed_record["exit_time"],
                realized_profit=closed_record["net_profit"],
                exit_reason="OPERATOR_CLOSE",
            )
            return self.store.get_trade(trade["trade_id"])

    def reconcile_open_trades(self):
        """Reconcile durable open records against positions after a restart."""

        with self.connection_factory():
            account = self._verified_account()
            positions = _as_tuple(self.api.positions_get(), "open positions")
            by_ticket = {int(position.ticket): position for position in positions}
            trades = self.store.list_trades(
                statuses=RECONCILE_STATUSES,
            )
            recovered = []
            for trade in trades:
                if trade["mode"] != self.config.mode.value:
                    self.store.activate_lockout(
                        "Recovered trade mode does not match configured operating mode."
                    )
                    raise TradeRejected(
                        "Restore the trade's original mode before reconciliation."
                    )
                if (
                    trade["account_login"] != account["login"]
                    or trade["account_server"] != account["server"]
                ):
                    self.store.activate_lockout(
                        "Recovered trade belongs to a different account identity."
                    )
                    raise TradeRejected("Recovered trade identity mismatch.")
                ticket = trade.get("position_ticket")
                position = by_ticket.get(int(ticket)) if ticket else None
                if position is None and trade["status"] == "PENDING_CONFIRMATION":
                    self.store.activate_lockout(
                        "Order acceptance remains unconfirmed after restart."
                    )
                    recovered.append({
                        "trade_id": trade["trade_id"],
                        "status": "PENDING_CONFIRMATION",
                    })
                    continue
                if position is None:
                    position_identifier = int(
                        trade.get("position_identifier") or ticket
                    )
                    history = (
                        self.api.history_deals_get(position=position_identifier)
                        if ticket
                        else None
                    )
                    if history is None:
                        self.store.activate_lockout(
                            "Unable to verify closed-position history after restart."
                        )
                        raise TradeRejected(
                            "Broker history is unavailable during reconciliation."
                        )
                    if not history:
                        self.store.activate_lockout(
                            f"Position {ticket} disappeared without broker close history."
                        )
                        raise TradeRejected(
                            "Position closure could not be verified."
                        )
                    try:
                        closed_record, deal_rows = self._closed_trade_record(
                            trade,
                            position_identifier,
                            history,
                        )
                        self.store.record_closed_trade(closed_record, deal_rows)
                    except (TradeRejected, ValueError, TypeError) as error:
                        self.store.activate_lockout(
                            f"Closed trade {trade['trade_id']} history is invalid: {error}"
                        )
                        raise TradeRejected(
                            "Broker close history could not be safely reconciled."
                        ) from error
                    self.store.update_trade(
                        trade["trade_id"],
                        status="CLOSED",
                        closed_at=closed_record["exit_time"],
                        realized_profit=closed_record["net_profit"],
                        exit_reason=closed_record["exit_reason"],
                        final_stop_loss=closed_record["final_stop_loss"],
                        final_take_profit=closed_record["final_take_profit"],
                    )
                    recovered.append({
                        "trade_id": trade["trade_id"],
                        "status": "CLOSED",
                    })
                    continue
                if getattr(position, "magic", None) != _MAGIC:
                    self.store.activate_lockout(
                        f"Recovered position {position.ticket} magic mismatch."
                    )
                    raise TradeRejected(
                        "Recovered position does not belong to NEXORA."
                    )
                if not position.sl or not position.tp:
                    self.store.activate_lockout(
                        f"Recovered position {position.ticket} lacks verified protection."
                    )
                    self.store.update_trade(
                        trade["trade_id"],
                        status="UNPROTECTED",
                        error="Recovered position lacks verified protection.",
                    )
                    recovered.append({
                        "trade_id": trade["trade_id"],
                        "status": "UNPROTECTED",
                    })
                    continue
                self.store.update_trade(trade["trade_id"], status="OPEN")
                recovered.append({"trade_id": trade["trade_id"], "status": "OPEN"})
            return recovered

    def _modify_stop(self, position, new_stop):
        info = self.api.symbol_info(position.symbol)
        if info is None:
            return False
        point = _finite_positive(getattr(info, "point", None), "Broker point size")
        is_buy = position.type == self.api.POSITION_TYPE_BUY
        old_stop = float(position.sl or 0)
        tick_size = _finite_positive(
            getattr(info, "trade_tick_size", None),
            "Broker tick size",
        )
        tolerance = max(point, tick_size) * 2
        new_stop = self._align_price(
            new_stop,
            tick_size,
            ROUND_FLOOR if is_buy else ROUND_CEILING,
        )
        improves = (
            new_stop > old_stop + tolerance
            if is_buy
            else old_stop == 0 or new_stop < old_stop - tolerance
        )
        if not improves:
            return False

        quote = self.quote_provider(position.symbol)
        current_price = float(quote["bid"] if is_buy else quote["ask"])
        stops_level = float(getattr(info, "trade_stops_level", 0) or 0) * point
        if is_buy and new_stop >= current_price - stops_level:
            return False
        if not is_buy and new_stop <= current_price + stops_level:
            return False

        request = {
            "action": self.api.TRADE_ACTION_SLTP,
            "symbol": position.symbol,
            "position": position.ticket,
            "sl": float(new_stop),
            "tp": float(position.tp),
            "magic": _MAGIC,
        }
        result = self.api.order_send(request)
        if (
            result is None
            or getattr(result, "retcode", None)
            != getattr(self.api, "TRADE_RETCODE_DONE", -1)
        ):
            return False
        refreshed = self.api.positions_get(ticket=position.ticket)
        if refreshed is None or not refreshed:
            return False
        return _close_enough(refreshed[0].sl, new_stop, tolerance)

    def _partial_close(self, position, close_volume):
        info = self.api.symbol_info(position.symbol)
        if info is None:
            return False
        volume_before = float(position.volume)
        quote = self.quote_provider(position.symbol)
        is_buy = position.type == self.api.POSITION_TYPE_BUY
        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "position": position.ticket,
            "volume": close_volume,
            "type": (
                self.api.ORDER_TYPE_SELL if is_buy else self.api.ORDER_TYPE_BUY
            ),
            "price": quote["bid"] if is_buy else quote["ask"],
            "deviation": 10,
            "magic": _MAGIC,
            "comment": "NEXORA partial take profit",
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(info),
        }
        result = self.api.order_send(request)
        accepted = (
            result is not None
            and getattr(result, "retcode", None)
            == getattr(self.api, "TRADE_RETCODE_DONE", -1)
        )
        if not accepted:
            return False
        refreshed = self.api.positions_get(ticket=position.ticket)
        if refreshed is None:
            return False
        if not refreshed:
            return True
        return float(refreshed[0].volume) < volume_before

    def manage_open_positions(self):
        """Apply break-even, one partial close, and ATR-based trailing stops."""

        results = []
        with self.connection_factory():
            account = self._verified_account()
            positions = _as_tuple(self.api.positions_get(), "open positions")
            current_by_ticket = {int(item.ticket): item for item in positions}
            records = self.store.list_trades(
                statuses=("OPEN", "UNPROTECTED")
            )
            if any(record["mode"] != self.config.mode.value for record in records):
                self.store.activate_lockout(
                    "Managed position mode differs from configured operating mode."
                )
                raise TradeRejected(
                    "Restore the operating mode used to open the position."
                )
            owned_tickets = {
                int(record["position_ticket"])
                for record in records
                if record.get("position_ticket") is not None
            }

            unknown = set(current_by_ticket) - owned_tickets
            if unknown:
                self.store.activate_lockout(
                    "Untracked MT5 position(s) detected: "
                    + ", ".join(str(ticket) for ticket in sorted(unknown))
                )
                results.append({
                    "status": "UNTRACKED_POSITION_LOCKOUT",
                    "tickets": sorted(unknown),
                })

            for record in records:
                ticket = int(record["position_ticket"])
                if (
                    record["account_login"] != account["login"]
                    or record["account_server"] != account["server"]
                ):
                    self.store.activate_lockout(
                        f"Managed trade {record['trade_id']} account identity mismatch."
                    )
                    raise TradeRejected("Managed trade account identity mismatch.")
                position = current_by_ticket.get(ticket)
                if position is None:
                    position_identifier = int(
                        record.get("position_identifier") or ticket
                    )
                    history = self.api.history_deals_get(
                        position=position_identifier
                    )
                    if history is None:
                        self.store.activate_lockout(
                            f"Could not verify close history for position {ticket}."
                        )
                        raise TradeRejected(
                            "Broker close history is unavailable."
                        )
                    if not history:
                        self.store.activate_lockout(
                            f"Position {ticket} is absent without close history."
                        )
                        raise TradeRejected(
                            "Position closure could not be verified."
                        )
                    try:
                        closed_record, deal_rows = self._closed_trade_record(
                            record,
                            position_identifier,
                            history,
                        )
                        self.store.record_closed_trade(closed_record, deal_rows)
                    except (TradeRejected, ValueError, TypeError) as error:
                        self.store.activate_lockout(
                            f"Closed trade {record['trade_id']} history is invalid: {error}"
                        )
                        raise TradeRejected(
                            "Broker close history could not be safely reconciled."
                        ) from error
                    self.store.update_trade(
                        record["trade_id"],
                        status="CLOSED",
                        closed_at=closed_record["exit_time"],
                        realized_profit=closed_record["net_profit"],
                        exit_reason=closed_record["exit_reason"],
                        final_stop_loss=closed_record["final_stop_loss"],
                        final_take_profit=closed_record["final_take_profit"],
                    )
                    results.append({"ticket": ticket, "status": "CLOSED"})
                    continue
                if getattr(position, "magic", None) != _MAGIC:
                    self.store.activate_lockout(
                        f"Managed position {ticket} magic mismatch."
                    )
                    raise TradeRejected(
                        "Managed position identity does not match the execution ledger."
                    )
                if (
                    record["status"] == "UNPROTECTED"
                    or not position.sl
                    or not position.tp
                ):
                    info = self.api.symbol_info(position.symbol)
                    protected = (
                        info is not None
                        and self._ensure_protection(
                            position,
                            float(record["stop_loss"]),
                            float(record["take_profit"]),
                            info,
                        )
                    )
                    refreshed = self.api.positions_get(ticket=ticket)
                    if protected and refreshed:
                        position = refreshed[0]
                        self.store.update_trade(
                            record["trade_id"],
                            status="OPEN",
                            error=None,
                        )
                    else:
                        closed = (
                            info is not None
                            and self._close_position(position, info)
                        )
                        if closed:
                            self.store.update_trade(
                                record["trade_id"],
                                status="PROTECTION_FAILED_CLOSED",
                                closed_at=datetime.now(timezone.utc).isoformat(),
                                error="Unprotected position closed during recovery.",
                            )
                            results.append({
                                "ticket": ticket,
                                "status": "PROTECTION_FAILED_CLOSED",
                            })
                            continue
                        self.store.activate_lockout(
                            f"Unprotected position {ticket} could not be repaired or closed."
                        )
                        results.append({
                            "ticket": ticket,
                            "status": "UNPROTECTED",
                        })
                        continue

                is_buy = position.type == self.api.POSITION_TYPE_BUY
                entry = float(record["entry_price"])
                stop_distance = abs(
                    float(record["entry_price"]) - float(record["stop_loss"])
                )
                if stop_distance <= 0:
                    self.store.activate_lockout(
                        f"Trade {record['trade_id']} has invalid initial risk distance."
                    )
                    raise TradeRejected("Stored trade risk distance is invalid.")

                quote = self.quote_provider(position.symbol)
                market_price = float(quote["bid"] if is_buy else quote["ask"])
                favorable_move = (
                    market_price - entry if is_buy else entry - market_price
                )
                r_multiple = favorable_move / stop_distance

                if r_multiple >= 1.0:
                    break_even_stop = entry + (
                        stop_distance * 0.05 if is_buy else -stop_distance * 0.05
                    )
                    changed = self._modify_stop(position, break_even_stop)
                    if changed:
                        refreshed = self.api.positions_get(ticket=ticket)
                        if refreshed is None or not refreshed:
                            self.store.activate_lockout(
                                f"Could not verify break-even update for {ticket}."
                            )
                            raise TradeRejected(
                                "Break-even stop update could not be verified."
                            )
                        position = refreshed[0]
                        results.append({
                            "ticket": ticket,
                            "action": "BREAK_EVEN",
                            "stop": break_even_stop,
                        })

                action = self.store.action_status(ticket, "PARTIAL_1_5R")
                if action and action["status"] in {"PENDING", "RECONCILE"}:
                    self.store.activate_lockout(
                        f"Partial close for position {ticket} needs reconciliation."
                    )
                    results.append({
                        "ticket": ticket,
                        "action": "PARTIAL_1_5R_RECONCILE",
                    })
                if r_multiple >= 1.5 and action is None:
                    info = self.api.symbol_info(position.symbol)
                    if info is None:
                        raise TradeRejected(
                            f"Broker specs unavailable for {position.symbol}."
                        )
                    step = _finite_positive(
                        getattr(info, "volume_step", None),
                        "Broker volume step",
                    )
                    minimum = _finite_positive(
                        getattr(info, "volume_min", None),
                        "Broker minimum volume",
                    )
                    close_volume = math.floor(
                        (float(position.volume) * 0.5) / step + 1e-9
                    ) * step
                    remaining = float(position.volume) - close_volume
                    if close_volume < minimum or remaining < minimum - 1e-9:
                        if self.store.claim_action(
                            ticket,
                            "PARTIAL_1_5R",
                            detail=(
                                f"Skipped: volume {position.volume} cannot be "
                                "split into two broker-valid legs."
                            ),
                        ):
                            self.store.finish_action(
                                ticket,
                                "PARTIAL_1_5R",
                                "SKIPPED",
                                (
                                    f"Volume {position.volume} cannot be split "
                                    "into two broker-valid legs."
                                ),
                            )
                            self.store.record_event(
                                "WARNING",
                                "PARTIAL_CLOSE_SKIPPED",
                                f"Position {ticket} is below valid partial-close volume.",
                            )
                    elif self.store.claim_action(
                        ticket,
                        "PARTIAL_1_5R",
                        detail=(
                            f"before={position.volume};close={close_volume};"
                            f"expected_remaining={remaining}"
                        ),
                    ):
                        success = self._partial_close(position, close_volume)
                        self.store.finish_action(
                            ticket,
                            "PARTIAL_1_5R",
                            "DONE" if success else "RECONCILE",
                            (
                                f"Closed {close_volume} lots."
                                if success
                                else "Partial close needs broker reconciliation."
                            ),
                        )
                        if not success:
                            self.store.activate_lockout(
                                f"Partial close for position {ticket} is uncertain."
                            )
                            raise TradeRejected(
                                "Partial close is uncertain; new entries are locked."
                            )
                        refreshed = self.api.positions_get(ticket=ticket)
                        if refreshed is None or not refreshed:
                            self.store.activate_lockout(
                                f"Could not verify partial close for position {ticket}."
                            )
                            raise TradeRejected(
                                "Partial close needs broker reconciliation."
                            )
                        position = refreshed[0]
                        results.append({
                            "ticket": ticket,
                            "action": "PARTIAL_1_5R",
                            "volume": close_volume,
                        })

                if r_multiple >= 2.0:
                    from core.market_data import get_historical_data

                    candles = get_historical_data(
                        position.symbol,
                        self.api.TIMEFRAME_M15,
                        bars=30,
                    )
                    if len(candles) < 15:
                        raise TradeRejected(
                            f"Insufficient completed candles for {position.symbol} "
                            "ATR trailing."
                        )
                    previous = candles["close"].shift(1)
                    true_range = candles[["high", "low"]].assign(
                        high_low=candles["high"] - candles["low"],
                        high_close=(candles["high"] - previous).abs(),
                        low_close=(candles["low"] - previous).abs(),
                    )[["high_low", "high_close", "low_close"]].max(axis=1)
                    atr = float(
                        true_range.ewm(
                            alpha=1 / 14,
                            min_periods=14,
                            adjust=False,
                        ).mean().iloc[-1]
                    )
                    if not math.isfinite(atr) or atr <= 0:
                        raise TradeRejected(
                            f"ATR unavailable for {position.symbol} trailing."
                        )
                    new_stop = (
                        market_price - 1.5 * atr
                        if is_buy
                        else market_price + 1.5 * atr
                    )
                    changed = self._modify_stop(position, new_stop)
                    if changed:
                        refreshed = self.api.positions_get(ticket=ticket)
                        if refreshed is None or not refreshed:
                            self.store.activate_lockout(
                                f"Could not verify trailing stop update for {ticket}."
                            )
                            raise TradeRejected(
                                "Trailing stop update could not be verified."
                            )
                        position = refreshed[0]
                        results.append({
                            "ticket": ticket,
                            "action": "ATR_TRAILING",
                            "stop": new_stop,
                        })

                self.store.update_trade(
                    record["trade_id"],
                    actual_volume=float(position.volume),
                    final_stop_loss=float(position.sl),
                    final_take_profit=float(position.tp),
                )

            self.store.heartbeat()
        return results

