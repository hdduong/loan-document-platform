"""Strict environment configuration for the Azure API runtime."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping
from urllib.parse import urlparse
from uuid import UUID

SUPPORTED_ENVIRONMENTS = frozenset({"dev", "test", "stage", "prod"})


class ConfigurationError(RuntimeError):
    """The runtime configuration is incomplete or unsafe."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _guid(environ: Mapping[str, str], name: str) -> str:
    value = _required(environ, name)
    return _guid_value(value, name)


def _guid_value(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a GUID") from exc


def _csv(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in environ.get(name, "").split(",") if value.strip()))


def _boolean(environ: Mapping[str, str], name: str, default: bool) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return normalized == "true"


def _integer(environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _origins(environ: Mapping[str, str], environment: str) -> tuple[str, ...]:
    origins = _csv(environ, "ALLOWED_ORIGINS")
    if not origins:
        raise ConfigurationError("ALLOWED_ORIGINS must contain at least one origin")
    normalized_origins: list[str] = []
    for origin in origins:
        try:
            parsed = urlparse(origin)
        except ValueError as exc:
            raise ConfigurationError("ALLOWED_ORIGINS entries contain an invalid hostname") from exc
        try:
            port = parsed.port
        except ValueError as exc:
            raise ConfigurationError("ALLOWED_ORIGINS entries contain an invalid port") from exc
        localhost = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or "*" in parsed.netloc
        ):
            raise ConfigurationError("ALLOWED_ORIGINS entries must be origins without paths")
        if parsed.scheme != "https" and not (environment != "prod" and parsed.scheme == "http" and localhost):
            raise ConfigurationError("ALLOWED_ORIGINS entries must use HTTPS")
        hostname = parsed.hostname or ""
        rendered_hostname = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        port_suffix = f":{port}" if port is not None and port != default_port else ""
        normalized_origins.append(f"{parsed.scheme}://{rendered_hostname}{port_suffix}")
    return tuple(dict.fromkeys(normalized_origins))


def _api_hostname(environ: Mapping[str, str]) -> str:
    hostname = _required(environ, "API_HOST_NAME")
    if (
        hostname != hostname.lower()
        or len(hostname) > 253
        or hostname.endswith(".")
        or not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            hostname,
        )
    ):
        raise ConfigurationError("API_HOST_NAME must be an exact DNS hostname")
    return hostname


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated configuration containing identifiers but no reusable secrets."""

    environment: str
    entra_tenant_id: str
    entra_api_audience: str
    allowed_client_ids: frozenset[str]
    denied_client_ids: frozenset[str]
    require_user_roles: bool
    allowed_origins: tuple[str, ...]
    api_host_name: str
    azure_managed_identity_client_id: str
    aws_federation_audience: str
    aws_federation_subject: str
    aws_role_arn: str
    aws_region: str
    aws_session_duration_seconds: int
    aws_credential_refresh_seconds: int
    upload_url_seconds: int
    download_url_seconds: int
    max_request_body_bytes: int
    jwt_leeway_seconds: int
    jwks_timeout_seconds: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if environ is None else environ
        environment = _required(values, "ENVIRONMENT_NAME")
        if environment not in SUPPORTED_ENVIRONMENTS:
            allowed = ", ".join(sorted(SUPPORTED_ENVIRONMENTS))
            raise ConfigurationError(f"ENVIRONMENT_NAME must be one of: {allowed}")

        audience = _guid(values, "ENTRA_API_AUDIENCE")
        allowed_clients = frozenset(
            _guid_value(value, "ALLOWED_CLIENT_IDS entry") for value in _csv(values, "ALLOWED_CLIENT_IDS")
        )
        if not allowed_clients:
            raise ConfigurationError("ALLOWED_CLIENT_IDS must contain at least one client")
        denied_clients = frozenset(
            _guid_value(value, "DENIED_CLIENT_IDS entry") for value in _csv(values, "DENIED_CLIENT_IDS")
        )

        federation_audience = _required(values, "AWS_FEDERATION_AUDIENCE")
        match = re.fullmatch(r"api://([0-9a-fA-F-]{36})", federation_audience)
        if match is None:
            raise ConfigurationError("AWS_FEDERATION_AUDIENCE must be api:// followed by an application GUID")
        federation_audience = f"api://{_guid_value(match.group(1), 'AWS_FEDERATION_AUDIENCE')}"

        role_arn = _required(values, "AWS_ROLE_ARN")
        if not re.fullmatch(r"arn:(aws|aws-us-gov):iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}", role_arn):
            raise ConfigurationError("AWS_ROLE_ARN must identify an IAM role")

        region = values.get("AWS_REGION", "us-west-2").strip()
        if not re.fullmatch(r"[a-z]{2}(-gov)?-[a-z]+-[0-9]", region):
            raise ConfigurationError("AWS_REGION has an invalid format")

        duration = _integer(values, "AWS_SESSION_DURATION_SECONDS", 3600, 900, 43200)
        refresh = _integer(values, "AWS_CREDENTIAL_REFRESH_SECONDS", 300, 60, 3600)
        if refresh >= duration - 60:
            raise ConfigurationError("AWS_CREDENTIAL_REFRESH_SECONDS is too large for the session duration")

        require_user_roles = _boolean(values, "REQUIRE_USER_ROLES", True)
        if environment == "prod" and not require_user_roles:
            raise ConfigurationError("REQUIRE_USER_ROLES cannot be disabled in production")

        settings = cls(
            environment=environment,
            entra_tenant_id=_guid(values, "ENTRA_TENANT_ID"),
            entra_api_audience=audience,
            allowed_client_ids=allowed_clients,
            denied_client_ids=denied_clients,
            require_user_roles=require_user_roles,
            allowed_origins=_origins(values, environment),
            api_host_name=_api_hostname(values),
            azure_managed_identity_client_id=_guid(values, "AZURE_MANAGED_IDENTITY_CLIENT_ID"),
            aws_federation_audience=federation_audience,
            aws_federation_subject=_guid(values, "AWS_FEDERATION_SUBJECT"),
            aws_role_arn=role_arn,
            aws_region=region,
            aws_session_duration_seconds=duration,
            aws_credential_refresh_seconds=refresh,
            upload_url_seconds=_integer(values, "UPLOAD_URL_SECONDS", 600, 60, 3600),
            download_url_seconds=_integer(values, "DOWNLOAD_URL_SECONDS", 120, 30, 900),
            max_request_body_bytes=_integer(values, "MAX_REQUEST_BODY_BYTES", 65536, 1024, 1048576),
            jwt_leeway_seconds=_integer(values, "JWT_LEEWAY_SECONDS", 30, 0, 120),
            jwks_timeout_seconds=_integer(values, "JWKS_TIMEOUT_SECONDS", 5, 1, 10),
        )
        if settings.max_grant_seconds + settings.aws_credential_refresh_seconds >= duration:
            raise ConfigurationError("AWS session duration must exceed the grant and refresh windows")
        return settings

    @property
    def entra_issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"

    @property
    def entra_jwks_uri(self) -> str:
        return f"https://login.microsoftonline.com/{self.entra_tenant_id}/discovery/v2.0/keys"

    @property
    def federation_scope(self) -> str:
        return f"{self.aws_federation_audience.rstrip('/')}/.default"

    @property
    def role_session_name(self) -> str:
        return f"azure-loan-api-{self.environment}"[:64]

    @property
    def max_grant_seconds(self) -> int:
        return max(self.upload_url_seconds, self.download_url_seconds)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""

    return Settings.from_env()
