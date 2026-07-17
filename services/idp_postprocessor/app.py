"""Post-process pinned AWS GenAI IDP workflow completions.

The screen configuration runs text-only OCR and lightweight extraction across
the complete package.  This Lambda chooses one borrower Closing Disclosure by
explicit, auditable rules, materializes only its exact source pages (plus an
immediately contiguous CD addendum), and submits that small PDF with the full
configuration.  A non-unique result is put on HOLD; no probabilistic tie-break
or page-range guess is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from urllib.parse import urlparse

try:  # AWS supplies these in Lambda; pure selector tests need neither package.
    import boto3
    from botocore.exceptions import ClientError
except ModuleNotFoundError:  # pragma: no cover - defensive outside Lambda
    boto3 = None  # type: ignore[assignment]

    class ClientError(Exception):
        """Fallback used only while importing pure helpers without boto3."""


LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

BORROWER_CD_CLASS = "L053_Closing_Disclosure"
ADDENDUM_CLASS = "L303_Addendum_To_Closing_Disclosure"
VARIANT_PRIORITY = {"PCCD": 3, "CORRECTED": 3, "FINAL": 2, "UNKNOWN": 1}
TERMINAL_STATUSES = {"SUCCEEDED", "HOLD", "REJECTED", "FAILED", "ARCHIVED"}
WORKFLOW_FAILURE_STATUSES = {"FAILED", "TIMED_OUT", "ABORTED"}
WORKFLOW_EVENT_STATUSES = {"RUNNING", "SUCCEEDED", *WORKFLOW_FAILURE_STATUSES}
WATCHDOG_SOURCE = "loan-document-platform.watchdog"
WATCHDOG_DETAIL_TYPE = "IDP Execution Watchdog"
ACTIVE_EXECUTION_PARTITION = "ACTIVE_EXECUTION"
WATCHDOG_MAX_ITEMS_CAP = 50
WATCHDOG_MIN_AGE_FLOOR_SECONDS = 300
_AWS: dict[str, Any] = {}


class EventError(ValueError):
    """The IDP completion event or routing metadata is permanently malformed."""


class RetryableState(RuntimeError):
    """A concurrent processor owns a short-lived lease."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log_reference(*values: Any) -> str:
    """Return a stable opaque reference without logging registry keys or ARNs."""

    material = "\x1f".join(str(value or "") for value in values).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def aws_resource(name: str) -> Any:
    if boto3 is None:  # pragma: no cover
        raise RuntimeError("boto3 is required for AWS operations")
    key = f"resource:{name}"
    if key not in _AWS:
        _AWS[key] = boto3.resource(name)
    return _AWS[key]


def aws_client(name: str) -> Any:
    if boto3 is None:  # pragma: no cover
        raise RuntimeError("boto3 is required for AWS operations")
    key = f"client:{name}"
    if key not in _AWS:
        _AWS[key] = boto3.client(name)
    return _AWS[key]


def table() -> Any:
    if "table" not in _AWS:
        _AWS["table"] = aws_resource("dynamodb").Table(required_env("TABLE_NAME"))
    return _AWS["table"]


def ddb_client() -> Any:
    return aws_client("dynamodb")


def s3_client() -> Any:
    return aws_client("s3")


def serialize_map(values: dict[str, Any]) -> dict[str, Any]:
    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()
    return {name: serializer.serialize(value) for name, value in values.items()}


def json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Cannot encode {type(value).__name__}")


def parse_json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise EventError("INVALID_WORKFLOW_JSON") from exc
    return value


def unwrap_document(value: Any) -> dict[str, Any]:
    value = parse_json_value(value)
    # Step Functions/Lambda integrations can add one or two Payload wrappers.
    for _ in range(4):
        if not isinstance(value, dict):
            raise EventError("WORKFLOW_DOCUMENT_REQUIRED")
        if "document" in value:
            document = parse_json_value(value["document"])
            if not isinstance(document, dict):
                raise EventError("WORKFLOW_DOCUMENT_REQUIRED")
            return document
        if "Result" in value and isinstance(value.get("Result"), (dict, str)):
            value = parse_json_value(value["Result"])
            continue
        if "Payload" in value:
            value = parse_json_value(value["Payload"])
            continue
        if "body" in value and len(value) <= 3:
            value = parse_json_value(value["body"])
            continue
        # A direct Document dict is also supported.
        if any(name in value for name in ("input_key", "document_id", "s3_uri", "compressed")):
            return value
        break
    raise EventError("WORKFLOW_DOCUMENT_REQUIRED")


def allowed_idp_buckets() -> set[str]:
    names = {
        os.environ.get("IDP_INPUT_BUCKET", "").strip(),
        os.environ.get("IDP_WORKING_BUCKET", "").strip(),
        os.environ.get("IDP_OUTPUT_BUCKET", "").strip(),
    }
    return {name for name in names if name}


def read_json_s3_uri(uri: str) -> dict[str, Any]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise EventError("INVALID_IDP_S3_URI")
    if parsed.netloc not in allowed_idp_buckets():
        raise EventError("UNTRUSTED_IDP_S3_BUCKET")
    key = parsed.path.lstrip("/")
    head = s3_client().head_object(Bucket=parsed.netloc, Key=key)
    if int(head.get("ContentLength", 0)) > int(os.environ.get("MAXIMUM_IDP_JSON_BYTES", str(20 * 1024 * 1024))):
        raise EventError("IDP_JSON_TOO_LARGE")
    raw = s3_client().get_object(Bucket=parsed.netloc, Key=key)["Body"].read()
    try:
        result = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EventError("INVALID_IDP_S3_JSON") from exc
    if not isinstance(result, dict):
        raise EventError("INVALID_IDP_S3_JSON")
    return result


def expand_compressed(document: dict[str, Any]) -> dict[str, Any]:
    if document.get("compressed") is not True:
        return document
    uri = str(document.get("s3_uri", ""))
    if not uri:
        raise EventError("COMPRESSED_DOCUMENT_URI_REQUIRED")
    return read_json_s3_uri(uri)


def workflow_header(event: dict[str, Any]) -> dict[str, str]:
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else event
    if not isinstance(detail, dict):
        raise EventError("WORKFLOW_DETAIL_REQUIRED")
    if event.get("source") and event.get("source") != "aws.states":
        raise EventError("UNEXPECTED_WORKFLOW_EVENT_SOURCE")
    if event.get("detail-type") and event.get("detail-type") != "Step Functions Execution Status Change":
        raise EventError("UNEXPECTED_WORKFLOW_DETAIL_TYPE")
    status = str(detail.get("status") or detail.get("workflow_status") or "").upper()
    if status not in WORKFLOW_EVENT_STATUSES:
        raise EventError("UNSUPPORTED_WORKFLOW_STATUS")
    execution_arn = str(detail.get("executionArn") or "")
    if status != "SUCCEEDED" and not execution_arn:
        raise EventError("WORKFLOW_EXECUTION_ARN_REQUIRED")
    state_machine_arn = str(detail.get("stateMachineArn") or "")
    allowed = {value.strip() for value in os.environ.get("IDP_STATE_MACHINE_ARNS", "").split(",") if value.strip()}
    if allowed and state_machine_arn not in allowed:
        raise EventError("UNEXPECTED_IDP_STATE_MACHINE")
    return {
        "status": status,
        "executionArn": execution_arn,
        "stateMachineArn": state_machine_arn,
        "eventId": str(event.get("id") or ""),
        "observedAt": str(event.get("time") or "") or utc_now(),
    }


def workflow_document(event: dict[str, Any], header: dict[str, str]) -> dict[str, Any] | None:
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else event
    if not isinstance(detail, dict):
        raise EventError("WORKFLOW_DETAIL_REQUIRED")
    status = header["status"]
    candidate = detail.get("output") if status == "SUCCEEDED" and detail.get("output") else detail.get("input")
    if candidate is None:
        candidate = event.get("document")
    if candidate is None:
        return None
    if parse_json_value(candidate) is None:
        return None
    return expand_compressed(unwrap_document(candidate))


