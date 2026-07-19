# Feature Specification: Azure API Control Plane

**Feature Branch**: `codex/spec-kit-claude-code`

**Created**: 2026-07-16

**Status**: In progress

**Input**: User description: "Move the platform's main loan and document logic to an Entra-protected Azure API. The Azure API invokes the pinned headless AWS IDP integration only when processing or artifact access requires AWS; do not expose a custom AWS Loan API or direct AppSync API to the browser."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use One Azure Product API (Priority: P1)

An authenticated user creates, reads, and archives loans and documents through one stable product API hosted in Azure. The user does not need an AWS identity and never interacts with an AWS management API.

**Why this priority**: This establishes the requested ownership boundary. Every later upload, processing, and download journey depends on the Azure API being the sole public domain API.

**Independent Test**: With synthetic Entra identities and an isolated registry, exercise the existing loan/document REST contract through the Azure endpoint and verify authorization, identity allocation, idempotency, and archive sequencing without deploying an AWS Loan API.

**Acceptance Scenarios**:

1. **Given** a caller with the required tenant, client, delegated scope, and assigned role, **When** the caller creates a loan and document, **Then** the Azure API returns distinct platform-issued `loanInstanceId`, `documentId`, and `uploadId` values.
2. **Given** a caller without the permission required by a route, **When** the caller invokes that route, **Then** the request fails before any AWS operation is attempted.
3. **Given** a previously archived `23051` loan and a newly active incarnation, **When** the active incarnation is archived, **Then** the API returns the next monotonic alias (`23051_002`) and includes every document by immutable reference.
4. **Given** the same mutation and idempotency key are retried, **When** the request is replayed, **Then** the original result is returned without allocating another identity or archive sequence.

---

### User Story 2 - Upload and Process Through Headless IDP (Priority: P1)

An authorized user uploads a PDF through a short-lived grant issued by the Azure API. The exact uploaded version is scanned and validated before the existing headless AWS IDP workflow receives it.

**Why this priority**: Secure upload and processing are the platform's primary business outcome and must remain intact while the public API moves clouds.

**Independent Test**: Upload a synthetic PDF through a constrained grant, complete the upload through the Azure API, emit synthetic clean and non-clean scan outcomes, and verify that only the exact clean version reaches the pinned IDP input boundary.

**Acceptance Scenarios**:

1. **Given** an authorized upload request with declared size and checksum, **When** the Azure API initializes the document, **Then** it returns platform identities and a short-lived, condition-constrained direct upload grant without returning AWS credentials.
2. **Given** uploaded bytes whose version, size, type, or checksum differs from the declared upload, **When** completion or validation runs, **Then** processing fails closed and that object never reaches IDP.
3. **Given** a clean, valid PDF version, **When** validation completes, **Then** the integration submits that exact version to the configured headless IDP screening input and records a separate upstream object/execution identity.
4. **Given** a threat, unsupported scan, ambiguous Closing Disclosure selection, or mismatched configuration, **When** processing evaluates the package, **Then** the document enters a terminal reject/hold state with no guessed result.

---

### User Story 3 - Read Status and Exact Artifacts (Priority: P1)

An authorized user reads document status and data points and downloads source or selected documents through Azure. Azure enforces loan ownership before accessing AWS-backed state or artifacts.

**Why this priority**: Processing has no user value unless the result and immutable source provenance can be retrieved safely.

**Independent Test**: Seed synthetic current and archived records with exact object versions, then verify status, JSON retrieval, and download grants for allowed and denied callers without exposing AppSync or S3 directly as a public application API.

**Acceptance Scenarios**:

1. **Given** a completed document owned by the caller's tenant, **When** the caller requests status or data points, **Then** the Azure API returns the platform state or the exact pinned JSON artifact.
2. **Given** an authorized artifact request, **When** the API issues a download grant, **Then** the grant is short lived, version pinned, and restricted to the requested object and response type.
3. **Given** an archived loan or document revision, **When** the caller reads it, **Then** the same identities and artifact versions recorded at archive time are returned.
4. **Given** a caller from another tenant or without the route permission, **When** the caller requests an existing object, **Then** the API does not disclose its existence or access its AWS artifact.

---

### User Story 4 - Reproduce and Operate the Cross-Cloud Boundary (Priority: P2)

An operator provisions and deploys the Azure API, its managed workload identity, the restricted AWS trust, the retained AWS data plane, and the pinned IDP release using reviewed scripts and short-lived deployment identities.

