from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from execution.account_manager import AccountManager
from execution.credential_store import (
    CredentialStoreError,
    WindowsCredentialStore,
)
from execution.trading_engine import TradingEngine
from execution.trading_store import TradingStore


class FakeVault:
    def __init__(self):
        self.values = {}

    def get_keyring(self):
        return type(
            "WinVaultKeyring",
            (),
            {"__module__": "keyring.backends.Windows"},
        )()

    def set_password(self, service, username, password):
        self.values[(service, username)] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_REAL = 2

    def terminal_info(self):
        return SimpleNamespace(
            connected=True,
            trade_allowed=True,
            tradeapi_disabled=False,
        )

    def account_info(self):
        return SimpleNamespace(
            login=123456,
            server="Broker-Demo",
            trade_mode=self.ACCOUNT_TRADE_MODE_DEMO,
            trade_allowed=True,
            trade_expert=True,
        )


def test_credential_store_requires_windows_vault_and_never_uses_plaintext():
    vault = FakeVault()
    credentials = WindowsCredentialStore(
        backend=vault,
        platform_name="Windows",
    )
    credentials.set_password("profile-1", "do-not-log-this")

    assert credentials.get_password("profile-1") == "do-not-log-this"
    credentials.delete_password("profile-1")
    with pytest.raises(CredentialStoreError):
        credentials.get_password("profile-1")
    with pytest.raises(CredentialStoreError, match="Windows"):
        WindowsCredentialStore(backend=vault, platform_name="Linux")


def test_profiles_credentials_switching_and_restart_persistence(tmp_path):
    database = tmp_path / "accounts.sqlite3"
    store = TradingStore(database)
    vault = FakeVault()
    manager = AccountManager(
        store,
        credential_store=WindowsCredentialStore(backend=vault),
    )
    first = manager.save_profile(
        "123456",
        "Broker-Demo",
        "DEMO",
        "private-demo-password",
    )
    second = manager.save_profile(
        "654321",
        "Other-Demo",
        "DEMO",
        "private-second-password",
    )
    manager.activate_profile(first["profile_id"])
    store.update_operational_config(
        agent_enabled=True,
        execution_enabled=True,
        runner_enabled=True,
    )
    manager.activate_profile(second["profile_id"])

    persisted = TradingStore(database)
    profiles = persisted.list_account_profiles()
    database_bytes = database.read_bytes()

    assert len(profiles) == 2
    assert persisted.get_active_account_profile()["profile_id"] == (
        second["profile_id"]
    )
    assert persisted.get_account_connection()["verified"] is False
    assert persisted.get_operational_config()["agent_enabled"] is False
    assert persisted.get_operational_config()["execution_enabled"] is False
    assert persisted.get_operational_config()["runner_enabled"] is False
    assert b"private-demo-password" not in database_bytes
    assert b"private-second-password" not in database_bytes


def test_connect_profile_uses_actual_identity_without_submitting_orders(tmp_path):
    store = TradingStore(tmp_path / "connect.sqlite3")
    vault = FakeVault()
    manager = AccountManager(
        store,
        credential_store=WindowsCredentialStore(backend=vault),
    )
    profile = manager.save_profile(
        "123456",
        "Broker-Demo",
        "DEMO",
        "private-password",
    )
    engine = TradingEngine(
        api=FakeMT5(),
        store=store,
        connection_factory=nullcontext,
    )

    connected = manager.connect_profile(profile["profile_id"], engine)

    assert connected["verified"] is True
    assert connected["matches_saved_profile"] is True
    assert store.get_account_connection()["verified"] is True
    assert store.list_trades() == []

    manager.disconnect(engine)
    assert store.get_active_account_profile() is None
    assert len(store.list_account_profiles()) == 1

    reconnected = manager.connect_profile(profile["profile_id"], engine)
    assert reconnected["verified"] is True
    assert store.get_active_account_profile()["profile_id"] == (
        profile["profile_id"]
    )
    assert store.list_trades() == []


def test_invalid_profile_and_missing_credentials_fail_closed(tmp_path):
    store = TradingStore(tmp_path / "invalid.sqlite3")
    manager = AccountManager(
        store,
        credential_store=WindowsCredentialStore(backend=FakeVault()),
    )
    with pytest.raises(ValueError):
        manager.save_profile(
            "not-a-login",
            "Broker-Demo",
            "DEMO",
            "password",
        )

    profile = store.save_account_profile("123456", "Broker-Demo", "DEMO")
    engine = TradingEngine(
        api=FakeMT5(),
        store=store,
        connection_factory=nullcontext,
    )
    with pytest.raises(ValueError, match="Credential Manager"):
        manager.connect_profile(profile["profile_id"], engine)


def test_account_switch_invalidates_github_authorization(tmp_path):
    store = TradingStore(tmp_path / "github-switch.sqlite3")
    first = store.save_account_profile("123456", "Broker-Demo", "DEMO")
    second = store.save_account_profile("654321", "Other-Demo", "DEMO")
    store.activate_account_profile(first["profile_id"])
    store.mark_account_connection_verified(
        123456,
        "Broker-Demo",
        "DEMO",
    )
    store.save_github_client_id("Iv1.publicclient123")
    store.save_github_authorization(
        "Silas-AFK",
        123456,
        "Broker-Demo",
    )

    store.activate_account_profile(second["profile_id"])

    configuration = store.get_operational_config()
    assert configuration["account_manager"]["github_authorized_login"] is None
    assert configuration["account_manager"]["live_activated_at"] is None
    assert store.get_account_connection()["verified"] is False
