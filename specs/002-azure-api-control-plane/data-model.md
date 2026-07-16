# Data Model: Azure API Control Plane

## Scope and Sources of Truth

The Azure API is the sole public owner of loan and document domain behavior.
The initial migration deliberately retains the existing DynamoDB table as the
single mutable registry shared by the Azure API and the AWS malware/IDP
processors. S3 remains the authoritative byte store, and the pinned headless IDP
deployment remains the processing system. No Azure database, AppSync registry,
or Jobs REST registry is introduced by this feature.

Authority is divided by field, not by cloud:

| Authority | Owns |
|---|---|
| Microsoft Entra ID | Tenant, human/service principal, client, token issuer, audience, scopes, and roles |
| Azure API | Product IDs, lifecycle commands, route authorization, idempotency results, active pointers, archive counters, and presigned-grant decisions |
| DynamoDB | Committed product state and conditional concurrency outcomes |
| S3 | Object `VersionId`, stored bytes, object metadata, and versioned artifact existence |
| GuardDuty/upload processor | Malware observations, deterministic PDF validation, and clean-version admission to IDP |
| Headless IDP/postprocessor | Stage execution ARNs, selection evidence, extraction provenance, and output artifact observations |
| AWS STS | Ephemeral assumed-role session credentials and their expiration |

An authority may copy another system's value into the registry as an immutable
reference, but the copied value does not change its issuer. For example, Azure
records an S3 `VersionId`; Azure does not issue that value.

## Identifier and Claim Rules

| Name | Issuer | Format or source | Scope | Substitution rule |
|---|---|---|---|---|
| `tenantId` | Entra | Validated `tid` UUID | Security and data partition | Never accepted from request content |
| `actorId` | Entra | Validated immutable `oid` | Idempotency/audit actor | Never use email, UPN, or display name |
| `clientId` | Entra | Validated `azp`/`appid` | Calling application | Distinct from `actorId` and managed identity IDs |
| `loanId` | Caller | `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` | Stable business key inside one tenant | Not an immutable incarnation ID |
| `loanInstanceId` | Azure product API | `lin_<uuid>` | One incarnation of a `loanId` | Never use `loanId` in its place |
| `documentId` | Azure product API | `doc_<uuid>` | One logical document inside one loan instance | Stable across replacement uploads |
| `uploadId` | Azure product API | `upl_<uuid>` | One physical PDF upload intent | Never use an IDP key or S3 version in its place |
| `processingExecutionId` | Azure product API | `run_<uuid>` | One platform processing orchestration | May map to multiple stage workflow ARNs |
| `eventId` | Platform component | `evt_<uuid>` or provider event ID | One outbox/provider observation | Not a processing execution ID |
| Archive sequence | DynamoDB transaction | Positive integer; display minimum three digits | Loan head or logical document | Server allocated, monotonic, never reused |
| `Idempotency-Key` | Caller | UUID normalized to lowercase | Actor + route + request hash | A replay key, not a resource ID |
| S3 `VersionId` | S3 | Opaque provider value | Exact bytes at bucket/key | Never parse or replace with `uploadId` |
| IDP `ObjectKey` | Platform IDP adapter/S3 | Exact `screen/...` or `full/...` input key | One IDP stage input object | Never treat as `documentId` |
| IDP workflow execution ARN | AWS Step Functions/IDP | Full opaque ARN | One upstream stage attempt | Never expose as `processingExecutionId` |
| IDP state-machine ARN | AWS IDP deployment | Full opaque ARN | Upstream workflow definition | Not an execution identity |
| Managed identity client/principal IDs | Azure/Entra | Deployment-owned UUIDs | Azure workload identity | Never use as end-user actor/client identity |
| AWS role/session IDs | AWS IAM/STS | ARN and opaque session identifiers | Cross-cloud AWS authorization | Never persist temporary credential material |

The headless deployment does not promise an IDP Jobs API `jobId`. If a future
adapter observes such an identifier, it must be stored as a new optional
upstream field and cannot replace `documentId`, `uploadId`, or
`processingExecutionId`.

Display aliases (`23051_001`, `doc_..._001`) are presentation values derived
from the stable base ID plus the committed numeric sequence. Clients store and
follow the base ID and numeric sequence; they never parse an alias to recover
identity.

## DynamoDB Key Continuity

The Azure migration preserves the existing table, item keys, GSI keys, and
conditional-write semantics so the retained AWS processors continue to operate
without a data copy or dual-write registry.