**Why this priority**: The architecture is not production ready until its identities, certificates, custom domain, least-privilege permissions, monitoring, and recovery path are reproducible.

**Independent Test**: Deploy to a synthetic non-production environment from empty resource groups/stacks, validate both positive and negative federation paths, rotate/redeploy without downtime, and run the documented smoke and recovery checks.

**Acceptance Scenarios**:

1. **Given** the approved environment configuration, **When** deployment runs, **Then** the Azure API receives a custom HTTPS hostname and managed workload identity without storing an AWS key or application secret.
2. **Given** a valid token from the exact Azure workload subject and audience, **When** the API requests AWS credentials, **Then** AWS returns short-lived credentials restricted to the platform data-plane operations.
3. **Given** a token with the wrong tenant, issuer, audience, or subject, **When** it is presented to AWS federation, **Then** role assumption fails.
4. **Given** a pull request or production deployment, **When** automation runs, **Then** validation uses no production document data and cloud deployment uses repository- and environment-restricted workload federation.
5. **Given** Azure CLI is installed through the Windows MSI `az.cmd` wrapper, **When** a provisioning script sends a Graph query URI or JSON body containing command-shell metacharacters, **Then** the script bypasses `cmd.exe`, preserves every argument exactly, and fails closed if the safe Azure CLI engine cannot be resolved.
6. **Given** a new Entra application has no existing delegated scopes or application roles, **When** provisioning initializes its permissions, **Then** the empty collections are accepted and every required permission receives a stable generated identifier.
7. **Given** custom Entra applications require unique delegated-scope and app-role values, **When** canonical permission `P` is provisioned, **Then** its scope is `P`, its role is `P.Role`, and the Azure API accepts only that exact role before normalizing it to `P` for private domain dispatch.
8. **Given** a first-install AWS environment has no IDP or platform stack and AWS CLI prefixes a service error with its standard `aws: [ERROR]:` marker, **When** the explicit bootstrap pass allows a missing stack, **Then** only the exact `DescribeStacks` not-found response is accepted and every access, throttling, validation, warning, or multiline error still fails closed.
9. **Given** the private platform and pinned IDP templates use the AWS Serverless transform, **When** CloudFormation assumes either split execution role, **Then** that role can create a change set only for the regional AWS-managed Serverless transform in addition to its existing stack-specific resource permissions.

### Edge Cases

