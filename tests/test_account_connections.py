from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from execution.operating_mode import OperatingMode, execution_state
from execution.trading_engine import TradeRejected, TradingEngine
from execution.trading_store import TradingStore


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self):
        self.login = 123456
        self.server = "Broker-Demo"
        self.trade_mode = self.ACCOUNT_TRADE_MODE_DEMO
        self.terminal_connected = True
        self.terminal_trade_allowed = True
        self.account_trade_allowed = True
        self.shutdown_calls = 0

    def terminal_info(self):
        return SimpleNamespace(
            connected=self.terminal_connected,
            trade_allowed=self.terminal_trade_allowed,
            tradeapi_disabled=False,
        )

    def account_info(self):
        return SimpleNamespace(
            login=self.login,
            server=self.server,
            trade_mode=self.trade_mode,
            trade_allowed=self.account_trade_allowed,
            trade_expert=self.account_trade_allowed,
        )

    def shutdown(self):
        self.shutdown_calls += 1


def _engine(path, api=None):
    store = TradingStore(path)
    engine = TradingEngine(
        api=api or FakeMT5(),
        store=store,
        connection_factory=nullcontext,
    )
    return engine, store


@pytest.mark.parametrize(
    ("login", "server", "account_type"),
    [
        ("abc", "Broker-Demo", "DEMO"),
        ("123456", "", "DEMO"),
        ("123456", "Broker-Demo\nInjected", "DEMO"),
        ("123456", "Broker-Demo", "UNKNOWN"),
    ],
)
def test_invalid_account_profiles_are_rejected(tmp_path, login, server, account_type):
    store = TradingStore(tmp_path / "invalid-account.sqlite3")

    with pytest.raises(ValueError):
        store.save_account_connection(login, server, account_type)

    assert store.get_account_connection() is None


def test_profile_persists_and_dashboard_runner_configuration_share_it(tmp_path):
    database = tmp_path / "shared-account.sqlite3"
    engine, store = _engine(database)
    engine.save_account_connection("123456", " Broker-Demo ", "demo")

    profile = TradingStore(database).get_account_connection()
    reloaded_engine = TradingEngine(
        api=FakeMT5(),
        store=TradingStore(database),
        connection_factory=nullcontext,
    )

    assert profile == {
        "login": 123456,
        "server": "Broker-Demo",
        "account_type": "DEMO",
        "verified": False,
        "verified_at": None,
    }
    assert reloaded_engine.config.expected_login == 123456
    assert reloaded_engine.config.expected_server == "Broker-Demo"
    assert reloaded_engine.config.expected_account_type == "DEMO"
    assert reloaded_engine.config.account_verified is False
    assert store.get_operational_config()["account_connection"] == profile


def test_verification_uses_actual_connected_identity(tmp_path):
    engine, store = _engine(tmp_path / "verify.sqlite3")
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")

    connected = engine.connect_account()
    verified = engine.verify_account_connection()

    assert connected["matches_saved_profile"] is True
    assert connected["terminal_trade_allowed"] is True
    assert verified == {
        "login": 123456,
        "server": "Broker-Demo",
        "account_type": "DEMO",
        "verified": True,
    }
    assert store.get_account_connection()["verified"] is True


def test_identity_verification_does_not_override_trade_permissions(tmp_path):
    api = FakeMT5()
    api.terminal_trade_allowed = False
    api.account_trade_allowed = False
    engine, store = _engine(tmp_path / "trade-permissions.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")

    engine.verify_account_connection()
    state = execution_state(
        config=engine.refresh_configuration(),
        api=api,
        store=store,
    )

    assert state["account_verified"] is True
    assert state["entry_permission"] is False
    assert any("trading permissions" in reason for reason in state["reasons"])


