"""Provider-neutral loan-domain tests for the retained AWS adapter boundary.

These tests enter through ``dispatch_request``, the same normalized boundary used
by the Azure transport. AWS services are deterministic doubles: no test acquires
credentials, uploads a PDF, or follows a signed URL.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from unittest.mock import Mock, patch

import pytest
from test_loan_api import api

TENANT_ID = "tenant-id"
LOAN_ID = "23051"
INSTANCE_ID = "lin_11111111-1111-4111-8111-111111111111"
DOCUMENT_ID = "doc_22222222-2222-4222-8222-222222222222"
UPLOAD_ID = "upl_33333333-3333-4333-8333-333333333333"
RUN_ID = "run_44444444-4444-4444-8444-444444444444"
CHECKSUM = base64.b64encode(b"x" * 32).decode("ascii")
NOW = "2026-07-16T00:00:00Z"
_MISSING = object()


def domain_event(
    method: str,
    path: str,
    permission: str,
    *,
    body: object = _MISSING,
    query: dict[str, str] | None = None,
    tenant_id: str = TENANT_ID,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "rawPath": path,
        "headers": {
            "Idempotency-Key": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "X-Correlation-Id": "adapter-boundary-test",
        },
        "requestContext": {
            "http": {"method": method},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "tid": tenant_id,
                        "azp": "client-id",
                        "oid": "actor-id",
                        "scp": permission,
                        "roles": [permission],
                    }
                }
            },
        },
    }
    if body is not _MISSING:
        event["body"] = json.dumps(body)
    if query is not None:
        event["queryStringParameters"] = query
    return event


def response_body(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(response["body"])


class BytesBody:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        return self.value if amount < 0 else self.value[:amount]

    def close(self) -> None:
        self.closed = True


class PresignS3:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, Any]] = []

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.post_calls.append(kwargs)
        return {
            "url": "https://upload.invalid/one-object",
            "fields": {**kwargs["Fields"], "policy": "ephemeral-test-policy"},
        }


def upload_request() -> dict[str, object]:
    return {
        "fileName": "closing-disclosure.pdf",
        "contentType": "application/pdf",
        "sizeBytes": 123,
        "checksumSha256": CHECKSUM,
    }


def valid_upload() -> dict[str, object]:
    return {
        "sourceBucket": "source",
        "sourceKey": "quarantine/source.pdf",
        "sizeBytes": 123,
        "checksumSha256": CHECKSUM,
        "uploadExpiresAt": "2099-01-01T00:00:00Z",
        "status": "AWAITING_UPLOAD",
        "malwareScanStatus": "NO_THREATS_FOUND",
    }


def valid_head() -> dict[str, object]:
    return {
        "VersionId": "source-version-17",
        "ContentLength": 123,
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


class UploadTable:
    def __init__(self, upload: dict[str, object]) -> None:
        self.upload = upload
        self.get_calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, object]:
        self.get_calls.append(kwargs)
        # boto3 materializes a new mapping per read. Returning a copy keeps
        # race tests from accidentally mutating an already-read snapshot.
        return {"Item": dict(self.upload)}


class CompletionS3:
    def __init__(self, head: dict[str, object] | None = None, *, missing: bool = False) -> None:
        self.head = valid_head() if head is None else head
        self.missing = missing
        self.head_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.prefix = BytesBody(b"%PDF-")

    def head_object(self, **kwargs: Any) -> dict[str, object]:
        self.head_calls.append(kwargs)
        if self.missing:
            raise api.ClientError({"Error": {"Code": "NoSuchKey"}})
        return self.head

    def get_object(self, **kwargs: Any) -> dict[str, object]:
        self.get_calls.append(kwargs)
        return {"Body": self.prefix}


class LambdaRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_upload_initialization_returns_only_a_constrained_direct_s3_grant() -> None:
    s3 = PresignS3()
    writes: list[dict[str, object]] = []
    with (
        patch.object(api, "S3", s3),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
        patch.object(api, "new_id", side_effect=[DOCUMENT_ID, UPLOAD_ID]),
        patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
    ):
        response = api.dispatch_request(
            domain_event("POST", f"/v1/loans/{LOAN_ID}/documents", "Document.Upload", body=upload_request())
        )

    assert response["statusCode"] == 201
    body = response_body(response)
    assert body["documentId"] == DOCUMENT_ID
    assert body["upload"]["url"] == "https://upload.invalid/one-object"
    assert len(s3.post_calls) == 1
    call = s3.post_calls[0]
    assert call["Bucket"] == api.SOURCE_BUCKET
    assert call["Key"].endswith(f"/{UPLOAD_ID}/source.pdf")
    assert call["Key"].startswith("quarantine/tenants/")
    assert call["Fields"]["Content-Type"] == "application/pdf"
    assert call["Fields"]["x-amz-checksum-sha256"] == CHECKSUM
    assert call["Fields"]["x-amz-server-side-encryption"] == "aws:kms"
    assert call["Fields"]["x-amz-server-side-encryption-aws-kms-key-id"] == api.DATA_KEY_ARN
    assert call["Fields"]["x-amz-meta-document-id"] == DOCUMENT_ID
    assert call["Fields"]["x-amz-meta-upload-id"] == UPLOAD_ID
    assert ["content-length-range", 123, 123] in call["Conditions"]
    assert "Body" not in call
    assert len(writes) == 4


def test_upload_initialization_rejects_pdf_bytes_before_aws_access() -> None:
    s3 = PresignS3()
    request = {**upload_request(), "pdfBase64": "JVBERi0xLjQ="}

    with (
        patch.object(api, "S3", s3),
        patch.object(api, "require_active_instance") as require_active_instance,
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents",
                "Document.Upload",
                body=request,
            )
        )

    assert response["statusCode"] == 400
    assert response_body(response)["code"] == "INVALID_REQUEST"
    require_active_instance.assert_not_called()
    assert s3.post_calls == []


def test_idempotent_upload_initialization_mints_a_fresh_grant_without_replaying_signed_fields() -> None:
    request = upload_request()
    stable_replay = {
        "status": 201,
        "body": {
            "loanId": LOAN_ID,
            "documentId": DOCUMENT_ID,
            "uploadId": UPLOAD_ID,
            "status": "AWAITING_UPLOAD",
        },
    }

    class ReplayTable:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def get_item(self, **kwargs: Any) -> dict[str, object]:
            key = str(kwargs["Key"]["SK"])
            if "#UPLOAD#" not in key:
                return {"Item": {"currentUploadId": UPLOAD_ID, "status": "AWAITING_UPLOAD"}}
            return {
                "Item": {
                    **request,
                    "status": "AWAITING_UPLOAD",
                    "sourceKey": "tenants/tenant-id/replay/source.pdf",
                }
            }

        def update_item(self, **kwargs: Any) -> None:
            self.updates.append(kwargs)

    table = ReplayTable()
    s3 = PresignS3()
    with (
        patch.object(api, "TABLE", table),
        patch.object(api, "S3", s3),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", stable_replay)),
        patch.object(api, "require_active_instance", return_value=({}, INSTANCE_ID)),
    ):
        response = api.dispatch_request(
            domain_event("POST", f"/v1/loans/{LOAN_ID}/documents", "Document.Upload", body=request)
        )

    body = response_body(response)
    assert response["statusCode"] == 201
    assert body["upload"]["url"] == "https://upload.invalid/one-object"
    assert "upload" not in stable_replay["body"]
    assert "expiresAt" not in stable_replay["body"]
    assert len(s3.post_calls) == 1
    assert len(table.updates) == 1


def test_completion_pins_the_exact_s3_version_for_state_and_processing() -> None:
    table = UploadTable(valid_upload())
    s3 = CompletionS3()
    lambda_client = LambdaRecorder()
    writes: list[dict[str, object]] = []

    def commit(items: list[dict[str, object]]) -> None:
        writes.extend(items)
        table.upload.update(
            {
                "status": "VALIDATING",
                "clientCompletedAt": NOW,
                "processingExecutionId": RUN_ID,
                "sourceVersionId": "source-version-17",
            }
        )

    with (
        patch.object(api, "TABLE", table),
        patch.object(api, "S3", s3),
        patch.object(api, "LAMBDA", lambda_client),
        patch.object(api, "UPLOAD_PROCESSOR_ARN", "processor-arn"),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
        ),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "new_id", return_value=RUN_ID),
        patch.object(api, "transact", side_effect=commit),
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )

    assert response["statusCode"] == 202
    assert response_body(response)["processingExecutionId"] == RUN_ID
    assert s3.head_calls == [
        {"Bucket": "source", "Key": "quarantine/source.pdf", "ChecksumMode": "ENABLED"}
    ]
    assert s3.get_calls[0]["VersionId"] == "source-version-17"
    assert s3.get_calls[0]["Range"] == "bytes=0-4"
    assert s3.prefix.closed is True
    values = writes[0]["Update"]["ExpressionAttributeValues"]
    assert values[":version"] == {"stub": "source-version-17"}
    invoked = json.loads(lambda_client.calls[0]["Payload"])
    assert invoked["detail"]["versionId"] == "source-version-17"


def test_completion_rereads_a_clean_scan_that_races_with_object_validation() -> None:
    upload = valid_upload()
    upload.pop("malwareScanStatus")
    table = UploadTable(upload)
    lambda_client = LambdaRecorder()

    class RacingS3(CompletionS3):
        def head_object(self, **kwargs: Any) -> dict[str, object]:
            # GuardDuty persisted its immutable-version scan after the API's
            # initial GetItem but before the completion transaction committed.
            table.upload.update(
                {
                    "malwareScanStatus": "NO_THREATS_FOUND",
                    "malwareScanVersionId": "source-version-17",
                }
            )
            return super().head_object(**kwargs)

    def commit(_items: list[dict[str, object]]) -> None:
        table.upload.update(
            {
                "status": "VALIDATING",
                "clientCompletedAt": NOW,
                "processingExecutionId": RUN_ID,
                "sourceVersionId": "source-version-17",
            }
        )

    with (
        patch.object(api, "TABLE", table),
        patch.object(api, "S3", RacingS3()),
        patch.object(api, "LAMBDA", lambda_client),
        patch.object(api, "UPLOAD_PROCESSOR_ARN", "processor-arn"),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
        ),
        patch.object(api, "utc_now", return_value=NOW),
        patch.object(api, "new_id", return_value=RUN_ID),
        patch.object(api, "transact", side_effect=commit),
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )

    assert response["statusCode"] == 202
    assert len(table.get_calls) == 2
    assert len(lambda_client.calls) == 1
    invoked = json.loads(lambda_client.calls[0]["Payload"])
    assert invoked["detail"]["versionId"] == "source-version-17"


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ({"VersionId": None}, "VERSION_REQUIRED"),
        ({"ContentLength": 124}, "SIZE_MISMATCH"),
        ({"ChecksumSHA256": "wrong"}, "CHECKSUM_MISMATCH"),
        ({"ContentType": "text/plain"}, "CONTENT_TYPE_MISMATCH"),
        ({"Metadata": {}}, "METADATA_MISMATCH"),
        ({"SSEKMSKeyId": "arn:aws:kms:us-west-2:111122223333:key/wrong"}, "ENCRYPTION_MISMATCH"),
    ],
)
def test_completion_rejects_mismatched_object_evidence(
    change: dict[str, object], expected_code: str
) -> None:
    head = valid_head()
    head.update(change)
    s3 = CompletionS3(head)
    with (
        patch.object(api, "TABLE", UploadTable(valid_upload())),
        patch.object(api, "S3", s3),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
        ),
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )

    assert response["statusCode"] == 422
    assert response_body(response)["code"] == expected_code
    assert s3.get_calls == []


@pytest.mark.parametrize(
    ("expires_at", "status", "code"),
    [
        ("2020-01-01T00:00:00Z", 410, "UPLOAD_EXPIRED"),
        ("2099-01-01T00:00:00Z", 409, "UPLOAD_NOT_READY"),
    ],
)
def test_completion_distinguishes_expired_from_not_yet_visible(
    expires_at: str, status: int, code: str
) -> None:
    upload = {**valid_upload(), "uploadExpiresAt": expires_at}
    with (
        patch.object(api, "TABLE", UploadTable(upload)),
        patch.object(api, "S3", CompletionS3(missing=True)),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": UPLOAD_ID}),
        ),
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )

    assert response["statusCode"] == status
    assert response_body(response)["code"] == code


def test_completion_rejects_pdf_bytes_and_stale_uploads_before_s3_access() -> None:
    s3 = CompletionS3()
    get_document = Mock()
    with patch.object(api, "S3", s3), patch.object(api, "get_document_item", get_document):
        body_response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
                body={"pdfBase64": "JVBERi0xLjQ="},
            )
        )
    assert body_response["statusCode"] == 400
    assert response_body(body_response)["code"] == "INVALID_REQUEST"
    get_document.assert_not_called()
    assert s3.head_calls == []

    with (
        patch.object(api, "S3", s3),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", None)),
        patch.object(
            api,
            "get_document_item",
            return_value=("pk", INSTANCE_ID, {"currentUploadId": "upl_99999999-9999-4999-8999-999999999999"}),
        ),
    ):
        stale_response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )
    assert stale_response["statusCode"] == 409
    assert response_body(stale_response)["code"] == "UPLOAD_NOT_CURRENT"
    assert s3.head_calls == []


def test_completion_idempotent_replay_returns_stable_result_without_repinning() -> None:
    replay = {
        "status": 202,
        "body": {
            "loanId": LOAN_ID,
            "documentId": DOCUMENT_ID,
            "uploadId": UPLOAD_ID,
            "processingExecutionId": RUN_ID,
            "status": "VALIDATING",
        },
    }
    reconcile = Mock()
    s3 = CompletionS3()
    with (
        patch.object(api, "S3", s3),
        patch.object(api, "replay_or_none", return_value=({"PK": "idem", "SK": "key"}, "hash", replay)),
        patch.object(api, "reconcile_upload_processor", reconcile),
    ):
        response = api.dispatch_request(
            domain_event(
                "POST",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/uploads/{UPLOAD_ID}/complete",
                "Document.Upload",
            )
        )

    assert response["statusCode"] == 202
    assert response_body(response) == replay["body"]
    reconcile.assert_called_once()
    replay_auth, replay_loan, replay_document, replay_upload = reconcile.call_args.args
    assert replay_auth["tenantId"] == TENANT_ID
    assert (replay_loan, replay_document, replay_upload) == (LOAN_ID, DOCUMENT_ID, UPLOAD_ID)
    assert s3.head_calls == []


def current_document() -> dict[str, object]:
    return {
        "documentId": DOCUMENT_ID,
        "status": "SUCCEEDED",
        "currentUploadId": UPLOAD_ID,
        "createdAt": NOW,
        "updatedAt": NOW,
        "fileName": "closing disclosure.pdf",
        "sourceBucket": "source",
        "sourceKey": "current/source.pdf",
        "sourceVersionId": "source-current-v1",
        "selectedBucket": "source",
        "selectedKey": "current/selected.pdf",
        "selectedVersionId": "selected-current-v2",
        "dataPointsBucket": "source",
        "dataPointsKey": "current/data.json",
        "dataPointsVersionId": "data-current-v3",
    }


class GrantS3:
    def __init__(self) -> None:
        self.grants: list[dict[str, Any]] = []

    def generate_presigned_url(self, operation: str, **kwargs: Any) -> str:
        self.grants.append({"operation": operation, **kwargs})
        return f"https://download.invalid/grant-{len(self.grants)}"


def test_current_status_and_all_download_grants_use_exact_versions() -> None:
    s3 = GrantS3()
    document = current_document()
    with (
        patch.object(api, "S3", s3),
        patch.object(api, "get_document_item", return_value=("pk", INSTANCE_ID, document)),
        patch.object(api, "query_all", return_value=[]),
    ):
        status = api.dispatch_request(
            domain_event("GET", f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}", "Document.Read")
        )
        source = api.dispatch_request(
            domain_event(
                "GET",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/download",
                "Document.Read",
                query={"artifact": "source"},
            )
        )
        selected = api.dispatch_request(
            domain_event(
                "GET",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/download",
                "Document.Read",
                query={"artifact": "selected"},
            )
        )
        data_points = api.dispatch_request(
            domain_event(
                "GET",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/data-points/download",
                "DataPoints.Read",
            )
        )

    status_body = response_body(status)
    assert status_body["status"] == "SUCCEEDED"
    assert status_body["artifacts"] == ["source", "selected", "data-points"]
    assert "Bucket" not in json.dumps(status_body)
    assert [call["Params"]["VersionId"] for call in s3.grants] == [
        "source-current-v1",
        "selected-current-v2",
        "data-current-v3",
    ]
    assert response_body(source)["contentType"] == "application/pdf"
    assert response_body(selected)["fileName"].endswith("-selected.pdf")
    assert response_body(data_points)["contentType"] == "application/json"
    assert all(call["Params"]["ResponseCacheControl"] == "no-store" for call in s3.grants)


def archive_fixture() -> tuple[dict[str, object], dict[str, object]]:
    archive = {
        "PK": f"TENANT#{TENANT_ID}#LOAN#{LOAN_ID}",
        "SK": "ARCHIVE#000000000001",
        "entityType": "LOAN_ARCHIVE",
        "loanId": LOAN_ID,
        "loanInstanceId": INSTANCE_ID,
        "archiveSequence": 1,
        "displayLoanId": f"{LOAN_ID}_001",
        "status": "ARCHIVED",
        "archivedAt": NOW,
        "documentCount": 1,
        "manifestBucket": "source",
        "manifestKey": "archives/manifest.json",
        "manifestVersionId": "manifest-v7",
    }
    manifest = {
        "schemaVersion": 1,
        "tenantId": TENANT_ID,
        "loanId": LOAN_ID,
        "loanInstanceId": INSTANCE_ID,
        "archiveSequence": 1,
        "documents": [
            {
                "documentId": DOCUMENT_ID,
                "status": "SUCCEEDED",
                "currentUploadId": UPLOAD_ID,
                "fileName": "closing-disclosure.pdf",
                "createdAt": NOW,
                "updatedAt": NOW,
                "source": {"bucket": "source", "key": "archive/source.pdf", "versionId": "source-archive-v4"},
                "selected": {
                    "bucket": "source",
                    "key": "archive/selected.pdf",
                    "versionId": "selected-archive-v5",
                },
                "dataPoints": {"bucket": "source", "key": "archive/data.json", "versionId": "data-archive-v6"},
            }
        ],
    }
    payload = json.dumps(manifest).encode("utf-8")
    archive["manifestChecksumSha256"] = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
    return archive, manifest


class ArchiveTable:
    def __init__(self, archive: dict[str, object]) -> None:
        self.archive = archive
        self.calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        key = kwargs["Key"]
        if key["PK"] == self.archive["PK"] and key["SK"] == self.archive["SK"]:
            return {"Item": self.archive}
        return {}


class ArchiveS3(GrantS3):
    def __init__(self, manifest: dict[str, object]) -> None:
        super().__init__()
        self.manifest = manifest
        self.manifest_reads: list[dict[str, Any]] = []
        self.manifest_bodies: list[BytesBody] = []

    def get_object(self, **kwargs: Any) -> dict[str, object]:
        self.manifest_reads.append(kwargs)
        payload = json.dumps(self.manifest).encode("utf-8")
        body = BytesBody(payload)
        self.manifest_bodies.append(body)
        checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        return {
            "Body": body,
            "ChecksumSHA256": checksum,
            "ContentLength": len(payload),
        }


def test_loan_archive_status_and_grants_freeze_manifest_and_artifact_versions() -> None:
    archive, manifest = archive_fixture()
    s3 = ArchiveS3(manifest)
    base = f"/v1/loans/{LOAN_ID}/archives/1/documents/{DOCUMENT_ID}"
    with (
        patch.object(api, "TABLE", ArchiveTable(archive)),
        patch.object(api, "S3", s3),
        patch.object(api, "query_all", return_value=[]),
    ):
        status = api.dispatch_request(domain_event("GET", base, "Document.Read"))
        source = api.dispatch_request(
            domain_event("GET", f"{base}/download", "Document.Read", query={"artifact": "source"})
        )
        selected = api.dispatch_request(
            domain_event("GET", f"{base}/download", "Document.Read", query={"artifact": "selected"})
        )
        data_points = api.dispatch_request(
            domain_event("GET", f"{base}/data-points/download", "DataPoints.Read")
        )

    status_body = response_body(status)
    assert status_body["loanArchiveSequence"] == 1
    assert status_body["artifacts"] == ["source", "selected", "data-points"]
    assert "archive/source.pdf" not in json.dumps(status_body)
    assert len(s3.manifest_reads) == 4
    assert all(body.closed for body in s3.manifest_bodies)
    assert all(call["VersionId"] == "manifest-v7" for call in s3.manifest_reads)
    assert [call["Params"]["VersionId"] for call in s3.grants] == [
        "source-archive-v4",
        "selected-archive-v5",
        "data-archive-v6",
    ]
    assert response_body(source)["contentType"] == "application/pdf"
    assert response_body(selected)["contentType"] == "application/pdf"
    assert response_body(data_points)["contentType"] == "application/json"


def test_archive_manifest_checksum_and_size_fail_closed_and_close_the_stream() -> None:
    archive, manifest = archive_fixture()
    valid_payload = json.dumps(manifest).encode("utf-8")
    valid_checksum = str(archive["manifestChecksumSha256"])

    class TrackingBody(BytesBody):
        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.read_amount: int | None = None

        def read(self, amount: int = -1) -> bytes:
            self.read_amount = amount
            return super().read(amount)

    def invoke(
        *,
        expected_checksum: str | None,
        returned_checksum: str | None,
        payload: bytes,
        maximum_bytes: int = api.MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES,
    ) -> tuple[str, TrackingBody]:
        record = dict(archive)
        if expected_checksum is None:
            record.pop("manifestChecksumSha256", None)
        else:
            record["manifestChecksumSha256"] = expected_checksum
        body = TrackingBody(payload)

        class S3:
            def get_object(self, **_: Any) -> dict[str, object]:
                response: dict[str, object] = {"Body": body}
                if returned_checksum is not None:
                    response["ChecksumSHA256"] = returned_checksum
                return response

        with (
            patch.object(api, "TABLE", ArchiveTable(record)),
            patch.object(api, "S3", S3()),
            patch.object(api, "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES", maximum_bytes),
            pytest.raises(api.ApiProblem) as caught,
        ):
            api.load_loan_archive(TENANT_ID, LOAN_ID, "1")
        assert body.closed is True
        return caught.value.code, body

    missing_expected, _ = invoke(
        expected_checksum=None,
        returned_checksum=valid_checksum,
        payload=valid_payload,
    )
    missing_returned, _ = invoke(
        expected_checksum=valid_checksum,
        returned_checksum=None,
        payload=valid_payload,
    )
    mismatched_header, _ = invoke(
        expected_checksum=valid_checksum,
        returned_checksum="different",
        payload=valid_payload,
    )
    tampered_bytes, _ = invoke(
        expected_checksum=valid_checksum,
        returned_checksum=valid_checksum,
        payload=valid_payload + b" ",
    )
    oversized = b"x" * 65
    oversized_checksum = base64.b64encode(hashlib.sha256(oversized).digest()).decode("ascii")
    oversized_code, oversized_body = invoke(
        expected_checksum=oversized_checksum,
        returned_checksum=oversized_checksum,
        payload=oversized,
        maximum_bytes=64,
    )

    assert {missing_expected, missing_returned, mismatched_header, tampered_bytes} == {
        "ARCHIVE_MANIFEST_INVALID"
    }
    assert oversized_code == "ARCHIVE_MANIFEST_TOO_LARGE"
    assert oversized_body.read_amount == 65

    archive_with_two, manifest_with_two = archive_fixture()
    manifest_with_two["documents"] = [
        *manifest_with_two["documents"],
        {
            **manifest_with_two["documents"][0],
            "documentId": "doc_55555555-5555-4555-8555-555555555555",
        },
    ]
    two_payload = json.dumps(manifest_with_two).encode("utf-8")
    archive_with_two["manifestChecksumSha256"] = base64.b64encode(
        hashlib.sha256(two_payload).digest()
    ).decode("ascii")
    two_document_s3 = ArchiveS3(manifest_with_two)
    with (
        patch.object(api, "TABLE", ArchiveTable(archive_with_two)),
        patch.object(api, "S3", two_document_s3),
        patch.object(api, "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS", 1),
        pytest.raises(api.ApiProblem) as too_many_documents,
    ):
        api.load_loan_archive(TENANT_ID, LOAN_ID, "1")
    assert too_many_documents.value.code == "ARCHIVE_MANIFEST_TOO_LARGE"
    assert two_document_s3.manifest_bodies[0].closed is True


class DataS3:
    def __init__(self, payload: bytes, content_length: int) -> None:
        self.payload = BytesBody(payload)
        self.content_length = content_length
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"Body": self.payload, "ContentLength": self.content_length}


def test_inline_data_points_are_bounded_and_read_only_from_the_pinned_version() -> None:
    document = current_document()
    payload = b'{"loanAmount":250000}'
    s3 = DataS3(payload, len(payload))
    path = f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/data-points"
    with (
        patch.object(api, "S3", s3),
        patch.object(api, "get_document_item", return_value=("pk", INSTANCE_ID, document)),
    ):
        response = api.dispatch_request(domain_event("GET", path, "DataPoints.Read"))

    assert response["statusCode"] == 200
    assert response_body(response) == {"loanAmount": 250000}
    assert s3.calls == [
        {"Bucket": "source", "Key": "current/data.json", "VersionId": "data-current-v3"}
    ]
    assert s3.payload.closed is True

    oversized = DataS3(b"{}", api.MAXIMUM_INLINE_DATA_POINTS_BYTES + 1)
    with (
        patch.object(api, "S3", oversized),
        patch.object(api, "get_document_item", return_value=("pk", INSTANCE_ID, document)),
    ):
        too_large = api.dispatch_request(domain_event("GET", path, "DataPoints.Read"))

    assert too_large["statusCode"] == 413
    assert response_body(too_large)["code"] == "DATA_POINTS_TOO_LARGE"
    assert oversized.calls[0]["VersionId"] == "data-current-v3"
    assert oversized.payload.closed is True


def test_cross_tenant_denial_occurs_before_registry_or_s3_access() -> None:
    get_document = Mock(side_effect=AssertionError("registry must not be called"))
    s3 = GrantS3()
    with patch.object(api, "get_document_item", get_document), patch.object(api, "S3", s3):
        response = api.dispatch_request(
            domain_event(
                "GET",
                f"/v1/loans/{LOAN_ID}/documents/{DOCUMENT_ID}/download",
                "Document.Read",
                tenant_id="other-tenant",
                query={"artifact": "source"},
            )
        )

    assert response["statusCode"] == 403
    assert response_body(response)["code"] == "TENANT_NOT_ALLOWED"
    get_document.assert_not_called()
    assert s3.grants == []