- An Azure request is cancelled or times out after an AWS mutation succeeds; an idempotent retry must recover the committed result.
- Temporary AWS credentials expire while a request is active; refresh must be synchronized and a returned upload/download grant must never outlive its effective credential policy.
- Multiple Azure API replicas allocate or archive the same logical record concurrently; conditional persistence must produce one winner and a deterministic conflict/replay result.
- More than one request reaches a replica while the HTTP scale-out rule is reacting; the per-replica domain lock must serialize the module-global AWS client seam because the scale target is not an admission-control cap.
- The client-complete call and malware event arrive in either order or more than once; reconciliation must advance only after both facts apply to the same exact object version.
- AWS IDP, registry, or object storage is unavailable; Azure returns a sanitized dependency error and does not fabricate success.
- An upstream IDP object key, workflow ARN, or job identifier differs from the platform `documentId` or `processingExecutionId`; mappings remain explicit and are never substituted.
- A large PDF or result exceeds synchronous limits; PDF bytes continue to bypass the API, and large artifacts use authorized object grants rather than inline API responses.
- The headless IDP deployment has no AppSync or Jobs REST endpoint; deployment and runtime must not assume either interface exists.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The Azure API MUST be the sole public API for loan, document, archive, processing-status, data-point, and artifact-grant operations.
- **FR-002**: The platform MUST NOT deploy or expose the custom AWS Loan API Lambda, its API Gateway, a browser-callable AppSync endpoint, or a Cognito service user for backend integration.
- **FR-003**: The existing versioned REST contract MUST remain authoritative for browser and service callers unless a response or path is explicitly revised in the same change.
- **FR-004**: The Azure API MUST cryptographically validate Entra access-token signature, exact issuer, audience, lifetime, tenant, token type, immutable actor, allowed client, emergency denylist, route scope, and assigned application role before domain or AWS access; app-only calls MUST also carry `idtyp=app` and `azpacr=2` from an allowlisted certificate-authenticated client.
- **FR-005**: Interactive callers MUST use authorization code with PKCE and MUST NOT possess a client secret, certificate private key, AWS credential, or direct AWS application permission.
- **FR-006**: The Azure workload MUST exchange its managed identity token for short-lived AWS credentials through a trust restricted to the expected issuer, audience, and workload subject; static AWS access keys are prohibited.
- **FR-007**: AWS permissions issued to the Azure workload MUST be limited to the exact registry, object prefixes, encryption keys, and integration actions needed by the product API.
- **FR-008**: The Azure API MUST issue and preserve distinct platform `loanInstanceId`, `documentId`, `uploadId`, and `processingExecutionId` values; AWS S3 `VersionId`, IDP object key/job identity, and workflow ARN remain separate upstream identifiers.
- **FR-009**: The retained registry MUST remain the single source of truth shared by the Azure API and AWS event processors for the initial migration; the feature MUST NOT introduce a second mutable loan/document registry.
- **FR-010**: Every mutation MUST retain canonical-request idempotency, conditional concurrency control, and monotonic loan/document archive semantics from the authoritative contract.
- **FR-011**: PDF bytes MUST upload directly to a versioned quarantine object using a short-lived grant constrained by key, content type, size, checksum, and server-side encryption; the browser MUST receive no AWS credentials.
- **FR-012**: Completion MUST pin the exact S3 version, size, and checksum and MUST reconcile safely with duplicated or reordered malware events.
- **FR-013**: Only a checksum-verified, parser-valid, malware-clean exact object version MAY cross the headless IDP input boundary.
- **FR-014**: The initial IDP adapter MUST use the pinned headless deployment's supported S3/event integration and MUST NOT require AppSync or the optional Jobs REST API.
- **FR-015**: The screening and full-extraction configuration versions, object versions, selection evidence, upstream execution identity, and final artifacts MUST remain traceable to the platform processing execution.
- **FR-016**: Status and artifact reads MUST first authorize the requested loan/document/archive in Azure, then access only the exact AWS record or object version associated with it.
- **FR-017**: Inline data-point retrieval MUST enforce a bounded result size; larger JSON and all PDF downloads MUST use short-lived, version-pinned grants.
- **FR-018**: Operational logs and errors MUST exclude tokens, credentials, signed URLs, document bytes/text, extracted values, sensitive filenames, and raw upstream workflow input/output.
- **FR-019**: Azure and AWS infrastructure, identity, custom-domain certificate configuration, deployment federation, alarms, backups, and recovery checks MUST be reproducible from reviewed infrastructure and scripts.
- **FR-020**: Production and pull-request automation MUST use synthetic data and short-lived cloud identities restricted to the exact repository, environment, and workload.
- **FR-021**: Every hand-authored production Python file MUST retain at least 80% line coverage individually and in aggregate; browser behavior changes MUST include deterministic Playwright coverage.
- **FR-022**: Upstream unavailability, invalid federation, scan uncertainty, configuration mismatch, and ambiguous classification MUST fail closed with sanitized, retry-safe outcomes.
- **FR-023**: Data-point editing or replacement by product callers is outside this migration; adding it requires a separate audited, versioned, idempotent contract rather than treating a stock IDP mutation as a product API.
- **FR-024**: Production `/v1` routes MUST accept only the configured custom API host; the Container Apps provider hostname MAY expose only `/health` and `/ready` for deployment probes, and production SPA publication MUST require recorded custom-domain binding and DNS cutover.
- **FR-025**: While the retained domain uses module-global AWS clients, each single-worker Azure API replica MUST serialize workload-session acquisition, client binding, and domain dispatch; its HTTP scale target MUST remain exactly `1` to minimize head-of-line waiting without treating that scaling signal as a hard admission cap, and horizontal scale MUST remain bounded by the configured maximum replicas.
- **FR-026**: PowerShell provisioning MUST invoke Azure CLI without passing Graph query URIs, JSON bodies, or other untrusted metacharacters through the Windows command processor; Windows MSI installations MUST use the bundled Azure CLI Python engine directly, while non-`cmd` installations MUST preserve native argument boundaries.
- **FR-027**: For canonical permission `P`, Entra provisioning MUST publish delegated scope `P` and collision-free application role `P.Role`; the Azure API MUST reject an unsuffixed or differently suffixed role and normalize only `P.Role` to `P` after validating the external token.
- **FR-028**: Missing-stack handling MAY remove one exact AWS CLI `aws: [ERROR]: ` service-error prefix, including its single trailing ASCII space, before classification, but MUST otherwise require the complete anchored CloudFormation `DescribeStacks` `ValidationError` not-found message and MUST NOT treat other CLI or service failures as absence; AWS command failures MUST report only the service and operation, never parameter values.
- **FR-029**: Each split CloudFormation execution role MUST allow `cloudformation:CreateChangeSet` on only the regional AWS-managed `Serverless-2016-10-31` transform ARN required to expand reviewed SAM templates; this permission MUST NOT broaden the GitHub deployment role or stack resource scope.

