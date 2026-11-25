from types import SimpleNamespace

import pytest

from execution import mt5_connection as connection_manager


@pytest.fixture(autouse=True)
def reset_connection_state():
    connection_manager._lease_count = 0
    connection_manager._manager_owns_connection = False
    connection_manager._thread_state.sessions = []
    yield
    connection_manager._lease_count = 0
    connection_manager._manager_owns_connection = False
    connection_manager._thread_state.sessions = []


def test_borrowed_terminal_is_not_initialized_or_shutdown(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(
        connection_manager.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "initialize",
        lambda: pytest.fail("A borrowed connection must not be reinitialized."),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )

    with connection_manager.mt5_connection():
        assert connection_manager._lease_count == 1

    assert shutdown_calls == []


def test_owned_connection_is_initialized_once_and_closed_after_last_lease(
    monkeypatch,
):
    state = {"initialized": False}
    calls = {"initialize": 0, "shutdown": 0}

    def terminal_info():
        return SimpleNamespace() if state["initialized"] else None

    def initialize():
        calls["initialize"] += 1
        state["initialized"] = True
        return True

    def shutdown():
        calls["shutdown"] += 1
        state["initialized"] = False

    monkeypatch.setattr(connection_manager.mt5, "terminal_info", terminal_info)
    monkeypatch.setattr(connection_manager.mt5, "initialize", initialize)
    monkeypatch.setattr(connection_manager.mt5, "shutdown", shutdown)

    with connection_manager.mt5_connection():
        with connection_manager.mt5_connection():
            assert connection_manager._lease_count == 2
        assert calls["shutdown"] == 0

    assert calls == {"initialize": 1, "shutdown": 1}


def test_next_lease_reinitializes_after_a_disconnection(monkeypatch):
    state = {"initialized": False}
    calls = {"initialize": 0, "shutdown": 0}

    def terminal_info():
        return SimpleNamespace(connected=True) if state["initialized"] else None

    def initialize():
        calls["initialize"] += 1
        state["initialized"] = True
        return True

    def shutdown():
        calls["shutdown"] += 1
        state["initialized"] = False

    monkeypatch.setattr(connection_manager.mt5, "terminal_info", terminal_info)
    monkeypatch.setattr(connection_manager.mt5, "initialize", initialize)
    monkeypatch.setattr(connection_manager.mt5, "shutdown", shutdown)

    with connection_manager.mt5_connection():
        assert state["initialized"] is True
    assert state["initialized"] is False

    with connection_manager.mt5_connection():
        assert state["initialized"] is True

    assert calls == {"initialize": 2, "shutdown": 2}


def test_failed_initialization_does_not_shutdown_external_state(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(connection_manager.mt5, "terminal_info", lambda: None)
    monkeypatch.setattr(connection_manager.mt5, "initialize", lambda: False)
    monkeypatch.setattr(connection_manager.mt5, "last_error", lambda: (1, "offline"))
    monkeypatch.setattr(
        connection_manager.mt5,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )

    with pytest.raises(connection_manager.MT5ConnectionError, match="offline"):
        with connection_manager.mt5_connection():
            pass

    assert shutdown_calls == []


def test_missing_terminal_info_after_init_closes_owned_connection(monkeypatch):
    shutdown_calls = []
    monkeypatch.setattr(connection_manager.mt5, "terminal_info", lambda: None)
    monkeypatch.setattr(connection_manager.mt5, "initialize", lambda: True)
    monkeypatch.setattr(connection_manager.mt5, "last_error", lambda: (2, "no terminal"))
    monkeypatch.setattr(
        connection_manager.mt5,
        "shutdown",
        lambda: shutdown_calls.append(True),
    )

    with pytest.raises(connection_manager.MT5ConnectionError, match="unavailable"):
        with connection_manager.mt5_connection():
            pass

    assert shutdown_calls == [True]


def test_saved_account_credentials_are_used_without_exposing_password(monkeypatch):
    state = {"initialized": False}
    calls = []

    def terminal_info():
        return SimpleNamespace(connected=True) if state["initialized"] else None

    def initialize(**kwargs):
        calls.append(kwargs)
        state["initialized"] = True
        return True

    def shutdown():
        state["initialized"] = False

    monkeypatch.setattr(connection_manager.mt5, "terminal_info", terminal_info)
    monkeypatch.setattr(connection_manager.mt5, "initialize", initialize)
    monkeypatch.setattr(connection_manager.mt5, "shutdown", shutdown)

    with connection_manager.mt5_connection(
        login=123456,
        password="do-not-log-this",
        server="Broker-Demo",
    ):
        assert state["initialized"] is True

    assert calls == [{
        "login": 123456,
        "password": "do-not-log-this",
        "server": "Broker-Demo",
    }]
    assert state["initialized"] is False


def test_saved_account_login_switches_borrowed_terminal_but_never_shuts_it_down(
    monkeypatch,
):
    calls = {"login": [], "shutdown": []}
    monkeypatch.setattr(
        connection_manager.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "account_info",
        lambda: SimpleNamespace(login=999999, server="Other"),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "login",
        lambda account, *, password, server: calls["login"].append(
            (account, password, server)
        ) or True,
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "shutdown",
        lambda: calls["shutdown"].append(True),
    )

    with connection_manager.mt5_connection(
        login=123456,
        password="private",
        server="Broker-Demo",
    ):
        assert connection_manager._manager_owns_connection is False

    assert calls == {
        "login": [(123456, "private", "Broker-Demo")],
        "shutdown": [],
    }


def test_credential_login_failure_is_sanitized_and_preserves_borrowed_terminal(
    monkeypatch,
):
    monkeypatch.setattr(
        connection_manager.mt5,
        "terminal_info",
        lambda: SimpleNamespace(connected=True),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "account_info",
        lambda: SimpleNamespace(login=999999, server="Other"),
    )
    monkeypatch.setattr(
        connection_manager.mt5,
        "login",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private password value")
        ),
    )

    with pytest.raises(
        connection_manager.MT5ConnectionError,
        match="Could not authenticate",
    ) as error:
        with connection_manager.mt5_connection(
            login=123456,
            password="private",
            server="Broker-Demo",
        ):
            pass

    assert "private" not in str(error.value)
