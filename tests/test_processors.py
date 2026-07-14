"""Focused tests for exact-version reconciliation and deterministic CD selection."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_module(name: str, relative_path: str):
    specification = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


upload_processor = load_module("upload_processor_app", "services/upload_processor/app.py")
postprocessor = load_module("idp_postprocessor_app", "services/idp_postprocessor/app.py")


def cd_section(section_id, pages, variant="UNKNOWN", issued="", loan_id="23051", evidence=""):
    return {
        "section_id": str(section_id),
        "classification": postprocessor.BORROWER_CD_CLASS,
        "page_ids": [str(page) for page in pages],
        "attributes": {
            "LoanIdentifier": {"value": loan_id},
            "DateIssued": issued,
            "DocumentVariant": variant,
            "VariantEvidenceText": evidence,
            "BorrowerSignaturePresent": False,
            "CoBorrowerSignaturePresent": False,
        },
    }


class UploadProcessorTests(unittest.TestCase):
    def test_guardduty_event_is_normalized_with_exact_version(self):
        event = {
            "id": "evt-1",
            "source": "aws.guardduty",
            "detail-type": upload_processor.GUARDDUTY_DETAIL_TYPE,
            "time": "2026-07-13T01:02:03Z",
            "detail": {
                "schemaVersion": "1.0",
                "scanStatus": "COMPLETED",
                "resourceType": "S3_OBJECT",
                "s3ObjectDetails": {"bucketName": "source", "objectKey": "a/b.pdf", "versionId": "version-a"},
                "scanResultDetails": {"scanResultStatus": "NO_THREATS_FOUND"},
            },
        }
        normalized = upload_processor.event_object(event)
        self.assertEqual("guardduty", normalized["kind"])
        self.assertEqual("version-a", normalized["versionId"])
        self.assertEqual("NO_THREATS_FOUND", normalized["scanResult"])

    def test_scan_record_key_is_version_specific(self):
        first = upload_processor.scan_sort_key("UPLOAD#one", "version-a")
        second = upload_processor.scan_sort_key("UPLOAD#one", "version-b")
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("UPLOAD#one#SCAN#"))

    def test_reconciliation_is_order_independent(self):
        clean = {"scanResultStatus": "NO_THREATS_FOUND", "scanVersionId": "v1"}
        self.assertEqual(
            "WAITING_FOR_CLIENT_COMPLETE",
            upload_processor.reconciliation_action(None, False, clean),
        )
        self.assertEqual(
            "VALIDATE",
            upload_processor.reconciliation_action("v1", True, clean),
        )
        self.assertEqual(
            "WAITING_FOR_SCAN",
            upload_processor.reconciliation_action("v1", True, None),
        )
        self.assertEqual(
            "WAITING_FOR_EXACT_SCAN",
            upload_processor.reconciliation_action(
                "v2", True, {"scanResultStatus": "NO_THREATS_FOUND", "scanVersionId": "v1"}
            ),
        )

    def test_non_clean_results_fail_closed(self):
        self.assertEqual(
            "REJECTED",
            upload_processor.reconciliation_action(
                "v1", True, {"scanResultStatus": "THREATS_FOUND", "scanVersionId": "v1"}
            ),
        )
        self.assertEqual(
            "HOLD",
            upload_processor.reconciliation_action(
                "v1", True, {"scanResultStatus": "ACCESS_DENIED", "scanVersionId": "v1"}
            ),
        )


class ClosingDisclosureSelectorTests(unittest.TestCase):
    def test_pattern_one_result_wrapper_is_supported(self):
        wrapped = {"Result": {"document": {"input_key": "screen/run/doc.pdf"}}}
        self.assertEqual("screen/run/doc.pdf", postprocessor.unwrap_document(wrapped)["input_key"])

    def test_screen_fields_are_hydrated_from_extraction_result(self):
        section = {
            "section_id": "1",
            "classification": postprocessor.BORROWER_CD_CLASS,
            "page_ids": ["1", "2", "3", "4", "5"],
            "attributes": {"page_indices": [0, 1, 2, 3, 4]},
            "extraction_result_uri": "s3://output/result.json",
        }
        original = postprocessor.read_json_s3_uri
        postprocessor.read_json_s3_uri = lambda _uri: {
            "inference_result": {
                "LoanIdentifier": "23051",
                "DocumentVariant": "CORRECTED",
                "VariantEvidenceText": "Corrected Closing Disclosure",
            }
        }
        try:
            hydrated = postprocessor.hydrate_screen_section(section)
        finally:
            postprocessor.read_json_s3_uri = original
        self.assertEqual("CORRECTED", hydrated["attributes"]["DocumentVariant"])
        self.assertEqual({"page_indices": [0, 1, 2, 3, 4]}, section["attributes"])

    def test_explicit_corrected_beats_newer_final(self):
        sections = [
            cd_section("1", range(1, 6), "CORRECTED", "2026-01-01", evidence="Corrected Closing Disclosure"),
            cd_section("2", range(6, 11), "FINAL", "2026-06-01", evidence="Final Closing Disclosure"),
        ]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual("SELECTED", decision["status"])
        self.assertEqual("1", decision["winner"]["sectionId"])

    def test_latest_issue_date_breaks_same_variant_rank(self):
        sections = [
            cd_section("1", range(1, 6), "CORRECTED", "2026-01-01", evidence="Corrected"),
            cd_section("2", range(6, 11), "PCCD", "2026-02-01", evidence="Post-consummation"),
        ]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual("2", decision["winner"]["sectionId"])

    def test_equal_evidence_is_held_instead_of_page_order_tiebreak(self):
        sections = [
            cd_section("1", range(1, 6), "CORRECTED", "2026-01-01", evidence="Corrected"),
            cd_section("2", range(6, 11), "CORRECTED", "2026-01-01", evidence="Corrected"),
        ]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual("HOLD", decision["status"])
        self.assertEqual("CLOSING_DISCLOSURE_AMBIGUOUS", decision["reason"])

    def test_contiguous_addendum_is_materialized_with_winner(self):
        sections = [
            cd_section("1", range(1, 6), "FINAL", "2026-01-01", evidence="Final"),
            {
                "section_id": "2",
                "classification": postprocessor.ADDENDUM_CLASS,
                "page_ids": ["6"],
                "excluded": True,
            },
            {"section_id": "3", "classification": "Other", "page_ids": ["7"]},
        ]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual([1, 2, 3, 4, 5, 6], decision["selectedPages"])

    def test_printed_loan_id_mismatch_is_held(self):
        sections = [cd_section("1", range(1, 6), "FINAL", "2026-01-01", loan_id="99999", evidence="Final")]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual("HOLD", decision["status"])
        self.assertEqual("CLOSING_DISCLOSURE_LOAN_ID_MISMATCH", decision["reason"])

    def test_variant_without_printed_evidence_is_downgraded(self):
        candidate = postprocessor.candidate_from_section(
            cd_section("1", range(1, 6), "CORRECTED", "2026-01-01", evidence="")
        )
        self.assertEqual("UNKNOWN", candidate["variant"])

    def test_invalid_page_ids_fail_closed(self):
        section = cd_section("1", range(1, 6), "FINAL", "2026-01-01", evidence="Final")
        section["page_ids"] = ["1", "3", "2"]
        decision = postprocessor.select_closing_disclosure([section], "23051")
        self.assertEqual("CLOSING_DISCLOSURE_PAGE_IDS_INVALID", decision["reason"])

    def test_candidate_over_selected_page_limit_is_held(self):
        section = cd_section("1", range(1, 10), "FINAL", "2026-01-01", evidence="Final")
        decision = postprocessor.select_closing_disclosure([section], "23051")
        self.assertEqual("HOLD", decision["status"])
        self.assertEqual("CLOSING_DISCLOSURE_PAGE_LIMIT_EXCEEDED", decision["reason"])

    def test_addendum_cannot_push_winner_over_selected_page_limit(self):
        sections = [
            cd_section("1", range(1, 9), "FINAL", "2026-01-01", evidence="Final"),
            {
                "section_id": "2",
                "classification": postprocessor.ADDENDUM_CLASS,
                "page_ids": ["9"],
            },
        ]
        decision = postprocessor.select_closing_disclosure(sections, "23051")
        self.assertEqual("HOLD", decision["status"])
        self.assertEqual("CLOSING_DISCLOSURE_PAGE_LIMIT_EXCEEDED", decision["reason"])


class WorkflowFailureReconciliationTests(unittest.TestCase):
    def test_actual_step_functions_failure_without_input_is_accepted(self):
        event = {
            "id": "event-1",
            "source": "aws.states",
            "detail-type": "Step Functions Execution Status Change",
            "time": "2026-07-13T12:00:00Z",
            "detail": {
                "status": "TIMED_OUT",
                "executionArn": "arn:aws:states:us-west-2:111122223333:execution:idp:one",
                "stateMachineArn": "arn:aws:states:us-west-2:111122223333:stateMachine:idp",
                "inputDetails": {"included": False},
            },
        }
        header = postprocessor.workflow_header(event)
        self.assertEqual("TIMED_OUT", header["status"])
        self.assertIsNone(postprocessor.workflow_document(event, header))

    def test_actual_failed_event_input_document_is_parsed(self):
        event = {
            "source": "aws.states",
            "detail-type": "Step Functions Execution Status Change",
            "detail": {
                "status": "FAILED",
                "executionArn": "arn:aws:states:us-west-2:111122223333:execution:idp:two",
                "input": json.dumps(
                    {"document": {"input_bucket": "idp-input", "input_key": "screen/run/doc.pdf"}}
                ),
            },
        }
        header = postprocessor.workflow_header(event)
        document = postprocessor.workflow_document(event, header)
        self.assertEqual("screen/run/doc.pdf", document["input_key"])

    def test_safe_failure_codes_are_stage_and_terminal_specific(self):
        self.assertEqual(
            "SCREENING_TIMED_OUT",
            postprocessor.workflow_failure_code("screen", "TIMED_OUT"),
        )
        self.assertEqual(
            "FULL_EXTRACTION_ABORTED",
            postprocessor.workflow_failure_code("full", "ABORTED"),
        )

    def test_failure_action_is_idempotent_and_never_regresses(self):
        execution = "arn:aws:states:us-west-2:111122223333:execution:idp:three"
        self.assertEqual(
            "APPLY",
            postprocessor.workflow_failure_action(
                {"status": "SCREENING"}, "screen", "FAILED", execution
            ),
        )
        self.assertEqual(
            "IDEMPOTENT",
            postprocessor.workflow_failure_action(
                {
                    "status": "FAILED",
                    "idpTerminalStatus": "FAILED",
                    "idpFailedWorkflowExecutionArn": execution,
                },
                "screen",
                "FAILED",
                execution,
            ),
        )
        self.assertEqual(
            "IGNORE",
            postprocessor.workflow_failure_action(
                {"status": "EXTRACTING"}, "screen", "FAILED", execution
            ),
        )
        self.assertEqual(
            "IGNORE",
            postprocessor.workflow_failure_action(
                {"status": "SUCCEEDED"}, "full", "TIMED_OUT", execution
            ),
        )

    def test_execution_gsi_lookup_recovers_route_without_workflow_input(self):
        execution = "arn:aws:states:us-west-2:111122223333:execution:idp:four"
        pk = "TENANT#tenant#LOAN#23051"
        document_sk = "INSTANCE#lin#DOC#doc"
        upload_sk = f"{document_sk}#UPLOAD#upl"
        lookup = {
            "PK": pk,
            "SK": f"{upload_sk}#WORKFLOW#SCREEN#hash",
            "entityType": "IDP_WORKFLOW",
            "executionArn": execution,
            "pipelineStage": "screen",
            "processingExecutionId": "run_1",
            "documentPK": pk,
            "documentSK": document_sk,
            "uploadSK": upload_sk,
        }
        upload = {
            "PK": pk,
            "SK": upload_sk,
            "uploadId": "upl",
            "processingExecutionId": "run_1",
        }
        document = {
            "PK": pk,
            "SK": document_sk,
            "currentUploadId": "upl",
            "processingExecutionId": "run_1",
            "screenInputBucket": "idp-input",
            "screenInputKey": "screen/run_1/doc.pdf",
            "screenInputVersionId": "version-1",
        }

        class FakeTable:
            def query(self, **_kwargs):
                return {"Items": [lookup]}

            def get_item(self, Key, **_kwargs):
                return {"Item": upload if Key["SK"] == upload_sk else document}

        prior = postprocessor._AWS.get("table")
        postprocessor._AWS["table"] = FakeTable()
        try:
            route = postprocessor.find_route_by_execution(execution)
        finally:
            if prior is None:
                postprocessor._AWS.pop("table", None)
            else:
                postprocessor._AWS["table"] = prior
        self.assertEqual("screen", route["stage"])
        self.assertEqual("run_1", route["runId"])
        self.assertEqual("version-1", route["inputVersionId"])


class WorkflowWatchdogTests(unittest.TestCase):
    def test_cutoff_never_checks_executions_younger_than_five_minutes(self):
        previous = os.environ.get("WATCHDOG_MIN_AGE_SECONDS")
        os.environ["WATCHDOG_MIN_AGE_SECONDS"] = "1"
        try:
            cutoff = postprocessor.watchdog_cutoff(
                datetime(2026, 7, 14, 12, 5, tzinfo=timezone.utc)
            )
        finally:
            if previous is None:
                os.environ.pop("WATCHDOG_MIN_AGE_SECONDS", None)
            else:
                os.environ["WATCHDOG_MIN_AGE_SECONDS"] = previous
        self.assertEqual("2026-07-14T12:00:00Z#\uffff", cutoff)

    def test_watchdog_rotates_nonterminals_and_errors_without_aborting_batch(self):
        state_machine = "arn:aws:states:us-west-2:111122223333:stateMachine:idp"
        running = "arn:aws:states:us-west-2:111122223333:execution:idp:running"
        pending = "arn:aws:states:us-west-2:111122223333:execution:idp:pending"
        failed = "arn:aws:states:us-west-2:111122223333:execution:idp:failed"
        timed_out = "arn:aws:states:us-west-2:111122223333:execution:idp:timed-out"
        aborted = "arn:aws:states:us-west-2:111122223333:execution:idp:aborted"
        broken = "arn:aws:states:us-west-2:111122223333:execution:idp:broken"
        succeeded = "arn:aws:states:us-west-2:111122223333:execution:idp:succeeded"
        items = [
            {
                "PK": "loan",
                "SK": f"workflow#{name}",
                "entityType": "IDP_WORKFLOW",
                "status": "SUCCEEDED" if name == "succeeded" else "RUNNING",
                "executionArn": arn,
                "GSI2PK": postprocessor.ACTIVE_EXECUTION_PARTITION,
                "GSI2SK": f"2026-07-14T11:00:00Z#{name}",
            }
            for name, arn in (
                ("running", running),
                ("pending", pending),
                ("failed", failed),
                ("timed-out", timed_out),
                ("aborted", aborted),
                ("broken", broken),
                ("succeeded", succeeded),
            )
        ]
        items.insert(
            0,
            {
                "PK": "loan",
                "SK": "workflow#invalid",
                "entityType": "INVALID",
                "GSI2PK": postprocessor.ACTIVE_EXECUTION_PARTITION,
                "GSI2SK": "2026-07-14T10:00:00Z#invalid",
            },
        )

        class FakeTable:
            def __init__(self):
                self.query_kwargs = None
                self.updates = []

            def query(self, **kwargs):
                self.query_kwargs = kwargs
                return {"Items": items}

            def update_item(self, **kwargs):
                self.updates.append(kwargs)
                return {}

        class FakeStepFunctions:
            def __init__(self):
                self.calls = []

            def describe_execution(self, *, executionArn):
                self.calls.append(executionArn)
                if executionArn == broken:
                    raise RuntimeError("temporary Step Functions error")
                status = {
                    running: "RUNNING",
                    pending: "PENDING_REDRIVE",
                    failed: "FAILED",
                    timed_out: "TIMED_OUT",
                    aborted: "ABORTED",
                    succeeded: "SUCCEEDED",
                }[executionArn]
                response = {
                    "status": status,
                    "executionArn": executionArn,
                    "stateMachineArn": state_machine,
                    "input": json.dumps({"document": {"input_key": "screen/run/document.pdf"}}),
                }
                if status == "SUCCEEDED":
                    response["output"] = json.dumps(
                        {"document": {"input_key": "screen/run/document.pdf"}}
                    )
                return response

        fake_table = FakeTable()
        fake_states = FakeStepFunctions()
        processed = []
        prior_table = postprocessor._AWS.get("table")
        prior_states = postprocessor._AWS.get("client:stepfunctions")
        original_process = postprocessor.process_workflow_event
        prior_limit = os.environ.get("WATCHDOG_MAX_ITEMS")
        os.environ["WATCHDOG_MAX_ITEMS"] = "500"
        postprocessor._AWS["table"] = fake_table
        postprocessor._AWS["client:stepfunctions"] = fake_states
        postprocessor.process_workflow_event = lambda event: processed.append(event) or {
            "status": event["detail"]["status"]
        }
        try:
            result = postprocessor.reconcile_watchdog()
        finally:
            postprocessor.process_workflow_event = original_process
            if prior_table is None:
                postprocessor._AWS.pop("table", None)
            else:
                postprocessor._AWS["table"] = prior_table
            if prior_states is None:
                postprocessor._AWS.pop("client:stepfunctions", None)
            else:
                postprocessor._AWS["client:stepfunctions"] = prior_states
            if prior_limit is None:
                os.environ.pop("WATCHDOG_MAX_ITEMS", None)
            else:
                os.environ["WATCHDOG_MAX_ITEMS"] = prior_limit

        self.assertEqual(50, fake_table.query_kwargs["Limit"])
        self.assertEqual("GSI2", fake_table.query_kwargs["IndexName"])
        self.assertIn("GSI2PK = :active", fake_table.query_kwargs["KeyConditionExpression"])
        self.assertEqual(postprocessor.ACTIVE_EXECUTION_PARTITION, fake_table.query_kwargs["ExpressionAttributeValues"][":active"])
        self.assertEqual(
            [running, pending, failed, timed_out, aborted, broken, succeeded],
            fake_states.calls,
        )
        self.assertEqual(
            ["FAILED", "TIMED_OUT", "ABORTED", "SUCCEEDED"],
            [event["detail"]["status"] for event in processed],
        )
        self.assertEqual(
            {
                "queried": 8,
                "running": 2,
                "reconciled": 4,
                "errors": 2,
                "rescheduled": 3,
                "invalidRemoved": 1,
                "limit": 50,
            },
            result,
        )
        self.assertEqual(4, len(fake_table.updates))
        self.assertEqual("REMOVE GSI2PK, GSI2SK", fake_table.updates[0]["UpdateExpression"])
        self.assertTrue(
            all(
                update["UpdateExpression"] == "SET GSI2SK=:next"
                for update in fake_table.updates[1:]
            )
        )
        self.assertTrue(
            all(
                "#" in update["ExpressionAttributeValues"][":next"]
                and update["ExpressionAttributeValues"][":next"]
                != update["ExpressionAttributeValues"][":current"]
                for update in fake_table.updates[1:]
            )
        )

    def test_described_terminal_execution_uses_existing_event_contract(self):
        execution = "arn:aws:states:us-west-2:111122223333:execution:idp:terminal"
        event = postprocessor.described_execution_event(
            {
                "status": "TIMED_OUT",
                "executionArn": execution,
                "stateMachineArn": "arn:aws:states:us-west-2:111122223333:stateMachine:idp",
                "input": "{}",
            },
            "2026-07-14T12:00:00Z",
        )
        self.assertEqual("aws.states", event["source"])
        self.assertEqual("TIMED_OUT", postprocessor.workflow_header(event)["status"])
        self.assertEqual(execution, event["detail"]["executionArn"])

    def test_success_marker_is_cleared_only_after_local_processing(self):
        execution = "arn:aws:states:us-west-2:111122223333:execution:idp:success"
        header = {
            "status": "SUCCEEDED",
            "executionArn": execution,
            "stateMachineArn": "arn:aws:states:us-west-2:111122223333:stateMachine:idp",
            "eventId": "event",
            "observedAt": "2026-07-14T12:00:00Z",
        }
        route = {"stage": "screen", "configMismatch": False}
        originals = {
            "workflow_header": postprocessor.workflow_header,
            "workflow_document": postprocessor.workflow_document,
            "route_workflow": postprocessor.route_workflow,
            "record_workflow_succeeded": postprocessor.record_workflow_succeeded,
            "process_screen": postprocessor.process_screen,
            "clear_active_execution": postprocessor.clear_active_execution,
        }
        calls = []
        postprocessor.workflow_header = lambda _event: header
        postprocessor.workflow_document = lambda _event, _header: {"sections": []}
        postprocessor.route_workflow = lambda _document: route
        postprocessor.record_workflow_succeeded = lambda _route, _header: calls.append("record")
        postprocessor.process_screen = lambda _route, _document, _execution: calls.append("process") or {
            "status": "HOLD"
        }
        postprocessor.clear_active_execution = lambda _route, _execution: calls.append("clear")
        try:
            postprocessor.process_workflow_event({})
            self.assertEqual(["record", "process", "clear"], calls)
            calls.clear()

            def fail_processing(*_args):
                calls.append("process")
                raise postprocessor.RetryableState("retry")

            postprocessor.process_screen = fail_processing
            with self.assertRaises(postprocessor.RetryableState):
                postprocessor.process_workflow_event({})
            self.assertEqual(["record", "process"], calls)
        finally:
            for name, value in originals.items():
                setattr(postprocessor, name, value)


if __name__ == "__main__":
    unittest.main()