### Key Entities

- **Loan Head**: Tenant-scoped stable business key that points to at most one active immutable loan instance and owns monotonic archive counters.
- **Loan Instance**: Platform-issued immutable incarnation of a business loan; archiving it includes all documents by reference.
- **Document**: Platform-issued stable logical document within a loan instance; points to one current upload and zero or more archived revisions.
- **Upload**: Platform-issued physical-upload intent with declared metadata, exact quarantine object identity, validation state, and one processing execution.
- **Processing Execution**: Platform orchestration identity that records stage, configuration provenance, and mappings to upstream IDP identifiers.
- **Artifact Reference**: Exact bucket, key, version, checksum, media type, and provenance for source PDF, selected PDF, or extracted JSON.
- **Workload Federation Trust**: Binding between the Azure managed workload issuer/audience/subject and a least-privilege AWS role session.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All canonical loan/document/archive contract tests pass through the Azure API with no deployed AWS Loan API endpoint.
- **SC-002**: In automated negative tests, 100% of wrong-issuer, wrong-audience, wrong-tenant, wrong-client, wrong-subject, missing-scope, and missing-role requests are rejected before protected data access.
- **SC-003**: In concurrency and replay tests, 100% of identical idempotent retries return the original identity/sequence and no test allocates duplicate archive aliases.
- **SC-004**: In upload-boundary tests, 100% of mismatched, malicious, unscanned, or non-current object versions are prevented from reaching IDP.
- **SC-005**: A user can initialize an upload and receive a direct-upload grant within two seconds at the 95th percentile under the agreed baseline load, excluding external identity-provider outages.
- **SC-006**: Status and small data-point reads complete within two seconds at the 95th percentile under the agreed baseline load, excluding IDP processing time and external outages.
- **SC-007**: Every successful synthetic processing run can be traced from platform document/upload/execution IDs to exact source, selected, configuration, upstream execution, and output versions.
- **SC-008**: Repository validation reports at least 80% line coverage for every hand-authored service file and the combined service suite, with no reduced threshold or widened exclusion.
- **SC-009**: A clean-environment scripted deployment produces the Azure custom HTTPS API, exact managed-identity federation trust, retained AWS data plane, and pinned headless IDP without storing a reusable AWS key or application secret.
- **SC-010**: Production acceptance demonstrates successful backup restore, credential/trust revocation, certificate renewal, dependency-failure alarms, and a synthetic end-to-end upload/status/download journey.
- **SC-011**: Automated PowerShell-helper tests prove that Azure CLI arguments containing `&`, `$`, and JSON punctuation remain single literal arguments and that Windows `az.cmd` resolution selects the bundled Python engine rather than executing the wrapper.
- **SC-012**: Deterministic provisioning and JWT tests prove that no generated scope and app role share a value, raw `P.Role` authorizes canonical permission `P`, and raw unsuffixed role `P` is rejected.
- **SC-013**: PowerShell-helper tests accept both prefixed and unprefixed exact CloudFormation stack-not-found messages while rejecting prefixed access errors, warning prefixes, multiline output, throttling, and unrelated validation failures.
- **SC-014**: Repository validation proves that exactly the platform and IDP CloudFormation execution roles include the regional AWS Serverless transform permission and that no shared execution role is restored.

## Assumptions

- DynamoDB remains the authoritative registry during this migration because the existing malware and IDP processors update it transactionally; moving state to an Azure database would require a separate reliable cross-cloud callback/outbox design.
- S3 remains the authoritative document/artifact store, and the existing GuardDuty, upload processor, postprocessor, and two-pass IDP workflow remain in AWS.
- The initial Azure runtime is a horizontally scalable HTTPS service with a durable user-assigned managed identity; exact hosting technology is selected in the implementation plan.
- The existing OpenAPI paths and response shapes remain stable. Descriptions and issuer ownership may change from "AWS API" to "platform/Azure API."
- AppSync may be evaluated in a later non-headless feature, but it is not deployed or required here.
- UI implementation remains a separate workstream, but any runtime configuration and hosting changes needed to point it to the Azure API are part of this migration.
