# Feature Specification: Loan Document Platform

> **Historical baseline**: This packet records the original AWS-hosted public
> API design. It is superseded for new implementation work by
> [`002-azure-api-control-plane`](../002-azure-api-control-plane/spec.md), which
> moves the sole public/domain API to Azure while retaining the private AWS data
> and headless IDP processing plane. Checked tasks here remain historical facts;
> they are not authorization to redeploy the retired AWS Loan API.

- **Feature Directory**: `001-loan-document-platform`
- **Created**: 2026-07-14
- **Status**: In progress — brownfield implementation baseline
- **Input**: Production-oriented loan lifecycle and document-processing API protected by Microsoft Entra, with AWS-generated identities, immutable archives, direct uploads, and a two-pass Closing Disclosure workflow.

## User Scenarios and Testing

### User Story 1 — Manage the loan lifecycle (Priority: P1)

An authorized operator creates a current loan using a business `loanId`, retrieves the current incarnation together with prior archives, archives the current incarnation and all of its documents, and can recreate the same business loan without losing history.

**Why this priority**: Every document and artifact belongs to an immutable loan incarnation. The rest of the platform cannot preserve history correctly without this boundary.

**Independent Test**: Create synthetic loan `23051`, archive it twice with a recreation between archives, and verify that the current response and archive reads distinguish immutable instances and expose aliases `23051_001` and `23051_002`.

**Acceptance Scenarios**:

1. **Given** no active `23051`, **when** an authorized caller creates it, **then** AWS returns a new immutable `loanInstanceId` and makes that instance current.
2. **Given** active `23051` and no archives, **when** the caller archives it, **then** the response returns sequence `1`, alias `23051_001`, and a snapshot that includes every document belonging to that instance.
3. **Given** the first instance is archived, **when** the caller recreates and later archives `23051`, **then** AWS returns a different `loanInstanceId` and alias `23051_002`.
4. **Given** an archive request already succeeded, **when** the caller retries the same intent with the same idempotency key, **then** AWS returns the original archive result without allocating another sequence.
5. **Given** a current loan and prior archives, **when** the caller retrieves the loan, **then** the response separates current state from ordered archive references.

---

### User Story 2 — Upload and process a loan package (Priority: P1)

An authorized operator creates a logical document under a current loan. AWS returns a stable `documentId`, a physical `uploadId`, and a constrained direct-upload grant. After the browser uploads the PDF and confirms completion, the platform safely processes the exact uploaded version.

**Why this priority**: It is the primary ingestion path and establishes the required separation between API metadata, untrusted bytes, and processing.

**Independent Test**: Create a synthetic document, upload a valid PDF through the returned S3 form, complete that exact upload, simulate a clean malware result, and verify the documented processing states and immutable provenance.

**Acceptance Scenarios**:

1. **Given** an active loan and valid size/checksum metadata, **when** the caller creates a document, **then** AWS returns a generated `documentId`, generated `uploadId`, expected upload constraints, and a short-lived presigned POST.
2. **Given** the presigned POST, **when** the browser uploads bytes, **then** the PDF goes directly to the versioned quarantine location and no OAuth token is sent to S3.
3. **Given** the expected object was uploaded, **when** the caller completes the upload, **then** AWS records its exact S3 version, checksum, and size; the completion request carries no PDF bytes.
4. **Given** a threat, unsupported scan, checksum mismatch, invalid/encrypted PDF, or limit violation, **when** validation runs, **then** processing fails closed and no IDP execution starts.
5. **Given** a clean valid package whose CD boundaries are unknown, **when** processing runs, **then** text-only OCR/classification covers all package pages and Forms+Tables extraction runs only on the deterministically selected CD pages.
6. **Given** conflicting or tied CD evidence, **when** selection runs, **then** the document enters `HOLD` instead of selecting by guesswork.

---

### User Story 3 — Read records and obtain controlled downloads (Priority: P1)

An authorized operator reads active or archived document metadata and data points, then requests a fresh, short-lived grant for an authorized source, selected-document, or data-points artifact.

**Why this priority**: Processing has no product value unless authorized users can retrieve its result without making storage public or exposing durable URLs.

**Independent Test**: For one successful active document and one archived version, read metadata/data points and verify each download endpoint returns a new expiring grant only for an artifact that belongs to the requested immutable context.

