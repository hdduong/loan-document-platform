"""Provider-neutral loan/document lifecycle service.

The Azure transport validates Entra JWTs cryptographically before dispatching
here.  This module independently enforces route claims, tenant/client policy,
object state transitions, idempotency, exact S3 version pinning, and short-lived
artifact grants.  A Lambda adapter remains only for bounded migration rollback;
the production AWS template does not deploy a public Loan API.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import quote

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

TABLE_NAME = os.environ["TABLE_NAME"]
SOURCE_BUCKET = os.environ["SOURCE_BUCKET"]
DATA_KEY_ARN = os.environ["DATA_KEY_ARN"]
ENTRA_TENANT_ID = os.environ["ENTRA_TENANT_ID"]
ORIGIN_VERIFY_SECRET = os.environ.get("ORIGIN_VERIFY_SECRET", "")
ALLOWED_CLIENT_IDS = {value.strip() for value in os.environ.get("ALLOWED_CLIENT_IDS", "").split(",") if value.strip()}
DENIED_CLIENT_IDS = {value.strip() for value in os.environ.get("DENIED_CLIENT_IDS", "").split(",") if value.strip()}
REQUIRE_USER_ROLES = os.environ.get("REQUIRE_USER_ROLES", "true").lower() == "true"
REQUIRE_CLIENT_ALLOWLIST = os.environ.get("REQUIRE_CLIENT_ALLOWLIST", "true").lower() == "true"
MAXIMUM_UPLOAD_BYTES = int(os.environ.get("MAXIMUM_UPLOAD_BYTES", str(100 * 1024 * 1024)))
UPLOAD_URL_SECONDS = int(os.environ.get("UPLOAD_URL_SECONDS", "600"))
DOWNLOAD_URL_SECONDS = int(os.environ.get("DOWNLOAD_URL_SECONDS", "120"))
MAXIMUM_INLINE_DATA_POINTS_BYTES = int(
    os.environ.get("MAXIMUM_INLINE_DATA_POINTS_BYTES", str(5 * 1024 * 1024))
)
MAXIMUM_QUERY_ITEMS = int(os.environ.get("MAXIMUM_QUERY_ITEMS", "5000"))
MAXIMUM_LOAN_ARCHIVE_DOCUMENTS = int(os.environ.get("MAXIMUM_LOAN_ARCHIVE_DOCUMENTS", "500"))
MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES = int(
    os.environ.get("MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES", str(4 * 1024 * 1024))
)
UPLOAD_PROCESSOR_ARN = os.environ["UPLOAD_PROCESSOR_ARN"]

AWS_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"mode": "standard", "total_max_attempts": 3},
    tcp_keepalive=True,
)
# The Azure host is the only production credential boundary. Importing this
# module must never consult ambient AWS credentials or initialize a network
# client; configure_aws_session binds every data-plane dependency per request.
DDB: Any | None = None
TABLE: Any | None = None
DDB_CLIENT: Any | None = None
S3: Any | None = None
LAMBDA: Any | None = None
SERIALIZER = TypeSerializer()
CREDENTIAL_EXPIRY_PROVIDER: Callable[[], datetime | None] | None = None

LOAN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
UUID_KEY_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DOCUMENT_ID_RE = re.compile(r"^doc_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
UPLOAD_ID_RE = re.compile(r"^upl_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TERMINAL_DOCUMENT_STATUSES = {"SUCCEEDED", "HOLD", "REJECTED", "FAILED"}
BLOCKING_LOAN_ARCHIVE_STATUSES = {
    "AWAITING_UPLOAD",
    "VALIDATING",
    "QUEUED",
    "SCREENING",
    "SELECTED",
    "EXTRACTING",
    "ARCHIVING",
}


class ApiProblem(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str = "") -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail or title


def validate_runtime_configuration() -> None:
    """Fail readiness when required private data-plane coordinates are unsafe."""

    if not TABLE_NAME or not SOURCE_BUCKET:
        raise RuntimeError("AWS_DATA_PLANE_CONFIGURATION_INVALID")
    if not re.fullmatch(r"arn:(?:aws|aws-us-gov):kms:[a-z0-9-]+:\d{12}:key/[A-Za-z0-9-]+", DATA_KEY_ARN):
        raise RuntimeError("AWS_KMS_CONFIGURATION_INVALID")
    if not re.fullmatch(
        r"arn:(?:aws|aws-us-gov):lambda:[a-z0-9-]+:\d{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?",
        UPLOAD_PROCESSOR_ARN,
    ):
        raise RuntimeError("UPLOAD_PROCESSOR_CONFIGURATION_INVALID")
    if UPLOAD_URL_SECONDS < 60 or DOWNLOAD_URL_SECONDS < 30:
        raise RuntimeError("SIGNED_GRANT_CONFIGURATION_INVALID")
    if MAXIMUM_UPLOAD_BYTES < 1024 or MAXIMUM_INLINE_DATA_POINTS_BYTES < 1024:
        raise RuntimeError("REQUEST_LIMIT_CONFIGURATION_INVALID")
    if not 100 <= MAXIMUM_QUERY_ITEMS <= 100_000:
        raise RuntimeError("QUERY_LIMIT_CONFIGURATION_INVALID")
    if not 1 <= MAXIMUM_LOAN_ARCHIVE_DOCUMENTS <= 5_000:
        raise RuntimeError("LOAN_ARCHIVE_DOCUMENT_LIMIT_CONFIGURATION_INVALID")
    if MAXIMUM_LOAN_ARCHIVE_DOCUMENTS > MAXIMUM_QUERY_ITEMS:
        raise RuntimeError("LOAN_ARCHIVE_DOCUMENT_LIMIT_CONFIGURATION_INVALID")
    if not 1024 <= MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES <= 20 * 1024 * 1024:
        raise RuntimeError("LOAN_ARCHIVE_MANIFEST_LIMIT_CONFIGURATION_INVALID")


def configure_aws_session(
    session: Any,
    credential_expiry_provider: Callable[[], datetime | None] | None = None,
    *,
    credential_expiration: datetime | None = None,
) -> None:
    """Bind refreshable AWS clients supplied by the hosting runtime.

    The Azure host installs a botocore refreshable-credentials session during
    application startup. Tests may inject an isolated fake session. No caller
    token or static credential is accepted by this boundary.
    """

    if credential_expiry_provider is not None and credential_expiration is not None:
        raise ValueError("Provide either credential_expiry_provider or credential_expiration")
    if credential_expiration is not None:
        if credential_expiration.tzinfo is None:
            raise ValueError("credential_expiration must be timezone-aware")

        def fixed_expiration() -> datetime:
            return credential_expiration

        credential_expiry_provider = fixed_expiration

    global DDB, TABLE, DDB_CLIENT, S3, LAMBDA, CREDENTIAL_EXPIRY_PROVIDER
    DDB = session.resource("dynamodb", config=AWS_CLIENT_CONFIG)
    TABLE = DDB.Table(TABLE_NAME)
    DDB_CLIENT = session.client("dynamodb", config=AWS_CLIENT_CONFIG)
    S3 = session.client("s3", config=AWS_CLIENT_CONFIG)
    LAMBDA = session.client("lambda", config=AWS_CLIENT_CONFIG)
    CREDENTIAL_EXPIRY_PROVIDER = credential_expiry_provider


def effective_grant_seconds(configured_seconds: int) -> int:
    """Cap a signed grant to the remaining temporary credential lifetime."""

    if configured_seconds < 1:
        raise ApiProblem(503, "GRANT_CONFIGURATION_ERROR", "Signed grant configuration is invalid")
    if CREDENTIAL_EXPIRY_PROVIDER is None:
        return configured_seconds
    expiry = CREDENTIAL_EXPIRY_PROVIDER()
    if expiry is None:
        raise ApiProblem(503, "AWS_CREDENTIALS_UNAVAILABLE", "AWS credentials are unavailable")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    remaining = int((expiry.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()) - 30
    if remaining < 30:
        raise ApiProblem(503, "AWS_CREDENTIALS_EXPIRING", "AWS credentials cannot safely sign a grant")
    return min(configured_seconds, remaining)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


def decimal_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Cannot JSON encode {type(value).__name__}")


def json_response(status: int, body: dict[str, Any], correlation_id: str, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    headers = {
        "content-type": "application/json",
        "cache-control": "no-store",
        "x-correlation-id": correlation_id,
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, default=decimal_default, separators=(",", ":")),
    }


def problem_response(problem: ApiProblem, correlation_id: str) -> dict[str, Any]:
    body = {
        "type": f"https://api.loans.invalid/problems/{problem.code.lower().replace('_', '-')}",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "code": problem.code,
        "correlationId": correlation_id,
        "errors": [],
    }
    response = json_response(problem.status, body, correlation_id)
    response["headers"]["content-type"] = "application/problem+json"
    return response


def header_value(event: dict[str, Any], name: str) -> str:
    """Return an API Gateway header without relying on header-name casing."""
    expected = name.casefold()
    for key, value in (event.get("headers") or {}).items():
        if str(key).casefold() == expected:
            return str(value)
    return ""


def correlation_id(event: dict[str, Any]) -> str:
    candidate = header_value(event, "x-correlation-id")
    try:
        return str(uuid.UUID(candidate))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid4())


def require_origin(event: dict[str, Any]) -> None:
    supplied = header_value(event, "x-origin-verify")
    if not ORIGIN_VERIFY_SECRET or not hmac.compare_digest(supplied, ORIGIN_VERIFY_SECRET):
        raise ApiProblem(403, "ORIGIN_NOT_ALLOWED", "Request origin is not allowed")


def parse_claim_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        return {str(item) for item in value}
    text = str(value).strip()
    if not text:
        return set()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return {str(item) for item in parsed}
        except json.JSONDecodeError:
            pass
    return {item for item in re.split(r"[\s,]+", text) if item}


def authorize(event: dict[str, Any], required_permission: str) -> dict[str, str]:
    jwt = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {})
    claims = jwt.get("claims") or {}
    if not claims:
        raise ApiProblem(401, "TOKEN_REQUIRED", "Authentication is required")
    tenant_id = str(claims.get("tid", ""))
    if not tenant_id or tenant_id.casefold() != ENTRA_TENANT_ID.casefold():
        raise ApiProblem(403, "TENANT_NOT_ALLOWED", "Token tenant is not allowed")

    client_id = str(claims.get("azp") or claims.get("appid") or "")
    normalized_client_id = client_id.casefold()
    denied_client_ids = {value.casefold() for value in DENIED_CLIENT_IDS}
    allowed_client_ids = {value.casefold() for value in ALLOWED_CLIENT_IDS}
    if REQUIRE_CLIENT_ALLOWLIST and not allowed_client_ids:
        raise ApiProblem(503, "AUTH_CONFIGURATION_ERROR", "No calling applications are allowlisted")
    if not client_id or normalized_client_id in denied_client_ids:
        raise ApiProblem(403, "CLIENT_NOT_ALLOWED", "Calling application is not allowed")
    if allowed_client_ids and normalized_client_id not in allowed_client_ids:
        raise ApiProblem(403, "CLIENT_NOT_ALLOWED", "Calling application is not allowlisted")

    actor_id = claims.get("oid") or claims.get("sub")
    if not actor_id:
        raise ApiProblem(403, "ACTOR_REQUIRED", "Token has no immutable actor identifier")

    scopes = parse_claim_values(claims.get("scp"))
    roles = parse_claim_values(claims.get("roles"))
    if scopes:
        if claims.get("idtyp") == "app":
            raise ApiProblem(403, "TOKEN_TYPE_NOT_ALLOWED", "An app-only token cannot use delegated scopes")
        if required_permission not in scopes:
            raise ApiProblem(403, "SCOPE_REQUIRED", f"Required delegated scope: {required_permission}")
        if REQUIRE_USER_ROLES and required_permission not in roles:
            raise ApiProblem(403, "ROLE_REQUIRED", f"Required assigned app role: {required_permission}")
        actor_type = "user"
    else:
        if claims.get("idtyp") != "app":
            raise ApiProblem(403, "TOKEN_TYPE_NOT_ALLOWED", "An app-only token must contain idtyp=app")
        if required_permission not in roles:
            raise ApiProblem(403, "ROLE_REQUIRED", f"Required application role: {required_permission}")
        actor_type = "servicePrincipal"

    return {
        "tenantId": tenant_id,
        "actorId": str(actor_id),
        "clientId": client_id,
        "actorType": actor_type,
    }


def parse_body(event: dict[str, Any], required: bool = True) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        if required:
            raise ApiProblem(400, "BODY_REQUIRED", "A JSON request body is required")
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ApiProblem(400, "INVALID_JSON", "Request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ApiProblem(400, "INVALID_JSON", "Request body must be a JSON object")
    return body


def loan_pk(tenant_id: str, loan_id: str) -> str:
    return f"TENANT#{tenant_id}#LOAN#{loan_id}"


def instance_sk(instance_id: str) -> str:
    return f"INSTANCE#{instance_id}"


def document_sk(instance_id: str, document_id: str) -> str:
    return f"INSTANCE#{instance_id}#DOC#{document_id}"


def upload_sk(instance_id: str, document_id: str, upload_id: str) -> str:
    return f"{document_sk(instance_id, document_id)}#UPLOAD#{upload_id}"


def document_archive_sk(instance_id: str, document_id: str, sequence: int) -> str:
    return f"{document_sk(instance_id, document_id)}#ARCHIVE#{sequence:012d}"


def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: SERIALIZER.serialize(value) for key, value in item.items()}


def serialize_values(values: dict[str, Any]) -> dict[str, Any]:
    return {key: SERIALIZER.serialize(value) for key, value in values.items()}


def validate_loan_id(value: str) -> str:
    if not LOAN_ID_RE.fullmatch(value or ""):
        raise ApiProblem(400, "INVALID_LOAN_ID", "loanId must be 1–64 letters, digits, underscore, or hyphen")
    return value


def require_path_id(value: str, regex: re.Pattern[str], name: str) -> str:
    if not regex.fullmatch(value or ""):
        raise ApiProblem(400, f"INVALID_{name.upper()}", f"Invalid {name}")
    return value


def parse_archive_sequence(value: str) -> int:
    try:
        sequence = int(value)
        if sequence < 1:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ApiProblem(400, "INVALID_ARCHIVE_SEQUENCE", "Archive sequence must be a positive integer") from exc
    return sequence


def query_all(**kwargs: Any) -> list[dict[str, Any]]:
    """Read DynamoDB query pages without allowing an unbounded partition load."""
    items: list[dict[str, Any]] = []
    request = dict(kwargs)
    while True:
        page = TABLE.query(**request)
        page_items = page.get("Items", [])
        if not isinstance(page_items, list):
            raise ApiProblem(503, "AWS_DATA_INVALID", "Registry query returned an invalid response")
        if len(items) + len(page_items) > MAXIMUM_QUERY_ITEMS:
            raise ApiProblem(
                409,
                "QUERY_RESULT_LIMIT_EXCEEDED",
                "Resource contains too many registry records to process safely",
            )
        items.extend(page_items)
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            return items
        request["ExclusiveStartKey"] = last_key


def canonical_request_hash(method: str, path: str, body: dict[str, Any]) -> str:
    canonical = json.dumps({"method": method, "path": path, "body": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def idempotency_identity(event: dict[str, Any], auth: dict[str, str], body: dict[str, Any]) -> tuple[dict[str, str], str]:
    key = header_value(event, "idempotency-key")
    if not UUID_KEY_RE.fullmatch(key):
        raise ApiProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key must be a UUID")
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "")
    route_hash = hashlib.sha256(f"{method}:{path}".encode("utf-8")).hexdigest()[:24]
    identity = {
        "PK": f"TENANT#{auth['tenantId']}#IDEMPOTENCY#{auth['actorId']}#{route_hash}",
        "SK": f"KEY#{key.lower()}",
    }
    return identity, canonical_request_hash(method, path, body)


def get_idempotent(identity: dict[str, str], request_hash: str) -> dict[str, Any] | None:
    item = TABLE.get_item(Key=identity, ConsistentRead=True).get("Item")
    if not item:
        return None
    if item.get("requestHash") != request_hash:
        raise ApiProblem(409, "IDEMPOTENCY_KEY_REUSED", "The idempotency key was already used with a different request")
    return {
        "status": int(item["responseStatus"]),
        "body": json.loads(item["responseBody"]),
    }


def idempotency_item(identity: dict[str, str], request_hash: str, status: int, body: dict[str, Any], now: str) -> dict[str, Any]:
    return {
        **identity,
        "entityType": "IDEMPOTENCY",
        "requestHash": request_hash,
        "responseStatus": status,
        "responseBody": json.dumps(body, default=decimal_default, separators=(",", ":")),
        "createdAt": now,
        "expiresAtEpoch": int(time.time()) + 7 * 24 * 60 * 60,
    }


def replay_or_none(event: dict[str, Any], auth: dict[str, str], body: dict[str, Any]) -> tuple[dict[str, str], str, dict[str, Any] | None]:
    identity, request_hash = idempotency_identity(event, auth, body)
    return identity, request_hash, get_idempotent(identity, request_hash)


def transact(items: list[dict[str, Any]]) -> None:
    try:
        DDB_CLIENT.transact_write_items(TransactItems=items, ClientRequestToken=str(uuid.uuid4()))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
            raise ApiProblem(409, "CONCURRENT_STATE_CHANGE", "The resource changed concurrently; refresh and retry") from exc
        raise


def get_head(pk: str) -> dict[str, Any]:
    item = TABLE.get_item(Key={"PK": pk, "SK": "HEAD"}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiProblem(404, "LOAN_NOT_FOUND", "Loan was not found")
    return item


def require_active_instance(pk: str) -> tuple[dict[str, Any], str]:
    head = get_head(pk)
    instance_id = head.get("currentInstanceId")
    if not instance_id or head.get("status") != "ACTIVE":
        raise ApiProblem(409, "LOAN_NOT_ACTIVE", "Loan has no active instance")
    return head, instance_id


def create_loan(event: dict[str, Any], auth: dict[str, str], cid: str) -> dict[str, Any]:
    body = parse_body(event)
    if set(body) != {"loanId"}:
        raise ApiProblem(400, "INVALID_REQUEST", "Only loanId is accepted")
    loan_id = validate_loan_id(str(body.get("loanId", "")))
    identity, request_hash, replay = replay_or_none(event, auth, body)
    if replay:
        return json_response(replay["status"], replay["body"], cid)

    now = utc_now()
    instance_id = new_id("lin")
    pk = loan_pk(auth["tenantId"], loan_id)
    response = {"loanId": loan_id, "loanInstanceId": instance_id, "status": "ACTIVE", "createdAt": now}
    instance = {
        "PK": pk,
        "SK": instance_sk(instance_id),
        "entityType": "LOAN_INSTANCE",
        **response,
        "createdBy": auth["actorId"],
        "createdByClientId": auth["clientId"],
        "updatedAt": now,
    }
    transaction = [
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": "HEAD"}),
                "UpdateExpression": "SET currentInstanceId=:instance, #status=:active, updatedAt=:now, createdAt=if_not_exists(createdAt,:now), lastLoanArchiveSequence=if_not_exists(lastLoanArchiveSequence,:zero) ADD revision :one",
                "ConditionExpression": "attribute_not_exists(currentInstanceId)",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":instance": instance_id, ":active": "ACTIVE", ":now": now, ":zero": 0, ":one": 1}),
            }
        },
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(instance), "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(idempotency_item(identity, request_hash, 201, response, now)), "ConditionExpression": "attribute_not_exists(PK)"}},
    ]
    try:
        transact(transaction)
    except ApiProblem:
        replay = get_idempotent(identity, request_hash)
        if replay:
            return json_response(replay["status"], replay["body"], cid)
        existing = TABLE.get_item(Key={"PK": pk, "SK": "HEAD"}, ConsistentRead=True).get("Item")
        if existing and existing.get("currentInstanceId"):
            raise ApiProblem(409, "LOAN_ALREADY_ACTIVE", "An active instance already exists for this loanId")
        raise
    return json_response(201, response, cid, {"location": f"/v1/loans/{quote(loan_id)}"})


def get_loan(tenant_id: str, loan_id: str, cid: str) -> dict[str, Any]:
    loan_id = validate_loan_id(loan_id)
    pk = loan_pk(tenant_id, loan_id)
    items = query_all(KeyConditionExpression=Key("PK").eq(pk), ConsistentRead=True)
    head = next((item for item in items if item.get("SK") == "HEAD"), None)
    if not head:
        raise ApiProblem(404, "LOAN_NOT_FOUND", "Loan was not found")

    archives = sorted((item for item in items if item.get("entityType") == "LOAN_ARCHIVE"), key=lambda item: int(item["archiveSequence"]), reverse=True)
    current = None
    current_id = head.get("currentInstanceId")
    if current_id:
        instance = next((item for item in items if item.get("entityType") == "LOAN_INSTANCE" and item.get("loanInstanceId") == current_id), None)
        documents = sorted(
            (document_summary(item) for item in items if item.get("entityType") == "DOCUMENT" and item.get("loanInstanceId") == current_id),
            key=lambda value: value["createdAt"],
        )
        current = {
            "loanInstanceId": current_id,
            "status": (instance or {}).get("status", head.get("status", "ACTIVE")),
            "createdAt": (instance or {}).get("createdAt", head.get("createdAt")),
            "documents": documents,
        }
    response = {
        "loanId": loan_id,
        "current": current,
        "archives": [loan_archive_summary(item) for item in archives],
    }
    return json_response(200, response, cid)


def document_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "documentId": item["documentId"],
        "status": item["status"],
        "currentUploadId": item.get("currentUploadId"),
        "createdAt": item["createdAt"],
        "updatedAt": item["updatedAt"],
    }


def loan_archive_summary(item: dict[str, Any]) -> dict[str, Any]:
    loan_id = item["loanId"]
    sequence = int(item["archiveSequence"])
    return {
        "loanId": loan_id,
        "loanInstanceId": item["loanInstanceId"],
        "archiveSequence": sequence,
        "displayLoanId": f"{loan_id}_{sequence:03d}",
        "status": "ARCHIVED",
        "archivedAt": item["archivedAt"],
        "documentCount": int(item.get("documentCount", 0)),
        "links": {"self": f"/v1/loans/{quote(loan_id)}/archives/{sequence}"},
    }


def validate_upload_request(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"fileName", "contentType", "sizeBytes", "checksumSha256"}
    if set(body) != allowed:
        raise ApiProblem(400, "INVALID_REQUEST", f"Required properties: {', '.join(sorted(allowed))}")
    file_name = str(body.get("fileName", ""))
    if not file_name or len(file_name) > 255 or any(character in file_name for character in "\r\n\0"):
        raise ApiProblem(400, "INVALID_FILE_NAME", "fileName must be 1–255 safe characters")
    if body.get("contentType") != "application/pdf" or not file_name.lower().endswith(".pdf"):
        raise ApiProblem(415, "PDF_REQUIRED", "Only PDF uploads are accepted")
    try:
        size = int(body["sizeBytes"])
    except (TypeError, ValueError) as exc:
        raise ApiProblem(400, "INVALID_SIZE", "sizeBytes must be an integer") from exc
    if size < 1 or size > MAXIMUM_UPLOAD_BYTES:
        raise ApiProblem(413, "UPLOAD_TOO_LARGE", f"PDF must be between 1 and {MAXIMUM_UPLOAD_BYTES} bytes")
    checksum = str(body.get("checksumSha256", ""))
    try:
        decoded = base64.b64decode(checksum, validate=True)
    except (ValueError, TypeError) as exc:
        raise ApiProblem(400, "INVALID_CHECKSUM", "checksumSha256 must be Base64 SHA-256") from exc
    if len(decoded) != 32:
        raise ApiProblem(400, "INVALID_CHECKSUM", "checksumSha256 must encode exactly 32 bytes")
    return {"fileName": file_name, "contentType": "application/pdf", "sizeBytes": size, "checksumSha256": checksum}


def create_presigned_upload(key: str, metadata: dict[str, str], request: dict[str, Any]) -> tuple[dict[str, Any], str]:
    grant_seconds = effective_grant_seconds(UPLOAD_URL_SECONDS)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=grant_seconds)).isoformat().replace("+00:00", "Z")
    fields = {
        "Content-Type": "application/pdf",
        "x-amz-checksum-sha256": request["checksumSha256"],
        "x-amz-server-side-encryption": "aws:kms",
        "x-amz-server-side-encryption-aws-kms-key-id": DATA_KEY_ARN,
    }
    conditions: list[Any] = [
        {"Content-Type": "application/pdf"},
        {"x-amz-checksum-sha256": request["checksumSha256"]},
        {"x-amz-server-side-encryption": "aws:kms"},
        {"x-amz-server-side-encryption-aws-kms-key-id": DATA_KEY_ARN},
        ["content-length-range", request["sizeBytes"], request["sizeBytes"]],
    ]
    for name, value in metadata.items():
        header = f"x-amz-meta-{name}"
        fields[header] = value
        conditions.append({header: value})
    presigned = S3.generate_presigned_post(
        Bucket=SOURCE_BUCKET,
        Key=key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=grant_seconds,
    )
    return {"method": "POST", "url": presigned["url"], "fields": presigned["fields"]}, expires_at


def renew_idempotent_upload_session(
    replay: dict[str, Any],
    auth: dict[str, str],
    loan_id: str,
    request: dict[str, Any],
    cid: str,
) -> dict[str, Any]:
    """Mint a new grant for a stable replay without persisting signed fields."""

    stable = replay.get("body")
    if not isinstance(stable, dict) or any(
        not stable.get(name) for name in ("loanId", "documentId", "uploadId")
    ):
        raise ApiProblem(500, "IDEMPOTENCY_RECORD_INVALID", "Stored upload identity is invalid")
    if stable["loanId"] != loan_id:
        raise ApiProblem(500, "IDEMPOTENCY_RECORD_INVALID", "Stored upload identity is invalid")

    document_id = require_path_id(str(stable["documentId"]), DOCUMENT_ID_RE, "documentId")
    upload_id = require_path_id(str(stable["uploadId"]), UPLOAD_ID_RE, "uploadId")
    pk = loan_pk(auth["tenantId"], loan_id)
    _, instance_id = require_active_instance(pk)
    document = TABLE.get_item(
        Key={"PK": pk, "SK": document_sk(instance_id, document_id)},
        ConsistentRead=True,
    ).get("Item")
    upload_item_key = {"PK": pk, "SK": upload_sk(instance_id, document_id, upload_id)}
    upload_item = TABLE.get_item(Key=upload_item_key, ConsistentRead=True).get("Item")
    if (
        not document
        or not upload_item
        or document.get("currentUploadId") != upload_id
        or document.get("status") != "AWAITING_UPLOAD"
        or upload_item.get("status") != "AWAITING_UPLOAD"
    ):
        raise ApiProblem(
            409,
            "UPLOAD_SESSION_NO_LONGER_ACTIVE",
            "The idempotent upload session is no longer awaiting a PDF",
        )
    for name in ("fileName", "contentType", "sizeBytes", "checksumSha256"):
        if upload_item.get(name) != request[name]:
            raise ApiProblem(500, "IDEMPOTENCY_RECORD_INVALID", "Stored upload request is inconsistent")

    grant, expires_at = create_presigned_upload(
        str(upload_item["sourceKey"]),
        {"document-id": document_id, "upload-id": upload_id, "loan-instance-id": instance_id},
        request,
    )
    try:
        TABLE.update_item(
            Key=upload_item_key,
            UpdateExpression="SET uploadExpiresAt=:expires, updatedAt=:now",
            ConditionExpression="#status=:awaiting AND sourceKey=:sourceKey",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":expires": expires_at,
                ":now": utc_now(),
                ":awaiting": "AWAITING_UPLOAD",
                ":sourceKey": upload_item["sourceKey"],
            },
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            raise ApiProblem(
                409,
                "UPLOAD_SESSION_NO_LONGER_ACTIVE",
                "The idempotent upload session is no longer awaiting a PDF",
            ) from exc
        raise
    response = {
        "loanId": loan_id,
        "documentId": document_id,
        "uploadId": upload_id,
        "status": "AWAITING_UPLOAD",
        "expiresAt": expires_at,
        "upload": grant,
    }
    return json_response(
        int(replay["status"]),
        response,
        cid,
        {"location": f"/v1/loans/{quote(loan_id)}/documents/{document_id}"},
    )


def create_document(event: dict[str, Any], auth: dict[str, str], loan_id: str, cid: str, document_id: str | None = None) -> dict[str, Any]:
    loan_id = validate_loan_id(loan_id)
    body = validate_upload_request(parse_body(event))
    identity, request_hash, replay = replay_or_none(event, auth, body)
    if replay:
        return renew_idempotent_upload_session(replay, auth, loan_id, body, cid)

    pk = loan_pk(auth["tenantId"], loan_id)
    head, instance_id = require_active_instance(pk)
    now = utc_now()
    is_replacement = document_id is not None
    document_id = require_path_id(document_id, DOCUMENT_ID_RE, "documentId") if document_id else new_id("doc")
    upload_id = new_id("upl")
    key = f"quarantine/tenants/{auth['tenantId']}/loans/{loan_id}/instances/{instance_id}/documents/{document_id}/uploads/{upload_id}/source.pdf"
    upload, expires_at = create_presigned_upload(
        key,
        {"document-id": document_id, "upload-id": upload_id, "loan-instance-id": instance_id},
        body,
    )
    response = {
        "loanId": loan_id,
        "documentId": document_id,
        "uploadId": upload_id,
        "status": "AWAITING_UPLOAD",
        "expiresAt": expires_at,
        "upload": upload,
    }
    stable_idempotency_response = {
        "loanId": loan_id,
        "documentId": document_id,
        "uploadId": upload_id,
        "status": "AWAITING_UPLOAD",
    }
    doc_key = {"PK": pk, "SK": document_sk(instance_id, document_id)}
    upload_item = {
        "PK": pk,
        "SK": upload_sk(instance_id, document_id, upload_id),
        "entityType": "UPLOAD",
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        "documentId": document_id,
        "uploadId": upload_id,
        "status": "AWAITING_UPLOAD",
        "fileName": body["fileName"],
        "contentType": body["contentType"],
        "sizeBytes": body["sizeBytes"],
        "checksumSha256": body["checksumSha256"],
        "sourceBucket": SOURCE_BUCKET,
        "sourceKey": key,
        "GSI1PK": f"OBJECT#{SOURCE_BUCKET}#{key}",
        "GSI1SK": f"UPLOAD#{upload_id}",
        "uploadExpiresAt": expires_at,
        "createdAt": now,
        "updatedAt": now,
        "createdBy": auth["actorId"],
    }
    transactions: list[dict[str, Any]] = [
        {
            "ConditionCheck": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": "HEAD"}),
                "ConditionExpression": "currentInstanceId=:instance AND #status=:active",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":instance": instance_id, ":active": "ACTIVE"}),
            }
        }
    ]
    if is_replacement:
        transactions.append(
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": serialize_item(doc_key),
                    "UpdateExpression": "SET #status=:awaiting, currentUploadId=:upload, updatedAt=:now, fileName=:fileName REMOVE processingExecutionId, failureCode, sourceBucket, sourceKey, sourceVersionId, sourceChecksumSha256, selectedBucket, selectedKey, selectedVersionId, selectedChecksumSha256, dataPointsBucket, dataPointsKey, dataPointsVersionId, dataPointsChecksumSha256",
                    "ConditionExpression": "#status=:archived AND attribute_not_exists(currentUploadId)",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": serialize_values({":awaiting": "AWAITING_UPLOAD", ":archived": "ARCHIVED", ":upload": upload_id, ":now": now, ":fileName": body["fileName"]}),
                }
            }
        )
    else:
        document_item = {
            **doc_key,
            "entityType": "DOCUMENT",
            "loanId": loan_id,
            "loanInstanceId": instance_id,
            "documentId": document_id,
            "currentUploadId": upload_id,
            "status": "AWAITING_UPLOAD",
            "fileName": body["fileName"],
            "lastDocumentArchiveSequence": 0,
            "createdAt": now,
            "updatedAt": now,
            "createdBy": auth["actorId"],
        }
        transactions.append({"Put": {"TableName": TABLE_NAME, "Item": serialize_item(document_item), "ConditionExpression": "attribute_not_exists(PK)"}})
    transactions.extend(
        [
            {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(upload_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {
                "Put": {
                    "TableName": TABLE_NAME,
                    "Item": serialize_item(
                        idempotency_item(
                            identity,
                            request_hash,
                            201,
                            stable_idempotency_response,
                            now,
                        )
                    ),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        ]
    )
    try:
        transact(transactions)
    except ApiProblem:
        replay = get_idempotent(identity, request_hash)
        if replay:
            return renew_idempotent_upload_session(replay, auth, loan_id, body, cid)
        if is_replacement:
            raise ApiProblem(409, "DOCUMENT_ACTIVE_VERSION_EXISTS", "Archive the current document version before uploading a replacement")
        raise
    return json_response(201, response, cid, {"location": f"/v1/loans/{quote(loan_id)}/documents/{document_id}"})


def get_document_item(tenant_id: str, loan_id: str, document_id: str) -> tuple[str, str, dict[str, Any]]:
    loan_id = validate_loan_id(loan_id)
    document_id = require_path_id(document_id, DOCUMENT_ID_RE, "documentId")
    pk = loan_pk(tenant_id, loan_id)
    _, instance_id = require_active_instance(pk)
    item = TABLE.get_item(Key={"PK": pk, "SK": document_sk(instance_id, document_id)}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiProblem(404, "DOCUMENT_NOT_FOUND", "Document was not found")
    return pk, instance_id, item


def invoke_upload_processor_if_ready(upload: dict[str, Any]) -> None:
    if not UPLOAD_PROCESSOR_ARN:
        return
    if upload.get("status") != "VALIDATING" or not upload.get("clientCompletedAt"):
        return
    if upload.get("malwareScanStatus") != "NO_THREATS_FOUND":
        return
    version_id = upload.get("sourceVersionId")
    if not version_id:
        raise ApiProblem(500, "UPLOAD_STATE_INVALID", "Completed upload has no pinned S3 version")
    LAMBDA.invoke(
        FunctionName=UPLOAD_PROCESSOR_ARN,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "source": "loan-api",
                "detail-type": "Client Upload Complete",
                "detail": {
                    "bucketName": upload["sourceBucket"],
                    "objectKey": upload["sourceKey"],
                    "versionId": version_id,
                },
            }
        ).encode("utf-8"),
    )


def reconcile_upload_processor(
    auth: dict[str, str], loan_id: str, document_id: str, upload_id: str
) -> None:
    if not UPLOAD_PROCESSOR_ARN:
        return
    try:
        pk, instance_id, document = get_document_item(auth["tenantId"], loan_id, document_id)
    except ApiProblem as problem:
        if problem.code in {"LOAN_NOT_ACTIVE", "LOAN_NOT_FOUND", "DOCUMENT_NOT_FOUND"}:
            return
        raise
    if document.get("currentUploadId") != upload_id:
        return
    upload = TABLE.get_item(
        Key={"PK": pk, "SK": upload_sk(instance_id, document_id, upload_id)}, ConsistentRead=True
    ).get("Item")
    if upload:
        invoke_upload_processor_if_ready(upload)


def complete_upload(event: dict[str, Any], auth: dict[str, str], loan_id: str, document_id: str, upload_id: str, cid: str) -> dict[str, Any]:
    body = parse_body(event, required=False)
    if body:
        raise ApiProblem(400, "INVALID_REQUEST", "The completion body must be empty; PDF bytes were already uploaded to S3")
    identity, request_hash, replay = replay_or_none(event, auth, body)
    if replay:
        reconcile_upload_processor(auth, loan_id, document_id, upload_id)
        return json_response(replay["status"], replay["body"], cid)

    pk, instance_id, document = get_document_item(auth["tenantId"], loan_id, document_id)
    upload_id = require_path_id(upload_id, UPLOAD_ID_RE, "uploadId")
    if document.get("currentUploadId") != upload_id:
        raise ApiProblem(409, "UPLOAD_NOT_CURRENT", "Upload is not the current version for this document")
    upload_key = {"PK": pk, "SK": upload_sk(instance_id, document_id, upload_id)}
    upload = TABLE.get_item(Key=upload_key, ConsistentRead=True).get("Item")
    if not upload:
        raise ApiProblem(404, "UPLOAD_NOT_FOUND", "Upload was not found")

    if upload.get("clientCompletedAt"):
        response = {
            "loanId": loan_id,
            "documentId": document_id,
            "uploadId": upload_id,
            "processingExecutionId": upload["processingExecutionId"],
            "status": upload.get("status", "VALIDATING"),
        }
        now = utc_now()
        try:
            transact(
                [
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": serialize_item(idempotency_item(identity, request_hash, 202, response, now)),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    }
                ]
            )
        except ApiProblem:
            replay = get_idempotent(identity, request_hash)
            if replay:
                reconcile_upload_processor(auth, loan_id, document_id, upload_id)
                return json_response(replay["status"], replay["body"], cid)
            raise
        reconcile_upload_processor(auth, loan_id, document_id, upload_id)
        return json_response(202, response, cid)

    try:
        head = S3.head_object(Bucket=upload["sourceBucket"], Key=upload["sourceKey"], ChecksumMode="ENABLED")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            if datetime.fromisoformat(upload["uploadExpiresAt"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
                raise ApiProblem(410, "UPLOAD_EXPIRED", "Upload session expired before an object was received") from exc
            raise ApiProblem(409, "UPLOAD_NOT_READY", "S3 object is not visible yet; retry completion") from exc
        raise
    version_id = head.get("VersionId")
    if not version_id:
        raise ApiProblem(422, "VERSION_REQUIRED", "Uploaded object is not versioned")
    if int(head.get("ContentLength", -1)) != int(upload["sizeBytes"]):
        raise ApiProblem(422, "SIZE_MISMATCH", "Uploaded object size does not match the declared size")
    if head.get("ChecksumSHA256") != upload["checksumSha256"]:
        raise ApiProblem(422, "CHECKSUM_MISMATCH", "Uploaded object SHA-256 does not match")
    if head.get("ContentType") != "application/pdf":
        raise ApiProblem(422, "CONTENT_TYPE_MISMATCH", "Uploaded object is not application/pdf")
    expected_metadata = {
        "document-id": document_id,
        "upload-id": upload_id,
        "loan-instance-id": instance_id,
    }
    metadata = head.get("Metadata") or {}
    if any(metadata.get(name) != value for name, value in expected_metadata.items()):
        raise ApiProblem(422, "METADATA_MISMATCH", "Uploaded object metadata does not match its upload session")
    if head.get("ServerSideEncryption") != "aws:kms" or head.get("SSEKMSKeyId") != DATA_KEY_ARN:
        raise ApiProblem(422, "ENCRYPTION_MISMATCH", "Uploaded object is not encrypted with the required KMS key")
    prefix_body = S3.get_object(
        Bucket=upload["sourceBucket"],
        Key=upload["sourceKey"],
        VersionId=version_id,
        Range="bytes=0-4",
    )["Body"]
    try:
        prefix = prefix_body.read()
    finally:
        close = getattr(prefix_body, "close", None)
        if callable(close):
            close()
    if not prefix.startswith(b"%PDF-"):
        raise ApiProblem(422, "INVALID_PDF", "Uploaded object does not have a PDF signature")

    now = utc_now()
    execution_id = new_id("run")
    response = {
        "loanId": loan_id,
        "documentId": document_id,
        "uploadId": upload_id,
        "processingExecutionId": execution_id,
        "status": "VALIDATING",
    }
    transaction = [
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item(upload_key),
                "UpdateExpression": "SET clientCompletedAt=:now, updatedAt=:now, #status=:validating, processingExecutionId=:execution, sourceVersionId=:version",
                "ConditionExpression": "attribute_not_exists(clientCompletedAt) AND #status=:awaiting",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":now": now, ":validating": "VALIDATING", ":awaiting": "AWAITING_UPLOAD", ":execution": execution_id, ":version": version_id}),
            }
        },
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": document_sk(instance_id, document_id)}),
                "UpdateExpression": "SET #status=:validating, updatedAt=:now, processingExecutionId=:execution",
                "ConditionExpression": "currentUploadId=:upload AND #status=:awaiting",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":validating": "VALIDATING", ":awaiting": "AWAITING_UPLOAD", ":upload": upload_id, ":now": now, ":execution": execution_id}),
            }
        },
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(idempotency_item(identity, request_hash, 202, response, now)), "ConditionExpression": "attribute_not_exists(PK)"}},
    ]
    try:
        transact(transaction)
    except ApiProblem:
        replay = get_idempotent(identity, request_hash)
        if replay:
            reconcile_upload_processor(auth, loan_id, document_id, upload_id)
            return json_response(replay["status"], replay["body"], cid)
        raise

    # Re-read after the transaction before using the scan summary. A clean
    # GuardDuty event can race between the initial upload read and this commit;
    # using that stale snapshot would leave both signals waiting on each other.
    # DynamoDB Streams provides the durable retry path; this direct invoke is a
    # latency optimization for the already-clean case.
    reconcile_upload_processor(auth, loan_id, document_id, upload_id)
    return json_response(202, response, cid)


def get_document(auth: dict[str, str], loan_id: str, document_id: str, cid: str) -> dict[str, Any]:
    pk, instance_id, item = get_document_item(auth["tenantId"], loan_id, document_id)
    archives = query_all(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{document_sk(instance_id, document_id)}#ARCHIVE#"),
        ConsistentRead=True,
    )
    artifacts = []
    if item.get("sourceKey"):
        artifacts.append("source")
    if item.get("selectedKey"):
        artifacts.append("selected")
    if item.get("dataPointsKey"):
        artifacts.append("data-points")
    response = {
        **document_summary(item),
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        "processingExecutionId": item.get("processingExecutionId"),
        "failureCode": item.get("failureCode"),
        "archives": [document_archive_summary(archive) for archive in sorted(archives, key=lambda value: int(value["archiveSequence"]), reverse=True)],
        "artifacts": artifacts,
        "links": {
            "self": f"/v1/loans/{quote(loan_id)}/documents/{document_id}",
            "dataPoints": f"/v1/loans/{quote(loan_id)}/documents/{document_id}/data-points",
        },
    }
    return json_response(200, response, cid)


def document_archive_summary(item: dict[str, Any], loan_archive_sequence: int | None = None) -> dict[str, Any]:
    sequence = int(item["archiveSequence"])
    document_id = item["documentId"]
    loan_id = item["loanId"]
    if loan_archive_sequence is None:
        base = f"/v1/loans/{quote(loan_id)}/documents/{document_id}"
    else:
        base = f"/v1/loans/{quote(loan_id)}/archives/{loan_archive_sequence}/documents/{document_id}"
    archive_base = f"{base}/archives/{sequence}"
    return {
        "documentId": document_id,
        "uploadId": item["uploadId"],
        "archiveSequence": sequence,
        "displayDocumentId": f"{document_id}_{sequence:03d}",
        "status": "ARCHIVED",
        "archivedAt": item["archivedAt"],
        "links": {
            "self": archive_base,
            "dataPoints": f"{archive_base}/data-points",
            "download": f"{archive_base}/download",
        },
    }


def archive_document(event: dict[str, Any], auth: dict[str, str], loan_id: str, document_id: str, cid: str) -> dict[str, Any]:
    body = parse_body(event, required=False)
    if body:
        raise ApiProblem(400, "INVALID_REQUEST", "The archive request body must be empty")
    identity, request_hash, replay = replay_or_none(event, auth, body)
    if replay:
        return json_response(replay["status"], replay["body"], cid)
    pk, instance_id, document = get_document_item(auth["tenantId"], loan_id, document_id)
    upload_id = document.get("currentUploadId")
    if not upload_id:
        raise ApiProblem(409, "DOCUMENT_ALREADY_ARCHIVED", "Document has no active version")
    if document.get("status") not in TERMINAL_DOCUMENT_STATUSES:
        raise ApiProblem(409, "DOCUMENT_PROCESSING", "Only a terminal document version can be archived")
    sequence = int(document.get("lastDocumentArchiveSequence", 0)) + 1
    now = utc_now()
    response = {
        "documentId": document_id,
        "uploadId": upload_id,
        "archiveSequence": sequence,
        "displayDocumentId": f"{document_id}_{sequence:03d}",
        "status": "ARCHIVED",
        "archivedAt": now,
    }
    archive = {
        "PK": pk,
        "SK": document_archive_sk(instance_id, document_id, sequence),
        "entityType": "DOCUMENT_ARCHIVE",
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        **response,
        "sourceBucket": document.get("sourceBucket"),
        "sourceKey": document.get("sourceKey"),
        "sourceVersionId": document.get("sourceVersionId"),
        "sourceChecksumSha256": document.get("sourceChecksumSha256"),
        "selectedBucket": document.get("selectedBucket"),
        "selectedKey": document.get("selectedKey"),
        "selectedVersionId": document.get("selectedVersionId"),
        "selectedChecksumSha256": document.get("selectedChecksumSha256"),
        "dataPointsBucket": document.get("dataPointsBucket"),
        "dataPointsKey": document.get("dataPointsKey"),
        "dataPointsVersionId": document.get("dataPointsVersionId"),
        "dataPointsChecksumSha256": document.get("dataPointsChecksumSha256"),
        "fileName": document.get("fileName"),
        "processingExecutionId": document.get("processingExecutionId"),
        "failureCode": document.get("failureCode"),
        "archivedBy": auth["actorId"],
    }
    transaction = [
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": document_sk(instance_id, document_id)}),
                "UpdateExpression": "SET #status=:archived, updatedAt=:now, lastDocumentArchiveSequence=:sequence REMOVE currentUploadId",
                "ConditionExpression": "currentUploadId=:upload AND lastDocumentArchiveSequence=:previous AND #status=:terminal",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":archived": "ARCHIVED", ":now": now, ":sequence": sequence, ":previous": sequence - 1, ":upload": upload_id, ":terminal": document["status"]}),
            }
        },
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(archive), "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(idempotency_item(identity, request_hash, 201, response, now)), "ConditionExpression": "attribute_not_exists(PK)"}},
    ]
    try:
        transact(transaction)
    except ApiProblem:
        replay = get_idempotent(identity, request_hash)
        if replay:
            return json_response(replay["status"], replay["body"], cid)
        raise
    return json_response(201, response, cid)


def archive_loan(event: dict[str, Any], auth: dict[str, str], loan_id: str, cid: str) -> dict[str, Any]:
    loan_id = validate_loan_id(loan_id)
    body = parse_body(event, required=False)
    if body:
        raise ApiProblem(400, "INVALID_REQUEST", "The archive request body must be empty")
    identity, request_hash, replay = replay_or_none(event, auth, body)
    if replay:
        return json_response(replay["status"], replay["body"], cid)

    pk = loan_pk(auth["tenantId"], loan_id)
    head, instance_id = require_active_instance(pk)
    instance_prefix = f"INSTANCE#{instance_id}"
    items = query_all(KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(instance_prefix), ConsistentRead=True)
    documents = [item for item in items if item.get("entityType") == "DOCUMENT"]
    if len(documents) > MAXIMUM_LOAN_ARCHIVE_DOCUMENTS:
        raise ApiProblem(
            409,
            "LOAN_ARCHIVE_DOCUMENT_LIMIT_EXCEEDED",
            "Loan contains too many documents to archive safely",
        )
    blocking = [item["documentId"] for item in documents if item.get("status") in BLOCKING_LOAN_ARCHIVE_STATUSES]
    if blocking:
        raise ApiProblem(409, "LOAN_HAS_PROCESSING_DOCUMENTS", f"Loan has {len(blocking)} incomplete or processing document(s)")

    sequence = int(head.get("lastLoanArchiveSequence", 0)) + 1
    now = utc_now()
    manifest = {
        "schemaVersion": 1,
        "tenantId": auth["tenantId"],
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        "archiveSequence": sequence,
        "archivedAt": now,
        "documents": [
            {
                "documentId": item["documentId"],
                "status": item["status"],
                "currentUploadId": item.get("currentUploadId"),
                "fileName": item.get("fileName"),
                "createdAt": item.get("createdAt"),
                "updatedAt": item.get("updatedAt"),
                "processingExecutionId": item.get("processingExecutionId"),
                "failureCode": item.get("failureCode"),
                "source": object_reference(item, "source"),
                "selected": object_reference(item, "selected"),
                "dataPoints": object_reference(item, "dataPoints"),
            }
            for item in documents
        ],
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(manifest_bytes) > MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES:
        raise ApiProblem(
            409,
            "LOAN_ARCHIVE_MANIFEST_LIMIT_EXCEEDED",
            "Loan archive manifest exceeds the configured size limit",
        )
    manifest_checksum = base64.b64encode(hashlib.sha256(manifest_bytes).digest()).decode("ascii")
    manifest_key = f"tenants/{auth['tenantId']}/loans/{loan_id}/instances/{instance_id}/archives/loans/{sequence:012d}/manifest.json"
    manifest_put = S3.put_object(
        Bucket=SOURCE_BUCKET,
        Key=manifest_key,
        Body=manifest_bytes,
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
        SSEKMSKeyId=DATA_KEY_ARN,
        ChecksumSHA256=manifest_checksum,
    )
    manifest_version = manifest_put.get("VersionId")
    if not manifest_version:
        raise ApiProblem(503, "ARCHIVE_STORAGE_UNAVAILABLE", "Archive manifest was not versioned")

    response = {
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        "archiveSequence": sequence,
        "displayLoanId": f"{loan_id}_{sequence:03d}",
        "status": "ARCHIVED",
        "archivedAt": now,
        "documentCount": len(documents),
    }
    archive = {
        "PK": pk,
        "SK": f"ARCHIVE#{sequence:012d}",
        "entityType": "LOAN_ARCHIVE",
        **response,
        "manifestBucket": SOURCE_BUCKET,
        "manifestKey": manifest_key,
        "manifestVersionId": manifest_version,
        "manifestChecksumSha256": manifest_checksum,
        "archivedBy": auth["actorId"],
    }
    outbox_id = new_id("evt")
    outbox = {
        "PK": pk,
        "SK": f"OUTBOX#{outbox_id}",
        "entityType": "OUTBOX",
        "eventId": outbox_id,
        "eventType": "LoanArchived",
        "loanId": loan_id,
        "loanInstanceId": instance_id,
        "archiveSequence": sequence,
        "status": "PENDING",
        "createdAt": now,
    }
    transaction = [
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": "HEAD"}),
                "UpdateExpression": "SET #status=:archived, updatedAt=:now, lastLoanArchiveSequence=:sequence REMOVE currentInstanceId ADD revision :one",
                "ConditionExpression": "currentInstanceId=:instance AND lastLoanArchiveSequence=:previous AND #status=:active",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":archived": "ARCHIVED", ":now": now, ":sequence": sequence, ":one": 1, ":instance": instance_id, ":previous": sequence - 1, ":active": "ACTIVE"}),
            }
        },
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": serialize_item({"PK": pk, "SK": instance_sk(instance_id)}),
                "UpdateExpression": "SET #status=:archived, archivedAt=:now, updatedAt=:now",
                "ConditionExpression": "#status=:active",
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": serialize_values({":archived": "ARCHIVED", ":active": "ACTIVE", ":now": now}),
            }
        },
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(archive), "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(outbox), "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Put": {"TableName": TABLE_NAME, "Item": serialize_item(idempotency_item(identity, request_hash, 201, response, now)), "ConditionExpression": "attribute_not_exists(PK)"}},
    ]
    try:
        transact(transaction)
    except ApiProblem:
        replay = get_idempotent(identity, request_hash)
        if replay:
            return json_response(replay["status"], replay["body"], cid)
        raise
    return json_response(201, response, cid)


def object_reference(item: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    key = item.get(f"{prefix}Key")
    if not key:
        return None
    return {
        "bucket": item.get(f"{prefix}Bucket"),
        "key": key,
        "versionId": item.get(f"{prefix}VersionId"),
        "checksumSha256": item.get(f"{prefix}ChecksumSha256"),
    }


def artifact_names(references: dict[str, Any]) -> list[str]:
    return [name for name in ("source", "selected", "data-points") if references.get(name)]


def load_loan_archive(tenant_id: str, loan_id: str, sequence_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    loan_id = validate_loan_id(loan_id)
    sequence = parse_archive_sequence(sequence_text)
    pk = loan_pk(tenant_id, loan_id)
    item = TABLE.get_item(Key={"PK": pk, "SK": f"ARCHIVE#{sequence:012d}"}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiProblem(404, "LOAN_ARCHIVE_NOT_FOUND", "Loan archive was not found")
    manifest_object = S3.get_object(
        Bucket=item["manifestBucket"],
        Key=item["manifestKey"],
        VersionId=item["manifestVersionId"],
        ChecksumMode="ENABLED",
    )
    body = manifest_object.get("Body")
    if body is None:
        raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest body is unavailable")
    try:
        if not callable(getattr(body, "read", None)):
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest body is unavailable")
        expected_checksum = item.get("manifestChecksumSha256")
        returned_checksum = manifest_object.get("ChecksumSHA256")
        if not isinstance(expected_checksum, str) or not expected_checksum:
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest checksum is missing")
        if not isinstance(returned_checksum, str) or not returned_checksum:
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive storage did not return a checksum")
        if not hmac.compare_digest(returned_checksum, expected_checksum):
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest checksum does not match its archive record")

        content_length = int(manifest_object.get("ContentLength", -1))
        if content_length > MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES:
            raise ApiProblem(500, "ARCHIVE_MANIFEST_TOO_LARGE", "Archive manifest exceeds the configured size limit")
        raw_manifest = body.read(MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES + 1)
        if not isinstance(raw_manifest, bytes):
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest body is invalid")
        if len(raw_manifest) > MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES:
            raise ApiProblem(500, "ARCHIVE_MANIFEST_TOO_LARGE", "Archive manifest exceeds the configured size limit")
        actual_checksum = base64.b64encode(hashlib.sha256(raw_manifest).digest()).decode("ascii")
        if not hmac.compare_digest(actual_checksum, expected_checksum):
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest bytes failed checksum verification")
        manifest = json.loads(raw_manifest)
    except ApiProblem:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest is invalid") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    expected_identity = {
        "tenantId": tenant_id,
        "loanId": loan_id,
        "loanInstanceId": item["loanInstanceId"],
        "archiveSequence": sequence,
    }
    if not isinstance(manifest, dict) or any(manifest.get(name) != value for name, value in expected_identity.items()):
        raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest identity does not match its archive record")
    if not isinstance(manifest.get("documents"), list):
        raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest documents are invalid")
    if len(manifest["documents"]) > MAXIMUM_LOAN_ARCHIVE_DOCUMENTS:
        raise ApiProblem(500, "ARCHIVE_MANIFEST_TOO_LARGE", "Archive manifest contains too many documents")
    document_ids: set[str] = set()
    for document in manifest["documents"]:
        if not isinstance(document, dict) or not DOCUMENT_ID_RE.fullmatch(str(document.get("documentId", ""))):
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest contains an invalid document")
        if document["documentId"] in document_ids:
            raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest contains duplicate documents")
        document_ids.add(document["documentId"])
    if int(item.get("documentCount", len(document_ids))) != len(document_ids):
        raise ApiProblem(500, "ARCHIVE_MANIFEST_INVALID", "Archive manifest document count does not match its archive record")
    return item, manifest


def archive_manifest_document_view(archive: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    loan_id = archive["loanId"]
    loan_sequence = int(archive["archiveSequence"])
    document_id = document["documentId"]
    references = {
        "source": document.get("source"),
        "selected": document.get("selected"),
        "data-points": document.get("dataPoints"),
    }
    base = f"/v1/loans/{quote(loan_id)}/archives/{loan_sequence}/documents/{document_id}"
    return {
        "loanId": loan_id,
        "loanInstanceId": archive["loanInstanceId"],
        "loanArchiveSequence": loan_sequence,
        "documentId": document_id,
        "status": document.get("status") or "ARCHIVED",
        "currentUploadId": document.get("currentUploadId"),
        "fileName": document.get("fileName"),
        "createdAt": document.get("createdAt"),
        "updatedAt": document.get("updatedAt"),
        "processingExecutionId": document.get("processingExecutionId"),
        "failureCode": document.get("failureCode"),
        "artifacts": artifact_names(references),
        "links": {
            "self": base,
            "dataPoints": f"{base}/data-points",
            "download": f"{base}/download",
        },
    }


def get_archive_manifest_document(
    auth: dict[str, str], loan_id: str, loan_sequence_text: str, document_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    document_id = require_path_id(document_id, DOCUMENT_ID_RE, "documentId")
    archive, manifest = load_loan_archive(auth["tenantId"], loan_id, loan_sequence_text)
    document = next(
        (
            candidate
            for candidate in manifest["documents"]
            if isinstance(candidate, dict) and candidate.get("documentId") == document_id
        ),
        None,
    )
    if not document:
        raise ApiProblem(404, "DOCUMENT_NOT_FOUND", "Document was not found in this loan archive")
    return archive, document


def get_loan_archive(auth: dict[str, str], loan_id: str, sequence_text: str, cid: str) -> dict[str, Any]:
    item, manifest = load_loan_archive(auth["tenantId"], loan_id, sequence_text)
    documents = [archive_manifest_document_view(item, document) for document in manifest["documents"] if isinstance(document, dict)]
    response = {**loan_archive_summary(item), "documents": documents}
    return json_response(200, response, cid)


def get_archived_loan_document(
    auth: dict[str, str], loan_id: str, loan_sequence_text: str, document_id: str, cid: str
) -> dict[str, Any]:
    archive, document = get_archive_manifest_document(auth, loan_id, loan_sequence_text, document_id)
    view = archive_manifest_document_view(archive, document)
    pk = loan_pk(auth["tenantId"], loan_id)
    instance_id = archive["loanInstanceId"]
    archives = query_all(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with(f"{document_sk(instance_id, document_id)}#ARCHIVE#"),
        ConsistentRead=True,
    )
    view["archives"] = [
        document_archive_summary(item, int(archive["archiveSequence"]))
        for item in sorted(archives, key=lambda value: int(value["archiveSequence"]), reverse=True)
    ]
    return json_response(200, view, cid)


def load_document_archive_from_instance(
    pk: str,
    instance_id: str,
    document_id: str,
    sequence_text: str,
) -> dict[str, Any]:
    sequence = parse_archive_sequence(sequence_text)
    item = TABLE.get_item(Key={"PK": pk, "SK": document_archive_sk(instance_id, document_id, sequence)}, ConsistentRead=True).get("Item")
    if not item:
        raise ApiProblem(404, "DOCUMENT_ARCHIVE_NOT_FOUND", "Document archive was not found")
    return item


def get_document_archive_from_instance(
    pk: str,
    instance_id: str,
    document_id: str,
    sequence_text: str,
    cid: str,
    loan_archive_sequence: int | None = None,
) -> dict[str, Any]:
    item = load_document_archive_from_instance(pk, instance_id, document_id, sequence_text)
    references = {
        "source": object_reference(item, "source"),
        "selected": object_reference(item, "selected"),
        "data-points": object_reference(item, "dataPoints"),
    }
    return json_response(
        200,
        {**document_archive_summary(item, loan_archive_sequence), "artifacts": artifact_names(references)},
        cid,
    )


def get_document_archive(auth: dict[str, str], loan_id: str, document_id: str, sequence_text: str, cid: str) -> dict[str, Any]:
    pk, instance_id, _ = get_document_item(auth["tenantId"], loan_id, document_id)
    return get_document_archive_from_instance(pk, instance_id, document_id, sequence_text, cid)


def get_archived_loan_document_archive(
    auth: dict[str, str],
    loan_id: str,
    loan_sequence_text: str,
    document_id: str,
    document_sequence_text: str,
    cid: str,
) -> dict[str, Any]:
    archive, _ = get_archive_manifest_document(auth, loan_id, loan_sequence_text, document_id)
    pk = loan_pk(auth["tenantId"], loan_id)
    return get_document_archive_from_instance(
        pk,
        archive["loanInstanceId"],
        document_id,
        document_sequence_text,
        cid,
        int(archive["archiveSequence"]),
    )


def get_active_document_archive_item(
    auth: dict[str, str], loan_id: str, document_id: str, document_sequence_text: str
) -> dict[str, Any]:
    pk, instance_id, _ = get_document_item(auth["tenantId"], loan_id, document_id)
    return load_document_archive_from_instance(pk, instance_id, document_id, document_sequence_text)


def get_loan_archive_document_archive_item(
    auth: dict[str, str],
    loan_id: str,
    loan_sequence_text: str,
    document_id: str,
    document_sequence_text: str,
) -> dict[str, Any]:
    archive, _ = get_archive_manifest_document(auth, loan_id, loan_sequence_text, document_id)
    pk = loan_pk(auth["tenantId"], loan_id)
    return load_document_archive_from_instance(
        pk, archive["loanInstanceId"], document_id, document_sequence_text
    )


def read_data_points(reference: dict[str, Any] | None, status: str, cid: str) -> dict[str, Any]:
    if not reference:
        if status not in TERMINAL_DOCUMENT_STATUSES and status != "ARCHIVED":
            raise ApiProblem(409, "DATA_POINTS_NOT_READY", "Data points are not ready")
        raise ApiProblem(404, "DATA_POINTS_NOT_FOUND", "Data points are unavailable")
    required = ("bucket", "key", "versionId")
    if any(not reference.get(name) for name in required):
        raise ApiProblem(500, "ARTIFACT_REFERENCE_INVALID", "Pinned data-point reference is invalid")
    object_ = S3.get_object(
        Bucket=reference["bucket"],
        Key=reference["key"],
        VersionId=reference["versionId"],
    )
    body = object_["Body"]
    try:
        content_length = int(object_.get("ContentLength", -1))
        if content_length > MAXIMUM_INLINE_DATA_POINTS_BYTES:
            raise ApiProblem(
                413,
                "DATA_POINTS_TOO_LARGE",
                "Data points are too large for an inline response; use the download endpoint",
            )
        raw = body.read(MAXIMUM_INLINE_DATA_POINTS_BYTES + 1)
        if len(raw) > MAXIMUM_INLINE_DATA_POINTS_BYTES:
            raise ApiProblem(
                413,
                "DATA_POINTS_TOO_LARGE",
                "Data points are too large for an inline response; use the download endpoint",
            )
        data = json.loads(raw)
    except ApiProblem:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ApiProblem(502, "DATA_POINTS_INVALID", "Pinned data points are not valid JSON") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return json_response(200, data, cid)


def get_data_points(auth: dict[str, str], loan_id: str, document_id: str, cid: str) -> dict[str, Any]:
    _, _, document = get_document_item(auth["tenantId"], loan_id, document_id)
    return read_data_points(object_reference(document, "dataPoints"), document.get("status", ""), cid)


def get_archived_data_points(
    auth: dict[str, str], loan_id: str, loan_sequence_text: str, document_id: str, cid: str
) -> dict[str, Any]:
    _, document = get_archive_manifest_document(auth, loan_id, loan_sequence_text, document_id)
    return read_data_points(document.get("dataPoints"), document.get("status", "ARCHIVED"), cid)


def get_document_archive_data_points(
    auth: dict[str, str], loan_id: str, document_id: str, document_sequence_text: str, cid: str
) -> dict[str, Any]:
    item = get_active_document_archive_item(auth, loan_id, document_id, document_sequence_text)
    return read_data_points(object_reference(item, "dataPoints"), "ARCHIVED", cid)


def get_loan_archive_document_archive_data_points(
    auth: dict[str, str],
    loan_id: str,
    loan_sequence_text: str,
    document_id: str,
    document_sequence_text: str,
    cid: str,
) -> dict[str, Any]:
    item = get_loan_archive_document_archive_item(
        auth, loan_id, loan_sequence_text, document_id, document_sequence_text
    )
    return read_data_points(object_reference(item, "dataPoints"), "ARCHIVED", cid)


def safe_download_name(file_name: str, suffix: str = "") -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", file_name or "document.pdf")[:180]
    if suffix:
        base, _, extension = stem.rpartition(".")
        stem = f"{base or 'document'}-{suffix}.{extension or 'pdf'}"
    return stem


def create_download_grant(
    references: dict[str, dict[str, Any] | None], file_name_base: str, artifact: str, cid: str
) -> dict[str, Any]:
    mapping = {
        "source": ("application/pdf", safe_download_name(file_name_base or "source.pdf")),
        "selected": ("application/pdf", safe_download_name(file_name_base or "selected.pdf", "selected")),
        "data-points": ("application/json", safe_download_name("data-points.json")),
    }
    if artifact not in mapping:
        raise ApiProblem(400, "INVALID_ARTIFACT", "artifact must be source, selected, or data-points")
    content_type, file_name = mapping[artifact]
    reference = references.get(artifact)
    if not reference:
        raise ApiProblem(404, "ARTIFACT_NOT_FOUND", "Artifact is unavailable")
    required = ("bucket", "key", "versionId")
    if any(not reference.get(name) for name in required):
        raise ApiProblem(500, "ARTIFACT_REFERENCE_INVALID", "Pinned artifact reference is invalid")
    params = {
        "Bucket": reference["bucket"],
        "Key": reference["key"],
        "VersionId": reference["versionId"],
        "ResponseContentType": content_type,
        "ResponseContentDisposition": f'attachment; filename="{file_name}"',
        "ResponseCacheControl": "no-store",
    }
    grant_seconds = effective_grant_seconds(DOWNLOAD_URL_SECONDS)
    url = S3.generate_presigned_url("get_object", Params=params, ExpiresIn=grant_seconds)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=grant_seconds)).isoformat().replace("+00:00", "Z")
    return json_response(200, {"downloadUrl": url, "fileName": file_name, "contentType": content_type, "expiresAt": expires_at}, cid)


def download_grant(auth: dict[str, str], loan_id: str, document_id: str, artifact: str, cid: str) -> dict[str, Any]:
    _, _, document = get_document_item(auth["tenantId"], loan_id, document_id)
    references = {
        "source": object_reference(document, "source"),
        "selected": object_reference(document, "selected"),
        "data-points": object_reference(document, "dataPoints"),
    }
    return create_download_grant(references, document.get("fileName", "source.pdf"), artifact, cid)


def archived_download_grant(
    auth: dict[str, str],
    loan_id: str,
    loan_sequence_text: str,
    document_id: str,
    artifact: str,
    cid: str,
) -> dict[str, Any]:
    _, document = get_archive_manifest_document(auth, loan_id, loan_sequence_text, document_id)
    references = {
        "source": document.get("source"),
        "selected": document.get("selected"),
        "data-points": document.get("dataPoints"),
    }
    return create_download_grant(references, document.get("fileName") or "source.pdf", artifact, cid)


def document_archive_download_grant(
    item: dict[str, Any], artifact: str, cid: str
) -> dict[str, Any]:
    references = {
        "source": object_reference(item, "source"),
        "selected": object_reference(item, "selected"),
        "data-points": object_reference(item, "dataPoints"),
    }
    return create_download_grant(references, item.get("fileName") or "source.pdf", artifact, cid)


RouteHandler = Callable[[dict[str, Any], dict[str, str], re.Match[str], str], dict[str, Any]]


def route_create_loan(event: dict[str, Any], auth: dict[str, str], _: re.Match[str], cid: str) -> dict[str, Any]:
    return create_loan(event, auth, cid)


def route_get_loan(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_loan(auth["tenantId"], match["loanId"], cid)


def route_archive_loan(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return archive_loan(event, auth, match["loanId"], cid)


def route_get_loan_archive(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_loan_archive(auth, match["loanId"], match["sequence"], cid)


def route_get_archived_document(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_archived_loan_document(auth, match["loanId"], match["loanSequence"], match["documentId"], cid)


def route_get_archived_document_archive(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    return get_archived_loan_document_archive(
        auth,
        match["loanId"],
        match["loanSequence"],
        match["documentId"],
        match["documentSequence"],
        cid,
    )


def route_get_archived_document_archive_data(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    return get_loan_archive_document_archive_data_points(
        auth,
        match["loanId"],
        match["loanSequence"],
        match["documentId"],
        match["documentSequence"],
        cid,
    )


def route_archived_document_archive_data_download(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    item = get_loan_archive_document_archive_item(
        auth,
        match["loanId"],
        match["loanSequence"],
        match["documentId"],
        match["documentSequence"],
    )
    return document_archive_download_grant(item, "data-points", cid)


def route_archived_document_archive_download(
    event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    artifact = (event.get("queryStringParameters") or {}).get("artifact", "")
    if artifact not in {"source", "selected"}:
        raise ApiProblem(400, "INVALID_ARTIFACT", "artifact must be source or selected")
    item = get_loan_archive_document_archive_item(
        auth,
        match["loanId"],
        match["loanSequence"],
        match["documentId"],
        match["documentSequence"],
    )
    return document_archive_download_grant(item, artifact, cid)


def route_get_archived_data(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_archived_data_points(auth, match["loanId"], match["loanSequence"], match["documentId"], cid)


def route_archived_data_download(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    return archived_download_grant(
        auth, match["loanId"], match["loanSequence"], match["documentId"], "data-points", cid
    )


def route_archived_document_download(
    event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    artifact = (event.get("queryStringParameters") or {}).get("artifact", "")
    if artifact not in {"source", "selected"}:
        raise ApiProblem(400, "INVALID_ARTIFACT", "artifact must be source or selected")
    return archived_download_grant(
        auth, match["loanId"], match["loanSequence"], match["documentId"], artifact, cid
    )


def route_create_document(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return create_document(event, auth, match["loanId"], cid)


def route_create_replacement(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return create_document(event, auth, match["loanId"], cid, match["documentId"])


def route_complete(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return complete_upload(event, auth, match["loanId"], match["documentId"], match["uploadId"], cid)


def route_get_document(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_document(auth, match["loanId"], match["documentId"], cid)


def route_archive_document(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return archive_document(event, auth, match["loanId"], match["documentId"], cid)


def route_get_document_archive(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_document_archive(auth, match["loanId"], match["documentId"], match["sequence"], cid)


def route_get_document_archive_data(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    return get_document_archive_data_points(
        auth, match["loanId"], match["documentId"], match["sequence"], cid
    )


def route_document_archive_data_download(
    _: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    item = get_active_document_archive_item(
        auth, match["loanId"], match["documentId"], match["sequence"]
    )
    return document_archive_download_grant(item, "data-points", cid)


def route_document_archive_download(
    event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str
) -> dict[str, Any]:
    artifact = (event.get("queryStringParameters") or {}).get("artifact", "")
    if artifact not in {"source", "selected"}:
        raise ApiProblem(400, "INVALID_ARTIFACT", "artifact must be source or selected")
    item = get_active_document_archive_item(
        auth, match["loanId"], match["documentId"], match["sequence"]
    )
    return document_archive_download_grant(item, artifact, cid)


def route_get_data(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return get_data_points(auth, match["loanId"], match["documentId"], cid)


def route_data_download(_: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    return download_grant(auth, match["loanId"], match["documentId"], "data-points", cid)


def route_document_download(event: dict[str, Any], auth: dict[str, str], match: re.Match[str], cid: str) -> dict[str, Any]:
    artifact = (event.get("queryStringParameters") or {}).get("artifact", "")
    if artifact not in {"source", "selected"}:
        raise ApiProblem(400, "INVALID_ARTIFACT", "artifact must be source or selected")
    return download_grant(auth, match["loanId"], match["documentId"], artifact, cid)


ROUTES: list[tuple[str, re.Pattern[str], str, RouteHandler]] = [
    ("POST", re.compile(r"^/v1/loans$"), "Loan.Create", route_create_loan),
    ("POST", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archive$"), "Loan.Archive", route_archive_loan),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<documentSequence>[^/]+)/data-points/download$"), "DataPoints.Read", route_archived_document_archive_data_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<documentSequence>[^/]+)/data-points$"), "DataPoints.Read", route_get_archived_document_archive_data),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<documentSequence>[^/]+)/download$"), "Document.Read", route_archived_document_archive_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<documentSequence>[^/]+)$"), "Document.Read", route_get_archived_document_archive),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/data-points/download$"), "DataPoints.Read", route_archived_data_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/data-points$"), "DataPoints.Read", route_get_archived_data),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)/download$"), "Document.Read", route_archived_document_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<loanSequence>[^/]+)/documents/(?P<documentId>[^/]+)$"), "Document.Read", route_get_archived_document),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/archives/(?P<sequence>[^/]+)$"), "Loan.Read", route_get_loan_archive),
    ("POST", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents$"), "Document.Upload", route_create_document),
    ("POST", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/uploads$"), "Document.Upload", route_create_replacement),
    ("POST", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/uploads/(?P<uploadId>[^/]+)/complete$"), "Document.Upload", route_complete),
    ("POST", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/archive$"), "Document.Archive", route_archive_document),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<sequence>[^/]+)/data-points/download$"), "DataPoints.Read", route_document_archive_data_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<sequence>[^/]+)/data-points$"), "DataPoints.Read", route_get_document_archive_data),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<sequence>[^/]+)/download$"), "Document.Read", route_document_archive_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/archives/(?P<sequence>[^/]+)$"), "Document.Read", route_get_document_archive),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/data-points/download$"), "DataPoints.Read", route_data_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/data-points$"), "DataPoints.Read", route_get_data),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)/download$"), "Document.Read", route_document_download),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)/documents/(?P<documentId>[^/]+)$"), "Document.Read", route_get_document),
    ("GET", re.compile(r"^/v1/loans/(?P<loanId>[^/]+)$"), "Loan.Read", route_get_loan),
]


def dispatch_request(event: dict[str, Any], *, enforce_origin: bool = False) -> dict[str, Any]:
    """Dispatch one normalized HTTP request from the Azure or rollback adapter."""

    cid = correlation_id(event)
    try:
        if enforce_origin:
            require_origin(event)
        path = event.get("rawPath", "")
        method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
        if path == "/health" and method == "GET":
            return json_response(200, {"status": "ok"}, cid)
        for route_method, pattern, permission, handler in ROUTES:
            match = pattern.fullmatch(path)
            if method == route_method and match:
                auth = authorize(event, permission)
                return handler(event, auth, match, cid)
        raise ApiProblem(404, "ROUTE_NOT_FOUND", "Route was not found")
    except ApiProblem as problem:
        LOGGER.info("api_problem code=%s status=%s correlation_id=%s", problem.code, problem.status, cid)
        return problem_response(problem, cid)
    except Exception as exc:
        LOGGER.error(
            "unhandled_api_error error_type=%s correlation_id=%s",
            type(exc).__name__,
            cid,
        )
        return problem_response(ApiProblem(500, "INTERNAL_ERROR", "Internal server error", "An unexpected error occurred"), cid)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Migration rollback adapter; no production AWS resource deploys it."""

    return dispatch_request(event, enforce_origin=True)
