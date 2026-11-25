from contextlib import nullcontext
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from execution.operating_mode import (
    LIVE_VERIFICATIONS,
    OperatingConfig,
    OperatingMode,
    OperatingModeError,
    permission_reasons,
    read_live_readiness,
    execution_state,
    verify_account_identity,
)
from execution.mt5_connection import MT5ConnectionError
from execution import runner
from execution.trading_engine import TradeRejected, TradingEngine
from execution.trading_store import TradingStore
from execution.trade_analytics import summarize_trades


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2
    SYMBOL_TRADE_MODE_DISABLED = 0
    SYMBOL_TRADE_MODE_LONGONLY = 1
    SYMBOL_TRADE_MODE_SHORTONLY = 2
    SYMBOL_TRADE_MODE_FULL = 4
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_TYPE_BUY_STOP_LIMIT = 6
    ORDER_TYPE_SELL_STOP_LIMIT = 7
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_REJECT = 10006
    TRADE_RETCODE_TIMEOUT = 10012
    TIMEFRAME_M15 = 15
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5

    def __init__(self):
        self.login = 123456
        self.server = "Broker-Demo"
        self.equity = 10000.0
        self.margin_free = 5000.0
        self.positions = []
        self.sent_requests = []
        self.fail_protection = False
        self.omit_protection = False
        self.fill_slippage = 0.0

    def terminal_info(self):
        return SimpleNamespace(
            connected=True,
            trade_allowed=True,
            tradeapi_disabled=False,
        )

    def account_info(self):
        return SimpleNamespace(
            login=self.login,
            server=self.server,
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            equity=self.equity,
            margin_free=self.margin_free,
            currency="USD",
            trade_allowed=True,
            trade_expert=True,
        )

    def symbol_info(self, symbol):
        return SimpleNamespace(
            trade_mode=self.SYMBOL_TRADE_MODE_FULL,
            trade_tick_size=0.0001,
            trade_tick_value_loss=10.0,
            trade_tick_value=10.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            point=0.00001,
            trade_stops_level=0,
            filling_mode=self.SYMBOL_FILLING_FOK,
            digits=5,
        )

    def positions_get(self, symbol=None, ticket=None):
        results = list(self.positions)
        if symbol is not None:
            results = [item for item in results if item.symbol == symbol]
        if ticket is not None:
            results = [item for item in results if item.ticket == ticket]
        return tuple(results)

    def orders_get(self):
        return ()

    def history_deals_get(self, position):
        return (
            SimpleNamespace(
                ticket=101,
                position_id=position,
                type=self.DEAL_TYPE_BUY,
                entry=self.DEAL_ENTRY_IN,
                volume=0.24,
                price=1.1002,
                time=1700000000,
                profit=0.0,
                commission=-0.1,
                swap=0.0,
                fee=0.0,
            ),
            SimpleNamespace(
                ticket=102,
                position_id=position,
                type=self.DEAL_TYPE_SELL,
                entry=self.DEAL_ENTRY_OUT,
                volume=0.24,
                price=1.1040,
                time=1700000900,
                profit=91.2,
                commission=-0.1,
                swap=0.0,
                fee=0.0,
                reason=self.DEAL_REASON_TP,
            ),
        )

    def order_calc_profit(self, order_type, symbol, volume, entry, close):
        signed = close - entry
        if order_type == self.ORDER_TYPE_SELL:
            signed *= -1
        return signed * 100000 * volume

    def order_calc_margin(self, order_type, symbol, volume, price):
        return volume * 500.0

    def order_check(self, request):
        return SimpleNamespace(retcode=0, comment="ok")

    def order_send(self, request):
        self.sent_requests.append(dict(request))
        if request["action"] == self.TRADE_ACTION_SLTP:
            if self.fail_protection:
                return SimpleNamespace(
                    retcode=10016,
                    comment="protection rejected",
                    order=0,
                    deal=0,
                )
            for position in self.positions:
                if position.ticket == request["position"]:
                    position.sl = request["sl"]
                    position.tp = request["tp"]
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE,
                comment="modified",
                order=request["position"],
                deal=0,
            )

        if "position" in request:
            for position in list(self.positions):
                if position.ticket != request["position"]:
                    continue
                if request["volume"] >= position.volume - 1e-8:
                    self.positions.remove(position)
                else:
                    position.volume -= request["volume"]
                break
            return SimpleNamespace(
                retcode=self.TRADE_RETCODE_DONE,
                comment="closed",
                order=request["position"],
                deal=0,
            )

        position = SimpleNamespace(
            ticket=7777,
            identifier=7777,
            symbol=request["symbol"],
            type=request["type"],
            volume=request["volume"],
            price_open=request["price"] + (
                self.fill_slippage
                if request["type"] == self.ORDER_TYPE_BUY
                else -self.fill_slippage
            ),
            sl=0.0 if self.omit_protection else request["sl"],
            tp=0.0 if self.omit_protection else request["tp"],
            magic=request["magic"],
        )
        self.positions.append(position)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            comment="done",
            order=7777,
            deal=8888,
        )


