from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl

import pytest
from fastapi.testclient import TestClient
from test_azure_api_settings import SPA_CLIENT_ID, TENANT_ID, environment

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.azure_api.auth import AuthProblem, Principal  # noqa: E402
from services.azure_api.aws_credentials import FederationError  # noqa: E402
from services.azure_api.main import create_app  # noqa: E402
from services.azure_api.settings import Settings  # noqa: E402


def settings(**overrides: str) -> Settings:
    values = environment()
    values.update(overrides)
    return Settings.from_env(values)


PRINCIPAL = Principal(
    tenant_id=TENANT_ID,
    actor_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    client_id=SPA_CLIENT_ID,
    actor_type="user",
    scopes=frozenset({"Loan.Read", "Loan.Create"}),
    roles=frozenset({"Loan.Read", "Loan.Create"}),
)

LOAN_ID = "23051"
DOCUMENT_ID = "doc_11111111-1111-4111-8111-111111111111"
UPLOAD_ID = "upl_22222222-2222-4222-8222-222222222222"
LOAN_ARCHIVE_SEQUENCE = 1
DOCUMENT_ARCHIVE_SEQUENCE = 2
CORRELATION_ID = "99999999-9999-4999-8999-999999999999"
IDEMPOTENCY_KEY = "88888888-8888-4888-8888-888888888888"
CREATE_LOAN_BODY = json.dumps({"loanId": LOAN_ID})
CREATE_UPLOAD_BODY = json.dumps(
    {
        "fileName": "closing-disclosure.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 1,
        "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
)

CANONICAL_ROUTE_CASES = (
    pytest.param("POST", "/v1/loans", "Loan.Create", CREATE_LOAN_BODY, id="create-loan"),
    pytest.param(
        "POST",
        f"/v1/loans/{LOAN_ID}/archive",
        "Loan.Archive",
        None,
        id="archive-loan",
    ),
    pytest.param(
        "POST",
        f"/v1/loans/{LOAN_ID}/documents",
        "Document.Upload",
        CREATE_UPLOAD_BODY,
        id="create-document-upload",
    ),
    pytest.param(
        "POST",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads",
        "Document.Upload",
        CREATE_UPLOAD_BODY,
        id="create-replacement-upload",
    ),
    pytest.param(
        "POST",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
        "Document.Upload",
        "{}",
        id="complete-upload",
    ),
    pytest.param(
        "POST",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/archive",
        "Document.Archive",
        None,
        id="archive-document",
    ),
    pytest.param("GET", f"/v1/loans/{LOAN_ID}", "Loan.Read", None, id="current-loan"),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}",
        "Loan.Read",
        None,
        id="loan-archive",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}",
        "Document.Read",
        None,
        id="current-document",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}"
        f"/archives/{DOCUMENT_ARCHIVE_SEQUENCE}",
        "Document.Read",
        None,
        id="document-archive",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}/documents/{DOCUMENT_ID}",
        "Document.Read",
        None,
        id="loan-archive-document",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}/documents/{DOCUMENT_ID}"
        f"/archives/{DOCUMENT_ARCHIVE_SEQUENCE}",
        "Document.Read",
        None,
        id="loan-archive-document-archive",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/data-points",
        "DataPoints.Read",
        None,
        id="current-data-points",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}"
        f"/archives/{DOCUMENT_ARCHIVE_SEQUENCE}/data-points",
        "DataPoints.Read",
        None,
        id="document-archive-data-points",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/data-points",
        "DataPoints.Read",
        None,
        id="loan-archive-data-points",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/archives/{DOCUMENT_ARCHIVE_SEQUENCE}/data-points",
        "DataPoints.Read",
        None,
        id="loan-archive-document-archive-data-points",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/data-points/download",
        "DataPoints.Read",
        None,
        id="current-data-points-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}"
        f"/archives/{DOCUMENT_ARCHIVE_SEQUENCE}/data-points/download",
        "DataPoints.Read",
        None,
        id="document-archive-data-points-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/data-points/download",
        "DataPoints.Read",
        None,
        id="loan-archive-data-points-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/archives/{DOCUMENT_ARCHIVE_SEQUENCE}/data-points/download",
        "DataPoints.Read",
        None,
        id="loan-archive-document-archive-data-points-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/download?artifact=source",
        "Document.Read",
        None,
        id="current-document-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}"
        f"/archives/{DOCUMENT_ARCHIVE_SEQUENCE}/download?artifact=selected",
        "Document.Read",
        None,
        id="document-archive-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/download?artifact=source",
        "Document.Read",
        None,
        id="loan-archive-document-download",
    ),
    pytest.param(
        "GET",
        f"/v1/loans/{LOAN_ID}/archives/{LOAN_ARCHIVE_SEQUENCE}"
        f"/documents/{DOCUMENT_ID}/archives/{DOCUMENT_ARCHIVE_SEQUENCE}"
        "/download?artifact=selected",
        "Document.Read",
        None,
        id="loan-archive-document-archive-download",
    ),
)


