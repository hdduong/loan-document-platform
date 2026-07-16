"""Synthetic unit coverage for the exact-version upload processor."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]


def load_upload_processor():
    path = ROOT / "services" / "upload_processor" / "app.py"
    spec = importlib.util.spec_from_file_location("upload_processor_coverage_app", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


processor = load_upload_processor()


def synthetic_pdf(*, pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if password is not None:
        writer.encrypt(password)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def checksum(value: bytes) -> str:
    return base64.b64encode(hashlib.sha256(value).digest()).decode("ascii")


def upload_record(pdf: bytes | None = None, **overrides: Any) -> dict[str, Any]:
    content = pdf if pdf is not None else synthetic_pdf()
    record: dict[str, Any] = {
        "PK": "TENANT#synthetic#LOAN#23051",
        "SK": "INSTANCE#lin_1#DOC#doc_1#UPLOAD#upl_1",
        "entityType": "UPLOAD",
        "loanInstanceId": "lin_1",
        "documentId": "doc_1",
        "uploadId": "upl_1",
        "processingExecutionId": "run_1",
        "sourceBucket": "source-bucket",
        "sourceKey": "quarantine/synthetic.pdf",
        "sourceVersionId": "version-1",
        "sizeBytes": len(content),
        "checksumSha256": checksum(content),
        "clientCompletedAt": "2026-07-16T00:00:00Z",
        "status": "VALIDATING",
    }
    record.update(overrides)
    return record


def stream_record(
    event_id: str,
    current: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "eventID": event_id,
        "eventName": "MODIFY",
        "eventSource": "aws:dynamodb",
        "dynamodb": {
            "SequenceNumber": event_id,
            "NewImage": processor.serialize_map(current),
            "OldImage": processor.serialize_map(previous or {}),
        },
    }


class ChunkBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def iter_chunks(self, *, chunk_size: int):
        assert chunk_size == 1024 * 1024
        yield from self.chunks


class ObjectS3:
    def __init__(
        self,
        upload: dict[str, Any],
        content: bytes,
        *,
        head_overrides: dict[str, Any] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.upload = upload
        self.content = content
        self.head_overrides = head_overrides or {}
        self.chunks = chunks if chunks is not None else [content]
        self.head_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        response = {
            "VersionId": kwargs["VersionId"],
            "ContentLength": self.upload["sizeBytes"],
            "ContentType": "application/pdf",
            "ChecksumSHA256": self.upload["checksumSha256"],
            "ServerSideEncryption": "aws:kms",
            "Metadata": {
                "document-id": self.upload["documentId"],
                "upload-id": self.upload["uploadId"],
                "loan-instance-id": self.upload["loanInstanceId"],
            },
        }
        response.update(self.head_overrides)
        return response

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        return {"Body": ChunkBody(self.chunks)}


def client_error(code: str) -> Exception:
    return processor.ClientError({"Error": {"Code": code}}, "SyntheticOperation")


@pytest.fixture(autouse=True)
def isolated_aws_cache(monkeypatch: pytest.MonkeyPatch):
    processor._AWS.clear()
    for name in (
        "TABLE_NAME",
        "SOURCE_BUCKET",
        "IDP_INPUT_BUCKET",
        "SCREEN_CONFIG_SHA256",
        "DATA_KEY_ARN",
        "IDP_INPUT_KEY_ARN",
        "MAXIMUM_UPLOAD_BYTES",
        "MAXIMUM_PDF_PAGES",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    processor._AWS.clear()


def test_environment_and_aws_helpers_cache_constructed_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDynamoResource:
        def __init__(self) -> None:
            self.table_names: list[str] = []

        def Table(self, name: str) -> dict[str, str]:
            self.table_names.append(name)
            return {"table": name}

    dynamodb = FakeDynamoResource()

    class FakeBoto3:
        def __init__(self) -> None:
            self.resources: list[str] = []
            self.clients: list[str] = []

        def resource(self, name: str) -> Any:
            self.resources.append(name)
            return dynamodb

        def client(self, name: str) -> dict[str, str]:
            self.clients.append(name)
            return {"client": name}

    fake_boto3 = FakeBoto3()
    monkeypatch.setattr(processor, "boto3", fake_boto3)
    monkeypatch.setenv("TABLE_NAME", "synthetic-table")

    assert processor.required_env("TABLE_NAME") == "synthetic-table"
    assert processor.table() == {"table": "synthetic-table"}
    assert processor.table() == {"table": "synthetic-table"}
    assert processor.ddb_client() == {"client": "dynamodb"}
    assert processor.s3_client() == {"client": "s3"}
    assert processor.s3_client() == {"client": "s3"}
    assert processor.aws_resource("dynamodb") is dynamodb
    assert processor.aws_resource("dynamodb") is dynamodb
    assert fake_boto3.resources == ["dynamodb"]
    assert fake_boto3.clients == ["dynamodb", "s3"]

    monkeypatch.setenv("BLANK_VALUE", "  ")
    with pytest.raises(RuntimeError, match="BLANK_VALUE"):
        processor.required_env("BLANK_VALUE")


def test_event_normalization_accepts_client_completion_and_rejects_bad_inputs() -> None:
    complete = {
        "source": "loan-api",
        "detail-type": processor.CLIENT_COMPLETE_DETAIL_TYPE,
        "detail": {
            "bucketName": "source-bucket",
            "objectKey": "quarantine/synthetic.pdf",
            "versionId": "version-1",
        },
    }
    assert processor.event_object(complete) == {
        "kind": "client-complete",
        "bucket": "source-bucket",
        "key": "quarantine/synthetic.pdf",
        "versionId": "version-1",
    }

    invalid_events = [
        ({}, "EVENT_DETAIL_REQUIRED"),
        ({"source": "unknown", "detail-type": "unknown", "detail": {}}, "UNSUPPORTED_EVENT"),
        (
            {
                "source": "aws.guardduty",
                "detail-type": processor.GUARDDUTY_DETAIL_TYPE,
                "detail": {"schemaVersion": "2.0", "resourceType": "S3_OBJECT"},
            },
            "UNSUPPORTED_GUARDDUTY_SCHEMA",
        ),
        (
            {
                "source": "aws.guardduty",
                "detail-type": processor.GUARDDUTY_DETAIL_TYPE,
                "detail": {"schemaVersion": "1.0", "resourceType": "S3_OBJECT"},
            },
            "INVALID_GUARDDUTY_DETAIL",
        ),
        ({**complete, "detail": {"versionId": "version-1"}}, "OBJECT_REFERENCE_REQUIRED"),
        ({**complete, "detail": {**complete["detail"], "versionId": "null"}}, "VERSION_ID_REQUIRED"),
    ]
    for event, expected in invalid_events:
        with pytest.raises(processor.EventError, match=expected):
            processor.event_object(event)


def test_find_upload_decodes_keys_and_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_BUCKET", "source-bucket")
    upload = upload_record()

    class LookupTable:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self.items = items
            self.keys: list[str] = []

        def query(self, **kwargs: Any) -> dict[str, Any]:
            key = kwargs["ExpressionAttributeValues"][":pk"]
            self.keys.append(key)
            if key.endswith("quarantine/synthetic.pdf"):
                return {"Items": self.items}
            return {"Items": []}

    found = LookupTable([upload, {"entityType": "OTHER"}])
    monkeypatch.setattr(processor, "table", lambda: found)
    assert processor.find_upload("source-bucket", "quarantine%2Fsynthetic.pdf") == upload
    assert len(found.keys) == 2

    monkeypatch.setattr(processor, "table", lambda: LookupTable([]))
    with pytest.raises(processor.RetryableState, match="UPLOAD_GSI_NOT_VISIBLE"):
        processor.find_upload("source-bucket", "missing.pdf")

    other = {**upload, "SK": f"{upload['SK']}#other"}
    monkeypatch.setattr(processor, "table", lambda: LookupTable([upload, other]))
    with pytest.raises(processor.EventError, match="AMBIGUOUS_OBJECT_LOOKUP"):
        processor.find_upload("source-bucket", "quarantine/synthetic.pdf")

    with pytest.raises(processor.EventError, match="UNEXPECTED_SOURCE_BUCKET"):
        processor.find_upload("wrong-bucket", "quarantine/synthetic.pdf")


def test_load_scan_uses_a_consistent_version_specific_read(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = upload_record()
    calls: list[dict[str, Any]] = []

    def get_item(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"Item": {"scanVersionId": "version-1", "scanResultStatus": "NO_THREATS_FOUND"}}

    monkeypatch.setattr(
        processor,
        "table",
        lambda: SimpleNamespace(get_item=get_item),
    )
    scan = processor.load_scan(upload, "version-1")
    assert scan == {"scanVersionId": "version-1", "scanResultStatus": "NO_THREATS_FOUND"}
    assert calls == [
        {
            "Key": {
                "PK": upload["PK"],
                "SK": processor.scan_sort_key(upload["SK"], "version-1"),
            },
            "ConsistentRead": True,
        }
    ]


@pytest.mark.parametrize(
    ("scan_status", "incoming", "existing", "expected"),
    [
        ("COMPLETED", "NO_THREATS_FOUND", None, "NO_THREATS_FOUND"),
        ("STARTED", "NO_THREATS_FOUND", None, "FAILED"),
        ("COMPLETED", "UNKNOWN_RESULT", None, "FAILED"),
        ("COMPLETED", "THREATS_FOUND", "NO_THREATS_FOUND", "CONFLICT"),
    ],
)
def test_record_scan_normalizes_and_persists_versioned_results(
    monkeypatch: pytest.MonkeyPatch,
    scan_status: str,
    incoming: str,
    existing: str | None,
    expected: str,
) -> None:
    upload = upload_record()

    class ScanTable:
        def __init__(self) -> None:
            self.puts: list[dict[str, Any]] = []
            self.updates: list[dict[str, Any]] = []

        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            if existing is None:
                return {}
            return {"Item": {"scanResultStatus": existing}}

        def put_item(self, **kwargs: Any) -> None:
            self.puts.append(kwargs)

        def update_item(self, **kwargs: Any) -> None:
            self.updates.append(kwargs)

    fake = ScanTable()
    monkeypatch.setattr(processor, "table", lambda: fake)
    result = processor.record_scan(
        upload,
        {
            "scanResult": incoming,
            "scanStatus": scan_status,
            "versionId": "version-1",
            "eventId": "event-1",
            "eventTime": "2026-07-16T00:00:00Z",
        },
    )

    assert result["scanResultStatus"] == expected
    assert result["scanVersionId"] == "version-1"
    assert result["SK"] == processor.scan_sort_key(upload["SK"], "version-1")
    assert fake.puts[0]["Item"] == result
    assert fake.updates[0]["ExpressionAttributeValues"][":result"] == expected


def test_transition_failure_is_atomic_idempotent_and_race_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TABLE_NAME", "synthetic-table")
    upload = upload_record()

    class FakeDdb:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.calls: list[dict[str, Any]] = []

        def transact_write_items(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)
            if self.error:
                raise self.error

    successful = FakeDdb()
    monkeypatch.setattr(processor, "ddb_client", lambda: successful)
    result = processor.transition_failure(upload, "INVALID_PDF", "REJECTED", "version-1")
    assert result == {"status": "REJECTED", "failureCode": "INVALID_PDF", "uploadId": "upl_1"}
    assert len(successful.calls[0]["TransactItems"]) == 2

    with pytest.raises(ValueError, match="Invalid failure disposition"):
        processor.transition_failure(upload, "bad", "QUEUED", "version-1")

    canceled = FakeDdb(client_error("TransactionCanceledException"))
    monkeypatch.setattr(processor, "ddb_client", lambda: canceled)
    monkeypatch.setattr(
        processor,
        "table",
        lambda: SimpleNamespace(
            get_item=lambda **_kwargs: {
                "Item": {"status": "REJECTED", "failureCode": "INVALID_PDF"}
            }
        ),
    )
    assert processor.transition_failure(upload, "INVALID_PDF", "REJECTED", "version-1")["status"] == "REJECTED"

    monkeypatch.setattr(
        processor,
        "table",
        lambda: SimpleNamespace(get_item=lambda **_kwargs: {"Item": {"status": "VALIDATING"}}),
    )
    with pytest.raises(processor.RetryableState, match="FAILURE_TRANSITION_RACED"):
        processor.transition_failure(upload, "INVALID_PDF", "REJECTED", "version-1")

    monkeypatch.setattr(processor, "ddb_client", lambda: FakeDdb(client_error("AccessDeniedException")))
    with pytest.raises(processor.ClientError):
        processor.transition_failure(upload, "INVALID_PDF", "REJECTED", "version-1")


def test_scan_dispositions_are_safe() -> None:
    assert processor.disposition_for_scan("THREATS_FOUND") == ("REJECTED", "MALWARE_DETECTED")
    assert processor.disposition_for_scan("CONFLICT") == ("HOLD", "MALWARE_SCAN_RESULT_CONFLICT")
    assert processor.disposition_for_scan("") == ("HOLD", "MALWARE_SCAN_FAILED")


def test_validation_lease_handles_terminal_success_and_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = upload_record()

    class LeaseTable:
        def __init__(
            self,
            first: dict[str, Any],
            error: Exception | None = None,
            refreshed: dict[str, Any] | None = None,
        ) -> None:
            self.responses = [first, refreshed or first]
            self.error = error
            self.updates: list[dict[str, Any]] = []

        def get_item(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Item": self.responses.pop(0)}

        def update_item(self, **kwargs: Any) -> None:
            self.updates.append(kwargs)
            if self.error:
                raise self.error

    terminal = LeaseTable({"status": "QUEUED"})
    monkeypatch.setattr(processor, "table", lambda: terminal)
    assert processor.acquire_validation_lease(upload, "version-1") is None

    available = LeaseTable({"status": "VALIDATING"})
    monkeypatch.setattr(processor, "table", lambda: available)
    token = processor.acquire_validation_lease(upload, "version-1")
    assert token
    assert available.updates[0]["ExpressionAttributeValues"][":version"] == "version-1"

    condition = client_error("ConditionalCheckFailedException")
    completed = LeaseTable({"status": "VALIDATING"}, condition, {"status": "SUCCEEDED"})
    monkeypatch.setattr(processor, "table", lambda: completed)
    assert processor.acquire_validation_lease(upload, "version-1") is None

    busy = LeaseTable({"status": "VALIDATING"}, condition, {"status": "VALIDATING"})
    monkeypatch.setattr(processor, "table", lambda: busy)
    with pytest.raises(processor.RetryableState, match="VALIDATION_LEASE_BUSY"):
        processor.acquire_validation_lease(upload, "version-1")

    denied = LeaseTable({"status": "VALIDATING"}, client_error("AccessDeniedException"))
    monkeypatch.setattr(processor, "table", lambda: denied)
    with pytest.raises(processor.ClientError):
        processor.acquire_validation_lease(upload, "version-1")


def test_validate_exact_pdf_accepts_a_synthetic_kms_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = synthetic_pdf(pages=2)
    upload = upload_record(pdf)
    fake = ObjectS3(upload, pdf, chunks=[b"", pdf[:100], pdf[100:]])
    monkeypatch.setattr(processor, "s3_client", lambda: fake)

    assert processor.validate_exact_pdf(upload, "version-1") == 2
    assert fake.head_calls[0]["ChecksumMode"] == "ENABLED"
    assert fake.get_calls[0]["VersionId"] == "version-1"


@pytest.mark.parametrize(
    ("head_overrides", "code", "disposition"),
    [
        ({"VersionId": "other"}, "SOURCE_VERSION_MISMATCH", "HOLD"),
        ({"ContentLength": 0}, "SIZE_MISMATCH", "REJECTED"),
        ({"ContentType": "text/plain"}, "CONTENT_TYPE_MISMATCH", "REJECTED"),
        ({"ChecksumSHA256": "wrong"}, "S3_CHECKSUM_MISMATCH", "REJECTED"),
        ({"ServerSideEncryption": "AES256"}, "SOURCE_ENCRYPTION_REQUIRED", "HOLD"),
        ({"Metadata": {}}, "SOURCE_METADATA_MISMATCH", "HOLD"),
    ],
)
def test_validate_exact_pdf_rejects_untrusted_head_metadata(
    monkeypatch: pytest.MonkeyPatch,
    head_overrides: dict[str, Any],
    code: str,
    disposition: str,
) -> None:
    pdf = synthetic_pdf()
    upload = upload_record(pdf)
    monkeypatch.setattr(
        processor,
        "s3_client",
        lambda: ObjectS3(upload, pdf, head_overrides=head_overrides),
    )
    with pytest.raises(processor.ValidationFailure) as caught:
        processor.validate_exact_pdf(upload, "version-1")
    assert caught.value.code == code
    assert caught.value.disposition == disposition


@pytest.mark.parametrize(
    ("content", "chunks", "maximum_pages", "expected"),
    [
        (b"not-a-pdf", None, None, "INVALID_PDF_SIGNATURE"),
        (b"%PDF-not-valid", None, None, "INVALID_PDF"),
        (synthetic_pdf(pages=0), None, None, "EMPTY_PDF"),
        (synthetic_pdf(pages=2), None, "1", "PDF_PAGE_LIMIT_EXCEEDED"),
        (synthetic_pdf(password="synthetic-password"), None, None, "ENCRYPTED_PDF"),
    ],
)
def test_validate_exact_pdf_rejects_invalid_pdf_content(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    chunks: list[bytes] | None,
    maximum_pages: str | None,
    expected: str,
) -> None:
    upload = upload_record(content)
    if maximum_pages is not None:
        monkeypatch.setenv("MAXIMUM_PDF_PAGES", maximum_pages)
    monkeypatch.setattr(processor, "s3_client", lambda: ObjectS3(upload, content, chunks=chunks))
    with pytest.raises(processor.ValidationFailure) as caught:
        processor.validate_exact_pdf(upload, "version-1")
    assert caught.value.code == expected


def test_validate_exact_pdf_detects_stream_length_and_checksum_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = synthetic_pdf()
    upload = upload_record(pdf)

    too_short = ObjectS3(upload, pdf, chunks=[pdf[:-1]])
    monkeypatch.setattr(processor, "s3_client", lambda: too_short)
    with pytest.raises(processor.ValidationFailure, match="SIZE_MISMATCH"):
        processor.validate_exact_pdf(upload, "version-1")

    too_long = ObjectS3(upload, pdf, chunks=[pdf + b"x"])
    monkeypatch.setattr(processor, "s3_client", lambda: too_long)
    with pytest.raises(processor.ValidationFailure, match="SIZE_MISMATCH"):
        processor.validate_exact_pdf(upload, "version-1")

    changed = b"X" + pdf[1:]
    wrong_checksum = ObjectS3(upload, pdf, chunks=[changed])
    monkeypatch.setattr(processor, "s3_client", lambda: wrong_checksum)
    with pytest.raises(processor.ValidationFailure, match="BYTE_CHECKSUM_MISMATCH"):
        processor.validate_exact_pdf(upload, "version-1")


def test_stage_screening_input_copies_exact_version_with_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = upload_record()
    monkeypatch.setenv("IDP_INPUT_BUCKET", "idp-input")
    monkeypatch.setenv("SCREEN_CONFIG_SHA256", "a" * 64)
    monkeypatch.setenv("DATA_KEY_ARN", "arn:aws:kms:us-west-2:111122223333:key/synthetic")

    class CopyS3:
        def __init__(self, version: str | None) -> None:
            self.version = version
            self.calls: list[dict[str, Any]] = []

        def copy_object(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"VersionId": self.version} if self.version else {}

    fake = CopyS3("staged-version")
    monkeypatch.setattr(processor, "s3_client", lambda: fake)
    staged = processor.stage_screening_input(upload, "version-1")
    assert staged == {
        "bucket": "idp-input",
        "key": "screen/run_1/doc_1/upl_1.pdf",
        "versionId": "staged-version",
    }
    call = fake.calls[0]
    assert call["CopySource"]["VersionId"] == "version-1"
    assert call["Metadata"]["pipeline-stage"] == "screen"
    assert call["Metadata"]["source-version-id-b64"] == processor.b64url("version-1")
    assert call["SSEKMSKeyId"].endswith("synthetic")

    monkeypatch.setattr(processor, "s3_client", lambda: CopyS3(None))
    with pytest.raises(RuntimeError, match="Versioning enabled"):
        processor.stage_screening_input(upload, "version-1")


def test_commit_queued_is_atomic_and_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABLE_NAME", "synthetic-table")
    monkeypatch.setenv("SCREEN_CONFIG_SHA256", "b" * 64)
    upload = upload_record()
    staged = {"bucket": "idp-input", "key": "screen/run/doc.pdf", "versionId": "staged-v1"}

    class FakeDdb:
        def __init__(self, error: Exception | None = None) -> None:
            self.error = error
            self.calls: list[dict[str, Any]] = []

        def transact_write_items(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)
            if self.error:
                raise self.error

    ddb = FakeDdb()
    monkeypatch.setattr(processor, "ddb_client", lambda: ddb)
    processor.commit_queued(upload, "version-1", "lease-1", 2, staged)
    assert len(ddb.calls[0]["TransactItems"]) == 2

    canceled = FakeDdb(client_error("TransactionCanceledException"))
    monkeypatch.setattr(processor, "ddb_client", lambda: canceled)
    monkeypatch.setattr(
        processor,
        "table",
        lambda: SimpleNamespace(
            get_item=lambda **_kwargs: {
                "Item": {"status": "QUEUED", "screenInputVersionId": "staged-v1"}
            }
        ),
    )
    processor.commit_queued(upload, "version-1", "lease-1", 2, staged)

    monkeypatch.setattr(
        processor,
        "table",
        lambda: SimpleNamespace(get_item=lambda **_kwargs: {"Item": {"status": "VALIDATING"}}),
    )
    with pytest.raises(processor.RetryableState, match="QUEUE_TRANSITION_RACED"):
        processor.commit_queued(upload, "version-1", "lease-1", 2, staged)

    monkeypatch.setattr(processor, "ddb_client", lambda: FakeDdb(client_error("AccessDeniedException")))
    with pytest.raises(processor.ClientError):
        processor.commit_queued(upload, "version-1", "lease-1", 2, staged)


class ReconcileTable:
    def __init__(self, upload: dict[str, Any], refreshed: dict[str, Any] | None = None) -> None:
        self.upload = upload
        self.refreshed = refreshed or upload
        self.get_count = 0
        self.updates: list[dict[str, Any]] = []

    def get_item(self, **_kwargs: Any) -> dict[str, Any]:
        self.get_count += 1
        return {"Item": self.upload if self.get_count == 1 else self.refreshed}

    def update_item(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


def test_reconcile_waits_for_scan_and_client_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = upload_record()
    fake = ReconcileTable(upload)
    monkeypatch.setattr(processor, "table", lambda: fake)
    monkeypatch.setattr(processor, "load_scan", lambda *_args: None)
    assert processor.reconcile(upload, "version-1")["status"] == "WAITING_FOR_SCAN"

    waiting = upload_record(sourceVersionId=None, clientCompletedAt=None)
    fake = ReconcileTable(waiting)
    monkeypatch.setattr(processor, "table", lambda: fake)
    monkeypatch.setattr(
        processor,
        "load_scan",
        lambda *_args: {"scanResultStatus": "NO_THREATS_FOUND", "scanVersionId": "version-1"},
    )
    result = processor.reconcile(waiting, "version-1")
    assert result["status"] == "WAITING_FOR_CLIENT_COMPLETE"
    assert fake.updates[0]["ExpressionAttributeValues"][":version"] == "version-1"


def test_reconcile_blocks_nonclean_and_validation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = upload_record()
    monkeypatch.setattr(processor, "table", lambda: ReconcileTable(upload))
    monkeypatch.setattr(
        processor,
        "load_scan",
        lambda *_args: {"scanResultStatus": "THREATS_FOUND", "scanVersionId": "version-1"},
    )
    blocked: list[tuple[str, str, str]] = []

    def block(_upload: dict[str, Any], code: str, disposition: str, version: str):
        blocked.append((code, disposition, version))
        return {"status": disposition, "failureCode": code}

    monkeypatch.setattr(processor, "transition_failure", block)
    assert processor.reconcile(upload, "version-1") == {
        "status": "REJECTED",
        "failureCode": "MALWARE_DETECTED",
    }

    monkeypatch.setattr(
        processor,
        "load_scan",
        lambda *_args: {"scanResultStatus": "NO_THREATS_FOUND", "scanVersionId": "version-1"},
    )
    monkeypatch.setattr(processor, "acquire_validation_lease", lambda *_args: "lease-1")

    def reject_pdf(*_args: Any) -> int:
        raise processor.ValidationFailure("INVALID_PDF")

    monkeypatch.setattr(processor, "validate_exact_pdf", reject_pdf)
    assert processor.reconcile(upload, "version-1") == {
        "status": "REJECTED",
        "failureCode": "INVALID_PDF",
    }
    assert blocked[-1] == ("INVALID_PDF", "REJECTED", "version-1")


def test_reconcile_returns_terminal_or_queues_exact_version(monkeypatch: pytest.MonkeyPatch) -> None:
    upload = upload_record()
    scan = {"scanResultStatus": "NO_THREATS_FOUND", "scanVersionId": "version-1"}
    monkeypatch.setattr(processor, "load_scan", lambda *_args: scan)

    fake = ReconcileTable(upload, {"status": "SUCCEEDED"})
    monkeypatch.setattr(processor, "table", lambda: fake)
    monkeypatch.setattr(processor, "acquire_validation_lease", lambda *_args: None)
    assert processor.reconcile(upload, "version-1") == {"status": "SUCCEEDED", "uploadId": "upl_1"}

    fake = ReconcileTable(upload)
    monkeypatch.setattr(processor, "table", lambda: fake)
    monkeypatch.setattr(processor, "acquire_validation_lease", lambda *_args: "lease-1")
    monkeypatch.setattr(processor, "validate_exact_pdf", lambda *_args: 3)
    staged = {"bucket": "idp-input", "key": "screen/run/doc.pdf", "versionId": "staged-v1"}
    monkeypatch.setattr(processor, "stage_screening_input", lambda *_args: staged)
    commits: list[tuple[Any, ...]] = []
    monkeypatch.setattr(processor, "commit_queued", lambda *args: commits.append(args))
    assert processor.reconcile(upload, "version-1") == {
        "status": "QUEUED",
        "uploadId": "upl_1",
        "documentId": "doc_1",
        "processingExecutionId": "run_1",
        "pageCount": 3,
    }
    assert commits[0][1:] == ("version-1", "lease-1", 3, staged)


def test_handler_routes_guardduty_and_client_events_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upload = upload_record(sourceVersionId=None)
    normalized = {
        "kind": "guardduty",
        "bucket": "source-bucket",
        "key": "quarantine/synthetic.pdf",
        "versionId": "version-1",
    }
    monkeypatch.setattr(processor, "event_object", lambda _event: normalized)
    monkeypatch.setattr(processor, "find_upload", lambda *_args: upload)
    monkeypatch.setattr(
        processor,
        "record_scan",
        lambda *_args: {"scanResultStatus": "THREATS_FOUND"},
    )
    monkeypatch.setattr(
        processor,
        "transition_failure",
        lambda _upload, code, disposition, version: {
            "status": disposition,
            "failureCode": code,
            "versionId": version,
        },
    )
    assert processor.handler({}, None) == {
        "status": "REJECTED",
        "failureCode": "MALWARE_DETECTED",
        "versionId": "version-1",
    }

    monkeypatch.setattr(
        processor,
        "record_scan",
        lambda *_args: {"scanResultStatus": "NO_THREATS_FOUND"},
    )
    monkeypatch.setattr(
        processor,
        "reconcile",
        lambda _upload, version: {"status": "WAITING_FOR_CLIENT_COMPLETE", "versionId": version},
    )
    assert processor.handler({}, None) == {
        "status": "WAITING_FOR_CLIENT_COMPLETE",
        "versionId": "version-1",
    }

    normalized["kind"] = "client-complete"
    monkeypatch.setattr(
        processor,
        "reconcile",
        lambda _upload, version: {"status": "WAITING_FOR_SCAN", "versionId": version},
    )
    assert processor.handler({}, None) == {"status": "WAITING_FOR_SCAN", "versionId": "version-1"}


def test_stream_handler_reconciles_completion_and_scan_changes_but_ignores_lease_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = upload_record()
    awaiting = dict(completed)
    awaiting["status"] = "AWAITING_UPLOAD"
    awaiting.pop("clientCompletedAt")
    awaiting.pop("sourceVersionId")

    scanned = {
        **completed,
        "malwareScanStatus": "NO_THREATS_FOUND",
        "malwareScanVersionId": "version-1",
    }
    lease_only = {**scanned, "processorLeaseToken": "lease-1"}
    calls: list[tuple[dict[str, Any], str]] = []
    monkeypatch.setattr(
        processor,
        "reconcile",
        lambda upload, version: calls.append((upload, version)) or {"status": "QUEUED"},
    )

    result = processor.handler(
        {
            "Records": [
                stream_record("completion", completed, awaiting),
                stream_record("scan", scanned, completed),
                stream_record("lease", lease_only, scanned),
            ]
        },
        None,
    )

    assert result == {"batchItemFailures": []}
    assert [version for _upload, version in calls] == ["version-1", "version-1"]
    assert all(upload["PK"] == completed["PK"] for upload, _version in calls)


def test_stream_handler_returns_only_failed_sequence_identifiers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    completed = upload_record(fileName="sensitive-closing-disclosure.pdf")
    awaiting = dict(completed)
    awaiting["status"] = "AWAITING_UPLOAD"
    awaiting.pop("clientCompletedAt")
    awaiting.pop("sourceVersionId")

    def retry(_upload: dict[str, Any], _version: str) -> None:
        raise processor.RetryableState("synthetic retry")

    monkeypatch.setattr(processor, "reconcile", retry)
    with caplog.at_level("WARNING"):
        result = processor.handler(
            {"Records": [stream_record("sequence-17", completed, awaiting)]},
            None,
        )

    assert result == {"batchItemFailures": [{"itemIdentifier": "sequence-17"}]}
    assert "sensitive-closing-disclosure.pdf" not in caplog.text
    assert "synthetic retry" not in caplog.text
