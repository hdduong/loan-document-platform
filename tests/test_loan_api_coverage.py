"""Synthetic lifecycle coverage for the loan API's AWS-facing behavior."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from test_loan_api import api

AUTH = {
    "tenantId": "tenant-id",
    "actorId": "actor-id",
    "clientId": "client-id",
    "actorType": "user",
}
LOAN_ID = "23051"
INSTANCE_ID = "lin_11111111-1111-4111-8111-111111111111"
DOCUMENT_ID = "doc_22222222-2222-4222-8222-222222222222"
UPLOAD_ID = "upl_33333333-3333-4333-8333-333333333333"
IDEMPOTENCY_IDENTITY = {"PK": "IDEMPOTENCY", "SK": "KEY#one"}
CHECKSUM = base64.b64encode(b"x" * 32).decode("ascii")
NOW = "2026-07-16T00:00:00Z"


def request_event(body: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "body": json.dumps(body) if body is not None else None,
        "headers": {"Idempotency-Key": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"},
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/v1/test",
    }


def response_body(response: dict[str, object]) -> dict[str, object]:
    return json.loads(str(response["body"]))


def problem_code(callable_: object) -> str:
    with pytest.raises(api.ApiProblem) as caught:
        callable_()  # type: ignore[operator]
    return caught.value.code


class BytesBody:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.value if amount < 0 else self.value[:amount]

    def close(self) -> None:
        self.closed = True


def test_azure_session_seam_rebinds_clients_and_caps_grants() -> None:
    expiry = datetime.now(timezone.utc) + timedelta(seconds=95)
    bindings: list[tuple[str, str, object]] = []
    previous = (
        api.DDB,
        api.TABLE,
        api.DDB_CLIENT,
        api.S3,
        api.LAMBDA,
        api.CREDENTIAL_EXPIRY_PROVIDER,
    )

    class Session:
        def resource(self, name: str, *, config: object) -> object:
            assert name == "dynamodb"
            bindings.append(("resource", name, config))

            class Resource:
                def Table(self, table_name: str) -> tuple[str, str]:  # noqa: N802
                    return ("table", table_name)

            return Resource()

        def client(self, name: str, *, config: object) -> tuple[str, str]:
            bindings.append(("client", name, config))
            return ("client", name)

    try:
        api.configure_aws_session(Session(), credential_expiration=expiry)

        assert api.TABLE == ("table", api.TABLE_NAME)
        assert api.DDB_CLIENT == ("client", "dynamodb")
        assert api.S3 == ("client", "s3")
        assert api.LAMBDA == ("client", "lambda")
        assert [name for _kind, name, _config in bindings] == [
            "dynamodb",
            "dynamodb",
            "s3",
            "lambda",
        ]
        assert all(config.connect_timeout == 3 for _kind, _name, config in bindings)
        assert all(config.read_timeout == 10 for _kind, _name, config in bindings)
        assert all(config.tcp_keepalive is True for _kind, _name, config in bindings)
        assert all(
            config.retries == {"mode": "standard", "total_max_attempts": 3}
            for _kind, _name, config in bindings
        )
        assert 1 <= api.effective_grant_seconds(600) <= 65
        with pytest.raises(ValueError, match="either"):
            api.configure_aws_session(
                Session(),
                credential_expiry_provider=lambda: expiry,
                credential_expiration=expiry,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            api.configure_aws_session(Session(), credential_expiration=datetime.now())
    finally:
        (
            api.DDB,
            api.TABLE,
            api.DDB_CLIENT,
            api.S3,
            api.LAMBDA,
            api.CREDENTIAL_EXPIRY_PROVIDER,
        ) = previous


def test_runtime_configuration_validates_query_and_archive_limits() -> None:
    api.validate_runtime_configuration()

    with patch.object(api, "MAXIMUM_QUERY_ITEMS", 99):
        with pytest.raises(RuntimeError, match="QUERY_LIMIT_CONFIGURATION_INVALID"):
            api.validate_runtime_configuration()
    with patch.object(api, "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS", 0):
        with pytest.raises(RuntimeError, match="LOAN_ARCHIVE_DOCUMENT_LIMIT_CONFIGURATION_INVALID"):
            api.validate_runtime_configuration()
    with (
        patch.object(api, "MAXIMUM_QUERY_ITEMS", 100),
        patch.object(api, "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS", 101),
    ):
        with pytest.raises(RuntimeError, match="LOAN_ARCHIVE_DOCUMENT_LIMIT_CONFIGURATION_INVALID"):
            api.validate_runtime_configuration()
    with patch.object(api, "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES", 1023):
        with pytest.raises(RuntimeError, match="LOAN_ARCHIVE_MANIFEST_LIMIT_CONFIGURATION_INVALID"):
            api.validate_runtime_configuration()


def test_request_parsing_validation_and_problem_serialization() -> None:
    encoded = base64.b64encode(json.dumps({"loanId": LOAN_ID}).encode()).decode()
    assert api.parse_body({"body": encoded, "isBase64Encoded": True}) == {"loanId": LOAN_ID}
    assert api.parse_body({}, required=False) == {}
    assert problem_code(lambda: api.parse_body({})) == "BODY_REQUIRED"
    assert problem_code(lambda: api.parse_body({"body": "[1]"})) == "INVALID_JSON"
    assert problem_code(lambda: api.parse_body({"body": "{"})) == "INVALID_JSON"

    assert api.decimal_default(Decimal("2")) == 2
    assert api.decimal_default(Decimal("2.5")) == 2.5
    with pytest.raises(TypeError):
        api.decimal_default(object())

    assert api.validate_loan_id(LOAN_ID) == LOAN_ID
    assert problem_code(lambda: api.validate_loan_id("bad/id")) == "INVALID_LOAN_ID"
    assert api.parse_archive_sequence("2") == 2
    assert problem_code(lambda: api.parse_archive_sequence("0")) == "INVALID_ARCHIVE_SEQUENCE"
    assert problem_code(lambda: api.require_path_id("bad", api.DOCUMENT_ID_RE, "documentId")) == (
        "INVALID_DOCUMENTID"
    )

    problem = api.problem_response(api.ApiProblem(409, "A_CODE", "Conflict", "detail"), "cid")
    assert problem["headers"]["content-type"] == "application/problem+json"
    assert response_body(problem)["code"] == "A_CODE"


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({}, "TOKEN_REQUIRED"),
        (
            {
                "tid": "wrong-tenant",
                "azp": "client-id",
                "oid": "actor",
                "scp": "Loan.Read",
                "roles": ["Loan.Read"],
            },
            "TENANT_NOT_ALLOWED",
        ),
        (
            {
                "tid": "tenant-id",
                "azp": "not-allowlisted",
                "oid": "actor",
                "scp": "Loan.Read",
                "roles": ["Loan.Read"],
            },
            "CLIENT_NOT_ALLOWED",
        ),
        (
            {
                "tid": "tenant-id",
                "azp": "client-id",
                "scp": "Loan.Read",
                "roles": ["Loan.Read"],
            },
            "ACTOR_REQUIRED",
        ),
        (
            {
                "tid": "tenant-id",
                "azp": "client-id",
                "oid": "actor",
                "scp": "Document.Read",
                "roles": ["Loan.Read"],
            },
            "SCOPE_REQUIRED",
        ),
    ],
)
def test_authorization_rejects_untrusted_claim_combinations(claims: dict[str, object], code: str) -> None:
    event = {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}
    assert problem_code(lambda: api.authorize(event, "Loan.Read")) == code


def test_idempotency_lookup_replays_only_an_identical_request() -> None:
    class Table:
        def __init__(self, request_hash: str) -> None:
            self.request_hash = request_hash

        def get_item(self, **_: object) -> dict[str, object]:
            return {
                "Item": {
                    "requestHash": self.request_hash,
                    "responseStatus": 201,
                    "responseBody": '{"loanId":"23051"}',
                }
            }

    with patch.object(api, "TABLE", Table("same")):
        assert api.get_idempotent(IDEMPOTENCY_IDENTITY, "same") == {
            "status": 201,
            "body": {"loanId": LOAN_ID},
        }
    with patch.object(api, "TABLE", Table("different")):
        assert problem_code(lambda: api.get_idempotent(IDEMPOTENCY_IDENTITY, "same")) == (
            "IDEMPOTENCY_KEY_REUSED"
        )


def test_dynamodb_transaction_conflicts_are_mapped_but_service_errors_propagate() -> None:
    class Ddb:
        def __init__(self, code: str) -> None:
            self.code = code

        def transact_write_items(self, **_: object) -> None:
            raise api.ClientError({"Error": {"Code": self.code}})

    with patch.object(api, "DDB_CLIENT", Ddb("TransactionCanceledException")):
        assert problem_code(lambda: api.transact([])) == "CONCURRENT_STATE_CHANGE"
    with patch.object(api, "DDB_CLIENT", Ddb("InternalServerError")):
        with pytest.raises(api.ClientError):
            api.transact([])


def test_active_loan_lookup_distinguishes_missing_archived_and_active_heads() -> None:
    class Table:
        def __init__(self, item: dict[str, object] | None) -> None:
            self.item = item

        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": self.item} if self.item else {}

    with patch.object(api, "TABLE", Table(None)):
        assert problem_code(lambda: api.get_head("pk")) == "LOAN_NOT_FOUND"
    with patch.object(api, "TABLE", Table({"status": "ARCHIVED"})):
        assert problem_code(lambda: api.require_active_instance("pk")) == "LOAN_NOT_ACTIVE"
    active = {"status": "ACTIVE", "currentInstanceId": INSTANCE_ID}
    with patch.object(api, "TABLE", Table(active)):
        assert api.require_active_instance("pk") == (active, INSTANCE_ID)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "INVALID_REQUEST"),
        (
            {
                "fileName": "bad\n.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 1,
                "checksumSha256": CHECKSUM,
            },
            "INVALID_FILE_NAME",
        ),
        (
            {
                "fileName": "file.txt",
                "contentType": "text/plain",
                "sizeBytes": 1,
                "checksumSha256": CHECKSUM,
            },
            "PDF_REQUIRED",
        ),
        (
            {
                "fileName": "file.pdf",
                "contentType": "application/pdf",
                "sizeBytes": "many",
                "checksumSha256": CHECKSUM,
            },
            "INVALID_SIZE",
        ),
        (
            {
                "fileName": "file.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 0,
                "checksumSha256": CHECKSUM,
            },
            "UPLOAD_TOO_LARGE",
        ),
        (
            {
                "fileName": "file.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 1,
                "checksumSha256": "not-base64",
            },
            "INVALID_CHECKSUM",
        ),
        (
            {
                "fileName": "file.pdf",
                "contentType": "application/pdf",
                "sizeBytes": 1,
                "checksumSha256": base64.b64encode(b"short").decode(),
            },
            "INVALID_CHECKSUM",
        ),
    ],
)
def test_upload_request_validation_rejects_unsafe_inputs(body: dict[str, object], code: str) -> None:
    assert problem_code(lambda: api.validate_upload_request(body)) == code


def test_create_loan_persists_head_instance_and_idempotency_record() -> None:
    writes: list[dict[str, object]] = []
    event = request_event({"loanId": LOAN_ID})
    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "new_id", return_value=INSTANCE_ID),
        patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
    ):
        response = api.create_loan(event, AUTH, "cid")

    body = response_body(response)
    assert response["statusCode"] == 201
    assert response["headers"]["location"] == f"/v1/loans/{LOAN_ID}"
    assert body["loanInstanceId"] == INSTANCE_ID
    assert len(writes) == 3
    assert "currentInstanceId" in writes[0]["Update"]["UpdateExpression"]


def test_create_loan_maps_a_lost_create_race_to_already_active() -> None:
    class Table:
        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": {"currentInstanceId": INSTANCE_ID}}

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "new_id", return_value=INSTANCE_ID),
        patch.object(api, "TABLE", Table()),
        patch.object(api, "transact", side_effect=api.ApiProblem(409, "RACE", "race")),
        patch.object(api, "get_idempotent", return_value=None),
    ):
        assert problem_code(lambda: api.create_loan(request_event({"loanId": LOAN_ID}), AUTH, "cid")) == (
            "LOAN_ALREADY_ACTIVE"
        )


def test_get_loan_returns_current_documents_and_newest_archives_first() -> None:
    items = [
        {
            "PK": "pk",
            "SK": "HEAD",
            "currentInstanceId": INSTANCE_ID,
            "status": "ACTIVE",
            "createdAt": NOW,
        },
        {
            "entityType": "LOAN_INSTANCE",
            "loanInstanceId": INSTANCE_ID,
            "status": "ACTIVE",
            "createdAt": NOW,
        },
        {
            "entityType": "DOCUMENT",
            "loanInstanceId": INSTANCE_ID,
            "documentId": DOCUMENT_ID,
            "status": "SUCCEEDED",
            "currentUploadId": UPLOAD_ID,
            "createdAt": NOW,
            "updatedAt": NOW,
        },
        {
            "entityType": "LOAN_ARCHIVE",
            "loanId": LOAN_ID,
            "loanInstanceId": "lin-old-1",
            "archiveSequence": 1,
            "archivedAt": NOW,
            "documentCount": 1,
        },
        {
            "entityType": "LOAN_ARCHIVE",
            "loanId": LOAN_ID,
            "loanInstanceId": "lin-old-2",
            "archiveSequence": 2,
            "archivedAt": NOW,
            "documentCount": 2,
        },
    ]
    with patch.object(api, "query_all", return_value=items):
        response = api.get_loan(AUTH["tenantId"], LOAN_ID, "cid")

    body = response_body(response)
    assert body["current"]["documents"][0]["documentId"] == DOCUMENT_ID
    assert [item["archiveSequence"] for item in body["archives"]] == [2, 1]
    assert body["archives"][0]["links"]["self"].endswith("/archives/2")


def test_presigned_upload_constrains_checksum_length_encryption_and_metadata() -> None:
    calls: list[dict[str, object]] = []

    class S3:
        def generate_presigned_post(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"url": "https://upload.invalid", "fields": kwargs["Fields"]}

    request = {
        "fileName": "loan.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 123,
        "checksumSha256": CHECKSUM,
    }
    with patch.object(api, "S3", S3()):
        grant, expires_at = api.create_presigned_upload(
            "quarantine/source.pdf", {"document-id": DOCUMENT_ID}, request
        )

    assert grant["method"] == "POST"
    assert expires_at.endswith("Z")
    assert calls[0]["Key"] == "quarantine/source.pdf"
    assert ["content-length-range", 123, 123] in calls[0]["Conditions"]
    assert calls[0]["Fields"]["x-amz-server-side-encryption-aws-kms-key-id"] == api.DATA_KEY_ARN
    assert calls[0]["Fields"]["x-amz-meta-document-id"] == DOCUMENT_ID


def test_create_document_persists_only_stable_idempotency_fields() -> None:
    request = {
        "fileName": "loan.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 123,
        "checksumSha256": CHECKSUM,
    }
    persisted: dict[str, object] = {}
    original_idempotency_item = api.idempotency_item

    def capture_idempotency(
        identity: dict[str, str], request_hash: str, status: int, body: dict[str, object], now: str
    ) -> dict[str, object]:
        persisted.update(body)
        return original_idempotency_item(identity, request_hash, status, body, now)

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(api, "new_id", side_effect=[DOCUMENT_ID, UPLOAD_ID]),
        patch.object(
            api,
            "create_presigned_upload",
            return_value=(
                {
                    "method": "POST",
                    "url": "https://upload.invalid",
                    "fields": {"x-amz-security-token": "must-not-persist"},
                },
                "2026-07-16T00:10:00Z",
            ),
        ),
        patch.object(api, "idempotency_item", side_effect=capture_idempotency),
        patch.object(api, "transact"),
    ):
        response = api.create_document(request_event(request), AUTH, LOAN_ID, "cid")

    assert response_body(response)["upload"]["url"] == "https://upload.invalid"
    assert persisted == {
        "loanId": LOAN_ID,
        "documentId": DOCUMENT_ID,
        "uploadId": UPLOAD_ID,
        "status": "AWAITING_UPLOAD",
    }
    assert "security-token" not in json.dumps(persisted).lower()


def test_idempotent_upload_replay_mints_a_fresh_grant_only_while_awaiting() -> None:
    request = {
        "fileName": "loan.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 123,
        "checksumSha256": CHECKSUM,
    }
    replay = {
        "status": 201,
        "body": {
            "loanId": LOAN_ID,
            "documentId": DOCUMENT_ID,
            "uploadId": UPLOAD_ID,
            "status": "AWAITING_UPLOAD",
            "expiresAt": "2020-01-01T00:00:00Z",
            "upload": {"url": "https://expired.invalid", "fields": {"token": "old"}},
        },
    }

    class Table:
        def __init__(self, status: str = "AWAITING_UPLOAD") -> None:
            self.status = status
            self.updates: list[dict[str, object]] = []

        def get_item(self, **kwargs: object) -> dict[str, object]:
            key = kwargs["Key"]
            if "#DOC#" in str(key["SK"]) and "#UPLOAD#" not in str(key["SK"]):
                return {
                    "Item": {
                        "currentUploadId": UPLOAD_ID,
                        "status": self.status,
                    }
                }
            return {
                "Item": {
                    "status": self.status,
                    "fileName": request["fileName"],
                    "contentType": request["contentType"],
                    "sizeBytes": request["sizeBytes"],
                    "checksumSha256": request["checksumSha256"],
                    "sourceKey": "tenants/t/loan.pdf",
                }
            }

        def update_item(self, **kwargs: object) -> None:
            self.updates.append(kwargs)

    table = Table()
    with (
        patch.object(api, "TABLE", table),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(
            api,
            "create_presigned_upload",
            return_value=(
                {"method": "POST", "url": "https://fresh.invalid", "fields": {"policy": "new"}},
                "2026-07-16T00:20:00Z",
            ),
        ),
    ):
        response = api.renew_idempotent_upload_session(replay, AUTH, LOAN_ID, request, "cid")

    body = response_body(response)
    assert body["upload"]["url"] == "https://fresh.invalid"
    assert body["expiresAt"] == "2026-07-16T00:20:00Z"
    assert table.updates[0]["ExpressionAttributeValues"][":expires"] == body["expiresAt"]

    with (
        patch.object(api, "TABLE", Table("VALIDATING")),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
    ):
        assert (
            problem_code(
                lambda: api.renew_idempotent_upload_session(replay, AUTH, LOAN_ID, request, "cid")
            )
            == "UPLOAD_SESSION_NO_LONGER_ACTIVE"
        )


def test_complete_upload_pins_and_validates_exact_s3_version_before_transition() -> None:
    upload = {
        "sourceBucket": "source",
        "sourceKey": "quarantine/source.pdf",
        "sizeBytes": 10,
        "checksumSha256": CHECKSUM,
        "uploadExpiresAt": "2099-01-01T00:00:00Z",
        "status": "AWAITING_UPLOAD",
        "malwareScanStatus": "NO_THREATS_FOUND",
    }

    class Table:
        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": upload}

    class S3:
        def __init__(self) -> None:
            self.get_calls: list[dict[str, object]] = []
            self.body = BytesBody(b"%PDF-")

        def head_object(self, **_: object) -> dict[str, object]:
            return {
                "VersionId": "version-1",
                "ContentLength": 10,
                "ChecksumSHA256": CHECKSUM,
                "ContentType": "application/pdf",
                "Metadata": {
                    "document-id": DOCUMENT_ID,
                    "upload-id": UPLOAD_ID,
                    "loan-instance-id": INSTANCE_ID,
                },
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": api.DATA_KEY_ARN,
            }

        def get_object(self, **kwargs: object) -> dict[str, object]:
            self.get_calls.append(kwargs)
            return {"Body": self.body}

    class Lambda:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def invoke(self, **kwargs: object) -> None:
            self.calls.append(kwargs)

    s3 = S3()
    lambda_client = Lambda()
    writes: list[dict[str, object]] = []
    document = {"currentUploadId": UPLOAD_ID}

    def commit(items: list[dict[str, object]]) -> None:
        writes.extend(items)
        upload.update(
            {
                "status": "VALIDATING",
                "clientCompletedAt": NOW,
                "sourceVersionId": "version-1",
            }
        )

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, document),
        ),
        patch.object(api, "TABLE", Table()),
        patch.object(api, "S3", s3),
        patch.object(api, "LAMBDA", lambda_client),
        patch.object(api, "UPLOAD_PROCESSOR_ARN", "processor-arn"),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "new_id", return_value="run_44444444-4444-4444-8444-444444444444"),
        patch.object(api, "transact", side_effect=commit),
    ):
        response = api.complete_upload(request_event(), AUTH, LOAN_ID, DOCUMENT_ID, UPLOAD_ID, "cid")

    assert response["statusCode"] == 202
    assert response_body(response)["status"] == "VALIDATING"
    assert s3.get_calls[0]["VersionId"] == "version-1"
    assert s3.get_calls[0]["Range"] == "bytes=0-4"
    assert s3.body.closed is True
    assert len(writes) == 3
    assert len(lambda_client.calls) == 1
    invoked = json.loads(lambda_client.calls[0]["Payload"])
    assert invoked["detail"]["versionId"] == "version-1"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"VersionId": None}, "VERSION_REQUIRED"),
        ({"ContentLength": 11}, "SIZE_MISMATCH"),
        ({"ChecksumSHA256": "wrong"}, "CHECKSUM_MISMATCH"),
        ({"ContentType": "text/plain"}, "CONTENT_TYPE_MISMATCH"),
        ({"Metadata": {}}, "METADATA_MISMATCH"),
        ({"ServerSideEncryption": "AES256"}, "ENCRYPTION_MISMATCH"),
    ],
)
def test_complete_upload_rejects_mismatched_s3_evidence(mutation: dict[str, object], code: str) -> None:
    upload = {
        "sourceBucket": "source",
        "sourceKey": "quarantine/source.pdf",
        "sizeBytes": 10,
        "checksumSha256": CHECKSUM,
        "uploadExpiresAt": "2099-01-01T00:00:00Z",
    }
    head: dict[str, object] = {
        "VersionId": "version-1",
        "ContentLength": 10,
        "ChecksumSHA256": CHECKSUM,
        "ContentType": "application/pdf",
        "Metadata": {
            "document-id": DOCUMENT_ID,
            "upload-id": UPLOAD_ID,
            "loan-instance-id": INSTANCE_ID,
        },
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": api.DATA_KEY_ARN,
    }
    head.update(mutation)

    class Table:
        def get_item(self, **_: object) -> dict[str, object]:
            return {"Item": upload}

    class S3:
        def head_object(self, **_: object) -> dict[str, object]:
            return head

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
        ),
        patch.object(api, "TABLE", Table()),
        patch.object(api, "S3", S3()),
    ):
        assert (
            problem_code(
                lambda: api.complete_upload(request_event(), AUTH, LOAN_ID, DOCUMENT_ID, UPLOAD_ID, "cid")
            )
            == code
        )


def test_complete_upload_distinguishes_not_ready_from_expired() -> None:
    class Table:
        def __init__(self, expires_at: str) -> None:
            self.expires_at = expires_at

        def get_item(self, **_: object) -> dict[str, object]:
            return {
                "Item": {
                    "sourceBucket": "source",
                    "sourceKey": "missing.pdf",
                    "uploadExpiresAt": self.expires_at,
                }
            }

    class S3:
        def head_object(self, **_: object) -> dict[str, object]:
            raise api.ClientError({"Error": {"Code": "NoSuchKey"}})

    for expires_at, expected in (
        ("2099-01-01T00:00:00Z", "UPLOAD_NOT_READY"),
        ("2020-01-01T00:00:00Z", "UPLOAD_EXPIRED"),
    ):
        with (
            patch.object(
                api,
                "replay_or_none",
                return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
            ),
            patch.object(
                api,
                "get_document_item",
                return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
            ),
            patch.object(api, "TABLE", Table(expires_at)),
            patch.object(api, "S3", S3()),
        ):
            assert (
                problem_code(
                    lambda: api.complete_upload(request_event(), AUTH, LOAN_ID, DOCUMENT_ID, UPLOAD_ID, "cid")
                )
                == expected
            )


def test_archive_document_freezes_all_pinned_artifact_references() -> None:
    document = {
        "currentUploadId": UPLOAD_ID,
        "status": "SUCCEEDED",
        "lastDocumentArchiveSequence": 2,
        "fileName": "loan.pdf",
        "sourceBucket": "source",
        "sourceKey": "source.pdf",
        "sourceVersionId": "source-v1",
        "sourceChecksumSha256": "source-checksum",
        "selectedBucket": "source",
        "selectedKey": "selected.pdf",
        "selectedVersionId": "selected-v1",
        "selectedChecksumSha256": "selected-checksum",
        "dataPointsBucket": "source",
        "dataPointsKey": "data.json",
        "dataPointsVersionId": "data-v1",
        "dataPointsChecksumSha256": "data-checksum",
    }
    writes: list[dict[str, object]] = []
    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, document),
        ),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
    ):
        response = api.archive_document(request_event(), AUTH, LOAN_ID, DOCUMENT_ID, "cid")

    body = response_body(response)
    assert body["archiveSequence"] == 3
    assert body["displayDocumentId"].endswith("_003")
    archived_item = writes[1]["Put"]["Item"]
    assert archived_item["sourceVersionId"]["stub"] == "source-v1"
    assert archived_item["dataPointsVersionId"]["stub"] == "data-v1"


@pytest.mark.parametrize(
    ("document", "code"),
    [
        ({"status": "SUCCEEDED"}, "DOCUMENT_ALREADY_ARCHIVED"),
        ({"status": "SCREENING", "currentUploadId": UPLOAD_ID}, "DOCUMENT_PROCESSING"),
    ],
)
def test_archive_document_rejects_non_archivable_state(document: dict[str, object], code: str) -> None:
    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, document),
        ),
    ):
        assert (
            problem_code(lambda: api.archive_document(request_event(), AUTH, LOAN_ID, DOCUMENT_ID, "cid"))
            == code
        )


def test_archive_loan_writes_versioned_manifest_and_atomic_registry_records() -> None:
    documents = [
        {
            "entityType": "DOCUMENT",
            "documentId": DOCUMENT_ID,
            "status": "SUCCEEDED",
            "currentUploadId": UPLOAD_ID,
            "fileName": "loan.pdf",
            "createdAt": NOW,
            "updatedAt": NOW,
            "sourceBucket": "source",
            "sourceKey": "source.pdf",
            "sourceVersionId": "source-v1",
            "sourceChecksumSha256": "checksum",
        }
    ]

    class S3:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def put_object(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return {"VersionId": "manifest-v1"}

    s3 = S3()
    writes: list[dict[str, object]] = []
    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(
            api,
            "require_active_instance",
            return_value=({"lastLoanArchiveSequence": 1}, INSTANCE_ID),
        ),
        patch.object(api, "query_all", return_value=documents),
        patch.object(api, "S3", s3),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "new_id", return_value="evt_44444444-4444-4444-8444-444444444444"),
        patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
    ):
        response = api.archive_loan(request_event(), AUTH, LOAN_ID, "cid")

    body = response_body(response)
    assert body["archiveSequence"] == 2
    assert body["documentCount"] == 1
    manifest = json.loads(s3.calls[0]["Body"])
    assert manifest["documents"][0]["source"]["versionId"] == "source-v1"
    assert s3.calls[0]["ChecksumSHA256"] == base64.b64encode(
        hashlib.sha256(s3.calls[0]["Body"]).digest()
    ).decode("ascii")
    assert len(writes) == 5


def test_archive_loan_enforces_document_and_serialized_manifest_limits_before_s3() -> None:
    document = {
        "entityType": "DOCUMENT",
        "documentId": DOCUMENT_ID,
        "status": "SUCCEEDED",
        "fileName": "loan.pdf",
    }

    class S3:
        def put_object(self, **_: object) -> dict[str, object]:
            raise AssertionError("oversized archive must not reach S3")

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(api, "S3", S3()),
        patch.object(api, "query_all", return_value=[document, {**document, "documentId": "doc_55555555-5555-4555-8555-555555555555"}]),
        patch.object(api, "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS", 1),
    ):
        assert problem_code(lambda: api.archive_loan(request_event(), AUTH, LOAN_ID, "cid")) == (
            "LOAN_ARCHIVE_DOCUMENT_LIMIT_EXCEEDED"
        )

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(api, "S3", S3()),
        patch.object(api, "query_all", return_value=[document]),
        patch.object(api, "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES", 64),
    ):
        assert problem_code(lambda: api.archive_loan(request_event(), AUTH, LOAN_ID, "cid")) == (
            "LOAN_ARCHIVE_MANIFEST_LIMIT_EXCEEDED"
        )


def test_archive_loan_fails_closed_for_processing_document_or_unversioned_manifest() -> None:
    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(
            api,
            "query_all",
            return_value=[
                {
                    "entityType": "DOCUMENT",
                    "documentId": DOCUMENT_ID,
                    "status": "EXTRACTING",
                }
            ],
        ),
    ):
        assert problem_code(lambda: api.archive_loan(request_event(), AUTH, LOAN_ID, "cid")) == (
            "LOAN_HAS_PROCESSING_DOCUMENTS"
        )

    class S3:
        def put_object(self, **_: object) -> dict[str, object]:
            return {}

    with (
        patch.object(
            api,
            "replay_or_none",
            return_value=(IDEMPOTENCY_IDENTITY, "request-hash", None),
        ),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(api, "query_all", return_value=[]),
        patch.object(api, "S3", S3()),
    ):
        assert problem_code(lambda: api.archive_loan(request_event(), AUTH, LOAN_ID, "cid")) == (
            "ARCHIVE_STORAGE_UNAVAILABLE"
        )


def test_document_views_data_points_and_downloads_keep_storage_coordinates_private() -> None:
    document = {
        "documentId": DOCUMENT_ID,
        "status": "SUCCEEDED",
        "currentUploadId": UPLOAD_ID,
        "createdAt": NOW,
        "updatedAt": NOW,
        "fileName": "unsafe name.pdf",
        "sourceBucket": "source",
        "sourceKey": "source.pdf",
        "sourceVersionId": "source-v1",
        "selectedBucket": "source",
        "selectedKey": "selected.pdf",
        "selectedVersionId": "selected-v1",
        "dataPointsBucket": "source",
        "dataPointsKey": "data.json",
        "dataPointsVersionId": "data-v1",
    }
    archive = {
        "loanId": LOAN_ID,
        "documentId": DOCUMENT_ID,
        "uploadId": UPLOAD_ID,
        "archiveSequence": 1,
        "archivedAt": NOW,
    }

    class S3:
        def __init__(self) -> None:
            self.grants: list[dict[str, object]] = []

        def get_object(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["VersionId"] == "data-v1"
            return {"Body": BytesBody(b'{"loanAmount": 100}'), "ContentLength": 19}

        def generate_presigned_url(self, _: str, **kwargs: object) -> str:
            self.grants.append(kwargs)
            return "https://download.invalid"

    s3 = S3()
    with (
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, document),
        ),
        patch.object(api, "query_all", return_value=[archive]),
        patch.object(api, "S3", s3),
    ):
        view = response_body(api.get_document(AUTH, LOAN_ID, DOCUMENT_ID, "cid"))
        data = response_body(api.get_data_points(AUTH, LOAN_ID, DOCUMENT_ID, "cid"))
        grant = response_body(api.download_grant(AUTH, LOAN_ID, DOCUMENT_ID, "selected", "cid"))

    assert view["artifacts"] == ["source", "selected", "data-points"]
    assert "sourceKey" not in json.dumps(view)
    assert data == {"loanAmount": 100}
    assert grant["fileName"] == "unsafe_name-selected.pdf"
    assert s3.grants[0]["Params"]["VersionId"] == "selected-v1"
    assert s3.grants[0]["Params"]["ResponseCacheControl"] == "no-store"


def test_data_and_download_helpers_fail_closed_for_missing_or_unpinned_artifacts() -> None:
    assert problem_code(lambda: api.read_data_points(None, "SCREENING", "cid")) == ("DATA_POINTS_NOT_READY")
    assert problem_code(lambda: api.read_data_points(None, "SUCCEEDED", "cid")) == ("DATA_POINTS_NOT_FOUND")
    assert (
        problem_code(
            lambda: api.read_data_points({"bucket": "source", "key": "data.json"}, "SUCCEEDED", "cid")
        )
        == "ARTIFACT_REFERENCE_INVALID"
    )
    assert problem_code(lambda: api.create_download_grant({}, "loan.pdf", "other", "cid")) == (
        "INVALID_ARTIFACT"
    )
    assert (
        problem_code(lambda: api.create_download_grant({"source": None}, "loan.pdf", "source", "cid"))
        == "ARTIFACT_NOT_FOUND"
    )
    assert (
        problem_code(
            lambda: api.create_download_grant(
                {"source": {"bucket": "source", "key": "source.pdf"}},
                "loan.pdf",
                "source",
                "cid",
            )
        )
        == "ARTIFACT_REFERENCE_INVALID"
    )


@pytest.mark.parametrize(
    ("body", "content_length", "expected"),
    [
        (b"{}", 5 * 1024 * 1024 + 1, "DATA_POINTS_TOO_LARGE"),
        (b"not-json", 8, "DATA_POINTS_INVALID"),
    ],
)
def test_inline_data_points_are_bounded_and_valid_json(
    body: bytes, content_length: int, expected: str
) -> None:
    stream = BytesBody(body)

    class S3:
        def get_object(self, **_: object) -> dict[str, object]:
            return {"Body": stream, "ContentLength": content_length}

    reference = {"bucket": "source", "key": "data.json", "versionId": "data-v1"}
    with patch.object(api, "S3", S3()):
        assert problem_code(lambda: api.read_data_points(reference, "SUCCEEDED", "cid")) == expected
    assert stream.closed is True


def test_lambda_handler_enforces_origin_dispatches_and_sanitizes_unhandled_errors() -> None:
    health_event = {
        "headers": {"x-origin-verify": api.ORIGIN_VERIFY_SECRET},
        "rawPath": "/health",
        "requestContext": {"http": {"method": "GET"}},
    }
    assert response_body(api.lambda_handler(health_event, None)) == {"status": "ok"}

    denied = api.lambda_handler(
        {
            "headers": {"x-origin-verify": "wrong"},
            "rawPath": "/health",
            "requestContext": {"http": {"method": "GET"}},
        },
        None,
    )
    assert denied["statusCode"] == 403
    assert response_body(denied)["code"] == "ORIGIN_NOT_ALLOWED"

    event = {
        "headers": {"x-origin-verify": api.ORIGIN_VERIFY_SECRET},
        "rawPath": "/synthetic",
        "requestContext": {"http": {"method": "GET"}},
    }
    route = ("GET", re.compile(r"^/synthetic$"), "Loan.Read", lambda *_: api.json_response(204, {}, "cid"))
    with patch.object(api, "ROUTES", [route]), patch.object(api, "authorize", return_value=AUTH):
        assert api.lambda_handler(event, None)["statusCode"] == 204

    exploding_route = ("GET", re.compile(r"^/synthetic$"), "Loan.Read", lambda *_: 1 / 0)
    with (
        patch.object(api, "ROUTES", [exploding_route]),
        patch.object(api, "authorize", return_value=AUTH),
    ):
        failure = api.lambda_handler(event, None)
    assert failure["statusCode"] == 500
    assert response_body(failure)["detail"] == "An unexpected error occurred"

    missing = {**event, "rawPath": "/missing"}
    with patch.object(api, "ROUTES", []):
        assert response_body(api.lambda_handler(missing, None))["code"] == "ROUTE_NOT_FOUND"