def workflow_event(event: dict[str, Any]) -> dict[str, Any]:
    header = workflow_header(event)
    document = workflow_document(event, header)
    if document is None:
        raise EventError("WORKFLOW_DOCUMENT_REQUIRED")
    return {
        **header,
        "document": document,
        "executionArn": header["executionArn"] or str(document.get("workflow_execution_arn") or ""),
    }


def decode_b64url(value: str, field: str) -> str:
    if not value or len(value) > 2048 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise EventError(f"INVALID_{field.upper()}")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        return raw.decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise EventError(f"INVALID_{field.upper()}") from exc


def input_reference(document: dict[str, Any]) -> tuple[str, str]:
    bucket = str(document.get("input_bucket", ""))
    key = str(document.get("input_key") or document.get("document_id") or document.get("id") or "")
    if not bucket:
        bucket = os.environ.get("IDP_INPUT_BUCKET", "")
    if bucket != required_env("IDP_INPUT_BUCKET") or not key:
        raise EventError("UNEXPECTED_IDP_INPUT_OBJECT")
    return bucket, key


def route_workflow(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve opaque DynamoDB locators, then pin the recorded IDP S3 version."""

    bucket, key = input_reference(document)
    latest = s3_client().head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    metadata = latest.get("Metadata") or {}
    stage = metadata.get("pipeline-stage", "")
    if stage not in {"screen", "full"}:
        raise EventError("INVALID_PIPELINE_STAGE")
    pk = decode_b64url(metadata.get("loan-pk-b64", ""), "loan_pk")
    upload_sk = decode_b64url(metadata.get("upload-sk-b64", ""), "upload_sk")
    document_sk = decode_b64url(metadata.get("document-sk-b64", ""), "document_sk")
    run_id = metadata.get("processing-execution-id", "")
    if not run_id:
        raise EventError("PROCESSING_EXECUTION_ID_REQUIRED")

    upload = table().get_item(Key={"PK": pk, "SK": upload_sk}, ConsistentRead=True).get("Item")
    logical_document = table().get_item(Key={"PK": pk, "SK": document_sk}, ConsistentRead=True).get("Item")
    if not upload or upload.get("entityType") != "UPLOAD" or not logical_document or logical_document.get("entityType") != "DOCUMENT":
        raise EventError("ROUTED_RECORD_NOT_FOUND")
    if upload.get("processingExecutionId") != run_id or logical_document.get("processingExecutionId") != run_id:
        raise EventError("PROCESSING_EXECUTION_MISMATCH")
    if logical_document.get("currentUploadId") != upload.get("uploadId"):
        raise EventError("UPLOAD_NOT_CURRENT")

    version_field = "screenInputVersionId" if stage == "screen" else "fullInputVersionId"
    key_field = "screenInputKey" if stage == "screen" else "fullInputKey"
    recorded_version = str(logical_document.get(version_field) or upload.get(version_field) or "")
    if logical_document.get(key_field) != key or not recorded_version:
        raise EventError("IDP_INPUT_NOT_PINNED")
    exact = s3_client().head_object(Bucket=bucket, Key=key, VersionId=recorded_version, ChecksumMode="ENABLED")
    if (
        latest.get("VersionId") != recorded_version
        or exact.get("VersionId") != recorded_version
        or (exact.get("Metadata") or {}) != metadata
    ):
        raise EventError("IDP_INPUT_VERSION_CHANGED")

    expected_config = os.environ.get("SCREEN_CONFIG_VERSION", "cd-screen-v1") if stage == "screen" else os.environ.get("FULL_CONFIG_VERSION", "cd-full-v1")
    if metadata.get("config-version") != expected_config or document.get("config_version") not in {None, "", expected_config}:
        return {
            "stage": stage,
            "pk": pk,
            "documentKey": {"PK": pk, "SK": document_sk},
            "uploadKey": {"PK": pk, "SK": upload_sk},
            "upload": upload,
            "document": logical_document,
            "runId": run_id,
            "inputBucket": bucket,
            "inputKey": key,
            "inputVersionId": recorded_version,
            "configMismatch": True,
        }
    return {
        "stage": stage,
        "pk": pk,
        "documentKey": {"PK": pk, "SK": document_sk},
        "uploadKey": {"PK": pk, "SK": upload_sk},
        "upload": upload,
        "document": logical_document,
        "runId": run_id,
        "inputBucket": bucket,
        "inputKey": key,
        "inputVersionId": recorded_version,
        "metadata": metadata,
        "configMismatch": False,
    }


def execution_gsi_pk(execution_arn: str) -> str:
    """Bound the GSI key size while retaining exact ARN verification in the item."""

    return f"EXECUTION#{hashlib.sha256(execution_arn.encode('utf-8')).hexdigest()}"


def workflow_lookup_key(route: dict[str, Any], execution_arn: str) -> dict[str, str]:
    digest = hashlib.sha256(execution_arn.encode("utf-8")).hexdigest()[:24]
    return {
        "PK": route["pk"],
        "SK": f"{route['uploadKey']['SK']}#WORKFLOW#{route['stage'].upper()}#{digest}",
    }


def workflow_lookup_values(
    route: dict[str, Any], execution_arn: str, state_machine_arn: str
) -> dict[str, Any]:
    return {
        ":entity": "IDP_WORKFLOW",
        ":execution": execution_arn,
        ":stateMachine": state_machine_arn or "unknown",
        ":stage": route["stage"],
        ":run": route["runId"],
        ":documentPK": route["documentKey"]["PK"],
        ":documentSK": route["documentKey"]["SK"],
        ":uploadSK": route["uploadKey"]["SK"],
        ":gsiPK": execution_gsi_pk(execution_arn),
        ":gsiSK": f"STAGE#{route['stage'].upper()}#RUN#{route['runId']}",
    }


def active_execution_sort_key(execution_arn: str, started_at: str) -> str:
    digest = hashlib.sha256(execution_arn.encode("utf-8")).hexdigest()
    return f"{started_at}#{digest}"


def record_workflow_running(route: dict[str, Any], header: dict[str, str]) -> dict[str, Any]:
    """Register an execution ARN and advance QUEUED screening to SCREENING."""

    execution_arn = header["executionArn"]
    now = utc_now()
    lookup_key = workflow_lookup_key(route, execution_arn)
    values = workflow_lookup_values(route, execution_arn, header["stateMachineArn"])
    values.update(
        {
            ":running": "RUNNING",
            ":now": now,
            ":activePK": ACTIVE_EXECUTION_PARTITION,
            ":activeSK": active_execution_sort_key(execution_arn, now),
        }
    )
    try:
        table().update_item(
            Key=lookup_key,
            UpdateExpression=(
                "SET entityType=:entity, executionArn=:execution, stateMachineArn=:stateMachine, "
                "pipelineStage=:stage, processingExecutionId=:run, documentPK=:documentPK, "
                "documentSK=:documentSK, uploadSK=:uploadSK, GSI1PK=:gsiPK, GSI1SK=:gsiSK, "
                "GSI2PK=:activePK, GSI2SK=if_not_exists(GSI2SK,:activeSK), "
                "#status=:running, createdAt=if_not_exists(createdAt,:now), updatedAt=:now"
            ),
            ConditionExpression=(
                "attribute_not_exists(executionArn) OR "
                "(executionArn=:execution AND (attribute_not_exists(#status) OR #status=:running))"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        existing = table().get_item(Key=lookup_key, ConsistentRead=True).get("Item", {})
        if existing.get("executionArn") != execution_arn:
            raise EventError("WORKFLOW_LOOKUP_CONFLICT") from exc
        # A terminal event won the race; never regress its audit record to RUNNING.

    if route["stage"] == "screen":
        target_status = "SCREENING"
        condition = "#status IN (:queued,:screening) AND processingExecutionId=:run"
        values = {
            ":queued": "QUEUED",
            ":screening": "SCREENING",
            ":run": route["runId"],
            ":execution": execution_arn,
            ":now": now,
        }
        update = (
            "SET #status=:screening, idpScreenWorkflowExecutionArn=:execution, "
            "screeningStartedAt=if_not_exists(screeningStartedAt,:now), updatedAt=:now"
        )
    else:
        target_status = "EXTRACTING"
        condition = "#status=:extracting AND processingExecutionId=:run"
        values = {
            ":extracting": "EXTRACTING",
            ":run": route["runId"],
            ":execution": execution_arn,
            ":now": now,
        }
        update = (
            "SET idpFullWorkflowExecutionArn=:execution, "
            "fullExtractionStartedAt=if_not_exists(fullExtractionStartedAt,:now), updatedAt=:now"
        )
    for item_key in (route["documentKey"], route["uploadKey"]):
        try:
            table().update_item(
                Key=item_key,
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # Late RUNNING delivery must not regress EXTRACTING/SUCCEEDED/FAILED.
            continue
    LOGGER.info(
        "idp_workflow_running stage=%s run_id=%s",
        route["stage"],
        route["runId"],
    )
    current_after = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
    return {
        "status": current_after.get("status", target_status),
        "processingExecutionId": route["runId"],
        "workflowExecutionArn": execution_arn,
    }


def record_workflow_succeeded(route: dict[str, Any], header: dict[str, str]) -> None:
    """Persist successful IDP execution provenance before local post-processing."""

    execution_arn = header["executionArn"]
    if not execution_arn:
        return
    now = utc_now()
    values = workflow_lookup_values(route, execution_arn, header["stateMachineArn"])
    values.update(
        {
            ":succeeded": "SUCCEEDED",
            ":event": header["eventId"] or "unknown",
            ":observed": header["observedAt"],
            ":now": now,
        }
    )
    table().update_item(
        Key=workflow_lookup_key(route, execution_arn),
        UpdateExpression=(
            "SET entityType=:entity, executionArn=:execution, stateMachineArn=:stateMachine, "
            "pipelineStage=:stage, processingExecutionId=:run, documentPK=:documentPK, "
            "documentSK=:documentSK, uploadSK=:uploadSK, GSI1PK=:gsiPK, GSI1SK=:gsiSK, "
            "#status=:succeeded, terminalEventId=:event, terminalObservedAt=:observed, "
            "terminalAt=:now, createdAt=if_not_exists(createdAt,:now), updatedAt=:now"
        ),
        ConditionExpression="attribute_not_exists(executionArn) OR executionArn=:execution",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
    )


def find_route_by_execution(execution_arn: str) -> dict[str, Any]:
    """Resolve terminal events whose Step Functions input was not included."""

    response = table().query(
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk",
        ExpressionAttributeValues={":pk": execution_gsi_pk(execution_arn)},
    )
    matches = [
        item
        for item in response.get("Items", [])
        if item.get("entityType") == "IDP_WORKFLOW" and item.get("executionArn") == execution_arn
    ]
    if not matches:
        raise RetryableState("WORKFLOW_EXECUTION_LOOKUP_NOT_VISIBLE")
    if len(matches) != 1:
        raise EventError("AMBIGUOUS_WORKFLOW_EXECUTION_LOOKUP")
    lookup = matches[0]
    pk = str(lookup.get("documentPK") or "")
    document_sk = str(lookup.get("documentSK") or "")
    upload_sk = str(lookup.get("uploadSK") or "")
    stage = str(lookup.get("pipelineStage") or "")
    run_id = str(lookup.get("processingExecutionId") or "")
    if not pk or not document_sk or not upload_sk or stage not in {"screen", "full"} or not run_id:
        raise EventError("INVALID_WORKFLOW_EXECUTION_LOOKUP")
    upload = table().get_item(Key={"PK": pk, "SK": upload_sk}, ConsistentRead=True).get("Item")
    logical_document = table().get_item(Key={"PK": pk, "SK": document_sk}, ConsistentRead=True).get("Item")
    if not upload or not logical_document:
        raise RetryableState("WORKFLOW_ROUTED_RECORD_NOT_VISIBLE")
    if upload.get("processingExecutionId") != run_id or logical_document.get("processingExecutionId") != run_id:
        raise EventError("WORKFLOW_LOOKUP_RUN_MISMATCH")
    if logical_document.get("currentUploadId") != upload.get("uploadId"):
        raise EventError("WORKFLOW_LOOKUP_UPLOAD_NOT_CURRENT")
    version_field = "screenInputVersionId" if stage == "screen" else "fullInputVersionId"
    key_field = "screenInputKey" if stage == "screen" else "fullInputKey"
    bucket_field = "screenInputBucket" if stage == "screen" else "fullInputBucket"
    return {
        "stage": stage,
        "pk": pk,
        "documentKey": {"PK": pk, "SK": document_sk},
        "uploadKey": {"PK": pk, "SK": upload_sk},
        "upload": upload,
        "document": logical_document,
        "runId": run_id,
        "inputBucket": logical_document.get(bucket_field),
        "inputKey": logical_document.get(key_field),
        "inputVersionId": logical_document.get(version_field),
        "executionLookupKey": {"PK": lookup["PK"], "SK": lookup["SK"]},
        "configMismatch": False,
    }


def clear_active_execution(route: dict[str, Any], execution_arn: str) -> None:
    """Remove a stale sparse-index marker after an idempotent terminal replay."""

    if not execution_arn:
        return
    try:
        table().update_item(
            Key=workflow_lookup_key(route, execution_arn),
            UpdateExpression="REMOVE GSI2PK, GSI2SK",
            ConditionExpression="executionArn=:execution",
            ExpressionAttributeValues={":execution": execution_arn},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


def attribute_value(attributes: Any, name: str) -> Any:
    if not isinstance(attributes, dict) or name not in attributes:
        return None
    value = attributes[name]
    # IDP extractors may wrap schema values with value/Value/normalizedValue.
    for _ in range(3):
        if not isinstance(value, dict):
            break
        matched = False
        for key in ("value", "Value", "normalizedValue", "text"):
            if key in value:
                value = value[key]
                matched = True
                break
        if not matched:
            break
    return value


def normalize_identity(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(character for character in text if character.isalnum())


def normalize_loan_identifier(value: Any) -> str:
    normalized = normalize_identity(value)
    if normalized.isdigit():
        return normalized.lstrip("0") or "0"
    return normalized


def parse_date_ordinal(value: Any) -> int:
    if value in (None, ""):
        return -1
    text = str(value).strip()
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, date_format).date().toordinal()
        except ValueError:
            continue
    return -1


def page_numbers(section: dict[str, Any]) -> list[int]:
    raw = section.get("page_ids")
    if not isinstance(raw, list) or not raw:
        return []
    pages: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
            return []
        page = int(value)
        if page in pages:
            return []
        pages.append(page)
    if pages != sorted(pages):
        return []
    return pages


def maximum_selected_pages() -> int:
    value = int(os.environ.get("MAX_SELECTED_PAGES", "8"))
    if value < 1 or value > 25:
        raise RuntimeError("MAX_SELECTED_PAGES must be between 1 and 25")
    return value


def candidate_from_section(section: dict[str, Any]) -> dict[str, Any] | None:
    if section.get("classification") != BORROWER_CD_CLASS:
        return None
    pages = page_numbers(section)
    if not pages:
        return None
    attributes = section.get("attributes") or {}
    raw_variant = str(attribute_value(attributes, "DocumentVariant") or "UNKNOWN").upper().strip()
    evidence = str(attribute_value(attributes, "VariantEvidenceText") or "").strip()
    # PCCD/CORRECTED/FINAL are accepted only with explicit printed evidence.
    variant = raw_variant if raw_variant in VARIANT_PRIORITY and raw_variant != "UNKNOWN" and evidence else "UNKNOWN"
    signature_count = sum(
        attribute_value(attributes, field) is True
        for field in ("BorrowerSignaturePresent", "CoBorrowerSignaturePresent")
    )
    execution_dates = [
        parse_date_ordinal(attribute_value(attributes, "BorrowerExecutionDate")),
        parse_date_ordinal(attribute_value(attributes, "CoBorrowerExecutionDate")),
    ]
    rank = (
        VARIANT_PRIORITY[variant],
        parse_date_ordinal(attribute_value(attributes, "DateIssued")),
        signature_count,
        max(execution_dates),
    )
    return {
        "sectionId": str(section.get("section_id", "")),
        "pages": pages,
        "variant": variant,
        "dateIssued": str(attribute_value(attributes, "DateIssued") or ""),
        "executionEvidence": signature_count,
        "rank": rank,
        "loanIdentifier": normalize_loan_identifier(attribute_value(attributes, "LoanIdentifier")),
        "identity": (
            normalize_identity(attribute_value(attributes, "PrimaryBorrowerName")),
            normalize_identity(attribute_value(attributes, "CoBorrowerName")),
            normalize_identity(attribute_value(attributes, "PropertyAddress")),
        ),
    }


def contiguous_addendum_pages(sections: list[dict[str, Any]], selected_pages: list[int]) -> list[int]:
    pages = list(selected_pages)
    next_page = pages[-1] + 1
    # Attach only directly adjacent addenda.  A gap or any intervening document
    # stops attachment, so pages are never inferred across section boundaries.
    ordered = sorted(
        ((page_numbers(section), section) for section in sections if page_numbers(section)),
        key=lambda pair: pair[0][0],
    )
    for section_pages, section in ordered:
        if section_pages[0] < next_page:
            continue
        if section_pages[0] > next_page:
            break
        if section.get("classification") != ADDENDUM_CLASS:
            break
        if section_pages != list(range(next_page, next_page + len(section_pages))):
            break
        pages.extend(section_pages)
        next_page = pages[-1] + 1
    return pages


def public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return audit evidence without copying borrower/property values."""

    return {
        "sectionId": candidate["sectionId"],
        "pages": candidate["pages"],
        "variant": candidate["variant"],
        "dateIssued": candidate["dateIssued"],
        "executionEvidence": candidate["executionEvidence"],
        "rank": list(candidate["rank"]),
        "loanIdentifierPresent": bool(candidate["loanIdentifier"]),
        "identityEvidencePresent": [bool(value) for value in candidate["identity"]],
    }


def hydrate_screen_section(section: dict[str, Any]) -> dict[str, Any]:
    """Load the screen extractor's inference_result from its pinned S3 URI."""

    hydrated = dict(section)
    if section.get("classification") != BORROWER_CD_CLASS:
        return hydrated
    embedded = section.get("attributes") if isinstance(section.get("attributes"), dict) else {}
    selection_fields = {
        "LoanIdentifier",
        "DateIssued",
        "DocumentVariant",
        "VariantEvidenceText",
        "BorrowerSignaturePresent",
    }
    if selection_fields.intersection(embedded):
        return hydrated
    uri = str(section.get("extraction_result_uri") or "")
    if not uri:
        return hydrated
    extraction = read_json_s3_uri(uri)
    inference = extraction.get("inference_result")
    if not isinstance(inference, dict):
        raise EventError("SCREEN_INFERENCE_RESULT_REQUIRED")
    hydrated["attributes"] = inference
    return hydrated


def select_closing_disclosure(sections: list[dict[str, Any]], loan_id: str) -> dict[str, Any]:
    """Select one CD deterministically or return a stable HOLD reason."""

    raw_candidates = [section for section in sections if section.get("classification") == BORROWER_CD_CLASS]
    parsed_candidates = [
        candidate for section in raw_candidates if (candidate := candidate_from_section(section))
    ]
    candidates = [
        candidate for candidate in parsed_candidates if len(candidate["pages"]) <= maximum_selected_pages()
    ]
    if not raw_candidates:
        return {"status": "HOLD", "reason": "CLOSING_DISCLOSURE_NOT_FOUND", "candidates": []}
    if parsed_candidates and not candidates:
        return {
            "status": "HOLD",
            "reason": "CLOSING_DISCLOSURE_PAGE_LIMIT_EXCEEDED",
            "candidates": [public_candidate(candidate) for candidate in parsed_candidates],
        }
    if not candidates:
        return {"status": "HOLD", "reason": "CLOSING_DISCLOSURE_PAGE_IDS_INVALID", "candidates": []}

    expected_loan = normalize_loan_identifier(loan_id)
    candidates_with_loan = [candidate for candidate in candidates if candidate["loanIdentifier"]]
    matching = [candidate for candidate in candidates_with_loan if candidate["loanIdentifier"] == expected_loan]
    if matching:
        candidates = matching
    elif candidates_with_loan:
        return {
            "status": "HOLD",
            "reason": "CLOSING_DISCLOSURE_LOAN_ID_MISMATCH",
            "candidates": [public_candidate(candidate) for candidate in candidates],
        }

    top_rank = max(candidate["rank"] for candidate in candidates)
    winners = [candidate for candidate in candidates if candidate["rank"] == top_rank]
    if len(winners) != 1:
        return {
            "status": "HOLD",
            "reason": "CLOSING_DISCLOSURE_AMBIGUOUS",
            "candidates": [public_candidate(candidate) for candidate in candidates],
        }

    winner = dict(winners[0])
    winner["pages"] = contiguous_addendum_pages(sections, winner["pages"])
    if len(winner["pages"]) > maximum_selected_pages():
        return {
            "status": "HOLD",
            "reason": "CLOSING_DISCLOSURE_PAGE_LIMIT_EXCEEDED",
            "candidates": [public_candidate(candidate) for candidate in candidates],
        }
    return {
        "status": "SELECTED",
        "reason": "UNIQUE_HIGHEST_EVIDENCE_RANK",
        "winner": public_candidate(winner),
        "selectedPages": winner["pages"],
        "candidates": [public_candidate(candidate) for candidate in candidates],
    }


def acquire_postprocess_lease(route: dict[str, Any]) -> str | None:
    current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
    if current.get("status") in TERMINAL_STATUSES:
        return None
    if route["stage"] == "screen" and current.get("status") == "EXTRACTING":
        return None
    token = str(uuid.uuid4())
    epoch = int(time.time())
    if route["stage"] == "screen":
        status_condition = "#status IN (:queued,:screening)"
        status_values = {":queued": "QUEUED", ":screening": "SCREENING"}
    else:
        status_condition = "#status=:extracting"
        status_values = {":extracting": "EXTRACTING"}
    try:
        table().update_item(
            Key=route["documentKey"],
            UpdateExpression="SET postprocessLeaseToken=:token, postprocessLeaseExpiresAt=:expires, updatedAt=:now",
            ConditionExpression=(
                f"{status_condition} AND currentUploadId=:upload AND processingExecutionId=:run "
                "AND (attribute_not_exists(postprocessLeaseExpiresAt) OR postprocessLeaseExpiresAt < :epoch)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":token": token,
                ":expires": epoch + 300,
                ":epoch": epoch,
                ":now": utc_now(),
                ":upload": route["upload"]["uploadId"],
                ":run": route["runId"],
                **status_values,
            },
        )
        return token
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        refreshed = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        if refreshed.get("status") in TERMINAL_STATUSES or (route["stage"] == "screen" and refreshed.get("status") == "EXTRACTING"):
            return None
        raise RetryableState("POSTPROCESS_LEASE_BUSY") from exc


def transition_status(route: dict[str, Any], status: str, code: str) -> dict[str, Any]:
    current_before = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
    if current_before.get("status") in TERMINAL_STATUSES and current_before.get("status") != status:
        return {
            "status": current_before.get("status"),
            "processingExecutionId": route["runId"],
            "ignored": True,
        }
    now = utc_now()
    document_values = serialize_map(
        {
            ":status": status,
            ":code": code,
            ":now": now,
            ":upload": route["upload"]["uploadId"],
            ":run": route["runId"],
        }
    )
    upload_values = serialize_map({":status": status, ":code": code, ":now": now, ":run": route["runId"]})
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["documentKey"]),
                "UpdateExpression": "SET #status=:status, failureCode=:code, updatedAt=:now REMOVE postprocessLeaseToken, postprocessLeaseExpiresAt",
                "ConditionExpression": "currentUploadId=:upload AND processingExecutionId=:run",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": document_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["uploadKey"]),
                "UpdateExpression": "SET #status=:status, failureCode=:code, updatedAt=:now",
                "ConditionExpression": "processingExecutionId=:run",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": upload_values,
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        if current.get("status") != status or current.get("failureCode") != code:
            raise RetryableState("STATUS_TRANSITION_RACED") from exc
    LOGGER.warning("idp_document_stopped status=%s code=%s run_id=%s", status, code, route["runId"])
    return {"status": status, "failureCode": code, "processingExecutionId": route["runId"]}


