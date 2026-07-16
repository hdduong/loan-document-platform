"""Synthetic coverage for IDP post-processing orchestration and AWS boundaries."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "idp_postprocessor_coverage_app", ROOT / "services" / "idp_postprocessor" / "app.py"
)
assert SPEC and SPEC.loader
postprocessor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(postprocessor)


def client_error(code: str) -> Exception:
    return postprocessor.ClientError(
        {"Error": {"Code": code, "Message": "synthetic failure"}}, "SyntheticOperation"
    )


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def route(stage: str = "screen") -> dict[str, Any]:
    document_sk = "INSTANCE#lin_test#DOCUMENT#doc_test"
    upload_sk = f"{document_sk}#UPLOAD#upl_test"
    return {
        "stage": stage,
        "pk": "TENANT#tenant_test#LOAN#23051",
        "documentKey": {"PK": "TENANT#tenant_test#LOAN#23051", "SK": document_sk},
        "uploadKey": {"PK": "TENANT#tenant_test#LOAN#23051", "SK": upload_sk},
        "upload": {
            "entityType": "UPLOAD",
            "loanId": "23051",
            "documentId": "doc_test",
            "uploadId": "upl_test",
            "processingExecutionId": "run_test",
            "sourceBucket": "source-test",
            "sourceKey": "quarantine/tenants/tenant/loans/loan/instances/instance/documents/document/uploads/upload/source.pdf",
            "sourceVersionId": "source-version",
        },
        "document": {
            "entityType": "DOCUMENT",
            "currentUploadId": "upl_test",
            "processingExecutionId": "run_test",
            "selectedPageIds": [1, 2],
        },
        "runId": "run_test",
        "inputBucket": "idp-input-test",
        "inputKey": f"{stage}/run_test/doc_test.pdf",
        "inputVersionId": "input-version",
        "configMismatch": False,
    }


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "TABLE_NAME": "registry-test",
        "SOURCE_BUCKET": "source-test",
        "DATA_KEY_ARN": "arn:aws:kms:us-west-2:111122223333:key/synthetic",
        "IDP_INPUT_BUCKET": "idp-input-test",
        "IDP_WORKING_BUCKET": "idp-working-test",
        "IDP_OUTPUT_BUCKET": "idp-output-test",
        "IDP_COMMIT": "synthetic-commit",
        "SCREEN_CONFIG_SHA256": "a" * 64,
        "FULL_CONFIG_SHA256": "b" * 64,
        "SCREEN_CONFIG_VERSION": "cd-screen-v1",
        "FULL_CONFIG_VERSION": "cd-full-v1",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    postprocessor._AWS.clear()


class Body:
    def __init__(self, value: bytes):
        self.value = value

    def read(self) -> bytes:
        return self.value

    def iter_chunks(self, chunk_size: int) -> list[bytes]:
        return [self.value[:chunk_size], self.value[chunk_size:]]


class RecordingTable:
    def __init__(self, items: dict[tuple[str, str], dict[str, Any]] | None = None):
        self.items = items or {}
        self.updates: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        self.get_override: dict[str, Any] | None = None

    def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:
        if self.get_override is not None:
            return {"Item": self.get_override}
        return {"Item": self.items.get((Key["PK"], Key["SK"]), {})}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        return {"Items": []}


class RecordingDynamoDB:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {}


def test_environment_aws_cache_and_json_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert postprocessor.required_env("TABLE_NAME") == "registry-test"
    monkeypatch.delenv("TABLE_NAME")
    with pytest.raises(RuntimeError, match="TABLE_NAME"):
        postprocessor.required_env("TABLE_NAME")

    fake_boto = SimpleNamespace(
        resource=lambda name: SimpleNamespace(Table=lambda table_name: (name, table_name)),
        client=lambda name: {"client": name},
    )
    monkeypatch.setattr(postprocessor, "boto3", fake_boto)
    monkeypatch.setenv("TABLE_NAME", "registry-test")
    assert postprocessor.table() == ("dynamodb", "registry-test")
    assert postprocessor.table() == ("dynamodb", "registry-test")
    assert postprocessor.ddb_client() == {"client": "dynamodb"}
    assert postprocessor.s3_client() == {"client": "s3"}
    assert postprocessor.json_default(Decimal("2")) == 2
    assert postprocessor.json_default(Decimal("2.5")) == 2.5
    assert postprocessor.json_default(date(2026, 7, 16)) == "2026-07-16"
    with pytest.raises(TypeError):
        postprocessor.json_default(object())
    with pytest.raises(postprocessor.EventError, match="INVALID_WORKFLOW_JSON"):
        postprocessor.parse_json_value("not-json")


def test_document_wrappers_and_workflow_contract_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    direct = {"input_key": "screen/run/document.pdf"}
    assert postprocessor.unwrap_document({"Payload": {"body": json.dumps(direct)}}) == direct
    assert postprocessor.unwrap_document({"document": json.dumps(direct)}) == direct
    with pytest.raises(postprocessor.EventError, match="WORKFLOW_DOCUMENT_REQUIRED"):
        postprocessor.unwrap_document({"Payload": []})
    with pytest.raises(postprocessor.EventError, match="WORKFLOW_DOCUMENT_REQUIRED"):
        postprocessor.workflow_event({"status": "SUCCEEDED"})
    with pytest.raises(postprocessor.EventError, match="INVALID_LOAN_PK"):
        postprocessor.decode_b64url("not+url", "loan_pk")
    with pytest.raises(postprocessor.EventError, match="INVALID_LOAN_PK"):
        postprocessor.decode_b64url("_w", "loan_pk")

    monkeypatch.setenv("IDP_STATE_MACHINE_ARNS", "arn:allowed")
    event = {
        "source": "aws.states",
        "detail-type": "Step Functions Execution Status Change",
        "detail": {"status": "RUNNING", "executionArn": "arn:execution", "stateMachineArn": "arn:other"},
    }
    with pytest.raises(postprocessor.EventError, match="UNEXPECTED_IDP_STATE_MACHINE"):
        postprocessor.workflow_header(event)
    for changed, message in (
        ({"source": "other"}, "UNEXPECTED_WORKFLOW_EVENT_SOURCE"),
        ({"detail-type": "other"}, "UNEXPECTED_WORKFLOW_DETAIL_TYPE"),
    ):
        candidate = {**event, **changed}
        with pytest.raises(postprocessor.EventError, match=message):
            postprocessor.workflow_header(candidate)


def test_read_compressed_json_from_allowlisted_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    class S3:
        def head_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ContentLength": 18}

        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Body": Body(b'{"input_key":"x"}')}

    monkeypatch.setattr(postprocessor, "s3_client", lambda: S3())
    assert postprocessor.expand_compressed(
        {"compressed": True, "s3_uri": "s3://idp-output-test/result.json"}
    ) == {"input_key": "x"}
    plain = {"input_key": "plain"}
    assert postprocessor.expand_compressed(plain) is plain
    for uri, message in (
        ("https://idp-output-test/result.json", "INVALID_IDP_S3_URI"),
        ("s3://untrusted/result.json", "UNTRUSTED_IDP_S3_BUCKET"),
    ):
        with pytest.raises(postprocessor.EventError, match=message):
            postprocessor.read_json_s3_uri(uri)
    with pytest.raises(postprocessor.EventError, match="COMPRESSED_DOCUMENT_URI_REQUIRED"):
        postprocessor.expand_compressed({"compressed": True})


def test_read_json_rejects_oversize_and_invalid_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    class S3:
        def __init__(self, size: int, raw: bytes):
            self.size = size
            self.raw = raw

        def head_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {"ContentLength": self.size}

        def get_object(self, **_kwargs: Any) -> dict[str, Any]:
            return {"Body": Body(self.raw)}

    monkeypatch.setenv("MAXIMUM_IDP_JSON_BYTES", "10")
    monkeypatch.setattr(postprocessor, "s3_client", lambda: S3(11, b"{}"))
    with pytest.raises(postprocessor.EventError, match="IDP_JSON_TOO_LARGE"):
        postprocessor.read_json_s3_uri("s3://idp-output-test/result.json")
    monkeypatch.setattr(postprocessor, "s3_client", lambda: S3(2, b"[]"))
    with pytest.raises(postprocessor.EventError, match="INVALID_IDP_S3_JSON"):
        postprocessor.read_json_s3_uri("s3://idp-output-test/result.json")
    monkeypatch.setattr(postprocessor, "s3_client", lambda: S3(2, b"{"))
    with pytest.raises(postprocessor.EventError, match="INVALID_IDP_S3_JSON"):
        postprocessor.read_json_s3_uri("s3://idp-output-test/result.json")


def route_dependencies(stage: str = "screen") -> tuple[dict[str, Any], RecordingTable, Any]:
    value = route(stage)
    version_field = "screenInputVersionId" if stage == "screen" else "fullInputVersionId"
    key_field = "screenInputKey" if stage == "screen" else "fullInputKey"
    value["document"].update({version_field: "input-version", key_field: value["inputKey"]})
    items = {
        (value["pk"], value["uploadKey"]["SK"]): value["upload"],
        (value["pk"], value["documentKey"]["SK"]): value["document"],
    }
    table = RecordingTable(items)
    metadata = {
        "pipeline-stage": stage,
        "loan-pk-b64": encoded(value["pk"]),
        "upload-sk-b64": encoded(value["uploadKey"]["SK"]),
        "document-sk-b64": encoded(value["documentKey"]["SK"]),
        "processing-execution-id": value["runId"],
        "config-version": "cd-screen-v1" if stage == "screen" else "cd-full-v1",
    }

    class S3:
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"VersionId": "input-version", "Metadata": metadata}

    return value, table, S3()


@pytest.mark.parametrize("stage", ["screen", "full"])
def test_route_workflow_pins_exact_version_and_configuration(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected, fake_table, fake_s3 = route_dependencies(stage)
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    monkeypatch.setattr(postprocessor, "s3_client", lambda: fake_s3)
    result = postprocessor.route_workflow(
        {"input_bucket": expected["inputBucket"], "input_key": expected["inputKey"]}
    )
    assert result["stage"] == stage
    assert result["inputVersionId"] == "input-version"
    assert result["configMismatch"] is False

    mismatch = postprocessor.route_workflow(
        {
            "input_bucket": expected["inputBucket"],
            "input_key": expected["inputKey"],
            "config_version": "unreviewed-config",
        }
    )
    assert mismatch["configMismatch"] is True
    assert "metadata" not in mismatch


def test_route_workflow_rejects_changed_exact_object(monkeypatch: pytest.MonkeyPatch) -> None:
    expected, fake_table, _fake_s3 = route_dependencies()

    class ChangedS3:
        def head_object(self, **kwargs: Any) -> dict[str, Any]:
            _, _, canonical = route_dependencies()
            response = canonical.head_object(**kwargs)
            if "VersionId" in kwargs:
                response["VersionId"] = "different-version"
            return response

    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    monkeypatch.setattr(postprocessor, "s3_client", lambda: ChangedS3())
    with pytest.raises(postprocessor.EventError, match="IDP_INPUT_VERSION_CHANGED"):
        postprocessor.route_workflow(
            {"input_bucket": expected["inputBucket"], "input_key": expected["inputKey"]}
        )
    with pytest.raises(postprocessor.EventError, match="UNEXPECTED_IDP_INPUT_OBJECT"):
        postprocessor.input_reference({"input_bucket": "other", "input_key": "x"})


@pytest.mark.parametrize(
    ("stage", "expected"), [("screen", "SCREENING"), ("full", "EXTRACTING")]
)
def test_record_workflow_running_updates_lookup_and_owned_records(
    stage: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = route(stage)
    fake_table = RecordingTable()
    fake_table.get_override = {"status": expected}
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    result = postprocessor.record_workflow_running(
        value,
        {"executionArn": "arn:execution", "stateMachineArn": "arn:state-machine"},
    )
    assert result["status"] == expected
    assert len(fake_table.updates) == 3
    assert "GSI2PK" in fake_table.updates[0]["UpdateExpression"]
    assert result["workflowExecutionArn"] == "arn:execution"


def test_workflow_audit_success_and_marker_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_table = RecordingTable()
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    value = route()
    header = {
        "executionArn": "arn:execution",
        "stateMachineArn": "arn:state-machine",
        "eventId": "event-test",
        "observedAt": "2026-07-16T12:00:00Z",
    }
    postprocessor.record_workflow_succeeded(value, header)
    postprocessor.clear_active_execution(value, "arn:execution")
    postprocessor.record_workflow_succeeded(value, {**header, "executionArn": ""})
    postprocessor.clear_active_execution(value, "")
    assert len(fake_table.updates) == 2
    assert "terminalEventId" in fake_table.updates[0]["UpdateExpression"]
    assert fake_table.updates[1]["UpdateExpression"] == "REMOVE GSI2PK, GSI2SK"


def test_lease_acquisition_terminal_and_busy_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    value = route()
    fake_table = RecordingTable()
    fake_table.get_override = {"status": "SCREENING"}
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    token = postprocessor.acquire_postprocess_lease(value)
    assert token
    assert fake_table.updates[0]["ExpressionAttributeValues"][":expires"] > 0

    fake_table.get_override = {"status": "SUCCEEDED"}
    assert postprocessor.acquire_postprocess_lease(value) is None
    fake_table.get_override = {"status": "EXTRACTING"}
    assert postprocessor.acquire_postprocess_lease(value) is None

    class BusyTable(RecordingTable):
        def update_item(self, **kwargs: Any) -> dict[str, Any]:
            raise client_error("ConditionalCheckFailedException")

    busy = BusyTable()
    busy.get_override = {"status": "SCREENING"}
    monkeypatch.setattr(postprocessor, "table", lambda: busy)
    with pytest.raises(postprocessor.RetryableState, match="POSTPROCESS_LEASE_BUSY"):
        postprocessor.acquire_postprocess_lease(value)


def test_transition_status_is_atomic_idempotent_and_non_regressive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = route()
    fake_table = RecordingTable()
    fake_table.get_override = {"status": "SCREENING"}
    ddb = RecordingDynamoDB()
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    monkeypatch.setattr(postprocessor, "ddb_client", lambda: ddb)
    result = postprocessor.transition_status(value, "HOLD", "AMBIGUOUS")
    assert result == {
        "status": "HOLD",
        "failureCode": "AMBIGUOUS",
        "processingExecutionId": "run_test",
    }
    assert len(ddb.calls[0]["TransactItems"]) == 2

    fake_table.get_override = {"status": "SUCCEEDED"}
    ignored = postprocessor.transition_status(value, "HOLD", "LATE")
    assert ignored["ignored"] is True
    assert ignored["status"] == "SUCCEEDED"

    canceled = RecordingDynamoDB(client_error("TransactionCanceledException"))
    fake_table.get_override = {"status": "HOLD", "failureCode": "AMBIGUOUS"}
    monkeypatch.setattr(postprocessor, "ddb_client", lambda: canceled)
    assert postprocessor.transition_status(value, "HOLD", "AMBIGUOUS")["status"] == "HOLD"


def test_transition_workflow_failure_records_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    value = route()
    fake_table = RecordingTable()
    fake_table.get_override = {"status": "SCREENING"}
    ddb = RecordingDynamoDB()
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    monkeypatch.setattr(postprocessor, "ddb_client", lambda: ddb)
    result = postprocessor.transition_workflow_failure(
        value,
        {
            "status": "TIMED_OUT",
            "executionArn": "arn:execution",
            "stateMachineArn": "arn:state-machine",
            "eventId": "event-test",
            "observedAt": "2026-07-16T12:00:00Z",
        },
    )
    assert result["failureCode"] == "SCREENING_TIMED_OUT"
    assert len(ddb.calls[0]["TransactItems"]) == 3
    lookup = ddb.calls[0]["TransactItems"][2]["Update"]
    assert "REMOVE GSI2PK, GSI2SK" in lookup["UpdateExpression"]
    with pytest.raises(postprocessor.EventError, match="UNSUPPORTED_WORKFLOW_FAILURE_STATUS"):
        postprocessor.workflow_failure_code("screen", "RUNNING")


def test_transition_workflow_failure_replays_and_late_events_are_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = route("full")
    execution = "arn:execution"
    cleared: list[str] = []
    fake_table = RecordingTable()
    monkeypatch.setattr(postprocessor, "table", lambda: fake_table)
    monkeypatch.setattr(
        postprocessor,
        "clear_active_execution",
        lambda _route, arn: cleared.append(arn),
    )
    header = {
        "status": "FAILED",
        "executionArn": execution,
        "stateMachineArn": "arn:state-machine",
        "eventId": "event-test",
        "observedAt": "2026-07-16T12:00:00Z",
    }
    fake_table.get_override = {
        "status": "FAILED",
        "failureCode": "FULL_EXTRACTION_FAILED",
        "idpTerminalStatus": "FAILED",
        "idpFailedWorkflowExecutionArn": execution,
    }
    replay = postprocessor.transition_workflow_failure(value, header)
    assert replay["idempotent"] is True
    fake_table.get_override = {"status": "SUCCEEDED"}
    ignored = postprocessor.transition_workflow_failure(value, header)
    assert ignored["ignored"] is True
    assert cleared == [execution, execution]


def test_versioned_artifacts_use_checksums_and_required_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class S3:
        def __init__(self):
            self.calls: list[dict[str, Any]] = []

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"VersionId": "artifact-version"}

    s3 = S3()
    monkeypatch.setattr(postprocessor, "s3_client", lambda: s3)
    value = route()
    assert postprocessor.artifact_prefix(value) == (
        "tenants/tenant/loans/loan/instances/instance/documents/document/uploads/upload/artifacts/run_test"
    )
    legacy = route()
    legacy["upload"]["sourceKey"] = "tenants/tenant/loan/document/upload/quarantine/source.pdf"
    assert postprocessor.artifact_prefix(legacy) == "tenants/tenant/loan/document/upload/artifacts/run_test"
    artifact = postprocessor.put_json_artifact(value, "selection.json", {"value": Decimal("2")})
    assert artifact["versionId"] == "artifact-version"
    assert s3.calls[0]["ServerSideEncryption"] == "aws:kms"
    assert json.loads(s3.calls[0]["Body"]) == {"value": 2}
    broken = route()
    broken["upload"]["sourceKey"] = "unexpected.pdf"
    with pytest.raises(postprocessor.EventError, match="UNEXPECTED_SOURCE_KEY"):
        postprocessor.artifact_prefix(broken)


def synthetic_pdf(page_count: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_selected_pdf_is_materialized_from_exact_source_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class S3:
        def __init__(self):
            self.gets: list[dict[str, Any]] = []
            self.puts: list[dict[str, Any]] = []

        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            self.gets.append(kwargs)
            return {"Body": Body(synthetic_pdf())}

        def put_object(self, **kwargs: Any) -> dict[str, Any]:
            self.puts.append(kwargs)
            return {"VersionId": "selected-version"}

    s3 = S3()
    monkeypatch.setattr(postprocessor, "s3_client", lambda: s3)
    result = postprocessor.materialize_selected_pdf(route(), [2])
    assert result["versionId"] == "selected-version"
    assert s3.gets[0]["VersionId"] == "source-version"
    assert len(PdfWriter().pages) == 0  # sanity-check the test uses no customer document
    selected_reader_bytes = s3.puts[0]["Body"]
    assert selected_reader_bytes.startswith(b"%PDF-")
    with pytest.raises(postprocessor.EventError, match="SELECTED_PAGE_OUT_OF_RANGE"):
        postprocessor.materialize_selected_pdf(route(), [3])


def test_full_input_copy_is_version_pinned_and_metadata_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class S3:
        def __init__(self):
            self.copy: dict[str, Any] | None = None

        def copy_object(self, **kwargs: Any) -> dict[str, Any]:
            self.copy = kwargs
            return {"VersionId": "full-version"}

    s3 = S3()
    monkeypatch.setattr(postprocessor, "s3_client", lambda: s3)
    selected = {
        "bucket": "source-test",
        "key": "selected.pdf",
        "versionId": "selected-version",
        "checksumSha256": "checksum",
    }
    result = postprocessor.stage_full_input(route(), selected, [1, 2])
    assert result == {
        "bucket": "idp-input-test",
        "key": "full/run_test/doc_test/upl_test.pdf",
        "versionId": "full-version",
    }
    assert s3.copy is not None
    assert s3.copy["CopySource"]["VersionId"] == "selected-version"
    assert s3.copy["Metadata"]["pipeline-stage"] == "full"
    assert s3.copy["Metadata"]["selected-pages"] == "1,2"
    assert postprocessor.decode_b64url(s3.copy["Metadata"]["loan-pk-b64"], "loan_pk") == route()["pk"]
    with pytest.raises(postprocessor.EventError, match="SELECTED_PAGE_LIMIT_EXCEEDED"):
        postprocessor.stage_full_input(route(), selected, [])


def selected_decision() -> dict[str, Any]:
    winner = {
        "sectionId": "section-1",
        "pages": [1, 2],
        "variant": "FINAL",
        "dateIssued": "2026-07-16",
        "executionEvidence": 1,
        "rank": [2, 1, 1, -1],
        "loanIdentifierPresent": True,
        "identityEvidencePresent": [True, False, True],
    }
    return {
        "status": "SELECTED",
        "reason": "UNIQUE_HIGHEST_EVIDENCE_RANK",
        "winner": winner,
        "selectedPages": [1, 2],
        "candidates": [winner],
    }


def test_commit_selected_and_succeeded_use_conditional_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = RecordingDynamoDB()
    monkeypatch.setattr(postprocessor, "ddb_client", lambda: ddb)
    decision_artifact = {
        "bucket": "source-test",
        "key": "decision.json",
        "versionId": "decision-version",
        "checksumSha256": "decision-checksum",
    }
    selected = {
        "bucket": "source-test",
        "key": "selected.pdf",
        "versionId": "selected-version",
        "checksumSha256": "selected-checksum",
    }
    full_input = {"bucket": "idp-input-test", "key": "full.pdf", "versionId": "full-version"}
    postprocessor.commit_selected(
        route(), "lease", selected_decision(), decision_artifact, selected, full_input, "arn:screen"
    )
    artifact = {
        "bucket": "source-test",
        "key": "data-points.json",
        "versionId": "data-version",
        "checksumSha256": "data-checksum",
    }
    postprocessor.commit_succeeded(route("full"), "lease", artifact, "arn:full")
    assert len(ddb.calls) == 2
    assert len(ddb.calls[0]["TransactItems"]) == 2
    assert "postprocessLeaseToken=:lease" in ddb.calls[0]["TransactItems"][0]["Update"]["ConditionExpression"]
    assert "dataPointsVersionId" in ddb.calls[1]["TransactItems"][0]["Update"]["UpdateExpression"]


def test_process_screen_selected_and_hold_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    value = route()
    calls: list[str] = []
    monkeypatch.setattr(postprocessor, "acquire_postprocess_lease", lambda _route: "lease")
    monkeypatch.setattr(postprocessor, "hydrate_screen_section", lambda section: section)
    monkeypatch.setattr(postprocessor, "select_closing_disclosure", lambda _sections, _loan: selected_decision())
    monkeypatch.setattr(
        postprocessor,
        "put_json_artifact",
        lambda *_args: {
            "bucket": "source-test",
            "key": "decision.json",
            "versionId": "decision-version",
            "checksumSha256": "checksum",
        },
    )
    monkeypatch.setattr(
        postprocessor,
        "materialize_selected_pdf",
        lambda *_args: {
            "bucket": "source-test",
            "key": "selected.pdf",
            "versionId": "selected-version",
            "checksumSha256": "checksum",
        },
    )
    monkeypatch.setattr(
        postprocessor,
        "stage_full_input",
        lambda *_args: {"bucket": "idp-input-test", "key": "full.pdf", "versionId": "full-version"},
    )
    monkeypatch.setattr(postprocessor, "commit_selected", lambda *_args: calls.append("committed"))
    result = postprocessor.process_screen(value, {"sections": [{"section_id": "1"}]}, "arn:screen")
    assert result == {"status": "EXTRACTING", "processingExecutionId": "run_test", "selectedPageCount": 2}
    assert calls == ["committed"]

    holds: list[str] = []
    monkeypatch.setattr(
        postprocessor,
        "transition_status",
        lambda _route, status, code: holds.append(code) or {"status": status, "failureCode": code},
    )
    assert postprocessor.process_screen(value, {}, "arn:screen")["failureCode"] == "SCREEN_OUTPUT_SECTIONS_REQUIRED"
    monkeypatch.setattr(
        postprocessor,
        "hydrate_screen_section",
        lambda _section: (_ for _ in ()).throw(postprocessor.EventError("bad extraction")),
    )
    assert postprocessor.process_screen(value, {"sections": [{}]}, "arn:screen")["failureCode"] == (
        "SCREEN_EXTRACTION_RESULT_INVALID"
    )


def test_full_payload_hydrates_extraction_and_processes_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = route("full")
    monkeypatch.setattr(
        postprocessor,
        "read_json_s3_uri",
        lambda _uri: {"inference_result": {"CashToClose": {"value": "123.45"}}},
    )
    document = {
        "sections": [
            {
                "section_id": "section-1",
                "classification": postprocessor.BORROWER_CD_CLASS,
                "page_ids": ["1", "2"],
                "attributes": {"stale": True},
                "confidence_threshold_alerts": ["synthetic-alert"],
                "extraction_result_uri": "s3://idp-output-test/result.json",
            }
        ]
    }
    payload = postprocessor.data_points_payload(value, document, "arn:full")
    assert payload["sections"][0]["attributes"] == {"CashToClose": {"value": "123.45"}}
    assert payload["provenance"]["workflowExecutionArn"] == "arn:full"

    monkeypatch.setattr(postprocessor, "acquire_postprocess_lease", lambda _route: "lease")
    monkeypatch.setattr(
        postprocessor,
        "put_json_artifact",
        lambda *_args: {
            "bucket": "source-test",
            "key": "data-points.json",
            "versionId": "data-version",
            "checksumSha256": "checksum",
        },
    )
    committed: list[str] = []
    monkeypatch.setattr(postprocessor, "commit_succeeded", lambda *_args: committed.append("done"))
    assert postprocessor.process_full(value, document, "arn:full")["status"] == "SUCCEEDED"
    assert committed == ["done"]

    holds: list[str] = []
    monkeypatch.setattr(
        postprocessor,
        "transition_status",
        lambda _route, status, code: holds.append(code) or {"status": status},
    )
    assert postprocessor.process_full(value, {}, "arn:full")["status"] == "HOLD"
    assert holds == ["FULL_OUTPUT_SECTIONS_REQUIRED"]


def test_process_workflow_event_routes_running_failure_and_full_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = route("full")
    header = {
        "status": "RUNNING",
        "executionArn": "arn:execution",
        "stateMachineArn": "arn:state-machine",
        "eventId": "event",
        "observedAt": "2026-07-16T12:00:00Z",
    }
    monkeypatch.setattr(postprocessor, "workflow_header", lambda _event: header)
    monkeypatch.setattr(postprocessor, "workflow_document", lambda _event, _header: {"input_key": "full.pdf"})
    monkeypatch.setattr(postprocessor, "route_workflow", lambda _document: value)
    monkeypatch.setattr(
        postprocessor,
        "record_workflow_running",
        lambda _route, _header: {"status": "EXTRACTING"},
    )
    assert postprocessor.process_workflow_event({})["status"] == "EXTRACTING"

    header["status"] = "FAILED"
    monkeypatch.setattr(
        postprocessor,
        "transition_workflow_failure",
        lambda _route, _header: {"status": "FAILED"},
    )
    assert postprocessor.process_workflow_event({})["status"] == "FAILED"

    calls: list[str] = []
    header["status"] = "SUCCEEDED"
    monkeypatch.setattr(postprocessor, "record_workflow_succeeded", lambda *_args: calls.append("audit"))
    monkeypatch.setattr(postprocessor, "process_full", lambda *_args: {"status": "SUCCEEDED"})
    monkeypatch.setattr(postprocessor, "clear_active_execution", lambda *_args: calls.append("clear"))
    assert postprocessor.process_workflow_event({})["status"] == "SUCCEEDED"
    assert calls == ["audit", "clear"]


def test_watchdog_validation_marker_errors_and_handler_dispatch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("WATCHDOG_MAX_ITEMS", "0")
    with pytest.raises(RuntimeError, match="positive"):
        postprocessor.watchdog_limit()
    monkeypatch.setenv("WATCHDOG_MAX_ITEMS", "not-a-number")
    with pytest.raises(RuntimeError, match="integer"):
        postprocessor.watchdog_limit()
    monkeypatch.setenv("WATCHDOG_MIN_AGE_SECONDS", "not-a-number")
    with pytest.raises(RuntimeError, match="integer"):
        postprocessor.watchdog_cutoff(datetime(2026, 7, 16, tzinfo=timezone.utc))
    with pytest.raises(postprocessor.EventError, match="UNSUPPORTED_WORKFLOW_STATUS"):
        postprocessor.described_execution_event({"status": "UNKNOWN"}, "now")
    with pytest.raises(postprocessor.EventError, match="WATCHDOG_EXECUTION_DESCRIPTION_INVALID"):
        postprocessor.described_execution_event({"status": "FAILED"}, "now")

    sensitive_pk = "TENANT#secret-tenant#LOAN#23051"
    sensitive_sk = "INSTANCE#secret-instance"
    assert postprocessor.move_active_marker({"PK": sensitive_pk, "SK": sensitive_sk}, None) is False
    assert sensitive_pk not in caplog.text
    assert sensitive_sk not in caplog.text
    assert postprocessor.log_reference(sensitive_pk, sensitive_sk) in caplog.text

    class FailingTable:
        def update_item(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("synthetic table outage")

    monkeypatch.setattr(postprocessor, "table", lambda: FailingTable())
    marker = {"PK": "pk", "SK": "sk", "GSI2SK": "old"}
    assert postprocessor.move_active_marker(marker, "next") is False

    monkeypatch.setattr(postprocessor, "reconcile_watchdog", lambda: {"kind": "watchdog"})
    monkeypatch.setattr(postprocessor, "process_workflow_event", lambda _event: {"kind": "workflow"})
    watchdog = {
        "source": postprocessor.WATCHDOG_SOURCE,
        "detail-type": postprocessor.WATCHDOG_DETAIL_TYPE,
    }
    assert postprocessor.handler(watchdog, None) == {"kind": "watchdog"}
    assert postprocessor.handler({}, None) == {"kind": "workflow"}
