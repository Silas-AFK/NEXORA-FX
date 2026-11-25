import json

import pytest

from execution.github_device_auth import (
    GitHubAuthError,
    poll_device_authorization,
    start_device_authorization,
)


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return json.dumps(self.value).encode("utf-8")


def test_start_device_authorization_returns_only_short_lived_flow_state():
    result = start_device_authorization(
        "Iv1.clientid123",
        opener=lambda *_args, **_kwargs: FakeResponse({
            "device_code": "transient-device-code",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 600,
        }),
        now=100,
    )

    assert result == {
        "device_code": "transient-device-code",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://github.com/login/device",
        "interval": 5,
        "expires_at": 700,
    }


def test_pending_device_authorization_does_not_retain_access_token():
    result = poll_device_authorization(
        "Iv1.clientid123",
        "transient-device-code",
        opener=lambda *_args, **_kwargs: FakeResponse({
            "error": "authorization_pending",
        }),
    )

    assert result == {"status": "authorization_pending"}


def test_completed_flow_fetches_identity_and_does_not_return_token():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.full_url == "https://github.com/login/oauth/access_token":
            return FakeResponse({"access_token": "must-not-be-persisted"})
        return FakeResponse({"login": "Silas-AFK"})

    result = poll_device_authorization(
        "Iv1.clientid123",
        "transient-device-code",
        opener=opener,
    )

    assert result == {"status": "authorized", "login": "Silas-AFK"}
    assert "must-not-be-persisted" not in repr(result)
    assert requests[1].get_header("Authorization") == (
        "Bearer must-not-be-persisted"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "device_code": "code",
            "user_code": "ABCD",
            "verification_uri": "https://attacker.invalid",
            "interval": 5,
            "expires_in": 600,
        },
    ],
)
def test_invalid_device_authorization_response_fails_closed(payload):
    with pytest.raises(GitHubAuthError):
        start_device_authorization(
            "Iv1.clientid123",
            opener=lambda *_args, **_kwargs: FakeResponse(payload),
        )


def test_invalid_client_id_is_rejected_before_network_call():
    with pytest.raises(ValueError, match="public GitHub"):
        start_device_authorization(
            "bad",
            opener=lambda *_args, **_kwargs: pytest.fail(
                "Invalid client IDs must be rejected before the request."
            ),
        )
