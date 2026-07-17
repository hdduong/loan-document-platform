"""Secretless Azure managed-identity to AWS STS credential exchange."""

from __future__ import annotations

import base64
import binascii
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import boto3
from azure.identity import ManagedIdentityCredential
from botocore import UNSIGNED
from botocore.config import Config

from .settings import Settings


class FederationError(RuntimeError):
    """A sanitized cross-cloud workload-federation failure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class AwsTemporaryCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: datetime

    def valid_for(self, now: datetime, seconds: int) -> bool:
        return (self.expiration - now).total_seconds() > seconds


@dataclass(frozen=True, slots=True)
class AwsSessionContext:
    """A boto session paired with the hard expiration of its STS credentials."""

    session: boto3.Session
    expiration: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unsigned_sts_client(settings: Settings) -> Any:
    return boto3.client(
        "sts",
        region_name=settings.aws_region,
        config=Config(
            signature_version=UNSIGNED,
            connect_timeout=3,
            read_timeout=10,
            retries={"total_max_attempts": 3, "mode": "standard"},
            tcp_keepalive=True,
        ),
    )


def _token_claims(token: str) -> dict[str, Any]:
    """Inspect claims for a fail-fast configuration check; AWS still verifies the signature."""

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(claims, dict):
            raise ValueError
        return claims
    except (IndexError, ValueError, TypeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise FederationError("FEDERATION_TOKEN_INVALID", "Managed identity returned an invalid token") from exc


def _expiration(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise FederationError("STS_RESPONSE_INVALID", "AWS STS returned an invalid expiration") from exc
    else:
        raise FederationError("STS_RESPONSE_INVALID", "AWS STS returned no credential expiration")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class AwsFederatedSessionProvider:
    """Cache and refresh one least-privilege AWS role session per API replica."""

    def __init__(
        self,
        settings: Settings,
        managed_identity: Any | None = None,
        sts_client_factory: Callable[[Settings], Any] = _unsigned_sts_client,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._managed_identity = managed_identity or ManagedIdentityCredential(
            client_id=settings.azure_managed_identity_client_id
        )
        self._sts_client_factory = sts_client_factory
        self._now = now
        self._cached: AwsTemporaryCredentials | None = None
        self._lock = threading.Lock()

    def credentials(self, minimum_validity_seconds: int = 0) -> AwsTemporaryCredentials:
        if minimum_validity_seconds < 0:
            raise ValueError("minimum_validity_seconds cannot be negative")
        required_window = max(minimum_validity_seconds, self._settings.aws_credential_refresh_seconds)
        if required_window >= self._settings.aws_session_duration_seconds - 60:
            raise FederationError("FEDERATION_WINDOW_INVALID", "The requested AWS credential window is too large")

        now = self._now()
        if self._cached and self._cached.valid_for(now, required_window):
            return self._cached
        with self._lock:
            now = self._now()
            if self._cached and self._cached.valid_for(now, required_window):
                return self._cached
            self._cached = self._exchange(now)
            if not self._cached.valid_for(now, minimum_validity_seconds):
                self._cached = None
                raise FederationError("STS_RESPONSE_INVALID", "AWS STS returned credentials with insufficient lifetime")
            return self._cached

    def _exchange(self, now: datetime) -> AwsTemporaryCredentials:
        try:
            access_token = self._managed_identity.get_token(self._settings.federation_scope)
            token = access_token.token
        except Exception as exc:
            raise FederationError("MANAGED_IDENTITY_UNAVAILABLE", "Managed identity token acquisition failed") from exc
        claims = _token_claims(token)
        if claims.get("aud") != self._settings.aws_federation_audience:
            raise FederationError("FEDERATION_TOKEN_INVALID", "Managed identity token audience is not allowed")
        if claims.get("sub") != self._settings.aws_federation_subject:
            raise FederationError("FEDERATION_TOKEN_INVALID", "Managed identity token subject is not allowed")

        try:
            response = self._sts_client_factory(self._settings).assume_role_with_web_identity(
                RoleArn=self._settings.aws_role_arn,
                RoleSessionName=self._settings.role_session_name,
                WebIdentityToken=token,
                DurationSeconds=self._settings.aws_session_duration_seconds,
            )
        except Exception as exc:
            raise FederationError("AWS_STS_UNAVAILABLE", "AWS workload federation failed") from exc

        values = response.get("Credentials") or {}
        credentials = AwsTemporaryCredentials(
            access_key_id=str(values.get("AccessKeyId", "")),
            secret_access_key=str(values.get("SecretAccessKey", "")),
            session_token=str(values.get("SessionToken", "")),
            expiration=_expiration(values.get("Expiration")),
        )
        if not credentials.access_key_id or not credentials.secret_access_key or not credentials.session_token:
            raise FederationError("STS_RESPONSE_INVALID", "AWS STS returned incomplete credentials")
        if not credentials.valid_for(now, 0):
            raise FederationError("STS_RESPONSE_INVALID", "AWS STS returned expired credentials")
        return credentials

    def session(self, minimum_validity_seconds: int = 0) -> boto3.Session:
        return self.session_context(minimum_validity_seconds).session

    def session_context(self, minimum_validity_seconds: int = 0) -> AwsSessionContext:
        values = self.credentials(minimum_validity_seconds)
        return AwsSessionContext(
            session=boto3.Session(
                aws_access_key_id=values.access_key_id,
                aws_secret_access_key=values.secret_access_key,
                aws_session_token=values.session_token,
                region_name=self._settings.aws_region,
            ),
            expiration=values.expiration,
        )

    def clear(self) -> None:
        with self._lock:
            self._cached = None

    def close(self) -> None:
        close = getattr(self._managed_identity, "close", None)
        if callable(close):
            close()