def workflow_failure_code(stage: str, terminal_status: str) -> str:
    prefix = "SCREENING" if stage == "screen" else "FULL_EXTRACTION"
    suffix = {
        "FAILED": "FAILED",
        "TIMED_OUT": "TIMED_OUT",
        "ABORTED": "ABORTED",
    }.get(terminal_status)
    if suffix is None:
        raise EventError("UNSUPPORTED_WORKFLOW_FAILURE_STATUS")
    return f"{prefix}_{suffix}"


def workflow_failure_action(
    current: dict[str, Any], stage: str, terminal_status: str, execution_arn: str
) -> str:
    """Return APPLY, IDEMPOTENT, or IGNORE without regressing a later state."""

    if (
        current.get("status") == "FAILED"
        and current.get("idpTerminalStatus") == terminal_status
        and current.get("idpFailedWorkflowExecutionArn") == execution_arn
    ):
        return "IDEMPOTENT"
    allowed = {"QUEUED", "SCREENING"} if stage == "screen" else {"EXTRACTING"}
    return "APPLY" if current.get("status") in allowed else "IGNORE"


def transition_workflow_failure(route: dict[str, Any], header: dict[str, str]) -> dict[str, Any]:
    """Conditionally persist a safe terminal reason and execution provenance."""

    terminal_status = header["status"]
    execution_arn = header["executionArn"]
    current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
    action = workflow_failure_action(current, route["stage"], terminal_status, execution_arn)
    if action == "IDEMPOTENT":
        clear_active_execution(route, execution_arn)
        return {
            "status": "FAILED",
            "failureCode": current.get("failureCode"),
            "processingExecutionId": route["runId"],
            "idempotent": True,
        }
    if action == "IGNORE":
        clear_active_execution(route, execution_arn)
        return {
            "status": current.get("status", "UNKNOWN"),
            "processingExecutionId": route["runId"],
            "ignored": True,
        }

    code = workflow_failure_code(route["stage"], terminal_status)
    now = utc_now()
    workflow_attribute = (
        "idpScreenWorkflowExecutionArn" if route["stage"] == "screen" else "idpFullWorkflowExecutionArn"
    )
    allowed_condition = (
        "#status IN (:queued,:screening)" if route["stage"] == "screen" else "#status=:extracting"
    )
    common = {
        ":failed": "FAILED",
        ":code": code,
        ":now": now,
        ":run": route["runId"],
        ":execution": execution_arn,
        ":terminal": terminal_status,
        ":event": header["eventId"] or "unknown",
        ":observed": header["observedAt"],
    }
    if route["stage"] == "screen":
        common.update({":queued": "QUEUED", ":screening": "SCREENING"})
    else:
        common[":extracting"] = "EXTRACTING"
    document_values = serialize_map({**common, ":upload": route["upload"]["uploadId"]})
    upload_values = serialize_map(common)
    failure_update = (
        "SET #status=:failed, failureCode=:code, failedAt=:now, updatedAt=:now, "
        "idpTerminalStatus=:terminal, idpFailedWorkflowExecutionArn=:execution, "
        "#workflowArn=:execution, idpFailureEventId=:event, idpFailureObservedAt=:observed "
        "REMOVE postprocessLeaseToken, postprocessLeaseExpiresAt"
    )

    lookup_key = workflow_lookup_key(route, execution_arn)
    lookup_raw = workflow_lookup_values(route, execution_arn, header["stateMachineArn"])
    lookup_raw.update(
        {
            ":terminal": terminal_status,
            ":event": header["eventId"] or "unknown",
            ":observed": header["observedAt"],
            ":now": now,
        }
    )
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["documentKey"]),
                "UpdateExpression": failure_update,
                "ConditionExpression": (
                    f"{allowed_condition} AND currentUploadId=:upload AND processingExecutionId=:run"
                ),
                "ExpressionAttributeNames": {"#status": "status", "#workflowArn": workflow_attribute},
                "ExpressionAttributeValues": document_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["uploadKey"]),
                "UpdateExpression": failure_update,
                "ConditionExpression": f"{allowed_condition} AND processingExecutionId=:run",
                "ExpressionAttributeNames": {"#status": "status", "#workflowArn": workflow_attribute},
                "ExpressionAttributeValues": upload_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(lookup_key),
                "UpdateExpression": (
                    "SET entityType=:entity, executionArn=:execution, stateMachineArn=:stateMachine, "
                    "pipelineStage=:stage, processingExecutionId=:run, documentPK=:documentPK, "
                    "documentSK=:documentSK, uploadSK=:uploadSK, GSI1PK=:gsiPK, GSI1SK=:gsiSK, "
                    "#status=:terminal, terminalEventId=:event, terminalObservedAt=:observed, "
                    "terminalAt=:now, createdAt=if_not_exists(createdAt,:now), updatedAt=:now "
                    "REMOVE GSI2PK, GSI2SK"
                ),
                "ConditionExpression": "attribute_not_exists(executionArn) OR executionArn=:execution",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_map(lookup_raw),
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        refreshed = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        refreshed_action = workflow_failure_action(
            refreshed, route["stage"], terminal_status, execution_arn
        )
        if refreshed_action == "IDEMPOTENT":
            clear_active_execution(route, execution_arn)
            return {
                "status": "FAILED",
                "failureCode": refreshed.get("failureCode"),
                "processingExecutionId": route["runId"],
                "idempotent": True,
            }
        if refreshed_action == "IGNORE":
            clear_active_execution(route, execution_arn)
            return {
                "status": refreshed.get("status", "UNKNOWN"),
                "processingExecutionId": route["runId"],
                "ignored": True,
            }
        raise RetryableState("WORKFLOW_FAILURE_TRANSITION_RACED") from exc
    LOGGER.warning(
        "idp_workflow_failed stage=%s terminal_status=%s failure_code=%s run_id=%s",
        route["stage"],
        terminal_status,
        code,
        route["runId"],
    )
    return {
        "status": "FAILED",
        "failureCode": code,
        "processingExecutionId": route["runId"],
    }


