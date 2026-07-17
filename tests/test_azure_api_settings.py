from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.azure_api.settings import ConfigurationError, Settings  # noqa: E402

TENANT_ID = "11111111-1111-4111-8111-111111111111"
API_CLIENT_ID = "22222222-2222-4222-8222-222222222222"
SPA_CLIENT_ID = "33333333-3333-4333-8333-333333333333"
IDENTITY_CLIENT_ID = "44444444-4444-4444-8444-444444444444"
FEDERATION_CLIENT_ID = "66666666-6666-4666-8666-666666666666"
FEDERATION_SUBJECT = "77777777-7777-4777-8777-777777777777"


def environment() -> dict[str, str]:
    return {
        "ENVIRONMENT_NAME": "prod",
        "ENTRA_TENANT_ID": TENANT_ID,
        "ENTRA_API_AUDIENCE": API_CLIENT_ID,
        "ALLOWED_CLIENT_IDS": f" {SPA_CLIENT_ID}, {SPA_CLIENT_ID} ",
        "DENIED_CLIENT_IDS": "55555555-5555-4555-8555-555555555555",
        "REQUIRE_USER_ROLES": "true",
        "ALLOWED_ORIGINS": "https://loans.example.com",
        "API_HOST_NAME": "api.loans.example.com",
        "AZURE_MANAGED_IDENTITY_CLIENT_ID": IDENTITY_CLIENT_ID,
        "AWS_FEDERATION_AUDIENCE": f"api://{FEDERATION_CLIENT_ID}",
        "AWS_FEDERATION_SUBJECT": FEDERATION_SUBJECT,
        "AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/loan-idp/azure-api-prod",
    }


def test_settings_load_safe_defaults_and_derived_values() -> None:
    settings = Settings.from_env(environment())

    assert settings.environment == "prod"
    assert settings.allowed_client_ids == frozenset({SPA_CLIENT_ID})
    assert settings.denied_client_ids == frozenset({"55555555-5555-4555-8555-555555555555"})
    assert settings.require_user_roles is True
    assert settings.aws_region == "us-west-2"
    assert settings.entra_issuer.endswith(f"/{TENANT_ID}/v2.0")
    assert settings.entra_jwks_uri.endswith(f"/{TENANT_ID}/discovery/v2.0/keys")
    assert settings.federation_scope == f"api://{FEDERATION_CLIENT_ID}/.default"
    assert settings.role_session_name == "azure-loan-api-prod"
    assert settings.max_grant_seconds == 600


def test_non_production_allows_local_http_and_explicit_overrides() -> None:
    values = environment()
    values.update(
        {
            "ENVIRONMENT_NAME": "dev",
            "ALLOWED_ORIGINS": "http://localhost:5173,https://dev.example.com",
            "REQUIRE_USER_ROLES": "false",
            "AWS_REGION": "us-gov-west-1",
            "AWS_SESSION_DURATION_SECONDS": "7200",
            "AWS_CREDENTIAL_REFRESH_SECONDS": "600",
            "UPLOAD_URL_SECONDS": "900",
            "DOWNLOAD_URL_SECONDS": "60",
            "MAX_REQUEST_BODY_BYTES": "8192",
            "JWT_LEEWAY_SECONDS": "0",
        }
    )

    settings = Settings.from_env(values)

    assert settings.allowed_origins == ("http://localhost:5173", "https://dev.example.com")
    assert settings.require_user_roles is False
    assert settings.aws_region == "us-gov-west-1"
    assert settings.aws_session_duration_seconds == 7200
    assert settings.max_request_body_bytes == 8192
    assert settings.jwt_leeway_seconds == 0
    assert settings.jwks_timeout_seconds == 5


@pytest.mark.parametrize("hostname", ["api", "localhost", "api.localhost", "127.0.0.1", "192.0.2.10", "::1"])
def test_production_api_hostname_requires_fully_qualified_dns_name(hostname: str) -> None:
    values = environment()
    values["API_HOST_NAME"] = hostname

    with pytest.raises(ConfigurationError, match="fully-qualified DNS hostname in production"):
        Settings.from_env(values)


@pytest.mark.parametrize("hostname", ["localhost", "api.localhost", "127.0.0.1"])
def test_non_production_api_hostname_preserves_local_development_hosts(hostname: str) -> None:
    values = environment()
    values.update({"ENVIRONMENT_NAME": "dev", "API_HOST_NAME": hostname})

    assert Settings.from_env(values).api_host_name == hostname


