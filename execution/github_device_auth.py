"""Short-lived GitHub OAuth device flow for operator identity verification."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"
_DEVICE_URI = "https://github.com/login/device"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{10,128}$")


class GitHubAuthError(RuntimeError):
    """Raised when the GitHub identity flow cannot be completed safely."""


def _read_json(response):
    try:
        payload = response.read(64 * 1024 + 1)
        if len(payload) > 64 * 1024:
            raise GitHubAuthError("GitHub returned an oversized response.")
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GitHubAuthError("GitHub returned an invalid response.") from error
    if not isinstance(data, dict):
        raise GitHubAuthError("GitHub returned an invalid response.")
    return data


def _request_json(request, *, opener=urlopen):
    try:
        with opener(request, timeout=10) as response:
            return _read_json(response)
    except HTTPError as error:
        raise GitHubAuthError(
            f"GitHub request failed with HTTP {error.code}."
        ) from None
    except (TimeoutError, URLError, OSError) as error:
        raise GitHubAuthError(
            "GitHub could not be reached. Check the network and try again."
        ) from None


def _validate_client_id(client_id):
    if (
        not isinstance(client_id, str)
        or _CLIENT_ID_PATTERN.fullmatch(client_id.strip()) is None
    ):
        raise ValueError("Enter a valid public GitHub OAuth client ID.")
    return client_id.strip()


def start_device_authorization(client_id, *, opener=urlopen, now=None):
    client_id = _validate_client_id(client_id)
    request = Request(
        _DEVICE_CODE_URL,
        data=urlencode({
            "client_id": client_id,
            "scope": "read:user",
        }).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "NEXORA-FX-Operator-Verification",
        },
        method="POST",
    )
    response = _request_json(request, opener=opener)
    device_code = response.get("device_code")
    user_code = response.get("user_code")
    verification_uri = response.get("verification_uri")
    interval = response.get("interval")
    expires_in = response.get("expires_in")
    if (
        not isinstance(device_code, str)
        or not device_code
        or len(device_code) > 2048
        or not isinstance(user_code, str)
        or not user_code
        or len(user_code) > 64
        or verification_uri != _DEVICE_URI
        or isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval < 1
        or interval > 300
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in < 1
        or expires_in > 1800
    ):
        raise GitHubAuthError("GitHub returned incomplete device authorization.")
    current_time = time.time() if now is None else now
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_uri": _DEVICE_URI,
        "interval": interval,
        "expires_at": current_time + expires_in,
    }


def poll_device_authorization(
    client_id,
    device_code,
    *,
    opener=urlopen,
):
    client_id = _validate_client_id(client_id)
    if not isinstance(device_code, str) or not device_code:
        raise ValueError("GitHub device authorization is missing.")
    request = Request(
        _ACCESS_TOKEN_URL,
        data=urlencode({
            "client_id": client_id,
            "device_code": device_code,
            "grant_type": _DEVICE_GRANT,
        }).encode("ascii"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "NEXORA-FX-Operator-Verification",
        },
        method="POST",
    )
    response = _request_json(request, opener=opener)
    error = response.get("error")
    if error in {
        "authorization_pending",
        "slow_down",
        "expired_token",
        "access_denied",
    }:
        return {"status": error}
    token = response.get("access_token")
    if not isinstance(token, str) or not token:
        raise GitHubAuthError("GitHub authorization was not completed.")

    user_request = Request(
        _USER_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "NEXORA-FX-Operator-Verification",
        },
        method="GET",
    )
    try:
        user = _request_json(user_request, opener=opener)
    finally:
        token = None
        response = None
    login = user.get("login")
    if not isinstance(login, str) or not login.strip():
        raise GitHubAuthError("GitHub did not return a valid username.")
    return {"status": "authorized", "login": login.strip()}
