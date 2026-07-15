# Research and Decisions: Loan Document Platform

All material architecture choices are resolved for the baseline. Live account/domain values remain deployment inputs, not design ambiguities.

## Source hosting and AWS delivery identity

**Decision**: Use the public GitHub repository and GitHub-hosted Actions. Production deployment assumes a short-lived AWS role through GitHub OIDC, restricted to the exact repository and protected environment.

**Rationale**: This matches the requested source host, preserves free public-repository Actions, avoids a CodeCommit mirror, and removes long-lived AWS access keys from GitHub.

**Alternatives rejected**: CodeCommit adds an unwanted second source of truth. Static IAM keys create avoidable credential rotation and exfiltration risk.

## Entra authentication and authorization

**Decision**: Register a single-tenant SPA and API. Use authorization code with PKCE for the React UI. Expose delegated scopes and matching user/application roles for loan, document, and data operations; delegated production access requires both the route scope and assigned role. Prefer workload federation for service callers and otherwise use one CA-issued certificate per workload/environment.

**Rationale**: SSO and OAuth use the user’s existing Entra control plane while role assignment prevents tenant-wide consent from granting every user every operation. Public browser clients cannot safely hold a secret or private certificate.

**Alternatives rejected**: Browser client secrets/certificates and service flows in the SPA are insecure. Scope-only authorization cannot express per-user/group assignment after admin consent.

## Public API and custom domains

**Decision**: Expose `api.loans.<domain>` through CloudFront and WAF. Use `origin-api.loans.<domain>` for the Regional API Gateway custom origin, require a CloudFront-injected origin-verification value, and disable the default execute-api endpoint. Host the SPA at `loans.<domain>`.

**Rationale**: A custom domain gives stable production URLs and exact Entra redirect/audience configuration. The separate protected origin prevents bypassing WAF/CloudFront while keeping API Gateway Regional in `us-west-2`.

**Alternatives rejected**: The default execute-api URL would work technically but weakens origin isolation and creates unstable/implementation-specific client configuration.

## Stable identity and archive model

**Decision**: Keep caller `loanId`, AWS `loanInstanceId`, logical `documentId`, physical `uploadId`, processing execution ID, and S3 version separate. Archive sequences are allocated in DynamoDB transactions. Loan archive freezes an immutable instance by reference; document archive freezes its current upload.

**Rationale**: Stable business/logical identifiers support UI references while immutable physical identities preserve auditability. Instance ownership means a loan and all documents can be archived in O(1) without object copying.

**Alternatives rejected**: Reusing `loanId` as physical identity loses incarnation history. Treating an upload or IDP job as `documentId` breaks replacement. Copying/renaming every object creates partial-failure, cost, and transaction-size risks.

## DynamoDB and idempotency

**Decision**: Use a DynamoDB single-table tenant/loan partition for heads, instances, documents, uploads, archives, and outbox events; use separate actor/route/key partitions for idempotency. Bind each idempotency key to a canonical request hash and stored response.

**Rationale**: Conditional transactions can atomically advance counters, move the current pointer, and write archive/outbox state. Canonical hashing distinguishes retry from key misuse.

**Alternatives rejected**: Eventually consistent independent tables complicate atomic lifecycle invariants. Client-generated archive suffixes cannot safely handle races or retries.

## Direct upload and malware boundary

**Decision**: Return a short-lived, condition-constrained presigned S3 POST to an opaque quarantine key. Require GuardDuty `NO_THREATS_FOUND` and deterministic PDF/integrity validation for the exact S3 version before IDP.

**Rationale**: Large PDF bytes bypass Lambda/API Gateway, while version/checksum pinning prevents scan/result confusion. Every non-clean/unknown state fails closed.

**Alternatives rejected**: Uploading through Lambda adds cost/limits. Submitting on upload completion before scan/validation crosses the untrusted-content boundary.

## Two-pass OCR and extraction

**Decision**: Run `cd-screen-v1` across all unknown package pages with Textract DetectDocumentText (empty feature list), page-level multimodal classification, all pages, LLM-determined sections, and one context page. Materialize the deterministically selected CD and run the supplied `cd-full-v1` Forms+Tables/Opus configuration only there.

**Rationale**: Candidate boundaries are unknown before submission, so every page needs inexpensive text OCR/classification. Forms+Tables and high-token extraction across all 54 sample pages caused most avoidable OCR/model cost; limiting full extraction to a typical 5–6 page winner preserves the reviewed accuracy baseline.

**Alternatives rejected**: Full Forms+Tables on every package page repeats expensive layout processing. Predeclared page ranges are unavailable. Replacing the proven extraction configuration is outside scope.

## Deterministic selection and model safety

**Decision**: Treat model output as untrusted, schema-bound data. Rank borrower CD candidates from explicit evidence and put ties/conflicts/missing evidence in `HOLD`. Record exact input, output, configuration, model/profile, and effective region provenance.

**Rationale**: A mortgage workflow needs reproducibility and explainable non-selection. Models have no tools or direct side effects.

**Alternatives rejected**: Selecting the first/highest-confidence candidate without deterministic evidence can silently choose the wrong borrower/version.

## Event reliability and observability

**Decision**: Use idempotent EventBridge/SQS consumers, bounded retries, DLQs, durable outbox events, and scheduled reconciliation. Log identifiers/status/correlation only. Alarm on denial, errors, queue age/DLQ, IDP/malware failures, database throttling, certificate expiry, and spend.

**Rationale**: Storage/security events are at-least-once and can be delayed; a watchdog is needed to detect missed or stranded work. Sensitive content is unnecessary for operational diagnosis.

**Alternatives rejected**: Event-only “fire and forget” processing can strand uploads. Payload logging creates unacceptable PII/token leakage.

## Spec Kit and Claude Code integration

**Decision**: Pin GitHub Spec Kit v0.12.15 at immutable commit `7b91c1eda46e1107a53831cd3f14f608b4b7bad0`, use its PowerShell scripts and native Claude skills, preserve project-authored `CLAUDE.md`, and commit `.specify/feature.json` for branch-independent active feature discovery.

**Rationale**: Current Claude integration is skills-based (`/speckit-*`) and does not generate `CLAUDE.md`. A reviewed lock plus sync script keeps generated skills/templates/scripts aligned and reproducible on Windows.

**Alternatives rejected**: Tracking upstream `main`, hand-copying templates, or adding legacy `.claude/commands` makes upgrades non-reproducible or duplicates the current integration.