### Primary business partition

```text
PK = TENANT#{tenantId}#LOAN#{loanId}

SK = HEAD
     INSTANCE#{loanInstanceId}
     ARCHIVE#{archiveSequence padded to 12 digits}
     INSTANCE#{loanInstanceId}#DOC#{documentId}
     INSTANCE#{loanInstanceId}#DOC#{documentId}#UPLOAD#{uploadId}
     INSTANCE#{loanInstanceId}#DOC#{documentId}#ARCHIVE#{archiveSequence padded to 12 digits}
     INSTANCE#{loanInstanceId}#DOC#{documentId}#UPLOAD#{uploadId}#SCAN#{sha256(s3VersionId)[0:32]}
     INSTANCE#{loanInstanceId}#DOC#{documentId}#UPLOAD#{uploadId}#WORKFLOW#{SCREEN|FULL}#{sha256(executionArn)[0:24]}
     OUTBOX#{eventId}
```

The 12-digit internal padding exists only for lexical sort order. Public aliases
use a minimum of three digits and expand beyond 999.

### Idempotency partition

```text
PK = TENANT#{tenantId}#IDEMPOTENCY#{actorId}#{sha256(method + ":" + path)[0:24]}
SK = KEY#{lowercase Idempotency-Key}
```

The record binds the key to a canonical request hash, response status, and
stored response body. Same key and same hash returns the prior outcome. Same key
and a different hash is a conflict. A bounded TTL may remove the replay record
only after the business result is durable; archive counters and resource records
never depend on that TTL.

### Secondary indexes

| Index | Partition/sort key | Purpose and verification rule |
|---|---|---|
| `GSI1` object lookup | `OBJECT#{bucket}#{key}` / `UPLOAD#{uploadId}` | Routes S3/GuardDuty events to a candidate upload; exact bucket, key, version, metadata, and checksum are still verified from the base item |
| `GSI1` workflow lookup | `EXECUTION#{sha256(executionArn)}` / `STAGE#{stage}#RUN#{processingExecutionId}` | Routes an IDP event; the full ARN stored on the item must equal the event ARN |
| `GSI2` active workflow | `ACTIVE_EXECUTION` / `{startedAt}#{sha256(executionArn)}` | Finds stale active IDP workflows for bounded reconciliation; removed at terminal observation |

Hashes bound index-key length and are lookup accelerators only. A hash match is
never sufficient authorization or identity evidence.

## Persisted Domain Entities

### Loan Head (`HEAD`)

**Purpose**: Mutable tenant/business-key pointer to at most one active immutable
loan instance and the monotonic loan archive counter.

**Core fields**: `tenantId` (derived from `PK`), `loanId`, optional
`currentInstanceId`, `lastLoanArchiveSequence`, `revision`, `status`,
`createdAt`, `updatedAt`.

**Field ownership**: Azure API owns the pointer, revision, status, and archive
counter. Every mutation conditions the expected pointer/revision and writes its
idempotency result in the same DynamoDB transaction.

**Rules**:

- At most one `currentInstanceId` exists.
- Recreating a loan allocates a new `loanInstanceId`; it never reuses an archived
  instance.
- Archiving removes the current pointer and increments the counter atomically.
- A loan archive is blocked while any owned document is in
  `AWAITING_UPLOAD`, `VALIDATING`, `QUEUED`, `SCREENING`, `SELECTED`,
  `EXTRACTING`, or `ARCHIVING`.

### Loan Instance (`LOAN_INSTANCE`)

**Purpose**: Immutable ownership boundary for one active-then-archived
incarnation.

**Core fields**: `loanId`, `loanInstanceId`, `status`, `createdAt`, `updatedAt`,
optional `archivedAt`, `createdBy`, `createdByClientId`.

**Field ownership**: Azure API issues the identity and performs the archive
transition. Documents, uploads, executions, and artifacts retain this identity
for their lifetime.

### Loan Archive (`LOAN_ARCHIVE`)

**Purpose**: Immutable sequence record that freezes one loan instance and all of
its already-owned documents by reference.

**Core fields**: `loanId`, `loanInstanceId`, `archiveSequence`,
`displayLoanId`, `status=ARCHIVED`, `archivedAt`, `archivedBy`, `documentCount`,
and a version-pinned manifest artifact (`manifestBucket`, `manifestKey`,
`manifestVersionId`, `manifestChecksumSha256`).

**Rules**: The archive does not copy or rename every document. Its manifest and
registry record never change after the transaction succeeds.