**Acceptance Scenarios**:

1. **Given** a successful current document, **when** the caller reads data points, **then** the API returns the immutable stored result and provenance without exposing storage credentials.
2. **Given** an active or archived artifact, **when** an authorized caller requests a download, **then** the API performs fresh authorization and returns a grant expiring within the configured short window.
3. **Given** a caller lacks the required permission or the artifact does not belong to the addressed loan instance/version, **when** a read or download is requested, **then** access fails without leaking existence or content.
4. **Given** an old signed grant, **when** its expiration passes, **then** it cannot be reused and the caller must request a new grant.

---

### User Story 4 — Archive and replace a logical document (Priority: P2)

An authorized operator archives the current physical version of a document while preserving the AWS-generated logical `documentId`, then uploads a replacement under the same logical document.

**Why this priority**: Corrections are common, but they must not break references or overwrite evidence used by earlier processing.

**Independent Test**: Archive two successive uploads for one `documentId` and verify aliases `<documentId>_001` and `<documentId>_002`, immutable upload/version references, and idempotent sequence allocation.

**Acceptance Scenarios**:

1. **Given** a document with a current upload, **when** it is archived, **then** AWS freezes that upload as archive sequence `1` while retaining the logical `documentId`.
2. **Given** an archived current upload, **when** the caller creates a replacement upload, **then** AWS returns a new `uploadId` under the same `documentId`.
3. **Given** a replacement was processed and archived, **when** archives are retrieved, **then** sequence `2` refers to the replacement and sequence `1` remains unchanged.
4. **Given** a repeated archive request with the same idempotency key, **when** it is retried, **then** no extra archive is allocated.

---

### User Story 5 — Enforce human and service authorization (Priority: P1)

Interactive users sign in to the React SPA with Entra SSO and PKCE. API callers use Entra OAuth tokens. Each operation is authorized by its declared permission, with matching user/application roles and no browser-held secret or AWS credential.

**Why this priority**: Loan documents and extracted data contain sensitive personal information; authentication without route-level authorization is insufficient.

**Independent Test**: Exercise each permission with valid delegated and app-only test tokens, then verify wrong tenant, audience, client, token type, scope/role, expiry, or denylisted clients fail closed.

**Acceptance Scenarios**:

1. **Given** an assigned interactive user, **when** the SPA signs in, **then** it uses authorization code with PKCE and stores the session only in `sessionStorage`.
2. **Given** a delegated token, **when** an API route is called, **then** both the route scope and matching assigned app role are required.
3. **Given** an app-only token, **when** an API route is called, **then** `idtyp=app`, the matching application role, expected tenant/audience, and an allowlisted client ID are required.
4. **Given** a browser session, **when** it uploads or downloads, **then** it never receives a client secret, private certificate, AWS credential, or persistent signed URL.

### Edge Cases

- Two create requests race for the same active `loanId`; only one immutable instance becomes current.
- An idempotency key is reused with a different canonical request; the API rejects the conflict rather than replaying either request.
- Archive sequence exceeds three digits; formatting expands and never truncates.
- A loan archive races with document upload or archive; conditional transactions prevent a partial snapshot.
- Upload completion occurs before S3 consistency/event delivery; the reconciliation path remains idempotent and pins the eventual exact version.
- GuardDuty delivers duplicate or out-of-order scan events; only the matching object version/checksum can advance.
- A document is archived while processing; immutable execution inputs/results remain attributable to the frozen version.
- The selected CD has fewer or more than the typical 5–6 pages; deterministic evidence and configured limits govern the outcome, not a hard-coded page count.
- Model output is invalid or contains unexpected fields; schema validation rejects it and records a safe status without logging content.
- A loan or document alias contains `_001`-like text; clients follow server-returned identifiers/sequences and never parse display aliases.

## Requirements

### Functional Requirements

