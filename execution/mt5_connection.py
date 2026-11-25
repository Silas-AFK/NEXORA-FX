import threading
from contextlib import contextmanager

import MetaTrader5 as mt5


class MT5ConnectionError(RuntimeError):
    """Raised when an MT5 session cannot be established safely."""


_connection_lock = threading.RLock()
_thread_state = threading.local()
_lease_count = 0
_manager_owns_connection = False


def _last_error_text():
    try:
        return str(mt5.last_error())
    except Exception as error:
        return f"MT5 error details unavailable: {error}"


@contextmanager
def mt5_connection():
    """Lease an MT5 session without shutting down externally owned sessions."""

    global _lease_count, _manager_owns_connection

    with _connection_lock:
        try:
            terminal = mt5.terminal_info()
        except Exception as error:
            raise MT5ConnectionError(
                f"Could not inspect MT5 terminal state: {error}"
            ) from error

        if terminal is None:
            try:
                initialized = mt5.initialize()
            except Exception as error:
                raise MT5ConnectionError(
                    f"MT5 initialization raised an error: {error}"
                ) from error

            if not initialized:
                raise MT5ConnectionError(
                    f"MT5 initialization failed: {_last_error_text()}"
                )

            _manager_owns_connection = True
            try:
                terminal = mt5.terminal_info()
            except Exception as error:
                try:
                    mt5.shutdown()
                finally:
                    _manager_owns_connection = False
                raise MT5ConnectionError(
                    f"Could not inspect MT5 terminal after initialization: {error}"
                ) from error

            if terminal is None:
                try:
                    mt5.shutdown()
                finally:
                    _manager_owns_connection = False
                raise MT5ConnectionError(
                    "MT5 initialized but terminal information is unavailable: "
                    f"{_last_error_text()}"
                )

        _lease_count += 1

    try:
        yield
    finally:
        with _connection_lock:
            _lease_count -= 1
            if _lease_count == 0 and _manager_owns_connection:
                _manager_owns_connection = False
                mt5.shutdown()


def connect():
    """Compatibility wrapper that retains a session until disconnect()."""

    try:
        session = mt5_connection()
        session.__enter__()
    except MT5ConnectionError as error:
        print(error)
        return False

    sessions = getattr(_thread_state, "sessions", None)
    if sessions is None:
        sessions = []
        _thread_state.sessions = sessions
    sessions.append(session)
    return True


def get_account_info():
    """Read account details within a safely leased MT5 connection."""

    with mt5_connection():
        account = mt5.account_info()
        if account is None:
            print("Could not retrieve account information:", _last_error_text())
            return None

        return {
            "login": account.login,
            "server": account.server,
            "currency": account.currency,
            "balance": account.balance,
            "equity": account.equity,
            "leverage": account.leverage,
            "margin_free": getattr(account, "margin_free", None),
        }


def disconnect():
    """Release this thread's compatibility lease, if one exists."""

    sessions = getattr(_thread_state, "sessions", None)
    if not sessions:
        return False

    session = sessions.pop()
    session.__exit__(None, None, None)
    return True
