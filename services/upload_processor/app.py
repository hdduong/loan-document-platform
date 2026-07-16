"""Reconcile upload completion with GuardDuty and queue an exact PDF version.

This Lambda accepts two at-least-once event sources:

* ``GuardDuty Malware Protection Object Scan Result`` events; and
* upload-record changes from the DynamoDB Stream.

The loan API also emits a synthetic ``Client Upload Complete`` event as a
latency optimization. The stream is the durable handoff if that direct invoke
is interrupted after the completion transaction commits.

The two signals may arrive in either order.  A GuardDuty result is stored under
the upload using a hash of the S3 VersionId, so a result for one version can
never authorize a different version of the same key.  Only the exact version
declared by the completion API and scanned ``NO_THREATS_FOUND`` is validated
and copied to the IDP input bucket.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote_plus

try:  # AWS supplies these in Lambda; optional imports keep pure unit tests light.
    import boto3
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - exercised only outside Lambda
    boto3 = None  # type: ignore[assignment]

    class ClientError(Exception):
        """Fallback used only while importing pure helpers without boto3."""


LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

GUARDDUTY_DETAIL_TYPE = "GuardDuty Malware Protection Object Scan Result"
CLIENT_COMPLETE_DETAIL_TYPE = "Client Upload Complete"
CLEAN_SCAN_RESULT = "NO_THREATS_FOUND"
THREAT_SCAN_RESULT = "THREATS_FOUND"
TERMINAL_UPLOAD_STATUSES = {"QUEUED", "EXTRACTING", "SUCCEEDED", "REJECTED", "HOLD", "FAILED"}
STREAM_SIGNAL_FIELDS = (
    "status",
    "clientCompletedAt",
    "sourceVersionId",
    "malwareScanStatus",
    "malwareScanVersionId",
)

_AWS: dict[str, Any] = {}


class EventError(ValueError):
    """The event is permanently malformed and should go to the DLQ."""


class ValidationFailure(ValueError):
    """The uploaded object is permanently unsafe or invalid."""

    def __init__(self, code: str, disposition: str = "REJECTED") -> None:
        super().__init__(code)
        self.code = code
        self.disposition = disposition


class RetryableState(RuntimeError):
    """A required eventually-consistent record or expired lease is pending."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def aws_resource(name: str) -> Any:
    if boto3 is None:  # pragma: no cover - defensive outside AWS/test doubles
        raise RuntimeError("boto3 is required for AWS operations")
    cache_key = f"resource:{name}"
    if cache_key not in _AWS:
        _AWS[cache_key] = boto3.resource(name)
    return _AWS[cache_key]


def aws_client(name: str) -> Any:
    if boto3 is None:  # pragma: no cover
        raise RuntimeError("boto3 is required for AWS operations")
    cache_key = f"client:{name}"
    if cache_key not in _AWS:
        _AWS[cache_key] = boto3.client(name)
    return _AWS[cache_key]


def table() -> Any:
    cache_key = "table"
    if cache_key not in _AWS:
        _AWS[cache_key] = aws_resource("dynamodb").Table(required_env("TABLE_NAME"))
    return _AWS[cache_key]


def ddb_client() -> Any:
    return aws_client("dynamodb")


def s3_client() -> Any:
    return aws_client("s3")


def serialize_map(values: dict[str, Any]) -> dict[str, Any]:
    # Imported lazily so selector/unit tests do not need the AWS SDK installed.
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {name: serializer.serialize(value) for name, value in values.items()}


def deserialize_stream_image(image: Any) -> dict[str, Any]:
    """Deserialize one DynamoDB Streams image and reject malformed records."""

    if not isinstance(image, dict):
        raise EventError("DYNAMODB_STREAM_IMAGE_REQUIRED")
    from boto3.dynamodb.types import TypeDeserializer

    deserializer = TypeDeserializer()
    try:
        return {str(name): deserializer.deserialize(value) for name, value in image.items()}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise EventError("DYNAMODB_STREAM_IMAGE_INVALID") from exc


