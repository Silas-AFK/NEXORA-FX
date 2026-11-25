import platform


_SERVICE_NAME = "NEXORA FX MT5 accounts"


class CredentialStoreError(RuntimeError):
    """Raised when secure operating-system credential storage is unavailable."""


class WindowsCredentialStore:
    def __init__(self, backend=None, *, platform_name=None):
        self.platform_name = platform_name or platform.system()
        if self.platform_name != "Windows":
            raise CredentialStoreError(
                "MT5 credentials can only be saved with Windows Credential Manager."
            )
        if backend is None:
            try:
                import keyring
            except ImportError as error:
                raise CredentialStoreError(
                    "Install the required Windows credential-storage package."
                ) from error
            backend = keyring

        try:
            selected = backend.get_keyring()
        except Exception as error:
            raise CredentialStoreError(
                "Windows Credential Manager is unavailable."
            ) from error
        if (
            selected.__class__.__module__ != "keyring.backends.Windows"
            or selected.__class__.__name__ != "WinVaultKeyring"
        ):
            raise CredentialStoreError(
                "A secure Windows Credential Manager backend is required; "
                "credentials were not saved."
            )
        self.backend = backend

    @staticmethod
    def _validate(profile_id, password=None):
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ValueError("A valid account profile reference is required.")
        if password is not None and (
            not isinstance(password, str)
            or not password
            or len(password) > 1024
        ):
            raise ValueError("MT5 password must be between 1 and 1024 characters.")

    def set_password(self, profile_id, password):
        self._validate(profile_id, password)
        try:
            self.backend.set_password(_SERVICE_NAME, profile_id, password)
        except Exception as error:
            raise CredentialStoreError(
                "Windows Credential Manager could not save the MT5 password."
            ) from error

    def get_password(self, profile_id):
        self._validate(profile_id)
        try:
            password = self.backend.get_password(_SERVICE_NAME, profile_id)
        except Exception as error:
            raise CredentialStoreError(
                "Windows Credential Manager could not read the MT5 password."
            ) from error
        if not isinstance(password, str) or not password:
            raise CredentialStoreError(
                "No saved MT5 password exists for this account; update the profile."
            )
        return password
