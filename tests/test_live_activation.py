import os
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from execution.trading_engine import TradeRejected, TradingEngine
from execution.trading_store import TradingStore
from execution.operating_mode import LIVE_VERIFICATIONS, save_live_readiness_item


class MockLiveMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self):
        self.order_send_calls = 0

    def terminal_info(self):
        return SimpleNamespace(
            connected=True,
            trade_allowed=True,
            tradeapi_disabled=False,
        )

    def account_info(self):
        return SimpleNamespace(
            login=123456,
            server="Broker-Live",
            trade_mode=self.ACCOUNT_TRADE_MODE_REAL,
            trade_allowed=True,
            trade_expert=True,
            equity=10000.0,
        )

    def positions_get(self, **_kwargs):
        return ()

    def orders_get(self, **_kwargs):
        return ()

    def order_send(self, _request):
        self.order_send_calls += 1
        raise AssertionError("Tests must never submit a broker order.")


def _prepared_live_engine(tmp_path, monkeypatch):
    readiness_file = tmp_path / "live_readiness.json"
    monkeypatch.setenv("NEXORA_LIVE_READINESS_FILE", str(readiness_file))
    api = MockLiveMT5()
    store = TradingStore(tmp_path / "live.sqlite3")
    store.period_baseline_window_open = lambda *_args, **_kwargs: True
    for period in ("daily", "weekly", "monthly"):
        store.set_period_baseline(
            period,
            10000.0,
            123456,
            "Broker-Live",
        )
    store.save_account_connection("123456", "Broker-Live", "LIVE")
    store.update_operational_config(
        mode="LIVE",
        agent_enabled=True,
        runner_enabled=True,
    )
    store.heartbeat(os.getpid())
    engine = TradingEngine(
        api=api,
        store=store,
        connection_factory=nullcontext,
    )
    engine.verify_account_connection()
    store.save_github_client_id("Iv1.publicclient123")
    store.save_github_authorization(
        "Silas-AFK",
        123456,
        "Broker-Live",
    )
    for requirement in LIVE_VERIFICATIONS:
        identity = (
            {"account_login": 123456, "account_server": "Broker-Live"}
            if requirement == "account_identity_verified"
            else {}
        )
        save_live_readiness_item(
            readiness_file,
            requirement,
            verified=True,
            evidence=f"mock-reviewed-{requirement}",
            **identity,
        )
    engine.refresh_configuration()
    return engine, store, api


def test_live_activation_requires_gates_and_separate_first_order_authorization(
    tmp_path,
    monkeypatch,
):
    engine, store, api = _prepared_live_engine(tmp_path, monkeypatch)

    activated_at = engine.activate_live(
        real_money_acknowledged=True,
        risk_limits_acknowledged=True,
    )
    engine.refresh_configuration()
    assert activated_at
    assert engine.config.live_authorized is True
    assert store.get_operational_config()["execution_enabled"] is False
    assert api.order_send_calls == 0

    with pytest.raises(TradeRejected, match="first LIVE order"):
        engine.enable_execution(operator_confirmed=True)

    engine.authorize_first_live_order(operator_confirmed=True)
    engine.refresh_configuration()
    assert store.get_account_manager_state()[
        "live_first_order_authorized_at"
    ]
    assert engine.enable_execution(operator_confirmed=True) == "LIVE"
    assert store.get_operational_config()["execution_enabled"] is True
    assert api.order_send_calls == 0
    assert store.list_trades() == []


def test_live_activation_rejects_missing_readiness_or_account_verification(
    tmp_path,
    monkeypatch,
):
    engine, store, _api = _prepared_live_engine(tmp_path, monkeypatch)
    store.invalidate_account_connection_verification()
    engine.refresh_configuration()

    with pytest.raises(TradeRejected, match="verified LIVE MT5 account"):
        engine.activate_live(
            real_money_acknowledged=True,
            risk_limits_acknowledged=True,
        )


def test_risk_policy_change_and_restart_invalidate_live_authorization(
    tmp_path,
    monkeypatch,
):
    engine, store, _api = _prepared_live_engine(tmp_path, monkeypatch)
    engine.activate_live(
        real_money_acknowledged=True,
        risk_limits_acknowledged=True,
    )
    assert store.get_account_manager_state()["live_activated_at"]

    policy = store.get_operational_config()["risk_policy"]
    policy["max_daily_loss_percent"] = 0.75
    store.update_operational_config(risk_policy=policy)
    assert store.get_account_manager_state()["live_activated_at"] is None

    engine.refresh_configuration()
    engine.authorize_github_operator("Silas-AFK")
    engine.refresh_configuration()
    engine.activate_live(
        real_money_acknowledged=True,
        risk_limits_acknowledged=True,
    )
    store.disarm_after_restart()

    assert store.get_account_connection()["verified"] is False
    assert store.get_account_manager_state()["github_authorized_login"] is None
    assert store.get_account_manager_state()["live_activated_at"] is None
    assert store.get_operational_config()["execution_enabled"] is False
