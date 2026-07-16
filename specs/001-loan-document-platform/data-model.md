# Data Model: Loan Document Platform

> **Historical baseline — do not execute.** This file records the superseded AWS-hosted product API. Use the current [Azure API control-plane specification](../002-azure-api-control-plane/spec.md) and its companion files for implementation.

## Identity Rules

| Identifier | Issuer | Scope | Mutability |
|---|---|---|---|
| `tenantId` | Entra `tid` | Security/data partition | Immutable |
| `loanId` | Caller | Unique active business key within tenant | Stable/reusable across incarnations |
| `loanInstanceId` | AWS API | One loan incarnation | Immutable |
| `documentId` | AWS API | Logical document within one instance | Stable across replacements |
| `uploadId` | AWS API | One physical PDF attempt/version | Immutable |
| `processingExecutionId` | AWS API | One screening/full run | Immutable |
| S3 `VersionId` | S3 | Exact stored object bytes | Immutable |
| archive sequence | DynamoDB transaction | Loan or document history | Monotonic/immutable |

Display aliases such as `23051_001` and `<documentId>_001` are server-owned presentation values. They are never parsed to discover identity.

## DynamoDB Key Model

The primary business partition is `TENANT#{tenantId}#LOAN#{loanId}`. Sort-key families are:

```text
HEAD
INSTANCE#{loanInstanceId}
ARCHIVE#{sequence-padded-for-sort}
INSTANCE#{loanInstanceId}#DOC#{documentId}
INSTANCE#{loanInstanceId}#DOC#{documentId}#UPLOAD#{uploadId}
INSTANCE#{loanInstanceId}#DOC#{documentId}#ARCHIVE#{sequence-padded-for-sort}
OUTBOX#{eventId}
```

Idempotency intents use a separate partition derived from tenant, immutable actor, route, and idempotency key. Uploads project an opaque object-key lookup to `GSI1` for scan-event reconciliation.

## Entities

### Loan Head

**Purpose**: Mutable pointer/counter record for one tenant/business loan.

**Core fields**: `tenantId`, `loanId`, optional `currentLoanInstanceId`, `nextLoanArchiveSequence`, revision/ETag, created/updated timestamps.

**Rules**:

- At most one current instance.
- Create is conditional on no current pointer.
- Archive transaction conditions the revision/current pointer, advances the counter, removes current, and writes archive/outbox records.
- Prior archive sequences are never reused, even when a loan is recreated.

### Loan Instance

**Purpose**: Immutable identity boundary for one active-then-archived loan incarnation.

**Core fields**: `tenantId`, `loanId`, `loanInstanceId`, `status`, created/updated/archived timestamps, actor/correlation metadata, optional archive sequence/reference.

**States**: `ACTIVE` → `ARCHIVING` → `ARCHIVED`. Failed conditional archive leaves the prior durable state and is reconciled/retried idempotently.

**Relationships**: Owns logical documents, uploads, executions, and artifacts. Once archived it is read-only.

### Loan Archive

**Purpose**: Read-only sequence and manifest/reference freezing a complete loan instance.

**Core fields**: `tenantId`, `loanId`, `loanInstanceId`, numeric sequence, display alias, manifest bucket/key/version/checksum, archived timestamp/actor, transaction revision.

**Rules**: Sequence is monotonic and formatted with a minimum of three digits. The record references the already immutable instance; it does not duplicate every document.

### Logical Document

**Purpose**: Stable document identity under one immutable loan instance.

**Core fields**: `tenantId`, `loanId`, `loanInstanceId`, `documentId`, optional `currentUploadId`, `nextDocumentArchiveSequence`, processing status, artifact availability, created/updated timestamps.

**Rules**: `documentId` comes from AWS and never changes for replacements. A replacement changes only `currentUploadId` after valid lifecycle transitions.

### Document Archive

**Purpose**: Read-only snapshot of one logical document’s current upload/artifacts.

**Core fields**: ownership identifiers, `documentId`, archive sequence/alias, frozen `uploadId`, source/selected/result artifact references, processing status/provenance, archived timestamp/actor.

