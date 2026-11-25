"""Shared account-profile, credential, and connection workflows."""

from execution.credential_store import WindowsCredentialStore
from execution.trading_engine import TradeRejected


class AccountManager:
    def __init__(self, store, credential_store=None):
        self.store = store
        self.credentials = credential_store

    def _credential_store(self):
        if self.credentials is None:
            self.credentials = WindowsCredentialStore()
        return self.credentials

    def save_profile(self, login, server, account_type, password):
        if not isinstance(password, str) or not password:
            raise ValueError("Enter the MT5 password to save this account.")
        profile = self.store.save_account_profile(login, server, account_type)
        self._credential_store().set_password(profile["profile_id"], password)
        self.store.set_account_profile_credential_ref(
            profile["profile_id"],
            profile["profile_id"],
        )
        if (
            self.store.get_account_manager_state()["active_profile_id"]
            == profile["profile_id"]
        ):
            self.store.invalidate_account_connection_verification()
        return self.store.get_account_profile(profile["profile_id"])

    def update_password(self, profile_id, password):
        if not isinstance(password, str) or not password:
            raise ValueError("Enter the MT5 password to update this account.")
        profile = self.store.get_account_profile(profile_id)
        if profile is None:
            raise ValueError("The selected account profile does not exist.")
        self._credential_store().set_password(profile_id, password)
        self.store.set_account_profile_credential_ref(profile_id, profile_id)
        if (
            self.store.get_account_manager_state()["active_profile_id"]
            == profile_id
        ):
            self.store.invalidate_account_connection_verification()
        return self.store.get_account_profile(profile_id)

    def activate_profile(self, profile_id):
        return self.store.activate_account_profile(profile_id)

    def connect_profile(self, profile_id, engine):
        profile = self.store.get_account_profile(profile_id)
        if profile is None:
            raise ValueError("The selected account profile does not exist.")
        if profile["credential_ref"] != profile_id:
            raise ValueError(
                "Save this account password in Windows Credential Manager first."
            )
        self._credential_store().get_password(profile_id)
        self.store.activate_account_profile(profile_id)
        engine.refresh_configuration()
        connected = engine.connect_account()
        if connected["matches_saved_profile"] is not True:
            raise TradeRejected(
                "Connected MT5 account does not match the selected profile."
            )
        verified = engine.verify_account_connection()
        return {**connected, **verified}

    def disconnect(self, engine):
        self.store.deactivate_account_profile()
        engine.refresh_configuration()