class Validator:
    def __init__(self, result: Principal | AuthProblem = PRINCIPAL) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def validate(self, token: str, permission: str) -> Principal:
        self.calls.append((token, permission))
        if isinstance(self.result, AuthProblem):
            raise self.result
        return self.result


class Federation:
    def __init__(self, result: object | FederationError = None) -> None:
        self.result = object() if result is None else result
        self.minimums: list[int] = []
        self.closed = False

    def session_context(self, minimum: int) -> object:
        self.minimums.append(minimum)
        if isinstance(self.result, FederationError):
            raise self.result
        return SimpleNamespace(
            session=self.result,
            expiration=datetime(2026, 7, 16, 1, tzinfo=timezone.utc),
        )

    def close(self) -> None:
        self.closed = True


class Domain:
    def __init__(self, result: Any = None) -> None:
        self.result = result or {
            "statusCode": 200,
            "headers": {"content-type": "application/json", "x-unsafe": "drop"},
            "body": json.dumps({"loanId": "23051"}),
        }
        self.sessions: list[object] = []
        self.expirations: list[datetime] = []
        self.events: list[dict[str, Any]] = []

    def validate_runtime_configuration(self) -> None:
        return None

    def configure_aws_session(self, session: object, *, credential_expiration: datetime) -> None:
        self.sessions.append(session)
        self.expirations.append(credential_expiration)

    def dispatch_request(self, event: dict[str, Any]) -> Any:
        self.events.append(event)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def client(
    *,
    validator: Validator | None = None,
    federation: Federation | None = None,
    domain: Any | None = None,
    configured_settings: Settings | None = None,
) -> tuple[TestClient, Validator, Federation, Any]:
    validator = validator or Validator()
    federation = federation or Federation()
    domain = domain or Domain()
    application = create_app(
        configured_settings or settings(),
        validator=validator,
        federation=federation,
        domain_loader=lambda: domain,
    )
    return TestClient(application, base_url="https://api.loans.example.com"), validator, federation, domain


@pytest.mark.parametrize(("method", "target", "permission", "body"), CANONICAL_ROUTE_CASES)
def test_canonical_routes_preserve_permissions_and_sanitized_envelopes(
    method: str,
    target: str,
    permission: str,
    body: str | None,
) -> None:
    test_client, validator, federation, domain = client()
    headers = {
        "authorization": "Bearer signed-route-token",
        "x-correlation-id": CORRELATION_ID,
    }
    if method == "POST":
        headers["idempotency-key"] = IDEMPOTENCY_KEY
    if body is not None:
        headers["content-type"] = "application/json"

    request_arguments: dict[str, Any] = {"headers": headers}
    if body is not None:
        request_arguments["content"] = body
    with test_client:
        response = test_client.request(method, target, **request_arguments)

    raw_path, separator, raw_query = target.partition("?")
    expected_query = dict(parse_qsl(raw_query, keep_blank_values=True)) or None
    expected_headers = {"x-correlation-id": CORRELATION_ID}
    if method == "POST":
        expected_headers["idempotency-key"] = IDEMPOTENCY_KEY
    if body is not None:
        expected_headers["content-type"] = "application/json"

    assert response.status_code == 200
    assert response.headers["x-correlation-id"] == CORRELATION_ID
    assert validator.calls == [("signed-route-token", permission)]
    assert federation.minimums == [900]
    assert len(domain.events) == 1
    event = domain.events[0]
    assert event == {
        "version": "2.0",
        "rawPath": raw_path,
        "rawQueryString": raw_query if separator else "",
        "headers": expected_headers,
        "queryStringParameters": expected_query,
        "body": body,
        "isBase64Encoded": False,
        "requestContext": {
            "http": {"method": method, "path": raw_path},
            "authorizer": {"jwt": {"claims": PRINCIPAL.safe_claims()}},
        },
    }