- **FR-001**: The API MUST accept a caller-provided stable `loanId` and generate an immutable `loanInstanceId` for each active incarnation.
- **FR-002**: At most one current instance MAY exist for a tenant and `loanId`; concurrent creation MUST be conditionally consistent.
- **FR-003**: Retrieving a loan MUST distinguish its current instance from all archived instances and return server-owned archive sequences/aliases.
- **FR-004**: Archiving a loan MUST freeze the immutable loan instance and logically include all documents belonging to it without copying or renaming every object.
- **FR-005**: Loan archive sequence allocation MUST be atomic, monotonic, minimum three-digit display formatting, and idempotent for a repeated intent.
- **FR-006**: After a loan is archived, a caller MAY create a new current instance for the same `loanId` without mutating prior instances.
- **FR-007**: The API MUST generate and return `documentId` before the first document upload.
- **FR-008**: `documentId` MUST remain stable across replacement uploads; every physical upload MUST receive a distinct `uploadId`.
- **FR-009**: Document archive sequence allocation MUST freeze the current upload/version and use the same atomic, monotonic, idempotent rules as loan archives.
- **FR-010**: Every state-changing API operation MUST require an idempotency key and MUST bind it to tenant, immutable actor, route, and a canonical request hash.
- **FR-011**: Reusing an idempotency key for different request content MUST fail without performing a second mutation.
- **FR-012**: Document creation/replacement MUST return a short-lived presigned S3 POST constrained to the exact opaque key, declared PDF type, size, checksum, and required encryption fields.
- **FR-013**: PDF bytes MUST upload directly to a private versioned quarantine bucket; API completion requests MUST carry metadata only.
- **FR-014**: Upload completion and downstream processing MUST pin the exact bucket, key, S3 version, size, and checksum.
- **FR-015**: No upload MAY reach IDP until the exact version has a `NO_THREATS_FOUND` result and passes PDF structure, encryption, page, size, and checksum validation.
- **FR-016**: Duplicate or out-of-order storage/scan events MUST be handled idempotently and MUST NOT advance a different object version.
- **FR-017**: Screening MUST use text-only OCR across every package page because document/CD boundaries are unknown before submission.
- **FR-018**: Page-level classification MUST inspect all pages with the reviewed one-page context and LLM-determined section splitting configuration.
- **FR-019**: The selector MUST use explicit borrower/CD evidence and MUST produce `HOLD` for conflicts, missing ranking evidence, or ties.
- **FR-020**: Full Forms+Tables extraction MUST run only on the materialized winning CD artifact and MUST preserve the reviewed full-extraction configuration unless regression evidence approves a new version.
- **FR-021**: Every processing execution MUST record source and selected artifact versions/checksums, configuration digest/version, model/profile, effective inference region, schema/prompt version, status, and output checksum.
- **FR-022**: Active and archived loan/document/data-point reads MUST resolve through immutable ownership references and MUST never read an unpinned “latest” object.
- **FR-023**: Download operations MUST perform fresh authorization and return grants that expire within one to five minutes for only the requested authorized artifact.
- **FR-024**: The SPA MUST authenticate interactive users through single-tenant Entra authorization code with PKCE, exact production redirect URIs, and no browser secret or certificate.
- **FR-025**: The API MUST validate Entra signature, exact v2 issuer, API audience, lifetime, tenant, token type, immutable actor, allowlisted client, emergency denylist, and route permission.
- **FR-026**: Delegated authorization MUST require the declared scope and matching assigned app role; app-only authorization MUST require the matching application role and `idtyp=app`.
- **FR-027**: The public API MUST be reached through the configured CloudFront/WAF hostname; its Regional origin MUST require an origin-verification value and the default `execute-api` endpoint MUST be disabled.
- **FR-028**: Logs, metrics, traces, alarms, and source control MUST exclude PDF/OCR content, extracted values, tokens, signed URLs, private keys, credentials, and sensitive filenames.
- **FR-029**: Queues/events MUST use bounded retry, deduplication/idempotency, DLQs, and reconciliation so a missed asynchronous event does not silently strand an upload.
- **FR-030**: Permanent purge MUST remain a separate legal-hold-aware administrator workflow; archive endpoints MUST NOT delete preserved business records or object versions.
- **FR-031**: CI MUST measure every hand-authored production Python service file independently and in aggregate and MUST fail when line coverage is below 80%; future authored React/TypeScript production files MUST independently meet 80% statements, lines, functions, and branches. Aggregation, threshold reduction, narrowed source inclusion, or unjustified exclusions MUST NOT conceal a deficient file.
- **FR-032**: Every affected SPA journey MUST have a Playwright integration test against the production build with deterministic synthetic identity, API, and storage behavior; critical hosted Entra/API/S3 journeys MUST pass an environment-gated synthetic Playwright smoke suite before production acceptance.