def _config(mode=OperatingMode.DEMO, *, armed=True, readiness_file=None):
    return OperatingConfig(
        mode=mode,
        expected_login=123456,
        expected_server="Broker-Demo",
        demo_armed=armed,
        live_authorized=False,
        risk_percent=0.25,
        max_daily_loss_percent=1.0,
        max_aggregate_stop_risk_percent=0.5,
        max_open_positions=2,
        live_readiness_file=readiness_file or "missing-live-readiness.json",
        expected_account_type="DEMO",
        account_verified=True,
        execution_enabled=True,
        agent_enabled=True,
        runner_enabled=True,
    )


def _engine(tmp_path, *, api=None, config=None, quote=None):
    fake = api or FakeMT5()
    store = TradingStore(tmp_path / "trading.sqlite3")
    store.baseline_window_open = lambda: True
    engine = TradingEngine(
        config=config or _config(),
        api=fake,
        store=store,
        connection_factory=nullcontext,
        quote_provider=quote or (
            lambda symbol: {
                "bid": 1.1000,
                "ask": 1.1002,
                "age_seconds": 0.1,
            }
        ),
        sleeper=lambda _seconds: None,
    )
    store.period_baseline_window_open = lambda period, now=None: True
    for period in ("daily", "weekly", "monthly"):
        store.set_period_baseline(
            period,
            fake.equity,
            fake.login,
            fake.server,
        )
    return engine, fake


def _signal(signal_id="signal-1", target=1.1040):
    return {
        "classification": "CONFIRMED",
        "direction": "BULLISH",
        "stop_loss": 1.0992,
        "take_profit": target,
        "signal_timestamp": "2026-01-01T00:15:00+00:00",
    }


def test_research_mode_and_unarmed_demo_never_submit(tmp_path):
    research_api = FakeMT5()
    research, _ = _engine(
        tmp_path / "research",
        api=research_api,
        config=_config(OperatingMode.RESEARCH),
    )
    with pytest.raises(TradeRejected, match="RESEARCH"):
        research.submit_signal("EURUSD", _signal(), signal_id="research-1")

    demo_api = FakeMT5()
    unarmed, _ = _engine(
        tmp_path / "unarmed",
        api=demo_api,
        config=_config(OperatingMode.DEMO, armed=False),
    )
    with pytest.raises(TradeRejected, match="explicitly armed"):
        unarmed.submit_signal("EURUSD", _signal(), signal_id="unarmed-1")

    assert research_api.sent_requests == []
    assert demo_api.sent_requests == []


def test_account_server_and_mode_must_match_allowlist():
    api = FakeMT5()
    config = _config()
    assert verify_account_identity(api, config)["verified"] is True
    api.server = "Unexpected-Server"
    result = verify_account_identity(api, config)
    assert result["verified"] is False
    assert any("server" in reason for reason in result["reasons"])