**Rules**: Allocation is conditional, monotonic, and idempotent. A snapshot never points to a later replacement.

### Upload

**Purpose**: One physical PDF ingestion and its integrity/security state.

**Core fields**: ownership identifiers, `documentId`, `uploadId`, opaque S3 key, expected content type/size/SHA-256, observed bucket/key/version/size/checksum, scan result, validation result, processing status, timestamps, expiry/reconciliation metadata.

**Processing states**:

```text
AWAITING_UPLOAD
  -> VALIDATING
  -> QUEUED
  -> SCREENING
  -> SELECTED
  -> EXTRACTING
  -> SUCCEEDED
```

Any applicable stage can enter `HOLD`, `REJECTED`, or `FAILED`. Lifecycle archive uses `ARCHIVING` → `ARCHIVED`. Only the exact clean/valid version can leave validation.

### Processing Execution

**Purpose**: Auditable record of one screening or full-extraction attempt.

**Core fields**: `processingExecutionId`, execution kind, ownership/upload identifiers, exact input artifact bucket/key/version/checksum, config name/digest, prompt/schema version, model/inference profile, effective region, start/end/status, selected page evidence, output artifact/checksum, correlation/error category.

**Rules**: Inputs are immutable; output is schema-validated; models have no side effects. Retrying creates an attributable execution rather than rewriting provenance.

### Artifact

**Purpose**: Immutable reference to versioned source PDF, selected CD PDF, data points, or archive manifest.

**Core fields**: artifact type, bucket/key/version, SHA-256, media type, size, ownership identifiers, producing execution/config, created timestamp.

**Rules**: Reads/downloads always use the stored version/checksum. “Latest” is never an evidence reference.

### Data Points Artifact

**Purpose**: Schema-validated final structured result for one upload/execution.

**Core fields**: artifact reference/checksum, schema version, producing execution ID, ownership/upload IDs, created timestamp, availability status.

**Rules**: The structured payload lives in private versioned storage; telemetry contains only safe identifiers/status.

### Idempotency Intent

**Purpose**: Make a single mutation safely replayable by the same actor.

**Core fields**: tenant, immutable actor ID, route/operation, key, canonical request hash, state, stored status/response reference, created/completed timestamps, transient TTL where applicable.

**Rules**: Same key/hash returns the stored result. Same key/different hash is a conflict. It cannot allocate another archive sequence.

### Outbox Event

**Purpose**: Durable handoff of committed business changes to asynchronous publishers/consumers.

**Core fields**: event ID/type, aggregate identifiers/revision, safe payload/reference, status, attempt count, next-attempt/published timestamps.

**Rules**: Written in the same transaction as the business change. Publication/consumption is idempotent and poison events go to a DLQ.

### Download Grant

**Purpose**: Transient response authorizing one caller to download one exact artifact.

**Core fields**: exact artifact reference, signed URL/form data, issued/expiry timestamps, safe response metadata.

**Rules**: Created only after fresh route authorization, expires in one to five minutes, is not persisted or logged, and grants no list access.

## Relationships

```text
Tenant
  └─ Loan Head (loanId)
      ├─ Loan Instance 1 ── Loan Archive 1
      │   └─ Logical Document (documentId)
      │       ├─ Upload 1 ── Processing Executions ── Artifacts
      │       ├─ Document Archive 1 (freezes Upload 1)
      │       ├─ Upload 2 ── Processing Executions ── Artifacts
      │       └─ Document Archive 2 (freezes Upload 2)
      └─ Loan Instance 2 (new current incarnation)
```

## Validation and Retention

- Identifiers must satisfy the canonical OpenAPI patterns; callers cannot supply AWS-owned IDs.
- Expected upload size/checksum/content type must match presigned policy and observed exact S3 version.
- Archived business records and referenced object versions have no TTL. Legal-hold-aware purge is separate.
- Idempotency/transient reconciliation records may use bounded TTL only after their business result is durable.
- DynamoDB PITR, deletion protection, AWS Backup, customer-managed KMS encryption, S3 versioning, Block Public Access, Bucket Owner Enforced, and TLS-only policies are required in production.
