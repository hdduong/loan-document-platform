# Quickstart: Azure API Control Plane

This quickstart validates and deploys the cross-cloud boundary with synthetic data. The Azure API is the only public product API. The retained AWS data plane provides DynamoDB, versioned S3, malware/PDF validation, event processors, and the pinned headless IDP workflow.

The commands below must be run from the repository root. Never paste a bearer token, managed-identity token, temporary AWS credential, signed URL, tenant/account identifier, real PDF, OCR text, or extracted value into a terminal transcript, issue, pull request, or test artifact.

## Prerequisites

- PowerShell 7, Git, Python 3.13, and the repository virtual environment
- Azure CLI with Bicep support and access to the intended subscription and Entra tenant
- AWS CLI v2, AWS SAM CLI, and an IAM Identity Center profile for the intended account
- Docker for building the Azure API container and the pinned IDP source
- Node.js 22 when the React application is present
- Administrator access only for the one-time Azure, Entra, AWS, DNS, and GitHub federation bootstrap

Use separate, least-privilege identities for GitHub deployment and the Azure API runtime. Do not create an AWS access key, Entra client secret, or reusable cross-cloud certificate for the Azure API.

## 1. Read the authoritative artifacts

Read these before changing or deploying the feature:

1. [Project constitution](../../.specify/memory/constitution.md)
2. [Feature specification](spec.md)
3. [Implementation plan](plan.md)
4. [Feature contracts](contracts/README.md)
5. [Canonical OpenAPI](../../contracts/openapi/loan-api.yaml)
6. [Architecture](../../docs/architecture.md) and [security controls](../../docs/security.md)

The root OpenAPI remains the browser and service-client wire contract. This feature must not create a second public API definition for Azure or AWS.

## 2. Run the local quality gates

Install the pinned development dependencies, then run every repository gate:

```powershell
./.venv/Scripts/python.exe -m pip install --disable-pip-version-check -r requirements-dev.txt
./.venv/Scripts/python.exe scripts/validate-repository.py
./scripts/test-powershell-syntax.ps1
./.venv/Scripts/python.exe -m compileall -q services scripts
./.venv/Scripts/ruff.exe check services scripts tests
./.venv/Scripts/pytest.exe -q --cov=services --cov-branch --cov-report=term-missing --cov-report=json:coverage.json
./.venv/Scripts/python.exe scripts/check-python-coverage.py coverage.json
./.venv/Scripts/openapi-spec-validator.exe contracts/openapi/loan-api.yaml
./.venv/Scripts/cfn-lint.exe "infra/**/*.yaml"
az bicep build --file ./infra/azure/main.bicep --stdout | Out-Null
```

The coverage command must report at least 80% lines for every hand-authored Python service file and for the combined service suite. A passing aggregate may not hide an individual file below 80%.

