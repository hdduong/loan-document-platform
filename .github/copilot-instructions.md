# Copilot code review instructions

Review every pull request against the [project constitution](../.specify/memory/constitution.md), [security policy](../docs/security.md), [architecture](../docs/architecture.md), active feature specification/plan/tasks, and canonical [OpenAPI contract](../contracts/openapi/loan-api.yaml).

Prioritize correctness, authorization, privacy, immutable identity/archive semantics, idempotency, exact S3 version/checksum handling, malware boundaries, deterministic IDP selection, failure reconciliation, least-privilege infrastructure, and tests. Treat mortgage documents, OCR text, extracted data, tokens, signed URLs, tenant/account identifiers, and private keys as prohibited review/logging content.

The Azure API is the sole public domain API. Flag any deployable AWS Loan API/API Gateway, browser-callable AppSync, Cognito backend user, optional Jobs REST dependency, caller-token forwarding to AWS, static/fallback cloud credential, duplicate mutable registry, or Entra-to-AWS trust that does not pin issuer, dedicated audience, and managed-identity subject. Verify that the Azure workload role is limited to the exact DynamoDB, S3, KMS, and processor resources used by implemented routes.

Flag any production file below the constitution's per-file 80% coverage floor, missing tests for changed behavior, lowered thresholds, narrowed source inclusion, unjustified exclusions, production UI test bypass, or missing Playwright coverage for an affected browser journey. Playwright fixtures and artifacts must remain synthetic and must not persist reusable auth state or contact unexpected Microsoft/AWS endpoints during pull-request tests.

Do not suggest weakening validation, branch protection, exact-head Copilot review, encryption, version pinning, provenance, or fail-closed behavior. Do not conflate `loanId`, `loanInstanceId`, `documentId`, `uploadId`, `processingExecutionId`, an IDP object/workflow identifier, or S3 `VersionId`. A loan archive freezes an immutable instance by reference; it does not copy every object.

For each finding, identify the concrete failure mode and an actionable correction. Avoid style-only comments unless they materially improve safety, maintainability, accessibility, or contract clarity. Verify that specifications, tasks, tests, scripts, and deployment documentation remain truthful about what is implemented versus what still requires live AWS/Entra acceptance.