### Logical Document (`DOCUMENT`)

**Purpose**: Stable product identity for a document across replacement uploads.

**Core fields**: ownership IDs, `documentId`, optional `currentUploadId`,
`lastDocumentArchiveSequence`, `status`, optional current
`processingExecutionId`, artifact-reference fields, `pageCount`,
`failureCode`, configuration provenance, `createdAt`, and `updatedAt`.

**Field ownership**:

- Azure API creates the record, changes `currentUploadId`, allocates archive
  sequences, starts a platform execution, and performs document archive and
  replacement transitions.
- AWS processors may advance only the current upload's processing status and
  populate validated provenance/artifact fields using conditional writes bound
  to `currentUploadId` and `processingExecutionId`.

An archived document clears `currentUploadId` but retains immutable archive
records. A replacement reuses `documentId`, allocates a new `uploadId`, and
clears current-version artifacts without mutating prior archive records.

### Upload (`UPLOAD`)

**Purpose**: One physical upload intent and all observations for the exact PDF
version processed from that intent.

**Azure/API-owned fields**: ownership IDs, `uploadId`, declared `fileName`
(sensitive), `contentType`, `sizeBytes`, `checksumSha256`, `sourceBucket`,
`sourceKey`, upload expiration, creator, `clientCompletedAt`, observed
`sourceVersionId`, and `processingExecutionId`.

**AWS-processor-owned fields**: malware summary, validation lease/result,
`pageCount`, screen/full input references, configuration digests,
selection-rule version, stage/status, failure code, and processing timestamps.

**Rules**:

- The expected source key is opaque to the caller and unique to the upload. New
  uploads use
  `quarantine/tenants/{tenantId}/loans/{loanId}/instances/{loanInstanceId}/documents/{documentId}/uploads/{uploadId}/source.pdf`.
- Completion pins the S3 `VersionId` and verifies declared size, SHA-256,
  content type, required metadata, PDF signature, and KMS encryption.
- A clean scan applies only when its bucket, key, and `scanVersionId` match the
  completed source version.
- Processor leases are transient coordination fields and may expire; they are
  not evidence that validation succeeded.
- No later replacement may modify this upload item or its referenced versions.

### Malware Scan Observation (`MALWARE_SCAN`)

**Purpose**: Immutable/version-specific GuardDuty observation used in either
event order relative to client completion.

**Core fields**: parent upload key, `uploadId`, `scanVersionId`, normalized
`scanResultStatus`, provider scan status, provider `eventId`, `scannedAt`, and
`updatedAt`.

**Rules**: Duplicate equal observations are idempotent. Contradictory results for
the same version become `CONFLICT` and fail closed. An upload-level malware
summary is a reconciliation aid; the version-specific observation remains the
evidence record.

### Processing Execution (logical aggregate)

**Purpose**: Platform-level orchestration that connects one exact upload to its
screening, selection, optional full extraction, and final artifacts.

**Core fields**: `processingExecutionId`, ownership/upload IDs, status/stage,
exact source reference, screen/full configuration versions and SHA-256 digests,
selector-rule version, model/inference evidence, timestamps, failure code,
selection decision reference, artifacts, and associated IDP workflow records.

For migration continuity this aggregate remains represented by the `UPLOAD`,
current `DOCUMENT`, `IDP_WORKFLOW`, and versioned artifact fields rather than a
new competing registry item. One `processingExecutionId` may map to multiple
upstream execution ARNs: at least one screening attempt and, after a successful
selection, a full-extraction attempt. The platform run remains stable across a
retry of the same stage; each distinct upstream ARN gets its own workflow item.

### IDP Input Mapping

**Purpose**: Explicitly relate the platform run to the exact headless IDP S3
input. It is stored on the upload/document aggregate.

| Stage | Registry fields | Current object-key convention | Source copied |
|---|---|---|---|
| Screen | `screenInputBucket`, `screenInputKey`, `screenInputVersionId` | `screen/{processingExecutionId}/{documentId}/{uploadId}.pdf` | Exact malware-clean quarantine version |
| Full | `fullInputBucket`, `fullInputKey`, `fullInputVersionId` | `full/{processingExecutionId}/{documentId}/{uploadId}.pdf` | Exact selected-CD artifact version |

The stage input key is the IDP `ObjectKey`. The adapter also writes routing
metadata including `processing-execution-id` and encoded registry keys. Events
must resolve and verify the stored mapping; consumers must not infer authority
by parsing the object-key path or trusting object metadata alone.