On an HTTPS-inspected workstation, pass the administrator-approved root CA to
the container build as an ephemeral BuildKit secret, for example
`--secret id=enterprise_ca,src=<ignored-local-ca.pem>`. The Dockerfile uses it
only for dependency download; it is not copied into the image or build context.
Never use `--trusted-host`, disable TLS verification, or commit the certificate.
Set `DOCKER_BUILDKIT=1` for any manual Docker build. Pull-request validation
sets it explicitly, while production uses `infra/azure/acr-build-api.yml`: the
repository-owned ACR task enables BuildKit and has distinct build and push steps
before deployment resolves and scans the immutable digest. This follows the
[ACR Tasks YAML reference](https://learn.microsoft.com/azure/container-registry/container-registry-tasks-reference-yaml).

When `apps/web/package.json` exists, also run the locked React checks against the production build:

```powershell
Push-Location ./apps/web
try {
    npm ci
    npx playwright install chromium
    npm run lint
    npm run typecheck
    npm run test:coverage
    npm run build
    npm run test:e2e:ci
} finally {
    Pop-Location
}
```

Pull-request Playwright tests use synthetic identity, API, and storage behavior and must reject unexpected Microsoft or AWS network calls.

## 3. Prove authorization and federation fail closed

Run the focused synthetic suites as well as the full suite:

```powershell
./.venv/Scripts/pytest.exe -q tests/test_azure_api_auth.py tests/test_azure_api_domain.py tests/test_azure_api_http.py
./.venv/Scripts/pytest.exe -q tests/test_aws_federation.py tests/test_aws_adapters.py
```

The tests must cover this matrix without acquiring a live token:

| Boundary | Negative case | Required observation |
|---|---|---|
| Azure API | Missing/malformed token, invalid signature, wrong issuer/audience/tenant, or invalid lifetime | `401`; registry, S3, Lambda, and STS doubles have zero calls |
| Azure API | Valid token from a disallowed client, wrong token type, app token without `azpacr=2`, or missing scope/role | `403`; no AWS operation is attempted |
| AWS STS | Wrong federation issuer, audience, or managed-identity subject | Role assumption is rejected; no AWS service client is returned |
| AWS STS | Expired token or expired temporary credentials | Synchronized refresh occurs once or the request fails with a sanitized dependency error |
| IDP adapter | AppSync or Jobs REST selected while deployment mode is headless | Configuration fails closed before network access |
| Artifact access | Wrong tenant, object version, checksum, or media type | No signed grant or object content is returned |

For a deployed non-production environment, verify the public boundary without a credential:

```powershell
$apiBaseUrl = "https://api.loans.example.com"

try {
    Invoke-WebRequest -Uri "$apiBaseUrl/v1/loans/synthetic" -Method Get -ErrorAction Stop | Out-Null
    throw "Protected request unexpectedly succeeded without an Entra token."
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw }
}

try {
    Invoke-WebRequest -Uri "$apiBaseUrl/v1/loans/synthetic" -Method Get -Headers @{ Authorization = "Bearer synthetic.invalid.token" } -ErrorAction Stop | Out-Null
    throw "Protected request unexpectedly accepted an invalid token."
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 401) { throw }
}
```

Replace only the hostname with the ignored environment value. Do not put a real token in the command history. The environment-gated acceptance suite must exercise valid wrong-audience and wrong-subject Entra tokens from dedicated synthetic test identities and emit only pass/fail reason codes; it must never print either token or the returned STS credentials.

## 4. Prepare ignored environment input

Create an environment file from the committed example and confirm Git ignores it:

```powershell
Copy-Item ./config/environments/prod.example.json ./config/environments/dev.json
git check-ignore ./config/environments/dev.json
```

Populate the ignored file locally with the reviewed Azure subscription/location/resource-group names, Entra tenant/application names, AWS account/region/profile, Route 53 zone, custom hostnames, alert destinations, budget, and pinned stack names. Do not add tokens, application secrets, private keys, AWS access keys, signed URLs, or document content.

Verify the operator sessions locally:

```powershell
az login --tenant <tenant-guid>
az account set --subscription <subscription-guid>
aws sso login --profile <identity-center-profile>
./scripts/bootstrap.ps1 -EnvironmentFile ./config/environments/dev.json
```

The bootstrap must reject a mismatched Azure tenant, Azure subscription, AWS account, or region before provisioning.

## 5. Deploy in the required order

The supported entry point is the idempotent orchestrator:

```powershell
./scripts/deploy-all.ps1 `
  -EnvironmentFile ./config/environments/dev.json `
  -SkipWeb `
  -BindApiCustomDomain
```

`-SkipWeb` is required while `apps/web/package.json` is absent. The
`-BindApiCustomDomain` phase creates the ownership record and binds the managed
certificate but does not change the API traffic record. After the default
health/readiness probes and host-pinned custom-domain product smoke pass, cut
traffic over separately:

```powershell
./scripts/cutover-api-domain.ps1 `
  -EnvironmentFile ./config/environments/dev.json
```

For a later all-in-one repeat after those gates have already been satisfied,
`deploy-all.ps1 -SkipWeb -BindApiCustomDomain -CutoverApiDomain` preserves the
same ordering. Do not use the combined cutover switch for the first production
release because it removes the operator acceptance pause.

It must enforce this order:

1. Provision the Entra API and SPA registrations, scopes, roles, assignments, and exact redirect URI.
2. Deploy the Azure foundation, including the user-assigned runtime identity,
   registry, Container Apps environment, monitoring, and Static Web App
   resource. Foundation creates no public Container App or default API endpoint.
3. Bind that exact managed-identity subject to the dedicated Entra-to-AWS federation audience.
4. Deploy the retained AWS data plane once, creating the registry, quarantine/artifact bucket, malware/event processors, postprocessor hook ARN, and the least-privilege runtime role whose trust pins issuer, audience, and subject.
5. Deploy the pinned IDP source with the headless template transformation and the postprocessor hook.
6. Deploy the AWS data plane a second time so processor policies and configuration reference the actual IDP input, working, output, KMS, and workflow resources.
7. Deploy the Azure API container revision and its default HTTPS endpoint with
   only non-secret configuration: Entra issuer/audience/client allowlist,
   managed-identity client ID, AWS role ARN/region, registry, bucket, key, and
   processor identifiers.
8. Validate `/health` and `/ready` on the Azure default endpoint, bind the custom
   HTTPS API hostname, exercise `/v1` through that staged custom host, and only
   then perform the separate DNS cutover.
9. Publish the SPA/runtime configuration after the Azure API hostname is healthy.

Use the individual scripts only to resume a failed phase. `scripts/provision-entra.ps1`, `scripts/deploy-azure.ps1`, `scripts/deploy-platform.ps1`, and `scripts/deploy-idp.ps1` are idempotent, but manually changing their order can create an untrusted workload or an IDP stack with incomplete hook/bucket permissions. A direct first-install platform pass must explicitly use `deploy-platform.ps1 -AllowMissingIdp`; every reuse, `-SkipIdp`, and post-IDP pass must omit that switch so missing outputs or lookup failures stop deployment and the deployed IDP wiring is verified.

## 6. Verify the headless IDP boundary

After deployment, verify the safe, non-secret outputs and CloudFormation resources:

- `vendor/idp.lock.json` still pins the reviewed version/commit and `deploymentMode` is `headless`.
- `scripts/deploy-idp.ps1` still applies `--headless`.
- The bootstrap stack exposes different platform and IDP CloudFormation
  execution-role ARNs plus different runtime permissions-boundary ARNs.
- Every platform/IDP runtime role carries the expected boundary, boundary
  removal is denied, and only reviewed managed policies/service principals are
  allowed for attachment and `iam:PassRole`.
- Both stacks return the committed policy that blocks replacement/deletion of
  S3, DynamoDB, and KMS resources during ordinary updates.
- No AppSync endpoint is configured in the Azure API.
- The optional private Jobs REST API is not assumed or required.
- A clean synthetic upload reaches the IDP input bucket only after exact-version malware and PDF validation.
- Screening and full-extraction submissions use the deterministic `screen/{processingExecutionId}/{documentId}/{uploadId}.pdf` and `full/{processingExecutionId}/{documentId}/{uploadId}.pdf` object-key forms recorded in the registry.
- Registry mappings keep platform `documentId`, `uploadId`, and `processingExecutionId` distinct from S3 `VersionId`, upstream object key, and workflow ARN.
- Status and result reads resolve through the retained registry and exact versioned S3 artifacts.

Do not test by copying an unscanned PDF directly into the IDP input bucket. Do not enable Cognito or create a service user to make the optional Jobs API callable.

## 7. Run pre-cutover smoke tests

Against the Azure default hostname in a synthetic non-production environment
(production permits only `/health` and `/ready` on that hostname):

1. Confirm `/health` succeeds and emits no environment identifiers or dependency details.
2. Confirm missing/invalid Entra tokens fail before the AWS adapter is called.
3. Use a synthetic authorized identity to create, read, archive, and recreate a loan; verify `_001` then `_002` and idempotent replay.
4. Initialize a synthetic PDF upload and verify the grant contains no bearer token or reusable AWS credential.
5. Complete the upload and prove the recorded bucket/key/version/size/checksum match the exact uploaded object.
6. Inject clean, threat, unknown, duplicate, and reordered scan events; only the exact clean current version may reach IDP.
7. Poll status through Azure, read bounded JSON, and request fresh version-pinned source/selected/data-point grants.
8. Revoke the AWS runtime-role trust or managed-identity assignment in non-production and verify the API fails closed with a sanitized dependency error; restore through the provisioning script.

Capture only status codes, opaque synthetic identifiers, durations, and reason codes. Do not persist response bodies containing signed grants or extracted values.

## 8. Cut over the custom API hostname

Use a staged DNS activation; do not delete or replace the retained AWS data
plane. The current repository never deploys an AWS product API.

1. At least one existing TTL before the change, reduce the API record TTL through reviewed infrastructure.
2. Pass `/health` and `/ready` against the default Container Apps hostname. In
   production, pass the authenticated `/v1` matrix against the staged custom
   hostname using reviewed host-pinned TLS resolution before DNS changes.
3. Validate the Azure custom-domain ownership record and managed certificate before sending product traffic.
4. Confirm the deployment record contains a passing pinned-Trivy result for the exact deployed image digest.
5. Run `scripts/cutover-api-domain.ps1`; it snapshots any existing A, AAAA,
   and CNAME records, points `apiHostName` to Azure, verifies HTTPS and deep
   readiness, and automatically restores the snapshot on failure.
6. Confirm the public hostname repeats the unauthenticated `401`, authorized lifecycle, upload, status, and download checks.
7. Monitor Azure 4xx/5xx/latency and federation failures together with AWS processor, DLQ, malware-gate, and IDP alarms for the approved observation window.
8. For a legacy installation created by an older repository revision, retire its
   public AWS API only through a separately reviewed historical-stack cleanup.
   The current templates cannot recreate that endpoint and preserve only
   DynamoDB, S3/KMS, GuardDuty, processors, IDP, backups, and alarms.

Verify DNS and HTTPS without displaying environment configuration:

```powershell
Resolve-DnsName api.loans.example.com
Invoke-WebRequest -Uri https://api.loans.example.com/health -Method Get | Select-Object StatusCode
```

## 9. Release evidence

A production release is incomplete until the pull request has passed repository validation and exact-head Copilot review, and the environment has recorded synthetic evidence for:

- positive and negative Entra authorization;
- positive and negative managed-identity-to-STS federation;
- no static cross-cloud credential;
- exact-version quarantine, scan, validation, IDP submission, and artifact retrieval;
- 80% per-file and aggregate service coverage plus applicable Playwright journeys;
- successful Bicep, CloudFormation, OpenAPI, Python, and PowerShell validation;
- custom-domain certificate health and reversible DNS cutover;
- alarm delivery, DLQ/reconciliation, backup restore, and trust revocation/recovery.

Evidence must contain no customer data, token, credential, signed URL, private key, or raw model content.
