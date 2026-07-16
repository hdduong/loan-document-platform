"""Integration seam between the Azure transport and the real loan domain."""

from __future__ import annotations

import base64
import importlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Iterator

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from test_azure_api_settings import SPA_CLIENT_ID, TENANT_ID, environment

from services.azure_api.auth import AuthProblem, Principal
from services.azure_api.main import create_app
from services.azure_api.settings import Settings

LOAN_ID = "23051"
INSTANCE_ID = "lin_11111111-1111-4111-8111-111111111111"
CREATED_AT = "2026-07-16T00:00:00Z"
CHECKSUM = base64.b64encode(b"x" * 32).decode("ascii")
PERMISSIONS = frozenset(
    {
        "Loan.Create",
        "Loan.Read",
        "Loan.Archive",
        "Document.Upload",
        "Document.Read",
        "Document.Archive",
        "DataPoints.Read",
    }
)
DESERIALIZER = TypeDeserializer()


class ConditionalFailure(RuntimeError):
    """One fake DynamoDB transaction condition did not hold."""


def _decode(values: dict[str, Any]) -> dict[str, Any]:
    return {name: DESERIALIZER.deserialize(value) for name, value in values.items()}


def _matches_key_condition(item: dict[str, Any], condition: Any) -> bool:
    expression = condition.get_expression()
    operator = expression["operator"]
    values = expression["values"]
    if operator == "AND":
        return all(_matches_key_condition(item, value) for value in values)
    attribute = values[0].name
    if operator == "=":
        return item.get(attribute) == values[1]
    if operator == "begins_with":
        return str(item.get(attribute, "")).startswith(str(values[1]))
    raise AssertionError(f"Unsupported key condition: {operator}")


def _condition_holds(
    record: dict[str, Any],
    expression: str,
    names: dict[str, str],
    values: dict[str, Any],
) -> bool:
    for clause in expression.split(" AND "):
        clause = clause.strip()
        if clause.startswith("attribute_not_exists("):
            attribute = clause.removeprefix("attribute_not_exists(").removesuffix(")")
            if attribute in record:
                return False
            continue
        left, right = clause.split("=", maxsplit=1)
        attribute = names.get(left.strip(), left.strip())
        if record.get(attribute) != values[right.strip()]:
            return False
    return True


