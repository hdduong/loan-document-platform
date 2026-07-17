# Production Readiness Checklist: Azure API Control Plane

**Purpose**: Separate repository-complete controls from environment evidence that
can exist only after deployment.

## Repository controls

- [x] Azure Container Apps is the sole public product API; runnable AWS API
  Gateway/Loan API/CloudFront edge sources are absent.
- [x] Headless IDP uses the supported private S3/event boundary; no AppSync,
  Cognito backend user, or optional Jobs REST dependency exists.
- [x] Entra user/app authorization, certificate-only app token enforcement,
  exact client allowlist, and route scope/role checks fail closed before AWS.
- [x] The module-global AWS domain seam is serialized per replica, the HTTP
  scale target is pinned to `1` as a scale-out signal rather than an admission
  cap, and bounded `apiMaxReplicas` still permits horizontal scale.
- [x] Production `/v1` routes enforce the configured custom API host, the
  provider FQDN is limited to `/health` and `/ready`, production SPA publication
  requires recorded API DNS cutover, and Uvicorn request-line access logs are
  disabled.
- [x] Azure UAMI federation pins tenant issuer, canonical dedicated audience,
  token subject, AWS role, and temporary credential lifetime.
- [x] `/ready` performs a live managed-identity token and AWS STS exchange, while
  `/health` remains a process-only liveness signal.
- [x] Upload/download grants are short-lived, exact-operation constrained, and
  never stored in idempotency records or logs.
- [x] The API streams and bounds request bodies; inline data-point JSON is
  bounded and larger output requires an exact-version download grant.
- [x] Azure foundation/final deployment, Entra/AWS federation, GitHub OIDC,
  private AWS runtime, IDP, SPA publication, and rollback-capable DNS cutover are
  scripted without reusable cloud credentials.
- [x] Azure and AWS budgets, baseline Azure 5xx/latency alerts, AWS processor-
  error/DLQ/DynamoDB-throttle alarms, immutable image digests, versioned/KMS
  object storage, PITR/backup, DLQs, and retention controls are represented in
  IaC.
- [x] Python services enforce at least 80% line coverage per file and in
  aggregate; the future React scaffold is required to enforce 80% per metric and
  Playwright integration coverage.
- [x] A pinned Trivy vulnerability/SBOM gate runs against the exact ACR digest;
  production deployment and DNS cutover fail if actionable HIGH/CRITICAL issues
  remain or evidence does not match the deployed digest.
- [ ] The React/TypeScript application exists under `apps/web`; its unit,
  accessibility, build, and Playwright suites pass.

## Validation evidence

- [x] Repository invariants, Python compilation/lint/tests/coverage, OpenAPI,
  PowerShell syntax, Azure Bicep compilation, and CloudFormation lint pass on the
  implementation branch.
- [ ] GitHub Actions validation passes on the pushed exact commit.
- [ ] Copilot reviews the exact current PR head; sound comments are fixed,
  feedback commits are re-reviewed, and actionable conversations are resolved.

## Live environment evidence

- [ ] Operator verifies the configured Azure tenant/subscription/resource group,
  AWS account/us-west-2 profile, Route 53 zone, and distinct UI/API hostnames.
- [ ] Product Entra registrations, delegated scopes, app roles, admin consent,
  SPA redirect URI, and optional certificate-authenticated service client are
  tested with allowed and denied identities.
- [ ] GitHub-to-Azure and GitHub-to-AWS environment OIDC subjects resolve only
  for `hdduong/aws-idp-custom-platform:environment:prod` and cannot administer
  tenant/subscription/account resources.
- [ ] A deployed Container App passes deep `/ready`; wrong federation audience,
  subject, tenant, expired token, and unrelated AWS resources are denied.
- [ ] Synthetic create-loan, upload initialization, direct PDF POST, completion,
  GuardDuty clean/threat paths, headless IDP screen/select/full, status,
  data-point/PDF download, document archive, and loan archive/recreate pass.
- [ ] Synthetic concurrency/load evidence meets the approved p95 target at the
  reviewed replica/SKU limits without credential or signed-grant leakage.
- [ ] Azure-managed certificates are healthy; API and UI DNS cutovers pass and
  recorded rollback procedures are exercised.
- [ ] AWS Backup restore and DynamoDB PITR restoration are exercised; versioned
  S3 artifact references remain valid.
- [ ] Azure and AWS budget notifications and the baseline API/processor/DLQ/
  DynamoDB alarms deliver to on-call; environment-specific auth, federation,
  readiness, STS, GuardDuty-outcome, IDP-workflow, and certificate-renewal
  alerts are configured and tested before production acceptance.
- [ ] Filled environment files, cloud outputs, tokens, credentials, PDFs, OCR,
  extracted values, filenames, and signed URLs are absent from Git and CI
  artifacts.