def test_forwarded_identity_headers_never_override_validated_principal() -> None:
    test_client, validator, _, domain = client()
    spoofed_identity_headers = {
        "x-ms-client-principal-id": "spoofed-object-id",
        "x-ms-client-principal-name": "attacker@example.invalid",
        "x-ms-client-principal": "spoofed-base64-principal",
        "x-ms-token-aad-access-token": "forwarded-access-token",
        "x-forwarded-user": "spoofed-forwarded-user",
        "x-forwarded-email": "spoofed-forwarded-email@example.invalid",
        "x-amzn-oidc-identity": "spoofed-amazon-identity",
        "x-amzn-oidc-data": "spoofed-amazon-token",
    }
    headers = {
        "authorization": "Bearer cryptographically-validated-token",
        "x-correlation-id": CORRELATION_ID,
        **spoofed_identity_headers,
    }

    with test_client:
        response = test_client.get(f"/v1/loans/{LOAN_ID}", headers=headers)

    assert response.status_code == 200
    assert validator.calls == [("cryptographically-validated-token", "Loan.Read")]
    event = domain.events[0]
    assert event["headers"] == {"x-correlation-id": CORRELATION_ID}
    assert event["requestContext"]["authorizer"]["jwt"]["claims"] == PRINCIPAL.safe_claims()
    serialized_event = json.dumps(event)
    assert "cryptographically-validated-token" not in serialized_event
    for spoofed_value in spoofed_identity_headers.values():
        assert spoofed_value not in serialized_event