class FakeTable:
    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.queries: list[dict[str, Any]] = []
        self.lock = threading.RLock()

    def seed(self, *items: dict[str, Any]) -> None:
        with self.lock:
            for item in items:
                self.records[(item["PK"], item["SK"])] = deepcopy(item)

    def get_item(self, *, Key: dict[str, str], **_kwargs: Any) -> dict[str, Any]:  # noqa: N803
        with self.lock:
            item = self.records.get((Key["PK"], Key["SK"]))
            return {"Item": deepcopy(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            self.queries.append(kwargs)
            condition = kwargs["KeyConditionExpression"]
            items = [
                deepcopy(item)
                for item in self.records.values()
                if _matches_key_condition(item, condition)
            ]
            return {"Items": sorted(items, key=lambda item: item["SK"])}

    def update_item(
        self,
        *,
        Key: dict[str, str],  # noqa: N803
        UpdateExpression: str,  # noqa: N803
        ConditionExpression: str,  # noqa: N803
        ExpressionAttributeNames: dict[str, str],  # noqa: N803
        ExpressionAttributeValues: dict[str, Any],  # noqa: N803
    ) -> dict[str, Any]:
        with self.lock:
            key = (Key["PK"], Key["SK"])
            record = self.records.get(key, {})
            if not _condition_holds(
                record,
                ConditionExpression,
                ExpressionAttributeNames,
                ExpressionAttributeValues,
            ):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem",
                )
            assignments = UpdateExpression.removeprefix("SET ").split(", ")
            for assignment in assignments:
                left, right = assignment.split("=", maxsplit=1)
                record[ExpressionAttributeNames.get(left, left)] = ExpressionAttributeValues[right]
            self.records[key] = record
            return {}


class FakeDynamoClient:
    def __init__(self, table: FakeTable) -> None:
        self.table = table
        self.transactions = 0

    @staticmethod
    def _key(operation: dict[str, Any]) -> tuple[str, str]:
        key = _decode(operation["Key"])
        return str(key["PK"]), str(key["SK"])

    def _validate(self, transaction: dict[str, Any]) -> None:
        if "Put" in transaction:
            item = _decode(transaction["Put"]["Item"])
            if (item["PK"], item["SK"]) in self.table.records:
                raise ConditionalFailure
            return
        operation = transaction.get("Update") or transaction.get("ConditionCheck")
        if operation is None:
            raise AssertionError(f"Unsupported transaction operation: {transaction}")
        record = self.table.records.get(self._key(operation), {})
        values = _decode(operation.get("ExpressionAttributeValues", {}))
        if not _condition_holds(
            record,
            operation["ConditionExpression"],
            operation.get("ExpressionAttributeNames", {}),
            values,
        ):
            raise ConditionalFailure

    def _apply_update(self, operation: dict[str, Any]) -> None:
        key = self._key(operation)
        record = self.table.records.get(key, {"PK": key[0], "SK": key[1]})
        values = _decode(operation["ExpressionAttributeValues"])
        expression = operation["UpdateExpression"]

        if "createdAt=if_not_exists" in expression:
            record.update(
                {
                    "currentInstanceId": values[":instance"],
                    "status": values[":active"],
                    "updatedAt": values[":now"],
                }
            )
            record.setdefault("createdAt", values[":now"])
            record.setdefault("lastLoanArchiveSequence", values[":zero"])
            record["revision"] = record.get("revision", 0) + values[":one"]
        elif "REMOVE processingExecutionId" in expression:
            record.update(
                {
                    "status": values[":awaiting"],
                    "currentUploadId": values[":upload"],
                    "updatedAt": values[":now"],
                    "fileName": values[":fileName"],
                }
            )
            removed = expression.split(" REMOVE ", maxsplit=1)[1].split(", ")
            for attribute in removed:
                record.pop(attribute, None)
        elif "lastDocumentArchiveSequence=:sequence" in expression:
            record.update(
                {
                    "status": values[":archived"],
                    "updatedAt": values[":now"],
                    "lastDocumentArchiveSequence": values[":sequence"],
                }
            )
            record.pop("currentUploadId", None)
        else:
            raise AssertionError(f"Unsupported update expression: {expression}")
        self.table.records[key] = record

    def transact_write_items(self, *, TransactItems: list[dict[str, Any]], **_kwargs: Any) -> None:  # noqa: N803
        with self.table.lock:
            snapshot = deepcopy(self.table.records)
            try:
                for transaction in TransactItems:
                    self._validate(transaction)
                for transaction in TransactItems:
                    if "Put" in transaction:
                        item = _decode(transaction["Put"]["Item"])
                        self.table.records[(item["PK"], item["SK"])] = item
                    elif "Update" in transaction:
                        self._apply_update(transaction["Update"])
                self.transactions += 1
            except ConditionalFailure as exc:
                self.table.records = snapshot
                raise ClientError(
                    {"Error": {"Code": "TransactionCanceledException"}},
                    "TransactWriteItems",
                ) from exc


class FakeS3:
    def __init__(self) -> None:
        self.presigned_posts: list[dict[str, Any]] = []

    def generate_presigned_post(self, **kwargs: Any) -> dict[str, Any]:
        self.presigned_posts.append(kwargs)
        sequence = len(self.presigned_posts)
        return {
            "url": f"https://uploads.example.test/{sequence}",
            "fields": {**kwargs["Fields"], "policy": f"policy-{sequence}"},
        }


class FakeDynamoResource:
    def __init__(self, table: FakeTable) -> None:
        self.table = table
        self.table_names: list[str] = []

    def Table(self, table_name: str) -> FakeTable:  # noqa: N802 - boto3 API spelling
        self.table_names.append(table_name)
        return self.table


class FakeAwsSession:
    def __init__(self, table: FakeTable) -> None:
        self.resource_ = FakeDynamoResource(table)
        self.dynamodb = FakeDynamoClient(table)
        self.s3 = FakeS3()
        self.resource_calls: list[str] = []
        self.client_calls: list[str] = []

    def resource(self, name: str, *_args: Any, **_kwargs: Any) -> FakeDynamoResource:
        self.resource_calls.append(name)
        assert name == "dynamodb"
        return self.resource_

    def client(self, name: str, *_args: Any, **_kwargs: Any) -> object:
        self.client_calls.append(name)
        if name == "dynamodb":
            return self.dynamodb
        if name == "s3":
            return self.s3
        if name == "lambda":
            return SimpleNamespace(service=name)
        raise AssertionError(f"Unexpected AWS client: {name}")


class Validator:
    def __init__(self, problem: AuthProblem | None = None) -> None:
        self.problem = problem
        self.calls: list[tuple[str, str]] = []

    def validate(self, token: str, permission: str) -> Principal:
        self.calls.append((token, permission))
        if self.problem is not None:
            raise self.problem
        return Principal(
            tenant_id=TENANT_ID,
            actor_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            client_id=SPA_CLIENT_ID,
            actor_type="user",
            scopes=PERMISSIONS,
            roles=PERMISSIONS,
        )


class Federation:
    def __init__(self, session: FakeAwsSession) -> None:
        self.session = session
        self.minimums: list[int] = []
        self.closed = False

    def session_context(self, minimum: int) -> SimpleNamespace:
        self.minimums.append(minimum)
        return SimpleNamespace(
            session=self.session,
            expiration=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class DomainHarness:
    domain: Any
    session: FakeAwsSession
    table: FakeTable


@pytest.fixture
def real_domain(monkeypatch: pytest.MonkeyPatch) -> Iterator[DomainHarness]:
    """Load the canonical production module without ambient AWS access."""

    table = FakeTable()
    import_session = FakeAwsSession(table)
    runtime_session = FakeAwsSession(table)
    module_name = "services.loan_api.app"
    previous_module = sys.modules.pop(module_name, None)

    monkeypatch.setenv("TABLE_NAME", "loan-registry-prod")
    monkeypatch.setenv("SOURCE_BUCKET", "loan-source-prod")
    monkeypatch.setenv(
        "DATA_KEY_ARN",
        "arn:aws:kms:us-west-2:111122223333:key/11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT_ID)
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", SPA_CLIENT_ID)
    monkeypatch.setenv("REQUIRE_USER_ROLES", "true")
    monkeypatch.setenv(
        "UPLOAD_PROCESSOR_ARN",
        "arn:aws:lambda:us-west-2:111122223333:function:loan-upload-processor-prod",
    )
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(boto3, "resource", import_session.resource)
    monkeypatch.setattr(boto3, "client", import_session.client)

    try:
        domain = importlib.import_module(module_name)
        yield DomainHarness(domain=domain, session=runtime_session, table=table)
    finally:
        sys.modules.pop(module_name, None)
        if previous_module is not None:
            sys.modules[module_name] = previous_module


def _application(
    harness: DomainHarness,
    validator: Validator | None = None,
) -> tuple[TestClient, Validator, Federation, Settings]:
    validator = validator or Validator()
    federation = Federation(harness.session)
    settings = Settings.from_env(environment())
    application = create_app(
        settings,
        validator=validator,
        federation=federation,
        domain_loader=lambda: harness.domain,
    )
    return TestClient(application, base_url="https://api.loans.example.com"), validator, federation, settings


def _headers(idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"authorization": "Bearer signed-user-token"}
    if idempotency_key is not None:
        headers["idempotency-key"] = idempotency_key
    return headers


def _upload_request(file_name: str = "closing-disclosure.pdf") -> dict[str, Any]:
    return {
        "fileName": file_name,
        "contentType": "application/pdf",
        "sizeBytes": 4096,
        "checksumSha256": CHECKSUM,
    }


def _seed_active_loan(table: FakeTable) -> None:
    table.seed(
        {
            "PK": f"TENANT#{TENANT_ID}#LOAN#{LOAN_ID}",
            "SK": "HEAD",
            "currentInstanceId": INSTANCE_ID,
            "status": "ACTIVE",
            "createdAt": CREATED_AT,
            "lastLoanArchiveSequence": 0,
        },
        {
            "PK": f"TENANT#{TENANT_ID}#LOAN#{LOAN_ID}",
            "SK": f"INSTANCE#{INSTANCE_ID}",
            "entityType": "LOAN_INSTANCE",
            "loanId": LOAN_ID,
            "loanInstanceId": INSTANCE_ID,
            "status": "ACTIVE",
            "createdAt": CREATED_AT,
        },
    )


def test_azure_adapter_dispatches_get_loan_through_real_domain(
    real_domain: DomainHarness,
) -> None:
    _seed_active_loan(real_domain.table)
    test_client, validator, federation, settings = _application(real_domain)

    with test_client:
        ready = test_client.get("/ready")
        response = test_client.get(f"/v1/loans/{LOAN_ID}", headers=_headers())

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert response.status_code == 200
    assert response.json() == {
        "loanId": LOAN_ID,
        "current": {
            "loanInstanceId": INSTANCE_ID,
            "status": "ACTIVE",
            "createdAt": CREATED_AT,
            "documents": [],
        },
        "archives": [],
    }
    assert validator.calls == [("signed-user-token", "Loan.Read")]
    assert federation.minimums == [0, settings.max_grant_seconds + settings.aws_credential_refresh_seconds]
    assert federation.closed is True
    assert real_domain.session.resource_calls == ["dynamodb"]
    assert real_domain.session.client_calls == ["dynamodb", "s3", "lambda"]
    assert real_domain.session.resource_.table_names == [real_domain.domain.TABLE_NAME]
    assert len(real_domain.table.queries) == 1
    assert real_domain.table.queries[0]["ConsistentRead"] is True


def test_create_loan_concurrency_idempotent_replay_and_conflict(
    real_domain: DomainHarness,
) -> None:
    test_client, _, _, _ = _application(real_domain)
    keys = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )

    def create(key: str) -> Any:
        return test_client.post(
            "/v1/loans",
            json={"loanId": LOAN_ID},
            headers=_headers(key),
        )

    with test_client, ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(create, keys))
        winner_index = next(index for index, response in enumerate(responses) if response.status_code == 201)
        winner_key = keys[winner_index]
        winner = responses[winner_index]
        loser = responses[1 - winner_index]
        replay = create(winner_key)
        conflicting_reuse = test_client.post(
            "/v1/loans",
            json={"loanId": "23052"},
            headers=_headers(winner_key),
        )

    assert winner.json()["loanId"] == LOAN_ID
    assert loser.status_code == 409
    assert loser.json()["code"] == "LOAN_ALREADY_ACTIVE"
    assert replay.status_code == 201
    assert replay.json() == winner.json()
    assert conflicting_reuse.status_code == 409
    assert conflicting_reuse.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_document_initialization_replay_replacement_and_001_002_archives(
    real_domain: DomainHarness,
) -> None:
    test_client, _, _, _ = _application(real_domain)
    loan_key = "33333333-3333-4333-8333-333333333333"
    upload_key = "44444444-4444-4444-8444-444444444444"
    first_archive_key = "55555555-5555-4555-8555-555555555555"
    replacement_key = "66666666-6666-4666-8666-666666666666"
    second_archive_key = "77777777-7777-4777-8777-777777777777"

    with test_client:
        loan = test_client.post(
            "/v1/loans",
            json={"loanId": LOAN_ID},
            headers=_headers(loan_key),
        )
        initialized = test_client.post(
            f"/v1/loans/{LOAN_ID}/documents",
            json=_upload_request(),
            headers=_headers(upload_key),
        )
        replayed = test_client.post(
            f"/v1/loans/{LOAN_ID}/documents",
            json=_upload_request(),
            headers=_headers(upload_key),
        )

        assert loan.status_code == 201
        assert initialized.status_code == 201
        assert replayed.status_code == 201
        first_upload = initialized.json()
        replay_upload = replayed.json()
        document_id = first_upload["documentId"]
        instance_id = loan.json()["loanInstanceId"]
        assert replay_upload["documentId"] == document_id
        assert replay_upload["uploadId"] == first_upload["uploadId"]
        assert replay_upload["upload"]["url"] != first_upload["upload"]["url"]

        pk = real_domain.domain.loan_pk(TENANT_ID, LOAN_ID)
        document_key = (pk, real_domain.domain.document_sk(instance_id, document_id))
        real_domain.table.records[document_key]["status"] = "SUCCEEDED"
        first_archive = test_client.post(
            f"/v1/loans/{LOAN_ID}/documents/{document_id}/archive",
            headers=_headers(first_archive_key),
        )
        replacement = test_client.post(
            f"/v1/loans/{LOAN_ID}/documents/{document_id}/uploads",
            json=_upload_request("closing-disclosure-v2.pdf"),
            headers=_headers(replacement_key),
        )
        real_domain.table.records[document_key]["status"] = "SUCCEEDED"
        second_archive = test_client.post(
            f"/v1/loans/{LOAN_ID}/documents/{document_id}/archive",
            headers=_headers(second_archive_key),
        )

    assert first_archive.status_code == 201
    assert first_archive.json()["displayDocumentId"] == f"{document_id}_001"
    assert replacement.status_code == 201
    assert replacement.json()["documentId"] == document_id
    assert replacement.json()["uploadId"] != first_upload["uploadId"]
    assert second_archive.status_code == 201
    assert second_archive.json()["displayDocumentId"] == f"{document_id}_002"

    document = real_domain.table.records[document_key]
    assert document["status"] == "ARCHIVED"
    assert int(document["lastDocumentArchiveSequence"]) == 2
    assert "currentUploadId" not in document
    assert (pk, real_domain.domain.document_archive_sk(instance_id, document_id, 1)) in real_domain.table.records
    assert (pk, real_domain.domain.document_archive_sk(instance_id, document_id, 2)) in real_domain.table.records

    idempotency_records = [
        item for item in real_domain.table.records.values() if item.get("entityType") == "IDEMPOTENCY"
    ]
    stored_upload_response = next(
        json.loads(item["responseBody"])
        for item in idempotency_records
        if document_id in item["responseBody"] and item["responseStatus"] == 201
    )
    assert "upload" not in stored_upload_response
    assert "expiresAt" not in stored_upload_response


def test_permission_denial_happens_before_aws_federation(
    real_domain: DomainHarness,
) -> None:
    validator = Validator(AuthProblem(403, "SCOPE_REQUIRED", "Required delegated scope: Loan.Create"))
    test_client, _, federation, _ = _application(real_domain, validator)

    with test_client:
        response = test_client.post(
            "/v1/loans",
            json={"loanId": LOAN_ID},
            headers=_headers("88888888-8888-4888-8888-888888888888"),
        )

    assert response.status_code == 403
    assert response.json()["code"] == "SCOPE_REQUIRED"
    assert federation.minimums == []
    assert real_domain.session.resource_calls == []
    assert real_domain.session.client_calls == []
    assert real_domain.table.records == {}