### IDP Workflow (`IDP_WORKFLOW`)

**Purpose**: One observed upstream workflow attempt for a stage of a platform
processing execution.

**Core fields**: full `executionArn`, `stateMachineArn`, `pipelineStage`
(`screen` or `full`), `processingExecutionId`, registry pointers
(`documentPK`, `documentSK`, `uploadSK`), status, start/update/terminal
timestamps, terminal provider event ID, and GSI routing fields.

**Rules**:

- Full ARN equality is verified after the hash-based lookup.
- The workflow's stage input `ObjectKey` and version must match the corresponding
  stored IDP input mapping.
- A workflow event may advance only the document/upload whose current
  `processingExecutionId` matches the record.
- Terminal observations remove the active-workflow GSI marker.
- Workflow input/output payloads are not copied into telemetry or this item.

### Artifact Reference (embedded value object)

**Purpose**: Immutable pointer to exact bytes without making storage public.

**Fields**: artifact type, `bucket`, `key`, `versionId`, `checksumSha256`, media
type, size where known, producing `processingExecutionId`, configuration/schema
version, and creation timestamp.

Artifact types are:

- `source`: exact quarantine source accepted at completion;
- `screen-input`: exact source copy submitted to IDP screening;
- `selected`: deterministic selected-CD PDF;
- `full-input`: exact selected copy submitted to full extraction;
- `data-points`: schema-validated final JSON;
- `selection-decision`: versioned deterministic evidence/rule result;
- `loan-manifest`: immutable loan archive manifest.

Generated processing artifacts are deliberately outside the top-level
`quarantine/` prefix. For new uploads they use
`tenants/{tenantId}/loans/{loanId}/instances/{loanInstanceId}/documents/{documentId}/uploads/{uploadId}/artifacts/{processingExecutionId}/...`;
loan manifests use the corresponding instance's `archives/loans/...` prefix.
Only the original untrusted source PDF is stored under `quarantine/`.

Public reads never resolve an unversioned "latest" object. Download grants are
created only after current/archive ownership is resolved and the exact artifact
reference is selected.

### Document Archive (`DOCUMENT_ARCHIVE`)

**Purpose**: Immutable snapshot of one logical document's current physical
upload and artifacts.

**Core fields**: ownership IDs, stable `documentId`, frozen `uploadId`, numeric
sequence, display alias, `status=ARCHIVED`, `archivedAt`, `archivedBy`, frozen
`processingExecutionId`, failure/status provenance, and exact source/selected/
data-point artifact references.

**Rules**: The snapshot never follows a later replacement. Allocation conditions
the previous counter, current upload, and terminal document state and writes the
archive plus idempotency outcome atomically.

### Idempotency Intent (`IDEMPOTENCY`)

**Purpose**: Recover a committed Azure-to-AWS mutation after caller retry,
network timeout, or Azure replica failure.

**Core fields**: derived tenant/actor/route key, canonical request hash,
`responseStatus`, serialized safe response, `createdAt`, and transient
`expiresAtEpoch`.

**Rules**: It stores no access token, AWS credential, or signed URL beyond the
safe replay lifetime permitted by the operation. An upload initialization replay
must not return an expired upload grant; implementation must either keep the
grant within the idempotency lifetime or deterministically issue a refreshed
grant for the already-created upload without allocating new IDs.

### Outbox Event (`OUTBOX`)

**Purpose**: Durable notification of a committed product state transition.

**Core fields**: `eventId`, event type, aggregate IDs/revision, sequence where
applicable, status, attempts, safe reference payload, timestamps, and optional
next-attempt/error category.

**Rules**: It is written in the same transaction as the business state change.
Publication and consumption are idempotent, bounded, and DLQ/reconciliation
aware. It never contains document text or extracted values.

## Transient Security and Grant Entities

These entities are runtime/configuration models and are not stored in the loan
partition.

### Request Principal

**Fields**: validated issuer, audience, `tenantId`, token type, `actorId`,
`clientId`, delegated scope set, application role set, issue/expiry times, and
denylist result.

**Rules**: The raw bearer token and human-readable identity claims are never
persisted. Route permission is decided before any DynamoDB, S3, KMS, STS, or IDP
operation. A request-body tenant or identity value is ignored for authorization.

### Azure Workload Identity

**Fields**: Azure tenant ID, user-assigned managed identity resource ID, managed
identity client ID, principal/object ID, token issuer, requested AWS trust
audience, enabled state, and environment.

