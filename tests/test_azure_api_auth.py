from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_azure_api_settings import API_CLIENT_ID, SPA_CLIENT_ID, TENANT_ID, environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.azure_api.auth import (
    AuthProblem,
    JwtValidator,
    _claim_values,
    bearer_token,
    required_permission,
)  # noqa: E402
from services.azure_api.settings import Settings  # noqa: E402

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()


def settings() -> Settings:
    return Settings.from_env(environment())


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    values: dict[str, Any] = {
        "aud": API_CLIENT_ID,
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "tid": TENANT_ID,
        "sub": "pairwise-user-subject",
        "oid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "azp": SPA_CLIENT_ID,
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 300,
        "scp": "Loan.Read Document.Read",
        "roles": ["Loan.Read", "Document.Read"],
    }
    values.update(overrides)
    return values


def token(payload: dict[str, Any], *, algorithm: str = "RS256", kid: str | None = "test-key") -> str:
    headers = {"kid": kid} if kid else {}
    key: Any = PRIVATE_KEY if algorithm == "RS256" else "test-only-hmac-secret-that-is-at-least-32-bytes"
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)


def validator() -> JwtValidator:
    return JwtValidator(settings(), signing_key_resolver=lambda _token: PUBLIC_KEY)


def problem_code(call: Any) -> str:
    with pytest.raises(AuthProblem) as raised:
        call()
    return raised.value.code


def test_valid_delegated_token_returns_only_safe_principal_claims() -> None:
    principal = validator().validate(token(claims()), "Loan.Read")

    assert principal.actor_type == "user"
    assert principal.scopes == frozenset({"Loan.Read", "Document.Read"})
    assert principal.safe_claims() == {
        "tid": TENANT_ID,
        "oid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "azp": SPA_CLIENT_ID,
        "roles": ["Document.Read", "Loan.Read"],
        "scp": "Document.Read Loan.Read",
    }


def test_valid_application_token_uses_appid_and_application_role() -> None:
    app_client = "66666666-6666-4666-8666-666666666666"
    values = environment()
    values["ALLOWED_CLIENT_IDS"] += f",{app_client}"
    service_settings = Settings.from_env(values)
    service_validator = JwtValidator(service_settings, signing_key_resolver=lambda _token: PUBLIC_KEY)
    payload = claims(
        azp="", appid=app_client, scp=None, idtyp="app", azpacr="2", roles=["Loan.Read"]
    )

    principal = service_validator.validate(token(payload), "Loan.Read")

    assert principal.actor_type == "servicePrincipal"
    assert principal.safe_claims()["idtyp"] == "app"
    assert "scp" not in principal.safe_claims()


@pytest.mark.parametrize(
    ("payload_change", "permission", "expected"),
    [
        ({"tid": "77777777-7777-4777-8777-777777777777"}, "Loan.Read", "TENANT_NOT_ALLOWED"),
        ({"azp": "77777777-7777-4777-8777-777777777777"}, "Loan.Read", "CLIENT_NOT_ALLOWED"),
        ({"azp": "55555555-5555-4555-8555-555555555555"}, "Loan.Read", "CLIENT_NOT_ALLOWED"),
        ({"scp": "Document.Read"}, "Loan.Read", "SCOPE_REQUIRED"),
        ({"roles": ["Document.Read"]}, "Loan.Read", "ROLE_REQUIRED"),
        ({"idtyp": "app"}, "Loan.Read", "TOKEN_TYPE_NOT_ALLOWED"),
        ({"scp": None, "idtyp": "user"}, "Loan.Read", "TOKEN_TYPE_NOT_ALLOWED"),
        (
            {"scp": None, "idtyp": "app", "azpacr": "2", "roles": ["Document.Read"]},
            "Loan.Read",
            "ROLE_REQUIRED",
        ),
        (
            {"scp": None, "idtyp": "app", "roles": ["Loan.Read"]},
            "Loan.Read",
            "CERTIFICATE_AUTH_REQUIRED",
        ),
    ],
)
def test_claim_authorization_failures_are_specific(
    payload_change: dict[str, Any], permission: str, expected: str
) -> None:
    assert problem_code(lambda: validator().validate(token(claims(**payload_change)), permission)) == expected


@pytest.mark.parametrize(
    "encoded",
    [
        lambda: token(claims(), algorithm="HS256"),
        lambda: token(claims(), kid=None),
        lambda: token(claims(iss="https://issuer.invalid")),
        lambda: token(claims(aud="wrong-audience")),
        lambda: token(claims(exp=int(time.time()) - 100)),
        lambda: jwt.encode(claims(), rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256", headers={"kid": "other"}),
        lambda: "not-a-jwt",
    ],
)
def test_cryptographic_token_failures_are_unauthorized(encoded: Any) -> None:
    assert problem_code(lambda: validator().validate(encoded(), "Loan.Read")) == "TOKEN_INVALID"


def test_roles_can_be_optional_for_delegated_callers() -> None:
    values = environment()
    values["ENVIRONMENT_NAME"] = "dev"
    values["REQUIRE_USER_ROLES"] = "false"
    optional = JwtValidator(Settings.from_env(values), signing_key_resolver=lambda _token: PUBLIC_KEY)

    assert optional.validate(token(claims(roles=[])), "Loan.Read").actor_type == "user"


@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("POST", "/v1/loans", "Loan.Create"),
        ("GET", "/v1/loans/23051", "Loan.Read"),
        ("POST", "/v1/loans/23051/archive", "Loan.Archive"),
        ("POST", "/v1/loans/23051/documents", "Document.Upload"),
        ("POST", "/v1/loans/23051/documents/doc_1/uploads", "Document.Upload"),
        ("POST", "/v1/loans/23051/documents/doc_1/uploads/upl_1/complete", "Document.Upload"),
        ("POST", "/v1/loans/23051/documents/doc_1/archive", "Document.Archive"),
        ("GET", "/v1/loans/23051/documents/doc_1", "Document.Read"),
        ("GET", "/v1/loans/23051/archives/1/documents/doc_1/download", "Document.Read"),
        ("GET", "/v1/loans/23051/documents/doc_1/archives/1/data-points", "DataPoints.Read"),
        ("DELETE", "/v1/loans/23051", None),
        ("GET", "/v1/loans/23051/unknown", None),
    ],
)
def test_routes_map_to_exact_permissions(method: str, path: str, permission: str | None) -> None:
    assert required_permission(method, path) == permission


def test_bearer_and_claim_parsing_reject_malformed_values() -> None:
    assert bearer_token("bEaReR abc.def") == "abc.def"
    assert _claim_values(["A", "", "B"]) == frozenset({"A", "B"})
    assert _claim_values("A, B C") == frozenset({"A", "B", "C"})
    assert _claim_values(42) == frozenset()
    for header in (None, "", "Basic abc", "Bearer", "Bearer one two"):
        assert problem_code(lambda header=header: bearer_token(header)) == "TOKEN_REQUIRED"
