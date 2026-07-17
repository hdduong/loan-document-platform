"""Focused, AWS-free regression tests for the provider-neutral loan domain service."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


class _KeyExpression:
    def __and__(self, other: object) -> "_KeyExpression":
        return self


class _Key:
    def __init__(self, name: str) -> None:
        self.name = name

    def eq(self, value: object) -> _KeyExpression:
        return _KeyExpression()

    def begins_with(self, value: object) -> _KeyExpression:
        return _KeyExpression()


class _Serializer:
    def serialize(self, value: object) -> dict[str, object]:
        return {"stub": value}


class _ClientError(Exception):
    def __init__(self, response: dict[str, object], operation_name: str = "operation") -> None:
        super().__init__(str(response))
        self.response = response
        self.operation_name = operation_name


class _DefaultTable:
    def get_item(self, **_: object) -> dict[str, object]:
        return {}

    def query(self, **_: object) -> dict[str, object]:
        return {"Items": []}


class _Resource:
    def Table(self, _: str) -> _DefaultTable:  # noqa: N802 - boto3 API spelling
        return _DefaultTable()


class _Client:
    pass


class _Config:
    def __init__(self, **kwargs: object) -> None:
        for name, value in kwargs.items():
            setattr(self, name, value)


def _load_api_module():
    boto3 = types.ModuleType("boto3")

    def reject_ambient_client(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("loan_api constructed an AWS dependency during import")

    boto3.resource = reject_ambient_client
    boto3.client = reject_ambient_client
    boto3_dynamodb = types.ModuleType("boto3.dynamodb")
    boto3_conditions = types.ModuleType("boto3.dynamodb.conditions")
    boto3_conditions.Key = _Key
    boto3_types = types.ModuleType("boto3.dynamodb.types")
    boto3_types.TypeSerializer = _Serializer
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = _Config
    botocore_exceptions = types.ModuleType("botocore.exceptions")
    botocore_exceptions.ClientError = _ClientError

    modules = {
        "boto3": boto3,
        "boto3.dynamodb": boto3_dynamodb,
        "boto3.dynamodb.conditions": boto3_conditions,
        "boto3.dynamodb.types": boto3_types,
        "botocore": botocore,
        "botocore.config": botocore_config,
        "botocore.exceptions": botocore_exceptions,
    }
    root = Path(__file__).resolve().parents[1]
    module_path = root / "services" / "loan_api" / "app.py"
    spec = importlib.util.spec_from_file_location("loan_api_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        os.environ,
        {
            "TABLE_NAME": "table",
            "SOURCE_BUCKET": "source",
            "DATA_KEY_ARN": "arn:aws:kms:us-west-2:111122223333:key/example",
            "ENTRA_TENANT_ID": "tenant-id",
            "ORIGIN_VERIFY_SECRET": "origin-secret",
            "ALLOWED_CLIENT_IDS": "client-id,service-client-id",
            "REQUIRE_USER_ROLES": "true",
            "UPLOAD_PROCESSOR_ARN": "arn:aws:lambda:us-west-2:111122223333:function:upload-processor",
        },
        clear=False,
    ), patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


api = _load_api_module()


def _claims_event(claims: dict[str, object]) -> dict[str, object]:
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


class AuthorizationTests(unittest.TestCase):
    def test_delegated_token_requires_matching_scope_and_role(self) -> None:
        claims = {
            "tid": "TENANT-ID",
            "azp": "CLIENT-ID",
            "oid": "user-object-id",
            "scp": "Loan.Read Document.Read",
            "roles": ["Loan.Read"],
        }
        auth = api.authorize(_claims_event(claims), "Loan.Read")
        self.assertEqual(auth["actorType"], "user")

        claims["roles"] = ["Document.Read"]
        with self.assertRaises(api.ApiProblem) as caught:
            api.authorize(_claims_event(claims), "Loan.Read")
        self.assertEqual(caught.exception.code, "ROLE_REQUIRED")

    def test_app_token_requires_idtyp_and_matching_role(self) -> None:
        claims = {
            "tid": "tenant-id",
            "azp": "service-client-id",
            "oid": "service-principal-object-id",
            "idtyp": "app",
            "roles": ["Document.Read"],
        }
        auth = api.authorize(_claims_event(claims), "Document.Read")
        self.assertEqual(auth["actorType"], "servicePrincipal")

        claims.pop("idtyp")
        with self.assertRaises(api.ApiProblem) as caught:
            api.authorize(_claims_event(claims), "Document.Read")
        self.assertEqual(caught.exception.code, "TOKEN_TYPE_NOT_ALLOWED")

    def test_app_token_cannot_be_smuggled_through_delegated_scope_branch(self) -> None:
        claims = {
            "tid": "tenant-id",
            "azp": "service-client-id",
            "oid": "service-principal-object-id",
            "idtyp": "app",
            "scp": "Document.Read",
            "roles": ["Document.Read"],
        }
        with self.assertRaises(api.ApiProblem) as caught:
            api.authorize(_claims_event(claims), "Document.Read")
        self.assertEqual(caught.exception.code, "TOKEN_TYPE_NOT_ALLOWED")

    def test_document_read_download_route_cannot_bypass_data_points_permission(self) -> None:
        match = {
            "loanId": "23051",
            "loanSequence": "1",
            "documentId": "doc_11111111-1111-4111-8111-111111111111",
            "documentSequence": "1",
            "sequence": "1",
        }
        event = {"queryStringParameters": {"artifact": "data-points"}}
        with self.assertRaises(api.ApiProblem) as active_caught:
            api.route_document_download(event, {}, match, "cid")
        with self.assertRaises(api.ApiProblem) as archive_caught:
            api.route_archived_document_download(event, {}, match, "cid")
        with self.assertRaises(api.ApiProblem) as version_caught:
            api.route_document_archive_download(event, {}, match, "cid")
        with self.assertRaises(api.ApiProblem) as archived_version_caught:
            api.route_archived_document_archive_download(event, {}, match, "cid")
        self.assertEqual(active_caught.exception.code, "INVALID_ARTIFACT")
        self.assertEqual(archive_caught.exception.code, "INVALID_ARTIFACT")
        self.assertEqual(version_caught.exception.code, "INVALID_ARTIFACT")
        self.assertEqual(archived_version_caught.exception.code, "INVALID_ARTIFACT")

    def test_empty_client_allowlist_fails_closed(self) -> None:
        claims = {
            "tid": "tenant-id",
            "azp": "client-id",
            "oid": "user-object-id",
            "scp": "Loan.Read",
            "roles": ["Loan.Read"],
        }
        with patch.object(api, "ALLOWED_CLIENT_IDS", set()):
            with self.assertRaises(api.ApiProblem) as caught:
                api.authorize(_claims_event(claims), "Loan.Read")
        self.assertEqual(caught.exception.code, "AUTH_CONFIGURATION_ERROR")


class IdempotencyAndPaginationTests(unittest.TestCase):
    def test_headers_are_case_insensitive_and_idempotency_key_is_canonical_uuid(self) -> None:
        event = {
            "headers": {"IDEMPOTENCY-KEY": "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"},
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": "/v1/loans",
        }
        identity, _ = api.idempotency_identity(
            event,
            {"tenantId": "tenant-id", "actorId": "actor"},
            {"loanId": "23051"},
        )
        self.assertTrue(identity["SK"].endswith("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"))

        event["headers"]["IDEMPOTENCY-KEY"] = "------------------------------------"
        with self.assertRaises(api.ApiProblem) as caught:
            api.idempotency_identity(event, {"tenantId": "tenant-id", "actorId": "actor"}, {})
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_KEY_REQUIRED")

    def test_query_all_follows_last_evaluated_key(self) -> None:
        class Table:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def query(self, **kwargs: object) -> dict[str, object]:
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {"Items": [{"page": 1}], "LastEvaluatedKey": {"PK": "next"}}
                return {"Items": [{"page": 2}]}

        table = Table()
        with patch.object(api, "TABLE", table):
            items = api.query_all(KeyConditionExpression="expression", ConsistentRead=True)
        self.assertEqual(items, [{"page": 1}, {"page": 2}])
        self.assertEqual(table.calls[1]["ExclusiveStartKey"], {"PK": "next"})

    def test_query_all_rejects_a_partition_above_the_configured_item_limit(self) -> None:
        class Table:
            def query(self, **_: object) -> dict[str, object]:
                return {"Items": [{"page": 1}, {"page": 2}]}

        with patch.object(api, "TABLE", Table()), patch.object(api, "MAXIMUM_QUERY_ITEMS", 1):
            with self.assertRaises(api.ApiProblem) as caught:
                api.query_all(KeyConditionExpression="expression", ConsistentRead=True)
        self.assertEqual(caught.exception.code, "QUERY_RESULT_LIMIT_EXCEEDED")
        self.assertNotIn("page", caught.exception.title)

    def test_recompletion_with_a_new_key_persists_its_idempotent_response(self) -> None:
        document_id = "doc_11111111-1111-4111-8111-111111111111"
        upload_id = "upl_22222222-2222-4222-8222-222222222222"
        event = {
            "headers": {"Idempotency-Key": "33333333-3333-4333-8333-333333333333"},
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": f"/v1/loans/23051/documents/{document_id}/uploads/{upload_id}/complete",
        }
        upload = {
            "clientCompletedAt": "2026-07-13T00:00:00Z",
            "processingExecutionId": "run_44444444-4444-4444-8444-444444444444",
            "status": "QUEUED",
        }

        class Table:
            def get_item(self, **_: object) -> dict[str, object]:
                return {"Item": upload}

        writes: list[dict[str, object]] = []
        auth = {"tenantId": "tenant-id", "actorId": "actor", "clientId": "client", "actorType": "user"}
        with (
            patch.object(api, "TABLE", Table()),
            patch.object(api, "get_document_item", return_value=("pk", "lin_instance", {"currentUploadId": upload_id})),
            patch.object(api, "get_idempotent", return_value=None),
            patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
        ):
            response = api.complete_upload(event, auth, "23051", document_id, upload_id, "cid")
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(json.loads(response["body"])["status"], "QUEUED")
        self.assertEqual(len(writes), 1)

    def test_completion_replay_rekicks_clean_upload_if_async_invoke_failed(self) -> None:
        document_id = "doc_11111111-1111-4111-8111-111111111111"
        upload_id = "upl_22222222-2222-4222-8222-222222222222"
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": f"/v1/loans/23051/documents/{document_id}/uploads/{upload_id}/complete",
        }
        replay = {
            "status": 202,
            "body": {
                "loanId": "23051",
                "documentId": document_id,
                "uploadId": upload_id,
                "processingExecutionId": "run_44444444-4444-4444-8444-444444444444",
                "status": "VALIDATING",
            },
        }
        upload = {
            "status": "VALIDATING",
            "clientCompletedAt": "2026-07-13T00:00:00Z",
            "malwareScanStatus": "NO_THREATS_FOUND",
            "sourceBucket": "source",
            "sourceKey": "quarantine/source.pdf",
            "sourceVersionId": "version-1",
        }

        class Table:
            def get_item(self, **_: object) -> dict[str, object]:
                return {"Item": upload}

        class Lambda:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def invoke(self, **kwargs: object) -> None:
                self.calls.append(kwargs)

        lambda_client = Lambda()
        auth = {"tenantId": "tenant-id", "actorId": "actor", "clientId": "client", "actorType": "user"}
        with (
            patch.object(api, "TABLE", Table()),
            patch.object(api, "LAMBDA", lambda_client),
            patch.object(api, "UPLOAD_PROCESSOR_ARN", "processor-arn"),
            patch.object(api, "replay_or_none", return_value=({}, "hash", replay)),
            patch.object(api, "get_document_item", return_value=("pk", "lin_instance", {"currentUploadId": upload_id})),
        ):
            response = api.complete_upload(event, auth, "23051", document_id, upload_id, "cid")
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual(len(lambda_client.calls), 1)
        payload = json.loads(lambda_client.calls[0]["Payload"])
        self.assertEqual(payload["detail"]["versionId"], "version-1")

    def test_replacement_upload_clears_prior_version_artifact_references(self) -> None:
        document_id = "doc_11111111-1111-4111-8111-111111111111"
        event = {
            "body": json.dumps(
                {
                    "fileName": "replacement.pdf",
                    "contentType": "application/pdf",
                    "sizeBytes": 100,
                    "checksumSha256": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                }
            ),
            "headers": {"Idempotency-Key": "33333333-3333-4333-8333-333333333333"},
            "requestContext": {"http": {"method": "POST"}},
            "rawPath": f"/v1/loans/23051/documents/{document_id}/uploads",
        }
        auth = {"tenantId": "tenant-id", "actorId": "actor", "clientId": "client", "actorType": "user"}
        writes: list[dict[str, object]] = []
        with (
            patch.object(api, "get_idempotent", return_value=None),
            patch.object(api, "require_active_instance", return_value=({"status": "ACTIVE"}, "lin_instance")),
            patch.object(api, "create_presigned_upload", return_value=({"method": "POST", "url": "https://upload", "fields": {}}, "2026-07-13T01:00:00Z")),
            patch.object(api, "transact", side_effect=lambda items: writes.extend(items)),
        ):
            response = api.create_document(event, auth, "23051", "cid", document_id)
        self.assertEqual(response["statusCode"], 201)
        document_update = next(item["Update"] for item in writes if "Update" in item)
        expression = document_update["UpdateExpression"]
        for attribute in ("sourceKey", "selectedKey", "dataPointsKey"):
            self.assertIn(attribute, expression)


class ArchiveReadTests(unittest.TestCase):
    document_id = "doc_11111111-1111-4111-8111-111111111111"
    instance_id = "lin_22222222-2222-4222-8222-222222222222"
    archive = {
        "PK": "TENANT#tenant-id#LOAN#23051",
        "SK": "ARCHIVE#000000000001",
        "entityType": "LOAN_ARCHIVE",
        "loanId": "23051",
        "loanInstanceId": instance_id,
        "archiveSequence": 1,
        "displayLoanId": "23051_001",
        "status": "ARCHIVED",
        "archivedAt": "2026-07-13T00:00:00Z",
        "documentCount": 1,
        "manifestBucket": "source",
        "manifestKey": "manifest.json",
        "manifestVersionId": "manifest-version",
    }
    manifest = {
        "schemaVersion": 1,
        "tenantId": "tenant-id",
        "loanId": "23051",
        "loanInstanceId": instance_id,
        "archiveSequence": 1,
        "documents": [
            {
                "documentId": document_id,
                "status": "SUCCEEDED",
                "currentUploadId": "upl_33333333-3333-4333-8333-333333333333",
                "fileName": "closing-disclosure.pdf",
                "createdAt": "2026-07-13T00:00:00Z",
                "updatedAt": "2026-07-13T00:01:00Z",
                "source": {"bucket": "source", "key": "source.pdf", "versionId": "source-version"},
                "selected": None,
                "dataPoints": {"bucket": "source", "key": "data.json", "versionId": "data-version"},
            }
        ],
    }

    def _table(self, manifest: dict[str, object] | None = None):
        payload = self.manifest if manifest is None else manifest
        raw = json.dumps(payload).encode("utf-8")
        archive = {
            **self.archive,
            "manifestChecksumSha256": base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii"),
        }

        class Table:
            def get_item(self, **kwargs: object) -> dict[str, object]:
                key = kwargs["Key"]
                return {"Item": archive} if key["SK"] == "ARCHIVE#000000000001" else {}

            def query(self, **_: object) -> dict[str, object]:
                return {"Items": []}

        return Table()

    def _s3(self, manifest: dict[str, object] | None = None):
        payload = self.manifest if manifest is None else manifest
        raw = json.dumps(payload).encode("utf-8")
        checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii")

        class S3:
            def get_object(self, **_: object) -> dict[str, object]:
                return {
                    "Body": io.BytesIO(raw),
                    "ChecksumSHA256": checksum,
                    "ContentLength": len(raw),
                }

            def generate_presigned_url(self, *_: object, **__: object) -> str:
                return "https://example.invalid/pinned-download"

        return S3()

    def test_archived_document_is_scoped_by_loan_archive_and_hides_s3_coordinates(self) -> None:
        auth = {"tenantId": "tenant-id"}
        with patch.object(api, "TABLE", self._table()), patch.object(api, "S3", self._s3()):
            response = api.get_archived_loan_document(auth, "23051", "1", self.document_id, "cid")
        body = json.loads(response["body"])
        self.assertEqual(body["loanArchiveSequence"], 1)
        self.assertEqual(body["artifacts"], ["source", "data-points"])
        self.assertIn("/archives/1/documents/", body["links"]["self"])
        self.assertNotIn("bucket", json.dumps(body))

    def test_archive_manifest_identity_is_verified(self) -> None:
        wrong_manifest = {**self.manifest, "loanId": "different"}
        with patch.object(api, "TABLE", self._table(wrong_manifest)), patch.object(api, "S3", self._s3(wrong_manifest)):
            with self.assertRaises(api.ApiProblem) as caught:
                api.load_loan_archive("tenant-id", "23051", "1")
        self.assertEqual(caught.exception.code, "ARCHIVE_MANIFEST_INVALID")

    def test_archived_download_uses_exact_s3_version(self) -> None:
        auth = {"tenantId": "tenant-id"}
        s3 = self._s3()
        calls: list[dict[str, object]] = []

        def generate_presigned_url(*_: object, **kwargs: object) -> str:
            calls.append(kwargs)
            return "https://example.invalid/pinned-download"

        s3.generate_presigned_url = generate_presigned_url
        with patch.object(api, "TABLE", self._table()), patch.object(api, "S3", s3):
            response = api.archived_download_grant(auth, "23051", "1", self.document_id, "source", "cid")
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(calls[0]["Params"]["VersionId"], "source-version")


class ContractTests(unittest.TestCase):
    def test_contract_includes_sequence_scoped_archive_routes_and_app_auth(self) -> None:
        contract = (Path(__file__).resolve().parents[1] / "contracts" / "openapi" / "loan-api.yaml").read_text(encoding="utf-8")
        self.assertIn("/archives/{loanArchiveSequence}/documents/{documentId}:", contract)
        self.assertIn("/archives/{loanArchiveSequence}/documents/{documentId}/data-points:", contract)
        self.assertIn("/archives/{documentArchiveSequence}/data-points/download:", contract)
        self.assertIn("clientCredentials:", contract)
        self.assertIn("items: { $ref: '#/components/schemas/ArchivedLoanDocumentView' }", contract)

    def test_contract_documents_adapter_wide_request_body_failures(self) -> None:
        contract_path = Path(__file__).resolve().parents[1] / "contracts" / "openapi" / "loan-api.yaml"
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

        product_operations = [
            operation
            for path, path_item in contract["paths"].items()
            if path.startswith("/v1/")
            for method, operation in path_item.items()
            if method in {"get", "post"}
        ]
        self.assertGreater(len(product_operations), 0)
        for operation in product_operations:
            with self.subTest(operationId=operation["operationId"]):
                self.assertEqual(
                    operation["responses"]["400"]["$ref"],
                    "#/components/responses/BadRequest",
                )
                self.assertEqual(
                    operation["responses"]["413"]["$ref"],
                    "#/components/responses/PayloadTooLarge",
                )


if __name__ == "__main__":
    unittest.main()