### Key Entities

- **Loan Head**: Tenant/business-key pointer to the one current immutable loan instance and next archive sequence.
- **Loan Instance**: AWS-generated immutable incarnation that owns documents, uploads, executions, and artifacts.
- **Loan Archive**: Read-only sequence/alias and manifest reference freezing one loan instance.
- **Logical Document**: Stable AWS-generated `documentId` within a loan instance and pointer to its current upload.
- **Document Archive**: Read-only sequence freezing one logical document’s physical upload and artifacts.
- **Upload**: One physical PDF version, expected/observed integrity metadata, scan state, validation state, and processing state.
- **Processing Execution**: One screening or full-extraction run with immutable inputs and provenance.
- **Artifact**: Versioned source, selected CD, or data-points object with checksum and ownership references.
- **Idempotency Intent**: Actor/route/key-bound canonical request and stored outcome.
- **Outbox Event**: Durable state-change notification published idempotently to asynchronous consumers.

## Assumptions

- One Entra tenant and one AWS account/environment are initially deployed, with separate registrations/stacks required for future environments.
- `loanId` is unique within the authenticated tenant, not globally.
- The existing `cd-full-v1` configuration is the accepted extraction-accuracy baseline; this feature does not redesign extraction fields.
- A typical winning CD is 5–6 pages, but selection is evidence-driven and not fixed to that count.
- Production DNS names, AWS account/profile, Entra tenant/subscription, group assignments, and CA-issued machine certificates are deployment inputs and are never committed.
- The $100 AWS Budget provides alerting; service concurrency and document limits provide enforceable spend controls.

## Out of Scope

- Permanent purge/deletion UI and legal-hold policy implementation.
- A tenant-wide “list every loan” endpoint.
- Replacing the supplied full Closing Disclosure extraction schema or claiming new extraction accuracy.
- Storing documents, OCR text, extracted data, tokens, private keys, or cloud credentials in GitHub.
- Browser-held confidential-client secrets, machine-client certificates, or AWS credentials.
- Copying all objects when a loan is archived.

## Success Criteria

### Measurable Outcomes

- **SC-001**: Automated lifecycle tests demonstrate two create/archive cycles for one `loanId`, producing distinct immutable instances and ordered `_001`/`_002` aliases with safe idempotent retries.
- **SC-002**: Automated document tests demonstrate AWS-generated stable `documentId`, unique replacement `uploadId` values, and ordered immutable document archives.
- **SC-003**: Synthetic end-to-end acceptance demonstrates direct quarantine upload, clean-version validation, all-page screening, deterministic winner selection, selected-page full extraction, and successful data-point read/download.
- **SC-004**: Negative acceptance tests demonstrate that every non-clean scan result, mismatched version/checksum, invalid PDF, tied/ambiguous selection, and invalid authorization case fails closed before unauthorized processing or disclosure.
- **SC-005**: Every mutation can be retried with the same idempotency key without an extra business effect, while changed content with the same key is rejected.
- **SC-006**: All active/archive document and data-point downloads require a fresh permission decision and expire within five minutes.
- **SC-007**: Repository validation, per-file and aggregate 80% production-code coverage, lint, OpenAPI validation, CloudFormation lint, PowerShell parsing, React typecheck/build, and applicable Playwright integration suites all pass before production deployment.
- **SC-008**: Production smoke tests confirm Entra SSO/API OAuth, custom UI/API hostnames, WAF/origin isolation, alarm delivery, and no sensitive payloads in sampled logs.
- **SC-009**: A documented restore exercise and machine-client certificate rotation exercise complete successfully before production acceptance.
- **SC-010**: Processing provenance lets an operator trace each result to exact source/selected versions, checksums, config/model versions, and execution identifiers without reading document content from logs.
- **SC-011**: A test change that lowers any in-scope production file to 79.99% line coverage or weakens a configured threshold fails CI.
- **SC-012**: Synthetic Playwright cases cover sign-in/permissions, loan lifecycle, direct upload/completion/status, document archive/replacement, reads/downloads, retry/hold/expiry behavior, accessibility, and prohibited browser persistence without contacting live services on pull requests.