**Authority**: Azure and Entra resource configuration. These identifiers are
deployment inputs/outputs, not customer data and not loan-table fields.

### AWS Federation Trust

**Fields**: AWS OIDC provider ARN, exact allowed issuer, audience, subject,
Azure tenant and managed-identity identifiers, role ARN, maximum session
duration, attached policy version/digest, permitted region, and enabled state.

**Rules**:

- Trust conditions require exact issuer, audience, and workload subject.
- The permissions policy names the exact DynamoDB table/indexes, S3 prefixes,
  KMS key usage, and integration actions needed by the Azure API.
- The role grants no CloudFormation, IAM mutation, AppSync Cognito operation,
  or arbitrary bucket/table access.
- Deployment identities and the runtime workload role are separate principals.

### Assumed AWS Role Session

**Fields**: role ARN, privacy-safe session name/correlation reference, issued and
expiry times, policy version/digest, and AWS request IDs where safe.

**Rules**: Access key ID, secret access key, session token, managed-identity
token, and signed requests are memory-only and never persisted or logged. A
cached session is refreshed before expiry with synchronized refresh across
concurrent requests. Revocation or trust disablement prevents new sessions.

### Upload or Download Grant

**Fields**: exact bucket/key/version where applicable, allowed HTTP method,
required form fields/headers, content constraints, response constraints,
issued/expiry timestamps, and safe filename/content type returned to the caller.

**Rules**: A grant is generated only after Azure route and ownership
authorization. It expires in the contract window, grants no list access, carries
no Entra token, and is not persisted or logged. A grant's expiration must not
exceed the operation's security policy even if the role session lives longer.

## State Transitions and Writers

### Loan lifecycle

```text
No HEAD/current instance
  -- Azure create + conditional transaction --> ACTIVE
ACTIVE
  -- Azure archive + conditional transaction --> ARCHIVED (no current pointer)
ARCHIVED (no current pointer)
  -- Azure recreate with new loanInstanceId --> ACTIVE
```

Each loan instance itself transitions only `ACTIVE -> ARCHIVED`. Archive retry
with the same idempotency key returns the original sequence.

### Document and upload lifecycle

```text
AWAITING_UPLOAD
  -- Azure completion pins exact S3 VersionId --> VALIDATING
VALIDATING
  -- exact clean scan + deterministic PDF validation --> QUEUED
QUEUED
  -- IDP screen execution observed --> SCREENING
SCREENING
  -- deterministic winner materialized --> SELECTED
SELECTED
  -- IDP full execution submitted/observed --> EXTRACTING
EXTRACTING
  -- schema-valid output and exact artifact recorded --> SUCCEEDED

Any applicable stage -- fail-closed decision --> HOLD | REJECTED | FAILED
SUCCEEDED | HOLD | REJECTED | FAILED
  -- Azure document archive transaction --> ARCHIVED
ARCHIVED
  -- Azure replacement with same documentId/new uploadId --> AWAITING_UPLOAD
```

Only Azure performs product create/archive/replacement transitions. AWS
processors perform validation and processing transitions with conditions on the
exact current `uploadId`, `processingExecutionId`, source version, and prior
status. `HOLD`, `REJECTED`, and `FAILED` are terminal for that upload but may be
archived before a replacement is created.

### Malware reconciliation

```text
No matching scan                         --> WAITING_FOR_SCAN
Scan exists, client completion absent    --> WAITING_FOR_CLIENT_COMPLETE
Clean scan for another S3 VersionId      --> WAITING_FOR_EXACT_SCAN
Exact NO_THREATS_FOUND + completion      --> VALIDATE
Threat                                   --> REJECTED
Unsupported/failed/conflicting/unknown   --> HOLD
```

Arrival order and duplication do not change the outcome.

### IDP workflow observation

```text
UNOBSERVED -> RUNNING -> SUCCEEDED
                     -> FAILED | TIMED_OUT | ABORTED
```

Provider terminal states map to sanitized platform `FAILED`/`HOLD` outcomes
according to stage-specific rules. The upstream ARN and terminal observation are
retained even when a retry creates another upstream execution ARN.

### Federation session

```text
NO_SESSION
  -> managed-identity token acquired
  -> STS role assumed
  -> ACTIVE_TEMPORARY_SESSION
  -> REFRESHED | EXPIRED | REVOKED
```

Failure at any federation step occurs before the AWS operation and returns a
sanitized dependency/authentication failure; it never falls back to a static
key.