def test_origins_normalize_default_ports_and_preserve_non_default_ports() -> None:
    values = environment()
    values.update(
        {
            "ENVIRONMENT_NAME": "dev",
            "ALLOWED_ORIGINS": (
                "https://LOANS.example.com.:443,https://loans.example.com,"
                "http://localhost.:80,"
                "http://localhost:5173,https://api.example.com:8443"
            ),
        }
    )

    settings = Settings.from_env(values)

    assert settings.allowed_origins == (
        "https://loans.example.com",
        "http://localhost",
        "http://localhost:5173",
        "https://api.example.com:8443",
    )


def test_origins_preserve_ipv6_brackets_and_port_semantics() -> None:
    values = environment()
    values.update(
        {
            "ENVIRONMENT_NAME": "dev",
            "ALLOWED_ORIGINS": (
                "http://[::1]:80,http://[::1]:5173,"
                "https://[2001:db8::1]:443,https://[2001:db8::1]:8443"
            ),
        }
    )

    settings = Settings.from_env(values)

    assert settings.allowed_origins == (
        "http://[::1]",
        "http://[::1]:5173",
        "https://[2001:db8::1]",
        "https://[2001:db8::1]:8443",
    )


@pytest.mark.parametrize(
    "hostname",
    [
        "https://api.example.com",
        "api.example.com:443",
        "*.example.com",
        "api.example.com.",
        "API.EXAMPLE.COM",
    ],
)
def test_api_hostname_must_be_an_exact_dns_name(hostname: str) -> None:
    values = environment()
    values["API_HOST_NAME"] = hostname

    with pytest.raises(ConfigurationError, match="exact DNS hostname"):
        Settings.from_env(values)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"ENVIRONMENT_NAME": ""}, "ENVIRONMENT_NAME is required"),
        ({"ENVIRONMENT_NAME": "9bad"}, "ENVIRONMENT_NAME must be one of"),
        ({"ENVIRONMENT_NAME": "production"}, "ENVIRONMENT_NAME must be one of"),
        ({"ENVIRONMENT_NAME": "prodd"}, "ENVIRONMENT_NAME must be one of"),
        ({"ENVIRONMENT_NAME": "PROD"}, "ENVIRONMENT_NAME must be one of"),
        ({"ENTRA_TENANT_ID": "not-a-guid"}, "ENTRA_TENANT_ID must be a GUID"),
        ({"ENTRA_API_AUDIENCE": "not-a-guid"}, "ENTRA_API_AUDIENCE must be a GUID"),
        ({"ALLOWED_CLIENT_IDS": "not-a-guid"}, "ALLOWED_CLIENT_IDS entry must be a GUID"),
        ({"AWS_FEDERATION_AUDIENCE": "api://not-a-guid"}, "AWS_FEDERATION_AUDIENCE must be"),
        ({"AWS_FEDERATION_SUBJECT": "not-a-guid"}, "AWS_FEDERATION_SUBJECT must be a GUID"),
        ({"ALLOWED_CLIENT_IDS": ""}, "ALLOWED_CLIENT_IDS must contain"),
        ({"REQUIRE_USER_ROLES": "yes"}, "REQUIRE_USER_ROLES must be true or false"),
        ({"AWS_ROLE_ARN": "not-an-arn"}, "AWS_ROLE_ARN must identify"),
        ({"AWS_REGION": "west"}, "AWS_REGION has an invalid format"),
        ({"ALLOWED_ORIGINS": "https://example.com/path"}, "origins without paths"),
        ({"ALLOWED_ORIGINS": "https://:443"}, "origins without paths"),
        ({"ALLOWED_ORIGINS": "https://[::1"}, "invalid hostname"),
        ({"ALLOWED_ORIGINS": "https://example.com:not-a-port"}, "invalid port"),
        ({"ALLOWED_ORIGINS": "https://example.com.."}, "origins without paths"),
        ({"ALLOWED_ORIGINS": "http://example.com"}, "must use HTTPS"),
        ({"ALLOWED_ORIGINS": "https://*.example.com"}, "origins without paths"),
        ({"REQUIRE_USER_ROLES": "false"}, "cannot be disabled in production"),
        ({"AWS_SESSION_DURATION_SECONDS": "soon"}, "must be an integer"),
        ({"JWKS_TIMEOUT_SECONDS": "30"}, "must be between"),
        ({"AWS_SESSION_DURATION_SECONDS": "899"}, "must be between"),
        (
            {"AWS_SESSION_DURATION_SECONDS": "900", "AWS_CREDENTIAL_REFRESH_SECONDS": "840"},
            "too large for the session duration",
        ),
        (
            {"AWS_SESSION_DURATION_SECONDS": "900", "AWS_CREDENTIAL_REFRESH_SECONDS": "200", "UPLOAD_URL_SECONDS": "700"},
            "must exceed the grant and refresh windows",
        ),
    ],
)
def test_settings_fail_closed_for_invalid_configuration(mutation: dict[str, str], message: str) -> None:
    values = environment()
    values.update(mutation)

    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env(values)