def test_profile_mismatch_invalidates_verification_and_disarms_execution(tmp_path):
    api = FakeMT5()
    engine, store = _engine(tmp_path / "mismatch.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    engine.verify_account_connection()
    store.update_operational_config(
        agent_enabled=True,
        execution_enabled=True,
        runner_enabled=True,
    )
    api.server = "Different-Server"

    result = engine.connect_account()

    assert result["matches_saved_profile"] is False
    assert store.get_account_connection()["verified"] is False
    settings = store.get_operational_config()
    assert settings["execution_enabled"] is False


def test_switch_disarms_and_requires_fresh_verification(tmp_path):
    engine, store = _engine(tmp_path / "switch.sqlite3")
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    engine.verify_account_connection()
    store.update_operational_config(
        agent_enabled=True,
        execution_enabled=True,
        runner_enabled=True,
    )

    switched = engine.switch_account_connection(
        "654321",
        "Other-Demo",
        "DEMO",
    )

    assert switched["login"] == 654321
    assert switched["verified"] is False
    settings = store.get_operational_config()
    assert settings["agent_enabled"] is False
    assert settings["execution_enabled"] is False
    assert settings["runner_enabled"] is False


def test_switch_is_blocked_while_a_managed_trade_needs_reconciliation(tmp_path):
    engine, store = _engine(tmp_path / "open-trade.sqlite3")
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    reservation = store.reserve_trade({
        "idempotency_key": "existing-position",
        "signal_id": "existing-position",
        "symbol": "EURUSD",
        "direction": "BUY",
        "mode": "DEMO",
        "account_login": 123456,
        "account_server": "Broker-Demo",
        "requested_volume": 0.1,
        "entry_price": 1.1,
        "stop_loss": 1.09,
        "take_profit": 1.12,
        "initial_risk": 10.0,
    })
    store.update_trade(reservation["trade"]["trade_id"], status="OPEN")

    with pytest.raises(ValueError, match="reconcile existing trades"):
        engine.switch_account_connection("654321", "Other-Demo", "DEMO")

    assert store.get_account_connection()["login"] == 123456


def test_failed_verification_fails_closed(tmp_path):
    api = FakeMT5()
    engine, store = _engine(tmp_path / "failed-verification.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    store.update_operational_config(execution_enabled=True)
    api.login = 999999

    with pytest.raises(TradeRejected, match="login"):
        engine.verify_account_connection()

    assert store.get_account_connection()["verified"] is False
    assert store.get_operational_config()["execution_enabled"] is False


def test_terminal_disconnect_invalidates_saved_verification(tmp_path):
    api = FakeMT5()
    engine, store = _engine(tmp_path / "disconnected.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    engine.verify_account_connection()
    store.update_operational_config(execution_enabled=True)
    api.terminal_connected = False

    with pytest.raises(TradeRejected, match="not connected"):
        engine.connect_account()

    assert store.get_account_connection()["verified"] is False
    assert store.get_operational_config()["execution_enabled"] is False


def test_verification_fails_closed_when_profile_is_missing(tmp_path):
    engine, store = _engine(tmp_path / "missing-profile.sqlite3")

    with pytest.raises(TradeRejected, match="Save an account profile"):
        engine.verify_account_connection()

    assert store.get_account_connection() is None
    assert store.get_operational_config()["execution_enabled"] is False


def test_disconnect_clears_profile_without_shutting_down_terminal(tmp_path):
    api = FakeMT5()
    engine, store = _engine(tmp_path / "disconnect.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "DEMO")
    store.update_operational_config(
        agent_enabled=True,
        execution_enabled=True,
        runner_enabled=True,
    )

    engine.disconnect_account()

    assert store.get_account_connection() is None
    assert store.get_operational_config()["execution_enabled"] is False
    assert store.get_operational_config()["agent_enabled"] is False
    assert api.shutdown_calls == 0


def test_live_profile_cannot_enable_backend_execution(tmp_path):
    api = FakeMT5()
    api.trade_mode = api.ACCOUNT_TRADE_MODE_REAL
    engine, store = _engine(tmp_path / "live-lock.sqlite3", api)
    engine.save_account_connection("123456", "Broker-Demo", "LIVE")
    store.update_operational_config(
        mode=OperatingMode.LIVE.value,
        agent_enabled=True,
        runner_enabled=True,
    )
    engine.verify_account_connection()

    with pytest.raises(TradeRejected, match="GitHub authorization"):
        engine.enable_execution(operator_confirmed=True)

    assert store.get_operational_config()["execution_enabled"] is False
