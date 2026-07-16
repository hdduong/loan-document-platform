from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_azure_api_settings import FEDERATION_CLIENT_ID, FEDERATION_SUBJECT, environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.azure_api import aws_credentials  # noqa: E402
from services.azure_api.aws_credentials import (  # noqa: E402
    AwsFederatedSessionProvider,
    FederationError,
    _expiration,
)
from services.azure_api.settings import Settings  # noqa: E402


def settings() -> Settings:
    return Settings.from_env(environment())


def web_token(
    audience: str = f"api://{FEDERATION_CLIENT_ID}", subject: str = FEDERATION_SUBJECT
) -> str:
    def encode(value: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{encode({'alg': 'RS256'})}.{encode({'aud': audience, 'sub': subject})}.signature"


def test_unsigned_sts_client_has_bounded_network_and_retry_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    sentinel = object()

    def client(name: str, **kwargs: Any) -> object:
        calls.append({"name": name, **kwargs})
        return sentinel

    monkeypatch.setattr(aws_credentials.boto3, "client", client)

    assert aws_credentials._unsigned_sts_client(settings()) is sentinel
    assert calls[0]["name"] == "sts"
    config = calls[0]["config"]
    assert config.signature_version == aws_credentials.UNSIGNED
    assert config.connect_timeout == 3
    assert config.read_timeout == 10
    assert config.tcp_keepalive is True
    assert config.retries == {"mode": "standard", "total_max_attempts": 3}


class ManagedIdentity:
    def __init__(self, value: str | Exception | None = None) -> None:
        self.value = value or web_token()
        self.scopes: list[str] = []
        self.closed = False

    def get_token(self, scope: str) -> Any:
        self.scopes.append(scope)
        if isinstance(self.value, Exception):
            raise self.value
        return SimpleNamespace(token=self.value)

    def close(self) -> None:
        self.closed = True


class StsClient:
    def __init__(self, clock: list[datetime], response: dict[str, Any] | Exception | None = None) -> None:
        self.clock = clock
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def assume_role_with_web_identity(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return self.response
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{len(self.calls)}",
                "SecretAccessKey": "secret",
                "SessionToken": "session",
                "Expiration": self.clock[0] + timedelta(hours=1),
            }
        }


def provider(
    clock: list[datetime],
    identity: ManagedIdentity | None = None,
    sts: StsClient | None = None,
) -> tuple[AwsFederatedSessionProvider, ManagedIdentity, StsClient]:
    identity = identity or ManagedIdentity()
    sts = sts or StsClient(clock)
    value = AwsFederatedSessionProvider(
        settings(),
        managed_identity=identity,
        sts_client_factory=lambda _settings: sts,
        now=lambda: clock[0],
    )
    return value, identity, sts


def federation_code(call: Any) -> str:
    with pytest.raises(FederationError) as raised:
        call()
    return raised.value.code


def test_credentials_are_exchanged_cached_refreshed_and_cleared() -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, identity, sts = provider(clock)

    first = value.credentials(600)
    assert value.credentials(600) is first
    assert identity.scopes == [f"api://{FEDERATION_CLIENT_ID}/.default"]
    assert sts.calls[0]["RoleArn"] == settings().aws_role_arn
    assert sts.calls[0]["RoleSessionName"] == "azure-loan-api-prod"
    assert sts.calls[0]["DurationSeconds"] == 3600

    clock[0] += timedelta(seconds=3100)
    second = value.credentials(600)
    assert second is not first
    assert len(sts.calls) == 2

    value.clear()
    assert value.credentials().access_key_id == "ASIA3"
    value.close()
    assert identity.closed is True


def test_session_uses_only_temporary_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, _, _ = provider(clock)
    captured: dict[str, Any] = {}
    sentinel = object()

    def fake_session(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(aws_credentials.boto3, "Session", fake_session)

    context = value.session_context(600)
    assert context.session is sentinel
    assert context.expiration == clock[0] + timedelta(hours=1)
    assert value.session(600) is sentinel
    assert captured == {
        "aws_access_key_id": "ASIA1",
        "aws_secret_access_key": "secret",
        "aws_session_token": "session",
        "region_name": "us-west-2",
    }


@pytest.mark.parametrize(
    ("identity_value", "expected"),
    [
        (RuntimeError("identity unavailable"), "MANAGED_IDENTITY_UNAVAILABLE"),
        ("not-a-token", "FEDERATION_TOKEN_INVALID"),
        (web_token(audience="wrong"), "FEDERATION_TOKEN_INVALID"),
        (web_token(subject="wrong"), "FEDERATION_TOKEN_INVALID"),
    ],
)
def test_managed_identity_failures_are_sanitized(identity_value: str | Exception, expected: str) -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, _, _ = provider(clock, identity=ManagedIdentity(identity_value))

    assert federation_code(value.credentials) == expected


def test_sts_transport_failure_is_sanitized() -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, _, _ = provider(clock, sts=StsClient(clock, RuntimeError("token must not leak")))

    assert federation_code(value.credentials) == "AWS_STS_UNAVAILABLE"


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"AccessKeyId": "A", "SecretAccessKey": "S", "SessionToken": "T", "Expiration": "bad"},
        {
            "AccessKeyId": "A",
            "SecretAccessKey": "S",
            "SessionToken": "T",
            "Expiration": datetime(2026, 7, 15, tzinfo=timezone.utc),
        },
        {
            "AccessKeyId": "A",
            "SecretAccessKey": "S",
            "SessionToken": "T",
            "Expiration": datetime(2026, 7, 16, 0, 5, tzinfo=timezone.utc),
        },
    ],
)
def test_invalid_sts_responses_fail_closed(credentials: dict[str, Any]) -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, _, _ = provider(clock, sts=StsClient(clock, {"Credentials": credentials}))

    assert federation_code(lambda: value.credentials(600)) == "STS_RESPONSE_INVALID"


def test_invalid_requested_windows_are_rejected_before_exchange() -> None:
    clock = [datetime(2026, 7, 16, tzinfo=timezone.utc)]
    value, identity, _ = provider(clock)

    with pytest.raises(ValueError, match="cannot be negative"):
        value.credentials(-1)
    assert federation_code(lambda: value.credentials(3540)) == "FEDERATION_WINDOW_INVALID"
    assert identity.scopes == []


def test_expiration_parses_utc_strings_and_rejects_missing_values() -> None:
    assert _expiration("2026-07-16T01:00:00Z") == datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    assert _expiration(datetime(2026, 7, 16, 1)).tzinfo == timezone.utc
    assert federation_code(lambda: _expiration(None)) == "STS_RESPONSE_INVALID"