## Cross-System Mapping Invariants

1. One `documentId` may own many `uploadId` values over time.
2. One `uploadId` identifies exactly one source bucket/key and, after
   completion, exactly one accepted source `VersionId`.
3. One platform `processingExecutionId` belongs to one upload attempt. It maps
   to a screen IDP `ObjectKey`, optionally a full IDP `ObjectKey`, and one or more
   stage execution ARNs.
4. Screen and full `ObjectKey` values identify upstream input objects only. The
   registry mapping, object metadata, exact version, and workflow record must
   agree before an event advances state.
5. An execution ARN is unique to one upstream attempt and stage. A hash-index
   lookup must be followed by full ARN and platform-run equality checks.
6. Source, selected, and result artifacts retain independent bucket/key/version/
   checksum tuples. No artifact inherits another artifact's `VersionId`.
7. Current document fields are convenience projections. Document and loan
   archives freeze exact IDs and artifact references and never follow a current
   pointer.
8. Entra user tokens authorize only Azure. They are never forwarded to AWS.
   Azure's workload token is used only for STS federation and is never accepted
   as a product caller token.

## Migration Invariants

1. **One registry**: DynamoDB remains the sole mutable loan/document registry.
   No Cosmos DB, AppSync table, or Azure cache becomes a second source of truth.
2. **Key compatibility**: Existing PK/SK/GSI layouts, ID prefixes, archive
   counters, item shapes, and object paths remain readable by both the Azure API
   and retained AWS processors.
3. **No identity rewrite**: Existing IDs retain their values and semantics.
   New IDs are described as platform-issued rather than AWS-API-issued, but no
   stored identifier is regenerated.
4. **Field-level writers**: Azure replaces the custom AWS Loan API as the writer
   of product command fields. AWS processors retain their current scan,
   validation, workflow, and artifact field ownership. Conditional expressions
   guard every shared item update.
5. **Single public mutator**: Cutover never leaves Azure and the custom AWS Loan
   API simultaneously accepting new public mutations. Rollback must restore one
   authority, not create dual writers.
6. **Replay continuity**: Canonical request hashing and idempotency keys continue
   across the hosting change. If DynamoDB commits but the Azure response is lost,
   retry returns the committed result without allocating another identity or
   archive sequence.
7. **Version continuity**: Existing S3 buckets, keys, versions, checksums,
   manifests, and IDP workflow mappings are not copied, renamed, or replaced as
   part of API cutover.
8. **Headless continuity**: Runtime integration uses the pinned S3/event path.
   It does not assume AppSync, Cognito groups/service users, or the optional Jobs
   REST API exists.
9. **Credential discontinuity**: No AWS access key is migrated into Azure. The
   only runtime AWS access is a short-lived STS session obtained from the exact
   managed-identity federation trust.
10. **Failure safety**: Cross-cloud timeout, cancellation, credential expiry, or
    dependency unavailability cannot fabricate success, advance an unverified
    version, or reveal whether another tenant's record exists.
11. **Audit continuity**: Actor/client IDs, correlation IDs, configuration
    digests, execution ARNs, artifact versions, and safe reason codes remain
    queryable without logging document content, extracted values, tokens, or
    signed grants.
12. **Activation evidence**: Before production DNS activation, synthetic
    lifecycle tests and a registry backup establish that Azure reads and
    mutations preserve all conditions, state transitions, and archived history.
    A legacy installation records its historical DNS snapshot separately; a
    clean deployment has no AWS application endpoint to restore.

## Validation and Retention

- All caller-visible IDs and payloads satisfy the canonical OpenAPI contract.
- All timestamps are UTC RFC 3339 values; archive sequences are positive
  integers; counters never decrease.
- Conditional transactions enforce current pointers, prior status, prior
  sequence, exact upload/run identity, and idempotency outcome together.
- DynamoDB PITR, deletion protection, encryption, and backup remain enabled.
- S3 versioning, Block Public Access, Bucket Owner Enforced, TLS-only policy,
  checksums, and customer-managed KMS encryption remain enabled.
- Archived registry records and referenced object versions have no transient
  TTL. Legal-hold-aware purge is a separate privileged feature.
- Idempotency, leases, grants, cached STS sessions, and reconciliation markers
  may expire only according to bounded policies that cannot erase committed
  business or provenance evidence.
- Tenant/account identifiers, credentials, tokens, signed URLs, filenames,
  document content, OCR, and extracted values remain excluded from source
  control and telemetry.
