# Copilot code review instructions

Review every pull request against the [project constitution](../.specify/memory/constitution.md), [security policy](../docs/security.md), [architecture](../docs/architecture.md), active feature specification/plan/tasks, and canonical [OpenAPI contract](../contracts/openapi/loan-api.yaml).

Prioritize correctness, authorization, privacy, immutable identity/archive semantics, idempotency, exact S3 version/checksum handling, malware boundaries, deterministic IDP selection, failure reconciliation, least-privilege infrastructure, and tests. Treat mortgage documents, OCR text, extracted data, tokens, signed URLs, tenant/account identifiers, and private keys as prohibited review/logging content.

Do not suggest weakening validation, branch protection, exact-head Copilot review, encryption, version pinning, provenance, or fail-closed behavior. Do not conflate `loanId`, `loanInstanceId`, `documentId`, `uploadId`, `processingExecutionId`, or S3 `VersionId`. A loan archive freezes an immutable instance by reference; it does not copy every object.

For each finding, identify the concrete failure mode and an actionable correction. Avoid style-only comments unless they materially improve safety, maintainability, accessibility, or contract clarity. Verify that specifications, tasks, tests, scripts, and deployment documentation remain truthful about what is implemented versus what still requires live AWS/Entra acceptance.