def artifact_prefix(route: dict[str, Any]) -> str:
    source_key = str(route["upload"]["sourceKey"])
    current_prefix = "quarantine/"
    current_suffix = "/source.pdf"
    if source_key.startswith(current_prefix) and source_key.endswith(current_suffix):
        base_key = source_key[len(current_prefix) : -len(current_suffix)]
    else:
        # Read compatibility for upload rows created before the dedicated top-level
        # quarantine prefix. New uploads never use this legacy layout.
        legacy_suffix = "/quarantine/source.pdf"
        if not source_key.endswith(legacy_suffix):
            raise EventError("UNEXPECTED_SOURCE_KEY")
        base_key = source_key[: -len(legacy_suffix)]

    segments = base_key.split("/")
    if (
        not base_key
        or any(segment in {"", ".", ".."} for segment in segments)
        or "\\" in base_key
        or any(ord(character) < 32 or ord(character) == 127 for character in base_key)
    ):
        raise EventError("UNEXPECTED_SOURCE_KEY")
    return f"{base_key}/artifacts/{route['runId']}"


def put_versioned_object(bucket: str, key: str, body: bytes, content_type: str) -> dict[str, str]:
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    response = s3_client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ChecksumSHA256=checksum,
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=required_env("DATA_KEY_ARN"),
    )
    version = response.get("VersionId")
    if not version:
        raise RuntimeError("Artifact bucket must have S3 Versioning enabled")
    return {"bucket": bucket, "key": key, "versionId": version, "checksumSha256": checksum}