def test_tokens_signed_grants_and_content_are_redacted_from_logs_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bearer_token = "eyJ.sensitive-bearer-token.signature"
    sensitive_content = "borrower-sensitive-content-1234"
    signed_url = (
        "https://quarantine.example.invalid/object.pdf?"
        "X-Amz-Signature=sensitive-signature&X-Amz-Security-Token=sensitive-session-token"
    )
    upload_response = {
        "upload": {
            "method": "POST",
            "url": signed_url,
            "fields": {
                "x-amz-signature": "sensitive-signature",
                "x-amz-security-token": "sensitive-session-token",
            },
        }
    }
    success_domain = Domain(
        {
            "statusCode": 201,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(upload_response),
        }
    )
    failure_domain = Domain(RuntimeError(f"{bearer_token} {signed_url} {sensitive_content}"))
    successful, _, _, _ = client(domain=success_domain)
    failed, _, _, _ = client(domain=failure_domain)
    request_body = json.dumps(
        {
            "fileName": f"{sensitive_content}.pdf",
            "contentType": "application/pdf",
            "sizeBytes": 1,
            "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        }
    )
    headers = {
        "authorization": f"Bearer {bearer_token}",
        "content-type": "application/json",
        "idempotency-key": IDEMPOTENCY_KEY,
        "x-correlation-id": CORRELATION_ID,
    }
    caplog.set_level(logging.DEBUG, logger="services.azure_api.main")

    with successful, failed:
        success_response = successful.post(
            f"/v1/loans/{LOAN_ID}/documents",
            content=request_body,
            headers=headers,
        )
        failure_response = failed.post(
            f"/v1/loans/{LOAN_ID}/documents",
            content=request_body,
            headers=headers,
        )

    assert success_response.status_code == 201
    assert success_response.json() == upload_response
    assert failure_response.status_code == 502
    assert failure_response.json()["code"] == "DOMAIN_DEPENDENCY_ERROR"
    assert sensitive_content in failure_domain.events[0]["body"]
    assert "authorization" not in failure_domain.events[0]["headers"]
    assert "domain_dispatch_failed" in caplog.text
    sensitive_markers = (
        bearer_token,
        sensitive_content,
        signed_url,
        "sensitive-signature",
        "sensitive-session-token",
    )
    for marker in sensitive_markers:
        assert marker not in caplog.text
        assert marker not in failure_response.text


def test_health_and_successful_domain_dispatch_preserve_contract() -> None:
    test_client, validator, federation, domain = client()
    correlation_id = "99999999-9999-4999-8999-999999999999"

    with test_client:
        assert test_client.get("/health").json() == {"status": "ok"}
        assert test_client.get("/ready").json() == {"status": "ready"}
        response = test_client.get(
            "/v1/loans/23051?include=current",
            headers={"authorization": "Bearer signed-token", "x-correlation-id": correlation_id},
        )

    assert response.status_code == 200
    assert response.json() == {"loanId": "23051"}
    assert response.headers["x-correlation-id"] == correlation_id
    assert "x-unsafe" not in response.headers
    assert validator.calls == [("signed-token", "Loan.Read")]
    assert federation.minimums == [0, 900]
    assert federation.closed is True
    assert domain.sessions == [federation.result]
    assert domain.expirations == [datetime(2026, 7, 16, 1, tzinfo=timezone.utc)]
    event = domain.events[0]
    assert event["requestContext"]["authorizer"]["jwt"]["claims"]["tid"] == TENANT_ID
    assert "authorization" not in event["headers"]
    assert event["queryStringParameters"] == {"include": "current"}


def test_production_product_routes_require_custom_host_but_default_host_keeps_health_probe() -> None:
    test_client, validator, federation, domain = client()

    with test_client:
        health = test_client.get("/health", headers={"host": "revision.azurecontainerapps.io"})
        rejected = test_client.get(
            "/v1/loans/23051",
            headers={
                "host": "revision.azurecontainerapps.io",
                "authorization": "Bearer signed-token",
            },
        )

    assert health.status_code == 200
    assert rejected.status_code == 421
    assert rejected.json()["code"] == "HOST_NOT_ALLOWED"
    assert validator.calls == []
    assert federation.minimums == []
    assert domain.events == []


def test_readiness_fails_closed_when_live_federation_cannot_be_proven() -> None:
    federation = Federation(FederationError("AWS_STS_UNAVAILABLE", "must not leak"))
    test_client, _, _, _ = client(federation=federation)

    with test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert federation.minimums == [0]
    assert "must not leak" not in response.text


def test_cors_allows_only_configured_origin_and_known_route() -> None:
    test_client, _, federation, _ = client()
    with test_client:
        allowed = test_client.options(
            "/v1/loans/23051",
            headers={
                "origin": "https://loans.example.com",
                "access-control-request-method": "GET",
            },
        )
        denied = test_client.get(
            "/health",
            headers={"origin": "https://attacker.example"},
        )
        unknown = test_client.options(
            "/v1/unknown",
            headers={
                "origin": "https://loans.example.com",
                "access-control-request-method": "GET",
            },
        )

    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == "https://loans.example.com"
    assert "Origin" in allowed.headers["vary"]
    assert denied.json()["code"] == "ORIGIN_NOT_ALLOWED"
    assert unknown.json()["code"] == "PREFLIGHT_NOT_ALLOWED"
    assert federation.minimums == []


def test_auth_and_route_failures_happen_before_aws() -> None:
    unauthorized, _, first_federation, _ = client()
    forbidden_validator = Validator(AuthProblem(403, "ROLE_REQUIRED", "Required app role"))
    forbidden, _, second_federation, _ = client(validator=forbidden_validator)
    unknown, _, third_federation, _ = client()

    with unauthorized, forbidden, unknown:
        missing = unauthorized.get("/v1/loans/23051")
        denied = forbidden.get("/v1/loans/23051", headers={"authorization": "Bearer token"})
        not_found = unknown.get("/v1/loans/23051/not-a-route", headers={"authorization": "Bearer token"})

    assert missing.json()["code"] == "TOKEN_REQUIRED"
    assert denied.json()["code"] == "ROLE_REQUIRED"
    assert not_found.json()["code"] == "ROUTE_NOT_FOUND"
    assert first_federation.minimums == second_federation.minimums == third_federation.minimums == []


def test_request_body_limits_and_utf8_validation_happen_before_aws() -> None:
    configured = settings(MAX_REQUEST_BODY_BYTES="1024")
    test_client, _, federation, _ = client(configured_settings=configured)

    with test_client:
        declared = test_client.post(
            "/v1/loans",
            content=b"{}",
            headers={"authorization": "Bearer token", "content-length": "2048"},
        )
        actual = test_client.post(
            "/v1/loans",
            content=b"x" * 1025,
            headers={"authorization": "Bearer token"},
        )
        chunked = test_client.post(
            "/v1/loans",
            content=(chunk for chunk in (b"x" * 600, b"y" * 600)),
            headers={"authorization": "Bearer token"},
        )
        malformed = test_client.post(
            "/v1/loans",
            content=b"{}",
            headers={"authorization": "Bearer token", "content-length": "invalid"},
        )
        mismatch = test_client.post(
            "/v1/loans",
            content=b"{}",
            headers={"authorization": "Bearer token", "content-length": "1"},
        )
        invalid = test_client.post(
            "/v1/loans",
            content=b"\xff",
            headers={"authorization": "Bearer token"},
        )

    assert declared.json()["code"] == "REQUEST_TOO_LARGE"
    assert declared.json()["detail"] == "Request body is too large"
    assert actual.json()["code"] == "REQUEST_TOO_LARGE"
    assert chunked.json()["code"] == "REQUEST_TOO_LARGE"
    assert chunked.json()["detail"] == "Request body is too large"
    assert malformed.json()["code"] == "INVALID_CONTENT_LENGTH"
    assert mismatch.json()["code"] == "CONTENT_LENGTH_MISMATCH"
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "INVALID_BODY"
    assert invalid.json()["detail"] == "Request body must be UTF-8"
    assert federation.minimums == []


def test_dependency_failures_are_sanitized(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    federation_failure = Federation(FederationError("AWS_STS_UNAVAILABLE", "must not leak"))
    first, _, _, _ = client(federation=federation_failure)
    second, _, _, _ = client(domain=SimpleNamespace())
    third, _, _, _ = client(domain=Domain(RuntimeError("must not leak")))

    with first, second, third:
        aws = first.get("/v1/loans/23051", headers={"authorization": "Bearer token"})
        unavailable = second.get("/v1/loans/23051", headers={"authorization": "Bearer token"})
        failed = third.get("/v1/loans/23051", headers={"authorization": "Bearer token"})

    assert aws.status_code == 503 and aws.json()["code"] == "AWS_STS_UNAVAILABLE"
    assert "must not leak" not in aws.text
    assert unavailable.status_code == 503 and unavailable.json()["code"] == "DOMAIN_ADAPTER_NOT_READY"
    assert failed.status_code == 502 and failed.json()["code"] == "DOMAIN_DEPENDENCY_ERROR"
    assert "must not leak" not in failed.text
    assert "must not leak" not in caplog.text


def test_async_and_encoded_domain_responses_are_supported() -> None:
    class AsyncDomain(Domain):
        async def dispatch_request(self, event: dict[str, Any]) -> dict[str, Any]:
            self.events.append(event)
            return {"accepted": True}

    encoded = base64.b64encode(b"pdf-bytes").decode()
    first, _, _, _ = client(domain=AsyncDomain())
    second, _, _, _ = client(
        domain=Domain(
            {
                "statusCode": 200,
                "headers": {"content-type": "application/pdf"},
                "body": encoded,
                "isBase64Encoded": True,
            }
        )
    )

    with first, second:
        async_response = first.get("/v1/loans/23051", headers={"authorization": "Bearer token"})
        binary_response = second.get("/v1/loans/23051", headers={"authorization": "Bearer token"})

    assert async_response.json() == {"accepted": True}
    assert binary_response.content == b"pdf-bytes"
    assert binary_response.headers["content-type"] == "application/pdf"


@pytest.mark.parametrize("malformed_body", ["not-base64!", "\N{SNOWMAN}", None, {"not": "base64"}])
def test_malformed_base64_domain_response_is_a_sanitized_adapter_error(
    malformed_body: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    test_client, _, _, _ = client(
        domain=Domain(
            {
                "statusCode": 200,
                "headers": {"content-type": "application/pdf"},
                "body": malformed_body,
                "isBase64Encoded": True,
            }
        )
    )
    caplog.set_level(logging.ERROR, logger="services.azure_api.main")

    with test_client:
        response = test_client.get("/v1/loans/23051", headers={"authorization": "Bearer token"})

    assert response.status_code == 502
    assert response.json()["code"] == "DOMAIN_RESPONSE_INVALID"
    assert response.json()["detail"] == "The domain service returned an invalid response"
    assert str(malformed_body) not in response.text
    assert str(malformed_body) not in caplog.text
    assert "domain_response_invalid" in caplog.text


def test_domain_session_configuration_and_dispatch_are_serialized() -> None:
    class ConcurrentDomain(Domain):
        def __init__(self) -> None:
            super().__init__()
            self.guard = threading.Lock()
            self.active = 0
            self.maximum_active = 0

        def dispatch_request(self, event: dict[str, Any]) -> Any:
            with self.guard:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            time.sleep(0.05)
            with self.guard:
                self.active -= 1
            return self.result

    domain = ConcurrentDomain()
    test_client, _, _, _ = client(domain=domain)
    headers = {"authorization": "Bearer token"}

    with test_client, ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(lambda _: test_client.get("/v1/loans/23051", headers=headers), range(2))
        )

    assert [response.status_code for response in responses] == [200, 200]
    assert domain.maximum_active == 1
