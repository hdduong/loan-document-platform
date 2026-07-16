# Quickstart: Loan Document Platform

> **Historical baseline — do not execute.** This file records the superseded AWS-hosted product API. Use the current [Azure API control-plane specification](../002-azure-api-control-plane/spec.md) and its companion files for implementation.

This is a synthetic-data developer and release path. It does not deploy until real account/domain inputs are supplied through ignored configuration.

## Prerequisites

- Git and GitHub CLI
- Python 3.13 and the repository virtual environment
- PowerShell 7
- Node.js 22 when implementing the React SPA
- `uv`/`uvx` for the pinned Spec Kit refresh
- AWS CLI, SAM CLI, and cfn-lint for AWS work
- Azure CLI plus Microsoft Graph permissions for Entra provisioning
- AWS IAM Identity Center access; do not create an access key

## 1. Read the governing artifacts

Read, in order:

1. [Project constitution](../../.specify/memory/constitution.md)
2. [Feature specification](spec.md)
3. [Implementation plan](plan.md)
4. [Tasks](tasks.md)
5. [Canonical OpenAPI](../../contracts/openapi/loan-api.yaml)
6. [Architecture](../../docs/architecture.md) and [security controls](../../docs/security.md)

`.specify/feature.json` already selects this feature independently of the Git branch name.

## 2. Verify Spec Kit and Claude Code

Restart Claude Code once after first checkout so it discovers `.claude/skills`. Verify `/skills` and `/memory`, then use the hyphenated commands:

```text
/speckit-clarify
/speckit-plan
/speckit-tasks
/speckit-analyze
/speckit-implement
/speckit-converge
```

Verify the feature packet is discoverable:

```powershell
./.specify/scripts/powershell/check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks
```

Refresh generated Spec Kit assets only when intentionally upgrading/reconciling the pinned version:

```powershell
./scripts/sync-spec-kit.ps1
```

## 3. Run repository gates

From the repository root:

```powershell
./.venv/Scripts/python.exe scripts/validate-repository.py
./scripts/test-powershell-syntax.ps1
./.venv/Scripts/ruff.exe check services scripts tests
./.venv/Scripts/pytest.exe -q
./.venv/Scripts/python.exe -m compileall -q services scripts
./.venv/Scripts/openapi-spec-validator.exe contracts/openapi/loan-api.yaml
./.venv/Scripts/cfn-lint.exe "infra/**/*.yaml"
```

When `apps/web/package.json` exists, also run its lockfile-based lint, unit test, build, Playwright, and accessibility suites.

## 4. Prepare ignored deployment input

Copy `config/environments/prod.example.json` to an ignored environment JSON file and supply:

- AWS account/IAM Identity Center profile and `us-west-2` target;
- Route 53 hosted zone/root domain and final UI/API/origin hostnames;
- Entra tenant/subscription, administrator and group assignments;
- exact production redirect/logout URLs;
- approved model/profile and deployment limits;
- CA-issued public certificate information for any confidential client.

Never commit the filled file, tenant/account identifiers, emails, credentials, private keys, generated state, or deployment output.

## 5. Provision in dependency order

```powershell
./scripts/bootstrap.ps1 -InstallMissing -EnvironmentFile <ignored-environment-file>
./scripts/provision-github.ps1 -EnvironmentFile <ignored-environment-file>
./scripts/provision-entra.ps1 -EnvironmentFile <ignored-environment-file>
./scripts/deploy-all.ps1 -EnvironmentFile <ignored-environment-file>
```

The scripts bootstrap GitHub OIDC before gated deployments, create Entra API/SPA registrations, deploy API/data/IDP, then edge/custom-domain resources and the public runtime configuration. Follow [the operations runbook](../../docs/runbook.md) for exact review and release gates.

## 6. Exercise the API lifecycle

Use a synthetic `loanId` and a freshly acquired Entra access token:

1. `POST /v1/loans` — create the current loan; save `loanInstanceId`.
2. `POST /v1/loans/{loanId}/documents` — create the logical document; AWS returns `documentId`, `uploadId`, and presigned POST.
3. Submit only the PDF form fields/bytes to S3 — do not send the Entra token.
4. `POST /v1/loans/{loanId}/documents/{documentId}/uploads/{uploadId}/complete` — confirm upload metadata; this sends no PDF bytes.
5. `GET /v1/loans/{loanId}/documents/{documentId}` — poll safe status until `SUCCEEDED`, `HOLD`, `REJECTED`, or `FAILED`.
6. Read `/data-points` and request `/download` or `/data-points/download` for fresh short-lived grants.
7. `POST /v1/loans/{loanId}/documents/{documentId}/archive` — freeze the current upload as `_001`; create a replacement upload to retain `documentId` with a new `uploadId`.
8. `POST /v1/loans/{loanId}/archive` — freeze the current instance and every owned document as `loanId_001`; recreate the base loan to obtain a new `loanInstanceId`.

Use a new UUID idempotency key for each new mutation intent and reuse that same key only for a safe retry of the same request.

## 7. Production acceptance

Before declaring production ready, complete every unchecked item in [production readiness](checklists/production-readiness.md): real Entra/custom-domain smoke tests, negative authorization/scan tests, alarm delivery, log-content sampling, backup restore, certificate rotation, WAF/origin bypass testing, and cost/latency evidence. No repository-only test substitutes for these live controls.