def stream_record_signal(record: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    """Return a changed VALIDATING upload and its exact source version."""

    if record.get("eventSource") != "aws:dynamodb":
        raise EventError("UNSUPPORTED_STREAM_SOURCE")
    if record.get("eventName") not in {"INSERT", "MODIFY"}:
        return None
    stream = record.get("dynamodb")
    if not isinstance(stream, dict):
        raise EventError("DYNAMODB_STREAM_DETAIL_REQUIRED")
    current = deserialize_stream_image(stream.get("NewImage"))
    if current.get("entityType") != "UPLOAD" or current.get("status") != "VALIDATING":
        return None
    previous = deserialize_stream_image(stream.get("OldImage") or {})
    if not any(previous.get(name) != current.get(name) for name in STREAM_SIGNAL_FIELDS):
        return None
    for name in ("PK", "SK", "uploadId", "clientCompletedAt", "sourceVersionId"):
        if not current.get(name):
            raise EventError("VALIDATING_UPLOAD_STREAM_RECORD_INVALID")
    return current, str(current["sourceVersionId"])


def handle_stream_event(event: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Reconcile a DynamoDB batch with partial failures and safe logging."""

    records = event.get("Records")
    if not isinstance(records, list) or not records:
        raise EventError("DYNAMODB_STREAM_RECORDS_REQUIRED")
    failures: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise EventError("DYNAMODB_STREAM_RECORD_INVALID")
        event_id = str(record.get("eventID", ""))
        if not event_id:
            raise EventError("DYNAMODB_STREAM_EVENT_ID_REQUIRED")
        stream = record.get("dynamodb")
        if not isinstance(stream, dict) or not stream.get("SequenceNumber"):
            raise EventError("DYNAMODB_STREAM_SEQUENCE_NUMBER_REQUIRED")
        sequence_number = str(stream["SequenceNumber"])
        try:
            signal = stream_record_signal(record)
            if signal is not None:
                reconcile(*signal)
        except Exception as exc:
            # The item identifier and exception type are sufficient for
            # operations; the stream image can contain sensitive filenames.
            LOGGER.warning(
                "upload_stream_record_failed event_id=%s error_type=%s",
                event_id,
                type(exc).__name__,
            )
            # Lambda expects the DynamoDB sequence number here, not eventID.
            failures.append({"itemIdentifier": sequence_number})
    return {"batchItemFailures": failures}


def event_object(event: dict[str, Any]) -> dict[str, str]:
    """Normalize an accepted event without trusting its object version implicitly."""

    detail_type = str(event.get("detail-type", ""))
    detail = event.get("detail")
    if not isinstance(detail, dict):
        raise EventError("EVENT_DETAIL_REQUIRED")

    if detail_type == GUARDDUTY_DETAIL_TYPE and event.get("source") == "aws.guardduty":
        if detail.get("schemaVersion") != "1.0" or detail.get("resourceType") != "S3_OBJECT":
            raise EventError("UNSUPPORTED_GUARDDUTY_SCHEMA")
        object_detail = detail.get("s3ObjectDetails")
        result_detail = detail.get("scanResultDetails")
        if not isinstance(object_detail, dict) or not isinstance(result_detail, dict):
            raise EventError("INVALID_GUARDDUTY_DETAIL")
        normalized = {
            "kind": "guardduty",
            "bucket": str(object_detail.get("bucketName", "")),
            "key": str(object_detail.get("objectKey", "")),
            "versionId": str(object_detail.get("versionId", "")),
            "scanStatus": str(detail.get("scanStatus", "")),
            "scanResult": str(result_detail.get("scanResultStatus", "")),
            "eventId": str(event.get("id", "")),
            "eventTime": str(event.get("time", "")) or utc_now(),
        }
    elif detail_type == CLIENT_COMPLETE_DETAIL_TYPE and event.get("source") == "loan-api":
        normalized = {
            "kind": "client-complete",
            "bucket": str(detail.get("bucketName", "")),
            "key": str(detail.get("objectKey", "")),
            "versionId": str(detail.get("versionId", "")),
        }
    else:
        raise EventError("UNSUPPORTED_EVENT")

    if not normalized["bucket"] or not normalized["key"]:
        raise EventError("OBJECT_REFERENCE_REQUIRED")
    if not normalized["versionId"] or normalized["versionId"] == "null":
        raise EventError("VERSION_ID_REQUIRED")
    return normalized


def find_upload(bucket: str, key: str) -> dict[str, Any]:
    expected_source_bucket = required_env("SOURCE_BUCKET")
    if bucket != expected_source_bucket:
        raise EventError("UNEXPECTED_SOURCE_BUCKET")

    # EventBridge object keys can be URL encoded.  Our generated keys contain no
    # literal '+' characters, so trying the decoded form is unambiguous.
    candidate_keys = [key]
    decoded = unquote_plus(key)
    if decoded != key:
        candidate_keys.append(decoded)

    matches: list[dict[str, Any]] = []
    for candidate in candidate_keys:
        response = table().query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"OBJECT#{bucket}#{candidate}"},
        )
        matches.extend(item for item in response.get("Items", []) if item.get("entityType") == "UPLOAD")
        if matches:
            break

    unique = {(item["PK"], item["SK"]): item for item in matches}
    if not unique:
        # A just-created GSI projection can lag the base table.  Raising makes
        # EventBridge/Lambda async retry instead of silently losing the signal.
        raise RetryableState("UPLOAD_GSI_NOT_VISIBLE")
    if len(unique) != 1:
        raise EventError("AMBIGUOUS_OBJECT_LOOKUP")
    return next(iter(unique.values()))


def scan_sort_key(upload_sk: str, version_id: str) -> str:
    digest = hashlib.sha256(version_id.encode("utf-8")).hexdigest()[:32]
    return f"{upload_sk}#SCAN#{digest}"


def load_scan(upload: dict[str, Any], version_id: str) -> dict[str, Any] | None:
    return table().get_item(
        Key={"PK": upload["PK"], "SK": scan_sort_key(upload["SK"], version_id)},
        ConsistentRead=True,
    ).get("Item")


def record_scan(upload: dict[str, Any], normalized: dict[str, str]) -> dict[str, Any]:
    """Persist one immutable-version scan; conflicting duplicate results fail closed."""

    result = normalized["scanResult"]
    scan_status = normalized["scanStatus"]
    if result not in {CLEAN_SCAN_RESULT, THREAT_SCAN_RESULT, "UNSUPPORTED", "ACCESS_DENIED", "FAILED"}:
        result = "FAILED"
    if scan_status != "COMPLETED" and result == CLEAN_SCAN_RESULT:
        # A clean result is authoritative only for a completed scan.
        result = "FAILED"

    key = {"PK": upload["PK"], "SK": scan_sort_key(upload["SK"], normalized["versionId"])}
    existing = table().get_item(Key=key, ConsistentRead=True).get("Item")
    if existing and existing.get("scanResultStatus") != result:
        result = "CONFLICT"

    item = {
        **key,
        "entityType": "MALWARE_SCAN",
        "uploadId": upload["uploadId"],
        "scanVersionId": normalized["versionId"],
        "scanResultStatus": result,
        "guardDutyScanStatus": scan_status,
        "eventId": normalized.get("eventId", ""),
        "scannedAt": normalized.get("eventTime", "") or utc_now(),
        "updatedAt": utc_now(),
    }
    table().put_item(Item=item)

    # This summary lets the API trigger reconciliation when the clean scan is
    # observed before /complete.  Authorization still uses the versioned record.
    table().update_item(
        Key={"PK": upload["PK"], "SK": upload["SK"]},
        UpdateExpression=(
            "SET malwareScanStatus=:result, malwareScanVersionId=:version, "
            "malwareScanEventId=:event, malwareScannedAt=:scanned, updatedAt=:now"
        ),
        ConditionExpression="attribute_exists(PK)",
        ExpressionAttributeValues={
            ":result": result,
            ":version": normalized["versionId"],
            ":event": normalized.get("eventId", ""),
            ":scanned": normalized.get("eventTime", "") or utc_now(),
            ":now": utc_now(),
        },
    )
    return item


def document_key(upload: dict[str, Any]) -> dict[str, str]:
    return {
        "PK": upload["PK"],
        "SK": f"INSTANCE#{upload['loanInstanceId']}#DOC#{upload['documentId']}",
    }


def transition_failure(upload: dict[str, Any], code: str, disposition: str, version_id: str) -> dict[str, Any]:
    """Atomically fail/hold the current upload and its logical document."""

    if disposition not in {"REJECTED", "HOLD", "FAILED"}:
        raise ValueError("Invalid failure disposition")
    now = utc_now()
    upload_values = serialize_map(
        {":status": disposition, ":code": code, ":now": now, ":version": version_id}
    )
    document_values = serialize_map(
        {":status": disposition, ":code": code, ":now": now, ":upload": upload["uploadId"]}
    )
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map({"PK": upload["PK"], "SK": upload["SK"]}),
                "UpdateExpression": (
                    "SET #status=:status, failureCode=:code, updatedAt=:now, "
                    "malwareScanVersionId=if_not_exists(malwareScanVersionId,:version) "
                    "REMOVE processorLeaseToken, processorLeaseExpiresAt"
                ),
                "ConditionExpression": "attribute_exists(PK)",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": upload_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(document_key(upload)),
                "UpdateExpression": "SET #status=:status, failureCode=:code, updatedAt=:now",
                "ConditionExpression": "currentUploadId=:upload",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": document_values,
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        current = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item", {})
        if current.get("status") != disposition or current.get("failureCode") != code:
            raise RetryableState("FAILURE_TRANSITION_RACED") from exc
    LOGGER.warning(
        "upload_blocked disposition=%s code=%s upload_id=%s",
        disposition,
        code,
        upload.get("uploadId"),
    )
    return {"status": disposition, "failureCode": code, "uploadId": upload["uploadId"]}


def disposition_for_scan(scan_result: str) -> tuple[str, str]:
    if scan_result == THREAT_SCAN_RESULT:
        return "REJECTED", "MALWARE_DETECTED"
    if scan_result == "CONFLICT":
        return "HOLD", "MALWARE_SCAN_RESULT_CONFLICT"
    return "HOLD", f"MALWARE_SCAN_{scan_result or 'FAILED'}"


def reconciliation_action(
    source_version_id: str | None,
    client_completed: bool,
    scan: dict[str, Any] | None,
) -> str:
    """Pure state decision used by both event orders and focused unit tests."""

    if not scan:
        return "WAITING_FOR_SCAN"
    result = str(scan.get("scanResultStatus", "FAILED"))
    if result == THREAT_SCAN_RESULT:
        return "REJECTED"
    if result != CLEAN_SCAN_RESULT:
        return "HOLD"
    if not client_completed or not source_version_id:
        return "WAITING_FOR_CLIENT_COMPLETE"
    if str(scan.get("scanVersionId", "")) != source_version_id:
        return "WAITING_FOR_EXACT_SCAN"
    return "VALIDATE"


def acquire_validation_lease(upload: dict[str, Any], version_id: str) -> str | None:
    current = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item", {})
    if current.get("status") in TERMINAL_UPLOAD_STATUSES:
        return None
    token = str(uuid.uuid4())
    now_epoch = int(time.time())
    try:
        table().update_item(
            Key={"PK": upload["PK"], "SK": upload["SK"]},
            UpdateExpression="SET processorLeaseToken=:token, processorLeaseExpiresAt=:expires, updatedAt=:now",
            ConditionExpression=(
                "#status=:validating AND attribute_exists(clientCompletedAt) AND sourceVersionId=:version "
                "AND (attribute_not_exists(processorLeaseExpiresAt) OR processorLeaseExpiresAt < :epoch)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":token": token,
                ":expires": now_epoch + 300,
                ":epoch": now_epoch,
                ":now": utc_now(),
                ":validating": "VALIDATING",
                ":version": version_id,
            },
        )
        return token
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        refreshed = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item", {})
        if refreshed.get("status") in TERMINAL_UPLOAD_STATUSES:
            return None
        raise RetryableState("VALIDATION_LEASE_BUSY") from exc


def validate_exact_pdf(upload: dict[str, Any], version_id: str) -> int:
    """Re-hash and strictly parse the exact source version; return page count."""

    s3 = s3_client()
    head = s3.head_object(
        Bucket=upload["sourceBucket"],
        Key=upload["sourceKey"],
        VersionId=version_id,
        ChecksumMode="ENABLED",
    )
    if head.get("VersionId") != version_id:
        raise ValidationFailure("SOURCE_VERSION_MISMATCH", "HOLD")
    if int(head.get("ContentLength", -1)) != int(upload["sizeBytes"]):
        raise ValidationFailure("SIZE_MISMATCH")
    if head.get("ContentType") != "application/pdf":
        raise ValidationFailure("CONTENT_TYPE_MISMATCH")
    if head.get("ChecksumSHA256") != upload["checksumSha256"]:
        raise ValidationFailure("S3_CHECKSUM_MISMATCH")
    if head.get("ServerSideEncryption") != "aws:kms":
        raise ValidationFailure("SOURCE_ENCRYPTION_REQUIRED", "HOLD")

    metadata = head.get("Metadata") or {}
    expected_metadata = {
        "document-id": upload["documentId"],
        "upload-id": upload["uploadId"],
        "loan-instance-id": upload["loanInstanceId"],
    }
    if any(metadata.get(name) != value for name, value in expected_metadata.items()):
        raise ValidationFailure("SOURCE_METADATA_MISMATCH", "HOLD")

    maximum_bytes = int(os.environ.get("MAXIMUM_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    body = s3.get_object(Bucket=upload["sourceBucket"], Key=upload["sourceKey"], VersionId=version_id)["Body"]
    digest = hashlib.sha256()
    total = 0
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as pdf_file:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > maximum_bytes or total > int(upload["sizeBytes"]):
                raise ValidationFailure("SIZE_MISMATCH")
            digest.update(chunk)
            pdf_file.write(chunk)
        if total != int(upload["sizeBytes"]):
            raise ValidationFailure("SIZE_MISMATCH")
        actual_checksum = base64.b64encode(digest.digest()).decode("ascii")
        if actual_checksum != upload["checksumSha256"]:
            raise ValidationFailure("BYTE_CHECKSUM_MISMATCH")
        pdf_file.seek(0)
        if pdf_file.read(5) != b"%PDF-":
            raise ValidationFailure("INVALID_PDF_SIGNATURE")
        pdf_file.seek(0)
        try:
            from pypdf import PdfReader
            from pypdf.errors import PdfReadError

            reader = PdfReader(pdf_file, strict=True)
            if reader.is_encrypted:
                raise ValidationFailure("ENCRYPTED_PDF")
            page_count = len(reader.pages)
        except ValidationFailure:
            raise
        except (PdfReadError, ValueError, TypeError, KeyError, OSError) as exc:
            raise ValidationFailure("INVALID_PDF") from exc

    maximum_pages = int(os.environ.get("MAXIMUM_PDF_PAGES", "250"))
    if page_count < 1:
        raise ValidationFailure("EMPTY_PDF")
    if page_count > maximum_pages:
        raise ValidationFailure("PDF_PAGE_LIMIT_EXCEEDED")
    return page_count


def b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def stage_screening_input(upload: dict[str, Any], version_id: str) -> dict[str, str]:
    processing_id = upload["processingExecutionId"]
    bucket = required_env("IDP_INPUT_BUCKET")
    key = f"screen/{processing_id}/{upload['documentId']}/{upload['uploadId']}.pdf"
    metadata = {
        "config-version": os.environ.get("SCREEN_CONFIG_VERSION", "cd-screen-v1"),
        "config-sha256": required_env("SCREEN_CONFIG_SHA256"),
        "pipeline-stage": "screen",
        "selector-rule-version": os.environ.get("SELECTOR_RULE_VERSION", "cd-selection-v1"),
        "processing-execution-id": processing_id,
        "loan-pk-b64": b64url(upload["PK"]),
        "upload-sk-b64": b64url(upload["SK"]),
        "document-sk-b64": b64url(document_key(upload)["SK"]),
        "source-version-id-b64": b64url(version_id),
    }
    response = s3_client().copy_object(
        Bucket=bucket,
        Key=key,
        CopySource={"Bucket": upload["sourceBucket"], "Key": upload["sourceKey"], "VersionId": version_id},
        Metadata=metadata,
        MetadataDirective="REPLACE",
        ContentType="application/pdf",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ.get("IDP_INPUT_KEY_ARN") or required_env("DATA_KEY_ARN"),
        ChecksumAlgorithm="SHA256",
    )
    destination_version = response.get("VersionId")
    if not destination_version:
        raise RuntimeError("IDP input bucket must have S3 Versioning enabled")
    return {"bucket": bucket, "key": key, "versionId": destination_version}


def commit_queued(upload: dict[str, Any], version_id: str, lease_token: str, page_count: int, staged: dict[str, str]) -> None:
    now = utc_now()
    values = {
        ":queued": "QUEUED",
        ":validating": "VALIDATING",
        ":clean": CLEAN_SCAN_RESULT,
        ":now": now,
        ":version": version_id,
        ":lease": lease_token,
        ":upload": upload["uploadId"],
        ":run": upload["processingExecutionId"],
        ":pageCount": page_count,
        ":sourceBucket": upload["sourceBucket"],
        ":sourceKey": upload["sourceKey"],
        ":checksum": upload["checksumSha256"],
        ":screenBucket": staged["bucket"],
        ":screenKey": staged["key"],
        ":screenVersion": staged["versionId"],
        ":screenConfig": os.environ.get("SCREEN_CONFIG_VERSION", "cd-screen-v1"),
        ":screenHash": required_env("SCREEN_CONFIG_SHA256"),
        ":selector": os.environ.get("SELECTOR_RULE_VERSION", "cd-selection-v1"),
    }
    upload_values = serialize_map(
        {
            key: values[key]
            for key in (
                ":queued",
                ":validating",
                ":clean",
                ":now",
                ":version",
                ":lease",
                ":pageCount",
                ":screenBucket",
                ":screenKey",
                ":screenVersion",
                ":screenConfig",
                ":screenHash",
                ":selector",
            )
        }
    )
    document_values = serialize_map(
        {
            key: values[key]
            for key in (
                ":queued",
                ":validating",
                ":now",
                ":version",
                ":upload",
                ":run",
                ":pageCount",
                ":sourceBucket",
                ":sourceKey",
                ":checksum",
                ":screenBucket",
                ":screenKey",
                ":screenVersion",
                ":screenConfig",
                ":screenHash",
                ":selector",
            )
        }
    )
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map({"PK": upload["PK"], "SK": upload["SK"]}),
                "UpdateExpression": (
                    "SET #status=:queued, updatedAt=:now, validatedAt=:now, pageCount=:pageCount, "
                    "screenInputBucket=:screenBucket, screenInputKey=:screenKey, "
                    "screenInputVersionId=:screenVersion, screenConfigVersion=:screenConfig, "
                    "screenConfigSha256=:screenHash, selectorRuleVersion=:selector "
                    "REMOVE processorLeaseToken, processorLeaseExpiresAt, failureCode"
                ),
                "ConditionExpression": (
                    "#status=:validating AND sourceVersionId=:version AND malwareScanStatus=:clean "
                    "AND processorLeaseToken=:lease"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": upload_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(document_key(upload)),
                "UpdateExpression": (
                    "SET #status=:queued, updatedAt=:now, sourceBucket=:sourceBucket, sourceKey=:sourceKey, "
                    "sourceVersionId=:version, sourceChecksumSha256=:checksum, pageCount=:pageCount, "
                    "screenInputBucket=:screenBucket, screenInputKey=:screenKey, "
                    "screenInputVersionId=:screenVersion, screenConfigVersion=:screenConfig, "
                    "screenConfigSha256=:screenHash, selectorRuleVersion=:selector REMOVE failureCode"
                ),
                "ConditionExpression": "currentUploadId=:upload AND processingExecutionId=:run AND #status=:validating",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": document_values,
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        current = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item", {})
        if current.get("status") != "QUEUED" or current.get("screenInputVersionId") != staged["versionId"]:
            raise RetryableState("QUEUE_TRANSITION_RACED") from exc


def reconcile(upload: dict[str, Any], requested_version_id: str) -> dict[str, Any]:
    upload = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item") or upload
    exact_version = str(upload.get("sourceVersionId") or requested_version_id)
    scan = load_scan(upload, exact_version)
    if not scan:
        return {"status": "WAITING_FOR_SCAN", "uploadId": upload["uploadId"], "versionId": exact_version}

    scan_result = str(scan.get("scanResultStatus", "FAILED"))
    # Keep the summary aligned to the exact version selected by /complete.
    table().update_item(
        Key={"PK": upload["PK"], "SK": upload["SK"]},
        UpdateExpression="SET malwareScanStatus=:result, malwareScanVersionId=:version, updatedAt=:now",
        ExpressionAttributeValues={":result": scan_result, ":version": exact_version, ":now": utc_now()},
    )
    upload["malwareScanStatus"] = scan_result
    upload["malwareScanVersionId"] = exact_version

    if scan_result != CLEAN_SCAN_RESULT:
        disposition, code = disposition_for_scan(scan_result)
        return transition_failure(upload, code, disposition, exact_version)
    if not upload.get("clientCompletedAt") or not upload.get("sourceVersionId"):
        return {"status": "WAITING_FOR_CLIENT_COMPLETE", "uploadId": upload["uploadId"], "versionId": exact_version}
    if upload["sourceVersionId"] != exact_version:
        raise RetryableState("SOURCE_VERSION_CHANGED")

    lease_token = acquire_validation_lease(upload, exact_version)
    if lease_token is None:
        refreshed = table().get_item(Key={"PK": upload["PK"], "SK": upload["SK"]}, ConsistentRead=True).get("Item", {})
        return {"status": refreshed.get("status", "UNKNOWN"), "uploadId": upload["uploadId"]}
    try:
        page_count = validate_exact_pdf(upload, exact_version)
        staged = stage_screening_input(upload, exact_version)
        commit_queued(upload, exact_version, lease_token, page_count, staged)
    except ValidationFailure as exc:
        return transition_failure(upload, exc.code, exc.disposition, exact_version)

    LOGGER.info(
        "upload_queued upload_id=%s document_id=%s run_id=%s page_count=%s",
        upload["uploadId"],
        upload["documentId"],
        upload["processingExecutionId"],
        page_count,
    )
    return {
        "status": "QUEUED",
        "uploadId": upload["uploadId"],
        "documentId": upload["documentId"],
        "processingExecutionId": upload["processingExecutionId"],
        "pageCount": page_count,
    }


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if isinstance(event.get("Records"), list):
        return handle_stream_event(event)
    normalized = event_object(event)
    upload = find_upload(normalized["bucket"], normalized["key"])
    if normalized["kind"] == "guardduty":
        scan = record_scan(upload, normalized)
        # A non-clean scan before /complete rejects or holds the upload session
        # immediately.  This is deliberately fail-closed; the client must create
        # a fresh upload instead of reusing a key that saw an unsafe version.
        if not upload.get("sourceVersionId") and scan["scanResultStatus"] != CLEAN_SCAN_RESULT:
            disposition, code = disposition_for_scan(str(scan["scanResultStatus"]))
            return transition_failure(upload, code, disposition, normalized["versionId"])
    return reconcile(upload, normalized["versionId"])
