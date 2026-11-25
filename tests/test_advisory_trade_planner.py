from unittest.mock import Mock
from types import SimpleNamespace

from execution import mt5_connection as connection_manager
from risk import trade_planner
from risk.risk_manager import (
    calculate_position_size,
    calculate_position_size_from_spec,
    check_daily_loss,
)
from risk.trade_planner import build_advisory_trade_plan


SPEC = {
    "tick_size": 0.0001,
    "tick_value": 10.0,
    "volume_min": 0.01,
    "volume_max": 100.0,
    "volume_step": 0.01,
}


def test_position_size_rounds_down_and_stays_within_risk_budget():
    result = calculate_position_size_from_spec(
        "EURUSD",
        entry_price=1.1000,
        stop_loss=1.0990,
        risk_percent=0.25,
        equity=10280,
        broker_spec=SPEC,
    )

    assert result["valid"] is True
    assert result["volume"] == 0.25
    assert result["estimated_risk"] <= result["risk_budget"]
    assert result["estimated_risk"] < result["risk_budget"]
    step_count = result["volume"] / result["volume_step"]
    assert abs(step_count - round(step_count)) < 1e-9


def test_minimum_volume_that_exceeds_budget_is_rejected():
    spec = dict(SPEC, volume_min=0.1)
    result = calculate_position_size_from_spec(
        "EURUSD",
        entry_price=1.1,
        stop_loss=1.09,
        risk_percent=0.25,
        equity=10000,
        broker_spec=spec,
    )

    assert result["valid"] is False
    assert "Minimum broker lot" in result["reason"]


def test_invalid_broker_spec_and_zero_stop_distance_are_rejected():
    invalid_spec = dict(SPEC, tick_value=float("nan"))
    invalid = calculate_position_size_from_spec(
        "EURUSD", 1.1, 1.09, 0.25, 10000, invalid_spec
    )
    zero_distance = calculate_position_size_from_spec(
        "EURUSD", 1.1, 1.1, 0.25, 10000, SPEC
    )

    assert invalid["valid"] is False
    assert zero_distance["valid"] is False
    assert "zero" in zero_distance["reason"]


def test_advisory_plan_validates_direction_risk_and_margin():
    plan = build_advisory_trade_plan(
        symbol="EURUSD",
        direction="BUY",
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
        equity=10000,
        broker_spec=SPEC,
        margin_estimate=100,
        free_margin=1000,
    )
    insufficient_margin = build_advisory_trade_plan(
        symbol="EURUSD",
        direction="BUY",
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
        equity=10000,
        broker_spec=SPEC,
        margin_estimate=1001,
        free_margin=1000,
    )
    wrong_levels = build_advisory_trade_plan(
        symbol="EURUSD",
        direction="BUY",
        entry=1.1,
        stop_loss=1.101,
        take_profit=1.102,
        equity=10000,
        broker_spec=SPEC,
    )

    assert plan["valid"] is True
    assert plan["estimated_monetary_risk"] <= plan["risk_budget"]
    assert plan["execution_permitted"] is False
    assert "order was sent" in plan["reason"]
    assert insufficient_margin["valid"] is False
    assert "margin" in insufficient_margin["reason"]
    assert wrong_levels["valid"] is False


def test_advisory_planner_rejects_risk_above_policy_cap():
    plan = build_advisory_trade_plan(
        symbol="EURUSD",
        direction="BUY",
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
        equity=10000,
        broker_spec=SPEC,
        risk_percent=0.26,
    )

    assert plan["valid"] is False
    assert "cannot exceed 0.25%" in plan["reason"]
    assert plan["execution_permitted"] is False


def test_risk_manager_borrows_existing_connection_without_shutdown(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(
        connection_manager.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "symbol_info",
        Mock(return_value=SimpleNamespace(
            trade_tick_size=0.0001,
            trade_tick_value_loss=10.0,
            trade_tick_value=10.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )

    result = calculate_position_size(
        "EURUSD",
        entry_price=1.1,
        stop_loss=1.099,
        risk_percent=0.25,
        equity=10000,
    )

    assert result["valid"] is True
    assert shutdown_calls == []


def test_broker_planner_reads_specs_but_never_submits_order(monkeypatch):
    monkeypatch.setattr(
        connection_manager.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True),
    )
    monkeypatch.setattr(
        trade_planner.mt5,
        "symbol_info",
        Mock(return_value=SimpleNamespace(
            trade_tick_size=0.0001,
            trade_tick_value_loss=10.0,
            trade_tick_value=10.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )),
    )
    monkeypatch.setattr(
        trade_planner.mt5,
        "account_info",
        lambda: SimpleNamespace(equity=10000.0, margin_free=5000.0),
    )
    monkeypatch.setattr(
        trade_planner.mt5,
        "order_calc_margin",
        Mock(return_value=100.0),
    )
    monkeypatch.setattr(
        trade_planner.mt5,
        "order_send",
        Mock(side_effect=AssertionError("Order submission must never be attempted.")),
    )

    plan = trade_planner.get_advisory_trade_plan(
        "EURUSD",
        "BUY",
        entry=1.1,
        stop_loss=1.099,
        take_profit=1.102,
    )

    assert plan["valid"] is True
    assert plan["margin_estimate"] == 100.0
    assert plan["execution_permitted"] is False


def test_daily_loss_limit_is_fail_closed_at_boundary():
    result = check_daily_loss(10000, 9900, max_daily_loss_percent=1.0)
    invalid = check_daily_loss(float("nan"), 9900)

    assert result["allowed"] is False
    assert invalid["allowed"] is False
