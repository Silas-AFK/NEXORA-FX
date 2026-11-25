from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from execution.mt5_connection import MT5ConnectionError
from execution.trading_engine import TradingEngine
from execution.trading_store import TradingStore
from streamlit.testing.v1 import AppTest


def test_dashboard_locks_execution_and_shows_mt5_connection_error():
    @contextmanager
    def unavailable_connection():
        raise MT5ConnectionError("simulated MT5 authorization failure")
        yield

    dashboard = (
        Path(__file__).resolve().parents[1]
        / "dashboard"
        / "dashboard.py"
    )
    with patch(
        "execution.mt5_connection.mt5_connection",
        unavailable_connection,
    ):
        app = AppTest.from_file(str(dashboard), default_timeout=90).run()

    assert not app.exception
    rendered_messages = [
        str(element.value)
        for element in [*app.caption, *app.error]
    ]
    assert any(
        "simulated MT5 authorization failure" in message
        for message in rendered_messages
    )
    assert any(
        "Backend entry permission: LOCKED" in message
        for message in rendered_messages
    )


def test_account_connections_page_renders_without_mt5_access():
    @contextmanager
    def unavailable_connection():
        raise MT5ConnectionError("simulated offline terminal")
        yield

    dashboard = (
        Path(__file__).resolve().parents[1]
        / "dashboard"
        / "dashboard.py"
    )
    with patch(
        "execution.mt5_connection.mt5_connection",
        unavailable_connection,
    ):
        app = AppTest.from_file(str(dashboard), default_timeout=90).run()
        app.sidebar.radio[0].set_value("Account Connections")
        app.run()

    assert not app.exception
    assert any(
        element.label == "MT5 account login"
        for element in app.text_input
    )
    assert any(
        element.label == "Broker server"
        for element in app.text_input
    )
    button_labels = {element.label for element in app.button}
    assert "Connect to MT5" in button_labels
    assert "Verify Saved Account" in button_labels
    assert "Disconnect Saved Account" in button_labels


def test_account_connections_form_persists_profile(tmp_path):
    @contextmanager
    def unavailable_connection():
        raise MT5ConnectionError("simulated offline terminal")
        yield

    dashboard = (
        Path(__file__).resolve().parents[1]
        / "dashboard"
        / "dashboard.py"
    )
    store = TradingStore(tmp_path / "dashboard-account.sqlite3")

    def engine_factory(**_kwargs):
        return TradingEngine(
            api=SimpleNamespace(),
            store=store,
            connection_factory=nullcontext,
        )

    with (
        patch(
            "execution.mt5_connection.mt5_connection",
            unavailable_connection,
        ),
        patch(
            "execution.trading_engine.TradingEngine",
            side_effect=engine_factory,
        ),
    ):
        app = AppTest.from_file(str(dashboard), default_timeout=90).run()
        app.sidebar.radio[0].set_value("Account Connections")
        app.run()
        app.text_input(key="account_profile_login").set_value("123456")
        app.text_input(key="account_profile_server").set_value("Broker-Demo")
        app.form_submit_button(key="save_account_profile").click().run()

    assert not app.exception
    profile = store.get_account_connection()
    assert profile["login"] == 123456
    assert profile["server"] == "Broker-Demo"
    assert profile["account_type"] == "DEMO"
    assert profile["verified"] is False