def put_json_artifact(route: dict[str, Any], name: str, payload: dict[str, Any]) -> dict[str, str]:
    raw = json.dumps(payload, default=json_default, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return put_versioned_object(required_env("SOURCE_BUCKET"), f"{artifact_prefix(route)}/{name}", raw, "application/json")


def materialize_selected_pdf(route: dict[str, Any], selected_pages: list[int]) -> dict[str, str]:
    if not selected_pages or len(selected_pages) > maximum_selected_pages():
        raise EventError("SELECTED_PAGE_LIMIT_EXCEEDED")
    source = route["upload"]
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as input_file:
        body = s3_client().get_object(
            Bucket=source["sourceBucket"],
            Key=source["sourceKey"],
            VersionId=source["sourceVersionId"],
        )["Body"]
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            if chunk:
                input_file.write(chunk)
        input_file.seek(0)
        try:
            from pypdf import PdfReader, PdfWriter
            from pypdf.errors import PdfReadError

            reader = PdfReader(input_file, strict=True)
            if reader.is_encrypted:
                raise EventError("SOURCE_PDF_ENCRYPTED")
            if any(page < 1 or page > len(reader.pages) for page in selected_pages):
                raise EventError("SELECTED_PAGE_OUT_OF_RANGE")
            writer = PdfWriter()
            for page in selected_pages:
                writer.add_page(reader.pages[page - 1])
            with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as output_file:
                writer.write(output_file)
                output_file.seek(0)
                selected_bytes = output_file.read()
        except EventError:
            raise
        except (PdfReadError, ValueError, TypeError, KeyError, OSError) as exc:
            raise EventError("SELECTED_PDF_MATERIALIZATION_FAILED") from exc
    return put_versioned_object(
        required_env("SOURCE_BUCKET"),
        f"{artifact_prefix(route)}/selected.pdf",
        selected_bytes,
        "application/pdf",
    )


def b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def stage_full_input(route: dict[str, Any], selected: dict[str, str], selected_pages: list[int]) -> dict[str, str]:
    if not selected_pages or len(selected_pages) > maximum_selected_pages():
        raise EventError("SELECTED_PAGE_LIMIT_EXCEEDED")
    destination_bucket = required_env("IDP_INPUT_BUCKET")
    destination_key = f"full/{route['runId']}/{route['upload']['documentId']}/{route['upload']['uploadId']}.pdf"
    metadata = {
        "config-version": os.environ.get("FULL_CONFIG_VERSION", "cd-full-v1"),
        "config-sha256": required_env("FULL_CONFIG_SHA256"),
        "pipeline-stage": "full",
        "selector-rule-version": os.environ.get("SELECTOR_RULE_VERSION", "cd-selection-v1"),
        "processing-execution-id": route["runId"],
        "loan-pk-b64": b64url(route["pk"]),
        "upload-sk-b64": b64url(route["uploadKey"]["SK"]),
        "document-sk-b64": b64url(route["documentKey"]["SK"]),
        "selected-version-id-b64": b64url(selected["versionId"]),
        "selected-pages": ",".join(str(page) for page in selected_pages),
    }
    response = s3_client().copy_object(
        Bucket=destination_bucket,
        Key=destination_key,
        CopySource={"Bucket": selected["bucket"], "Key": selected["key"], "VersionId": selected["versionId"]},
        Metadata=metadata,
        MetadataDirective="REPLACE",
        ContentType="application/pdf",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=os.environ.get("IDP_INPUT_KEY_ARN") or required_env("DATA_KEY_ARN"),
        ChecksumAlgorithm="SHA256",
    )
    version = response.get("VersionId")
    if not version:
        raise RuntimeError("IDP input bucket must have S3 Versioning enabled")
    return {"bucket": destination_bucket, "key": destination_key, "versionId": version}


def commit_selected(
    route: dict[str, Any],
    lease_token: str,
    decision: dict[str, Any],
    decision_artifact: dict[str, str],
    selected: dict[str, str],
    full_input: dict[str, str],
    execution_arn: str,
) -> None:
    winner = decision["winner"]
    now = utc_now()
    values = {
        ":extracting": "EXTRACTING",
        ":queued": "QUEUED",
        ":screening": "SCREENING",
        ":now": now,
        ":run": route["runId"],
        ":upload": route["upload"]["uploadId"],
        ":lease": lease_token,
        ":selectedBucket": selected["bucket"],
        ":selectedKey": selected["key"],
        ":selectedVersion": selected["versionId"],
        ":selectedChecksum": selected["checksumSha256"],
        ":selectedPages": decision["selectedPages"],
        ":winnerSection": winner["sectionId"],
        ":variant": winner["variant"],
        ":decisionBucket": decision_artifact["bucket"],
        ":decisionKey": decision_artifact["key"],
        ":decisionVersion": decision_artifact["versionId"],
        ":fullBucket": full_input["bucket"],
        ":fullKey": full_input["key"],
        ":fullVersion": full_input["versionId"],
        ":fullConfig": os.environ.get("FULL_CONFIG_VERSION", "cd-full-v1"),
        ":fullHash": required_env("FULL_CONFIG_SHA256"),
        ":screenWorkflow": execution_arn or "unknown",
    }
    updates = (
        "SET #status=:extracting, updatedAt=:now, selectedAt=:now, selectedBucket=:selectedBucket, "
        "selectedKey=:selectedKey, selectedVersionId=:selectedVersion, selectedChecksumSha256=:selectedChecksum, "
        "selectedPageIds=:selectedPages, selectedSectionId=:winnerSection, selectedDocumentVariant=:variant, "
        "selectionDecisionBucket=:decisionBucket, selectionDecisionKey=:decisionKey, "
        "selectionDecisionVersionId=:decisionVersion, fullInputBucket=:fullBucket, fullInputKey=:fullKey, "
        "fullInputVersionId=:fullVersion, fullConfigVersion=:fullConfig, fullConfigSha256=:fullHash, "
        "idpScreenWorkflowExecutionArn=:screenWorkflow"
    )
    document_values = serialize_map(values)
    upload_values = serialize_map({key: value for key, value in values.items() if key not in {":upload", ":lease"}})
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["documentKey"]),
                "UpdateExpression": updates + " REMOVE failureCode, postprocessLeaseToken, postprocessLeaseExpiresAt",
                "ConditionExpression": (
                    "#status IN (:queued,:screening) AND currentUploadId=:upload AND processingExecutionId=:run "
                    "AND postprocessLeaseToken=:lease"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": document_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["uploadKey"]),
                "UpdateExpression": updates + " REMOVE failureCode",
                "ConditionExpression": "#status IN (:queued,:screening) AND processingExecutionId=:run",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": upload_values,
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        if current.get("status") != "EXTRACTING" or current.get("fullInputVersionId") != full_input["versionId"]:
            raise RetryableState("FULL_QUEUE_TRANSITION_RACED") from exc


def selection_provenance(route: dict[str, Any], decision: dict[str, Any], execution_arn: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "processingExecutionId": route["runId"],
        "documentId": route["upload"]["documentId"],
        "selectorRuleVersion": os.environ.get("SELECTOR_RULE_VERSION", "cd-selection-v1"),
        "screenConfigVersion": os.environ.get("SCREEN_CONFIG_VERSION", "cd-screen-v1"),
        "screenConfigSha256": required_env("SCREEN_CONFIG_SHA256"),
        "idpVersion": os.environ.get("IDP_VERSION", "0.5.16"),
        "idpCommit": required_env("IDP_COMMIT"),
        "workflowExecutionArn": execution_arn or None,
        "decidedAt": utc_now(),
        "decision": decision,
    }


def process_screen(route: dict[str, Any], idp_document: dict[str, Any], execution_arn: str) -> dict[str, Any]:
    lease = acquire_postprocess_lease(route)
    if lease is None:
        current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        return {"status": current.get("status", "UNKNOWN"), "processingExecutionId": route["runId"]}
    sections = idp_document.get("sections")
    if not isinstance(sections, list):
        return transition_status(route, "HOLD", "SCREEN_OUTPUT_SECTIONS_REQUIRED")
    try:
        hydrated_sections = [hydrate_screen_section(section) for section in sections]
    except EventError:
        return transition_status(route, "HOLD", "SCREEN_EXTRACTION_RESULT_INVALID")
    decision = select_closing_disclosure(hydrated_sections, str(route["upload"]["loanId"]))
    provenance = selection_provenance(route, decision, execution_arn)
    decision_artifact = put_json_artifact(route, "selection-decision.json", provenance)
    if decision["status"] != "SELECTED":
        return transition_status(route, "HOLD", str(decision["reason"]))
    try:
        selected = materialize_selected_pdf(route, decision["selectedPages"])
        full_input = stage_full_input(route, selected, decision["selectedPages"])
    except EventError as exc:
        return transition_status(route, "HOLD", str(exc))
    commit_selected(route, lease, decision, decision_artifact, selected, full_input, execution_arn)
    LOGGER.info(
        "cd_selected run_id=%s document_id=%s page_count=%s variant=%s",
        route["runId"],
        route["upload"]["documentId"],
        len(decision["selectedPages"]),
        decision["winner"]["variant"],
    )
    return {
        "status": "EXTRACTING",
        "processingExecutionId": route["runId"],
        "selectedPageCount": len(decision["selectedPages"]),
    }


def section_result(section: dict[str, Any]) -> dict[str, Any]:
    result = {
        "sectionId": str(section.get("section_id", "")),
        "classification": str(section.get("classification", "")),
        "pageIds": page_numbers(section),
        "attributes": section.get("attributes"),
        "confidenceThresholdAlerts": section.get("confidence_threshold_alerts") or [],
    }
    uri = section.get("extraction_result_uri")
    if uri:
        result["extractionResultUri"] = str(uri)
        extraction = read_json_s3_uri(str(uri))
        result["extractionResult"] = extraction
        inference = extraction.get("inference_result")
        if isinstance(inference, dict):
            result["attributes"] = inference
    return result


def data_points_payload(route: dict[str, Any], idp_document: dict[str, Any], execution_arn: str) -> dict[str, Any]:
    sections = idp_document.get("sections")
    if not isinstance(sections, list):
        raise EventError("FULL_OUTPUT_SECTIONS_REQUIRED")
    return {
        "schemaVersion": "1.0",
        "loanId": route["upload"]["loanId"],
        "documentId": route["upload"]["documentId"],
        "uploadId": route["upload"]["uploadId"],
        "processingExecutionId": route["runId"],
        "generatedAt": utc_now(),
        "selectedPageIds": route["document"].get("selectedPageIds") or [],
        "sections": [section_result(section) for section in sections],
        "provenance": {
            "idpVersion": os.environ.get("IDP_VERSION", "0.5.16"),
            "idpCommit": required_env("IDP_COMMIT"),
            "screenConfigVersion": os.environ.get("SCREEN_CONFIG_VERSION", "cd-screen-v1"),
            "screenConfigSha256": required_env("SCREEN_CONFIG_SHA256"),
            "fullConfigVersion": os.environ.get("FULL_CONFIG_VERSION", "cd-full-v1"),
            "fullConfigSha256": required_env("FULL_CONFIG_SHA256"),
            "selectorRuleVersion": os.environ.get("SELECTOR_RULE_VERSION", "cd-selection-v1"),
            "workflowExecutionArn": execution_arn or None,
        },
    }


def commit_succeeded(route: dict[str, Any], lease: str, artifact: dict[str, str], execution_arn: str) -> None:
    now = utc_now()
    raw_values = {
            ":succeeded": "SUCCEEDED",
            ":extracting": "EXTRACTING",
            ":now": now,
            ":run": route["runId"],
            ":upload": route["upload"]["uploadId"],
            ":lease": lease,
            ":bucket": artifact["bucket"],
            ":key": artifact["key"],
            ":version": artifact["versionId"],
            ":checksum": artifact["checksumSha256"],
            ":workflow": execution_arn or "unknown",
            ":idpVersion": os.environ.get("IDP_VERSION", "0.5.16"),
            ":idpCommit": required_env("IDP_COMMIT"),
        }
    document_values = serialize_map(raw_values)
    upload_values = serialize_map({key: value for key, value in raw_values.items() if key not in {":upload", ":lease"}})
    expression = (
        "SET #status=:succeeded, updatedAt=:now, completedAt=:now, dataPointsBucket=:bucket, "
        "dataPointsKey=:key, dataPointsVersionId=:version, dataPointsChecksumSha256=:checksum, "
        "idpFullWorkflowExecutionArn=:workflow, idpVersion=:idpVersion, idpCommit=:idpCommit"
    )
    transactions = [
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["documentKey"]),
                "UpdateExpression": expression + " REMOVE failureCode, postprocessLeaseToken, postprocessLeaseExpiresAt",
                "ConditionExpression": (
                    "#status=:extracting AND currentUploadId=:upload AND processingExecutionId=:run "
                    "AND postprocessLeaseToken=:lease"
                ),
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": document_values,
            }
        },
        {
            "Update": {
                "TableName": required_env("TABLE_NAME"),
                "Key": serialize_map(route["uploadKey"]),
                "UpdateExpression": expression + " REMOVE failureCode",
                "ConditionExpression": "#status=:extracting AND processingExecutionId=:run",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": upload_values,
            }
        },
    ]
    try:
        ddb_client().transact_write_items(TransactItems=transactions, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
            raise
        current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        if current.get("status") != "SUCCEEDED" or current.get("dataPointsVersionId") != artifact["versionId"]:
            raise RetryableState("SUCCESS_TRANSITION_RACED") from exc


def process_full(route: dict[str, Any], idp_document: dict[str, Any], execution_arn: str) -> dict[str, Any]:
    lease = acquire_postprocess_lease(route)
    if lease is None:
        current = table().get_item(Key=route["documentKey"], ConsistentRead=True).get("Item", {})
        return {"status": current.get("status", "UNKNOWN"), "processingExecutionId": route["runId"]}
    try:
        payload = data_points_payload(route, idp_document, execution_arn)
    except EventError as exc:
        return transition_status(route, "HOLD", str(exc))
    artifact = put_json_artifact(route, "data-points.json", payload)
    commit_succeeded(route, lease, artifact, execution_arn)
    LOGGER.info("idp_extraction_succeeded run_id=%s document_id=%s", route["runId"], route["upload"]["documentId"])
    return {"status": "SUCCEEDED", "processingExecutionId": route["runId"]}


def is_watchdog_event(event: dict[str, Any]) -> bool:
    return event.get("source") == WATCHDOG_SOURCE and event.get("detail-type") == WATCHDOG_DETAIL_TYPE


def watchdog_limit() -> int:
    try:
        configured = int(os.environ.get("WATCHDOG_MAX_ITEMS", str(WATCHDOG_MAX_ITEMS_CAP)))
    except ValueError as exc:
        raise RuntimeError("WATCHDOG_MAX_ITEMS must be an integer") from exc
    if configured < 1:
        raise RuntimeError("WATCHDOG_MAX_ITEMS must be positive")
    return min(configured, WATCHDOG_MAX_ITEMS_CAP)


def watchdog_cutoff(now: datetime | None = None) -> str:
    try:
        configured = int(
            os.environ.get("WATCHDOG_MIN_AGE_SECONDS", str(WATCHDOG_MIN_AGE_FLOOR_SECONDS))
        )
    except ValueError as exc:
        raise RuntimeError("WATCHDOG_MIN_AGE_SECONDS must be an integer") from exc
    age_seconds = max(configured, WATCHDOG_MIN_AGE_FLOOR_SECONDS)
    instant = (now or datetime.now(timezone.utc)) - timedelta(seconds=age_seconds)
    timestamp = instant.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return f"{timestamp}#\uffff"


def described_execution_event(description: dict[str, Any], observed_at: str) -> dict[str, Any]:
    status = str(description.get("status") or "").upper()
    if status not in WORKFLOW_EVENT_STATUSES:
        raise EventError("UNSUPPORTED_WORKFLOW_STATUS")
    execution_arn = str(description.get("executionArn") or "")
    state_machine_arn = str(description.get("stateMachineArn") or "")
    if not execution_arn or not state_machine_arn:
        raise EventError("WATCHDOG_EXECUTION_DESCRIPTION_INVALID")
    detail: dict[str, Any] = {
        "status": status,
        "executionArn": execution_arn,
        "stateMachineArn": state_machine_arn,
    }
    if description.get("input") is not None:
        detail["input"] = description["input"]
    if description.get("output") is not None:
        detail["output"] = description["output"]
    return {
        "id": f"watchdog-{hashlib.sha256(f'{execution_arn}:{status}'.encode()).hexdigest()[:24]}",
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "time": observed_at,
        "detail": detail,
    }


def move_active_marker(item: dict[str, Any], next_sort_key: str | None) -> bool:
    """Conditionally reschedule or remove the exact sparse marker read from GSI2."""

    pk = item.get("PK")
    sk = item.get("SK")
    current_sort_key = item.get("GSI2SK")
    if not pk or not sk or not current_sort_key:
        LOGGER.error("active_execution_marker_key_invalid marker_ref=%s", log_reference(pk, sk))
        return False
    values = {
        ":active": ACTIVE_EXECUTION_PARTITION,
        ":current": current_sort_key,
    }
    if next_sort_key is None:
        update = "REMOVE GSI2PK, GSI2SK"
    else:
        update = "SET GSI2SK=:next"
        values[":next"] = next_sort_key
    try:
        table().update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=update,
            ConditionExpression="GSI2PK=:active AND GSI2SK=:current",
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            LOGGER.exception("active_execution_marker_move_failed marker_ref=%s", log_reference(pk, sk))
        return False
    except Exception:  # noqa: BLE001 - a marker maintenance error must not abort the bounded batch
        LOGGER.exception("active_execution_marker_move_failed marker_ref=%s", log_reference(pk, sk))
        return False


def reschedule_active_marker(item: dict[str, Any], execution_arn: str, observed_at: str) -> bool:
    return move_active_marker(item, active_execution_sort_key(execution_arn, observed_at))


def process_workflow_event(event: dict[str, Any]) -> dict[str, Any]:
    header = workflow_header(event)
    document = workflow_document(event, header)
    if header["status"] == "RUNNING":
        if document is None:
            raise EventError("RUNNING_WORKFLOW_INPUT_REQUIRED")
        route = route_workflow(document)
        if route["configMismatch"]:
            return transition_status(route, "HOLD", "IDP_CONFIG_VERSION_MISMATCH")
        return record_workflow_running(route, header)

    if header["status"] in WORKFLOW_FAILURE_STATUSES:
        route = route_workflow(document) if document is not None else find_route_by_execution(header["executionArn"])
        if route["configMismatch"]:
            return transition_status(route, "HOLD", "IDP_CONFIG_VERSION_MISMATCH")
        return transition_workflow_failure(route, header)

    if document is None:
        raise EventError("WORKFLOW_DOCUMENT_REQUIRED")
    workflow = {**header, "document": document}
    route = route_workflow(document)
    if route["configMismatch"]:
        return transition_status(route, "HOLD", "IDP_CONFIG_VERSION_MISMATCH")
    record_workflow_succeeded(route, header)
    if route["stage"] == "screen":
        result = process_screen(route, workflow["document"], workflow["executionArn"])
    else:
        result = process_full(route, workflow["document"], workflow["executionArn"])
    # Keep the sparse marker until all local processing succeeds so a transient
    # failure remains eligible for the next bounded watchdog pass.
    clear_active_execution(route, workflow["executionArn"])
    return result


def reconcile_watchdog() -> dict[str, Any]:
    """Reconcile at most 50 old active executions without scanning the table."""

    limit = watchdog_limit()
    response = table().query(
        IndexName=os.environ.get("ACTIVE_EXECUTION_INDEX_NAME", "GSI2"),
        KeyConditionExpression="GSI2PK = :active AND GSI2SK <= :cutoff",
        ExpressionAttributeValues={
            ":active": ACTIVE_EXECUTION_PARTITION,
            ":cutoff": watchdog_cutoff(),
        },
        ScanIndexForward=True,
        Limit=limit,
    )
    summary: dict[str, Any] = {
        "queried": len(response.get("Items", [])),
        "running": 0,
        "reconciled": 0,
        "errors": 0,
        "rescheduled": 0,
        "invalidRemoved": 0,
        "limit": limit,
    }
    states = aws_client("stepfunctions")
    observed_at = utc_now()
    for item in response.get("Items", []):
        execution_arn = str(item.get("executionArn") or "")
        if item.get("entityType") != "IDP_WORKFLOW" or not execution_arn:
            LOGGER.error(
                "invalid_active_execution_marker marker_ref=%s",
                log_reference(item.get("PK"), item.get("SK")),
            )
            summary["errors"] += 1
            if move_active_marker(item, None):
                summary["invalidRemoved"] += 1
            continue
        try:
            description = states.describe_execution(executionArn=execution_arn)
            status = str(description.get("status") or "").upper()
            if status == "RUNNING":
                summary["running"] += 1
                if reschedule_active_marker(item, execution_arn, observed_at):
                    summary["rescheduled"] += 1
                continue
            if status not in {"SUCCEEDED", *WORKFLOW_FAILURE_STATUSES}:
                LOGGER.warning(
                    "watchdog_execution_not_terminal execution_ref=%s status=%s",
                    log_reference(execution_arn),
                    status,
                )
                summary["running"] += 1
                if reschedule_active_marker(item, execution_arn, observed_at):
                    summary["rescheduled"] += 1
                continue
            process_workflow_event(described_execution_event(description, observed_at))
            summary["reconciled"] += 1
        except Exception:  # noqa: BLE001 - one poisoned execution must not starve the bounded batch
            LOGGER.exception("watchdog_reconciliation_failed execution_ref=%s", log_reference(execution_arn))
            summary["errors"] += 1
            if reschedule_active_marker(item, execution_arn, observed_at):
                summary["rescheduled"] += 1
    return summary


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if is_watchdog_event(event):
        return reconcile_watchdog()
    return process_workflow_event(event)