def test_live_mode_requires_all_readiness_evidence_and_explicit_authorization(
    tmp_path,
    monkeypatch,
):
    readiness_file = tmp_path / "live_readiness.json"
    config = _config(
        OperatingMode.LIVE,
        readiness_file=readiness_file,
    )
    readiness = {
        item: {
            "verified": True,
            "evidence": f"reviewed {item}",
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
        for item in LIVE_VERIFICATIONS
    }
    readiness_file.write_text(
        json.dumps(readiness),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "NEXORA_LIVE_AUTHORIZATION",
        "I_EXPLICITLY_AUTHORIZE_LIVE_TRADING",
    )
    live_config = OperatingConfig(
        **{
            **config.__dict__,
            "live_authorized": True,
        }
    )
    readiness["account_identity_verified"].update({
        "account_login": 123456,
        "account_server": "Broker-Demo",
    })
    readiness_file.write_text(
        json.dumps(readiness),
        encoding="utf-8",
    )
    assert all(
        read_live_readiness(
            readiness_file,
            account_login=123456,
            account_server="Broker-Demo",
        ).values()
    )
    live_reasons = permission_reasons(
        live_config,
        {"verified": True, "reasons": []},
    )
    assert any(
        "first LIVE order" in reason or "GitHub" in reason
        for reason in live_reasons
    )

    monkeypatch.delenv("NEXORA_LIVE_AUTHORIZATION")
    assert any(
        "authorization" in item.lower()
        for item in permission_reasons(
            config,
            {"verified": True, "reasons": []},
        )
    )


def test_environment_defaults_fail_closed_and_risk_values_are_bounded(monkeypatch):
    monkeypatch.delenv("NEXORA_OPERATING_MODE", raising=False)
    monkeypatch.delenv("NEXORA_DEMO_ARMED", raising=False)
    monkeypatch.delenv("NEXORA_LIVE_AUTHORIZATION", raising=False)
    monkeypatch.setenv("NEXORA_EXPECTED_LOGIN", "not-a-login")
    monkeypatch.setenv("NEXORA_EXPECTED_SERVER", "ignored")
    config = OperatingConfig.from_environment()
    assert config.mode == OperatingMode.DEMO
    assert config.expected_login is None
    assert config.expected_server is None
    assert config.execution_enabled is False
    assert config.agent_enabled is False
    assert config.runner_enabled is False
    assert config.risk_percent == 0.25
    assert config.max_daily_loss_percent == 1.0
    assert config.max_aggregate_stop_risk_percent == 0.5
    assert config.max_open_positions == 2

    monkeypatch.setenv(
        "NEXORA_LIVE_AUTHORIZATION",
        "I_EXPLICITLY_AUTHORIZE_LIVE_TRADING",
    )
    live_settings = OperatingConfig.from_environment(
        settings={"mode": "LIVE"},
    )
    assert live_settings.live_authorized is False

    monkeypatch.setenv("NEXORA_RISK_PERCENT", "0.26")
    with pytest.raises(OperatingModeError, match="no greater than 0.25"):
        OperatingConfig.from_environment()


def test_daily_baseline_only_accepts_utc_day_start_window(tmp_path, monkeypatch):
    class FixedDateTime(datetime):
        fixed_now = datetime(2026, 1, 1, 0, 4, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.fixed_now

    monkeypatch.setattr("execution.trading_store.datetime", FixedDateTime)
    store = TradingStore(tmp_path / "baseline.sqlite3")
    store.set_daily_baseline(10000, 123456, "Broker-Demo")
    with pytest.raises(ValueError, match="already exists"):
        store.set_daily_baseline(9000, 123456, "Broker-Demo")

    FixedDateTime.fixed_now = datetime(2026, 1, 1, 0, 6, tzinfo=timezone.utc)
    next_day_store = TradingStore(tmp_path / "late-baseline.sqlite3")
    with pytest.raises(ValueError, match="window is closed"):
        next_day_store.set_daily_baseline(10000, 123456, "Broker-Demo")


def test_runner_submits_only_in_armed_demo_mode_and_research_stays_read_only(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        runner,
        "record_signal",
        lambda *args, **kwargs: {"signal_id": "persisted-signal"},
    )
    analysis = {
        "valid": True,
        "signal_timestamp": "2026-01-01T00:15:00+00:00",
        "signal_result": _signal(),
    }

    research, research_api = _engine(
        tmp_path / "runner-research",
        config=_config(OperatingMode.RESEARCH),
    )
    research_result = runner.scan_cycle(
        research,
        ["EURUSD"],
        scanner=lambda symbol: analysis,
    )
    assert research_result[0]["classification"] == "CONFIRMED"
    assert research_api.sent_requests == []

    demo, demo_api = _engine(tmp_path / "runner-demo")
    demo_result = runner.scan_cycle(
        demo,
        ["EURUSD"],
        scanner=lambda symbol: analysis,
    )
    order_results = [
        row for row in demo_result
        if row.get("execution") is not None
    ]
    assert len(order_results) == 1
    assert order_results[0]["execution"]["protected"] is True
    assert len(demo_api.sent_requests) == 1


def test_mt5_disconnection_fails_closed_before_submission(tmp_path):
    class Disconnected:
        def __enter__(self):
            raise MT5ConnectionError("offline")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    engine, api = _engine(tmp_path)
    engine.connection_factory = Disconnected
    with pytest.raises(MT5ConnectionError, match="offline"):
        engine.submit_signal("EURUSD", _signal(), signal_id="offline")
    assert api.sent_requests == []


def test_submission_sizes_to_quarter_percent_and_deduplicates(tmp_path):
    engine, api = _engine(tmp_path)
    first = engine.submit_signal("EURUSD", _signal(), signal_id="signal-1")
    duplicate = engine.submit_signal("EURUSD", _signal(), signal_id="signal-1")

    assert first["submitted"] is True
    assert first["protected"] is True
    assert first["trade"]["requested_volume"] == pytest.approx(0.24)
    assert first["trade"]["initial_risk"] <= 25.0
    assert first["trade"]["status"] == "OPEN"
    assert duplicate["duplicate"] is True
    assert len(api.sent_requests) == 1
    request = api.sent_requests[0]
    assert request["sl"] == pytest.approx(1.0992)
    assert request["tp"] == pytest.approx(1.1040)


def test_missing_baseline_blocks_and_daily_loss_locks_out(tmp_path):
    engine, api = _engine(tmp_path)
    engine.store = TradingStore(tmp_path / "new-ledger.sqlite3")
    with pytest.raises(TradeRejected, match="baseline is missing"):
        engine.submit_signal("EURUSD", _signal(), signal_id="no-baseline")
    assert api.sent_requests == []

    engine.store.period_baseline_window_open = lambda period, now=None: True
    for period in ("daily", "weekly", "monthly"):
        engine.store.set_period_baseline(
            period,
            10000,
            api.login,
            api.server,
        )
    api.equity = 9850
    with pytest.raises(TradeRejected, match="Maximum daily loss reached"):
        engine.submit_signal("EURUSD", _signal(), signal_id="daily-limit")
    assert engine.store.get_lockout()["active"] is True
    assert api.sent_requests == []


@pytest.mark.parametrize(
    ("period", "equity", "starting_equity"),
    [
        ("weekly", 9790.0, {"daily": 9790.0, "weekly": 10000.0, "monthly": 10000.0}),
        ("monthly", 9500.0, {"daily": 9500.0, "weekly": 9500.0, "monthly": 10000.0}),
    ],
)
def test_weekly_and_monthly_loss_limits_block_entries(
    tmp_path,
    period,
    equity,
    starting_equity,
):
    engine, api = _engine(tmp_path)
    store = TradingStore(tmp_path / f"{period}-limits.sqlite3")
    store.period_baseline_window_open = lambda _period, now=None: True
    for baseline_period, start_equity in starting_equity.items():
        store.set_period_baseline(
            baseline_period,
            start_equity,
            api.login,
            api.server,
        )
    engine.store = store
    api.equity = equity

    with pytest.raises(
        TradeRejected,
        match=f"Maximum {period} loss reached",
    ):
        engine.submit_signal(
            "EURUSD",
            _signal(),
            signal_id=f"{period}-loss",
        )

    assert api.sent_requests == []


def test_aggregate_stop_risk_limit_blocks_entry(tmp_path):
    engine, api = _engine(tmp_path)
    api.positions.append(SimpleNamespace(
        ticket=5,
        symbol="GBPUSD",
        type=api.POSITION_TYPE_BUY,
        volume=0.30,
        price_open=1.1000,
        sl=1.0990,
        tp=1.1050,
        magic=722025,
    ))
    existing = engine.store.reserve_trade({
        "idempotency_key": "existing-position",
        "signal_id": "existing",
        "symbol": "GBPUSD",
        "direction": "BUY",
        "mode": "DEMO",
        "account_login": api.login,
        "account_server": api.server,
        "requested_volume": 0.30,
        "entry_price": 1.1000,
        "stop_loss": 1.0990,
        "take_profit": 1.1050,
        "initial_risk": 30.0,
    })
    engine.store.update_trade(
        existing["trade"]["trade_id"],
        position_ticket=5,
        actual_volume=0.30,
        status="OPEN",
    )
    with pytest.raises(TradeRejected, match="aggregate stop-risk"):
        engine.submit_signal("EURUSD", _signal(), signal_id="aggregate-limit")
    assert api.sent_requests == []


def test_existing_symbol_position_blocks_pyramiding(tmp_path):
    engine, api = _engine(tmp_path)
    api.positions.append(SimpleNamespace(
        ticket=6,
        symbol="EURUSD",
        type=api.POSITION_TYPE_BUY,
        volume=0.10,
        price_open=1.1000,
        sl=1.0990,
        tp=1.1050,
        magic=722025,
    ))
    existing = engine.store.reserve_trade({
        "idempotency_key": "existing-eurusd",
        "signal_id": "existing-eurusd",
        "symbol": "EURUSD",
        "direction": "BUY",
        "mode": "DEMO",
        "account_login": api.login,
        "account_server": api.server,
        "requested_volume": 0.10,
        "entry_price": 1.1000,
        "stop_loss": 1.0990,
        "take_profit": 1.1050,
        "initial_risk": 10.0,
    })
    engine.store.update_trade(
        existing["trade"]["trade_id"],
        position_ticket=6,
        actual_volume=0.10,
        status="OPEN",
    )

    with pytest.raises(TradeRejected, match="Maximum positions per symbol"):
        engine.submit_signal("EURUSD", _signal(), signal_id="second-eurusd")
    assert api.sent_requests == []


def test_untracked_manual_position_causes_lockout(tmp_path):
    engine, api = _engine(tmp_path)
    api.positions.append(SimpleNamespace(
        ticket=9,
        symbol="EURUSD",
        type=api.POSITION_TYPE_BUY,
        volume=0.01,
        price_open=1.1000,
        sl=1.0990,
        tp=1.1020,
        magic=0,
    ))

    with pytest.raises(TradeRejected, match="Unreconciled broker positions"):
        engine.submit_signal("EURUSD", _signal(), signal_id="external-position")
    assert engine.store.get_lockout()["active"] is True
    assert api.sent_requests == []


def test_missing_stop_protection_attempts_close_and_persist_lockout(tmp_path):
    api = FakeMT5()
    api.omit_protection = True
    api.fail_protection = True
    engine, _ = _engine(tmp_path, api=api)
    with pytest.raises(TradeRejected, match="position was closed"):
        engine.submit_signal("EURUSD", _signal(), signal_id="no-protection")
    assert api.positions == []
    assert engine.store.get_lockout()["active"] is True
    assert engine.store.list_trades()[0]["status"] == "PROTECTION_FAILED_CLOSED"


def test_adverse_fill_slippage_cannot_exceed_risk_budget(tmp_path):
    api = FakeMT5()
    api.fill_slippage = 0.0002
    engine, _ = _engine(tmp_path, api=api)

    with pytest.raises(TradeRejected, match="slippage"):
        engine.submit_signal("EURUSD", _signal(), signal_id="slipped-fill")

    assert api.positions == []
    assert engine.store.get_lockout()["active"] is True
    assert engine.store.list_trades()[0]["status"] == "SLIPPAGE_LIMIT_BREACH_CLOSED"


@pytest.mark.parametrize(
    "outcome",
    ["no_response", "timeout_without_position", "timeout_retcode"],
)
def test_ambiguous_order_outcome_stays_pending_and_blocks_retries(
    tmp_path,
    outcome,
):
    engine, api = _engine(tmp_path)
    if outcome == "no_response":
        api.order_send = lambda _request: None
    elif outcome == "timeout_retcode":
        api.order_send = lambda _request: SimpleNamespace(
            retcode=api.TRADE_RETCODE_TIMEOUT,
            comment="timeout",
        )
    else:
        def timeout(_request):
            raise TimeoutError("broker response timed out")

        api.order_send = timeout

    with pytest.raises(TradeRejected, match="outcome"):
        engine.submit_signal(
            "EURUSD",
            _signal(),
            signal_id=f"ambiguous-{outcome}",
        )

    trade = engine.store.list_trades()[0]
    assert trade["status"] == "PENDING_CONFIRMATION"
    assert engine.store.get_lockout()["active"] is True
    with pytest.raises(TradeRejected, match="lockout"):
        engine.submit_signal(
            "EURUSD",
            _signal(),
            signal_id="different-signal-after-timeout",
        )
    assert trade["status"] == "PENDING_CONFIRMATION"


def test_explicit_broker_rejection_is_not_misclassified_as_timeout(tmp_path):
    engine, api = _engine(tmp_path)
    api.order_send = lambda _request: SimpleNamespace(
        retcode=api.TRADE_RETCODE_REJECT,
        comment="request rejected",
    )

    with pytest.raises(TradeRejected, match="request rejected"):
        engine.submit_signal(
            "EURUSD",
            _signal(),
            signal_id="explicit-rejection",
        )

    assert engine.store.list_trades()[0]["status"] == "REJECTED"
    assert engine.store.get_lockout()["active"] is False


def test_order_send_timeout_reconciles_actual_fill_before_failing(tmp_path):
    engine, api = _engine(tmp_path)
    send = api.order_send

    def execute_then_timeout(request):
        send(request)
        raise TimeoutError("response was lost after fill")

    api.order_send = execute_then_timeout
    result = engine.submit_signal(
        "EURUSD",
        _signal(),
        signal_id="fill-after-timeout",
    )

    assert result["submitted"] is True
    assert result["protected"] is True
    assert result["trade"]["status"] == "OPEN"
    assert len(api.positions) == 1
    assert engine.store.get_lockout()["active"] is False


def test_emergency_lockout_blocks_entries_and_requires_flat_account_to_clear(
    tmp_path,
):
    engine, api = _engine(tmp_path)
    result = engine.submit_signal("EURUSD", _signal(), signal_id="open-before-stop")
    engine.emergency_stop("test stop")
    with pytest.raises(TradeRejected, match="lockout"):
        engine.submit_signal("EURUSD", _signal(), signal_id="blocked")
    with pytest.raises(TradeRejected, match="positions or pending"):
        engine.clear_emergency_stop("attempt", operator_confirmed=True)
    api.positions.clear()
    engine.clear_emergency_stop("review complete", operator_confirmed=True)
    assert engine.store.get_lockout()["active"] is False
    assert result["submitted"] is True


def test_break_even_partial_take_profit_and_trailing_stop_are_managed(
    tmp_path,
    monkeypatch,
):
    engine, api = _engine(tmp_path)
    engine.submit_signal("EURUSD", _signal(target=1.1050), signal_id="manage-me")
    engine.quote_provider = lambda symbol: {
        "bid": 1.1023,
        "ask": 1.1025,
        "age_seconds": 0.1,
    }
    candles = pd.DataFrame({
        "time": pd.date_range(
            "2026-01-01",
            periods=20,
            freq="15min",
            tz="UTC",
        ),
        "open": [1.1000] * 20,
        "high": [1.1002] * 20,
        "low": [1.0998] * 20,
        "close": [1.1000] * 20,
    })
    monkeypatch.setattr(
        "core.market_data.get_historical_data",
        lambda *args, **kwargs: candles,
    )

    actions = engine.manage_open_positions()

    action_names = [action.get("action") for action in actions]
    assert "BREAK_EVEN" in action_names
    assert "PARTIAL_1_5R" in action_names
    assert "ATR_TRAILING" in action_names
    assert api.positions[0].volume == pytest.approx(0.12)
    assert api.positions[0].sl >= 1.1017
    assert engine.store.action_status(7777, "PARTIAL_1_5R")["status"] == "DONE"
    second_cycle = engine.manage_open_positions()
    assert not any(
        action.get("action") == "PARTIAL_1_5R"
        for action in second_cycle
    )


def test_restart_reconciles_verified_protected_position(tmp_path):
    engine, api = _engine(tmp_path)
    engine.submit_signal("EURUSD", _signal(), signal_id="restart")
    restarted = TradingEngine(
        config=_config(),
        api=api,
        store=engine.store,
        connection_factory=nullcontext,
        quote_provider=engine.quote_provider,
    )

    result = restarted.reconcile_open_trades()

    assert result == [{"trade_id": engine.store.list_trades()[0]["trade_id"], "status": "OPEN"}]
    assert restarted.store.get_lockout()["active"] is False


def test_closed_trade_reconciliation_persists_broker_verified_history(tmp_path):
    engine, api = _engine(tmp_path)
    opened = engine.submit_signal(
        "EURUSD",
        _signal(),
        signal_id="history-reconcile",
    )
    api.positions.clear()

    result = engine.reconcile_open_trades()

    assert result == [{"trade_id": opened["trade"]["trade_id"], "status": "CLOSED"}]
    closed = engine.store.list_closed_trades()
    assert len(closed) == 1
    assert closed[0]["direction"] == "BUY"
    assert closed[0]["exit_reason"] == "TAKE_PROFIT"
    assert closed[0]["net_profit"] == pytest.approx(91.0)
    assert closed[0]["deal_tickets"] == [101, 102]
    assert engine.store.count_closed_deals() == 2
    assert engine.store.list_trades()[0]["status"] == "CLOSED"


def test_operator_close_requires_confirmation_and_records_verified_history(tmp_path):
    engine, api = _engine(tmp_path)
    opened = engine.submit_signal(
        "EURUSD",
        _signal(),
        signal_id="operator-close",
    )
    ticket = opened["trade"]["position_ticket"]
    with pytest.raises(TradeRejected, match="confirmation"):
        engine.close_managed_position(ticket)
    assert len(api.positions) == 1

    closed = engine.close_managed_position(
        ticket,
        operator_confirmed=True,
    )

    assert closed["status"] == "CLOSED"
    assert engine.store.list_closed_trades()[0]["exit_reason"] == "OPERATOR_CLOSE"
    assert api.positions == []


def test_persistent_runner_settings_disarm_without_rearming_on_restart(tmp_path):
    store = TradingStore(tmp_path / "runner-controls.sqlite3")
    store.update_operational_config(
        mode="DEMO",
        agent_enabled=True,
        execution_enabled=True,
        runner_enabled=True,
    )

    store.disarm_after_restart()

    settings = store.get_operational_config()
    assert settings["runner_enabled"] is True
    assert settings["agent_enabled"] is False
    assert settings["execution_enabled"] is False


def test_trade_analytics_uses_net_verified_results_and_risk_amount():
    stats = summarize_trades([
        {
            "net_profit": 10.0,
            "planned_risk_amount": 20.0,
            "duration_seconds": 60,
        },
        {
            "net_profit": -20.0,
            "planned_risk_amount": 20.0,
            "duration_seconds": 120,
        },
        {
            "net_profit": 0.0,
            "planned_risk_amount": 0.0,
        },
    ])

    assert stats["trades"] == 3
    assert stats["net_profit"] == pytest.approx(-10.0)
    assert stats["win_rate_percent"] == pytest.approx(100 / 3)
    assert stats["average_reward_to_risk"] == pytest.approx(-0.25)
    assert stats["max_drawdown"] == pytest.approx(20.0)
    assert stats["max_consecutive_losses"] == 1


def test_runner_claim_blocks_a_second_recent_process(tmp_path):
    store = TradingStore(tmp_path / "runner-claim.sqlite3")
    store.claim_runner_process(12345)

    with pytest.raises(RuntimeError, match="already has a current heartbeat"):
        store.claim_runner_process(54321)


def test_execution_snapshot_reports_live_period_loss_lock(tmp_path):
    engine, api = _engine(tmp_path)
    api.equity = 9850.0

    state = execution_state(
        config=engine.config,
        api=api,
        store=engine.store,
    )

    assert state["entry_permission"] is False
    assert "Maximum daily loss reached." in state["reasons"]


def test_paused_runner_does_not_scan_symbols(tmp_path):
    engine, api = _engine(tmp_path)
    engine.config = OperatingConfig(
        **{
            **engine.config.__dict__,
            "agent_enabled": False,
        }
    )
    scanner_calls = []

    results = runner.scan_cycle(
        engine,
        ["EURUSD"],
        scanner=lambda symbol: scanner_calls.append(symbol),
    )

    assert results == []
    assert scanner_calls == []
    assert api.sent_requests == []


def test_trading_store_persists_unique_trade_keys_and_lockout(tmp_path):
    store = TradingStore(tmp_path / "ledger.sqlite3")
    record = {
        "idempotency_key": "EURUSD:signal-1",
        "signal_id": "signal-1",
        "symbol": "EURUSD",
        "direction": "BUY",
        "mode": "DEMO",
        "account_login": 123456,
        "account_server": "Broker-Demo",
        "requested_volume": 0.25,
        "entry_price": 1.1002,
        "stop_loss": 1.0992,
        "take_profit": 1.1040,
        "initial_risk": 25.0,
    }
    first = store.reserve_trade(record)
    duplicate = store.reserve_trade(record)
    store.activate_lockout("test")

    assert first["inserted"] is True
    assert duplicate["inserted"] is False
    assert store.get_lockout()["active"] is True
    assert store.list_trades()[0]["idempotency_key"] == "EURUSD:signal-1"
