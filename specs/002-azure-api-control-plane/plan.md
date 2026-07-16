# Implementation Plan: Azure API Control Plane

**Branch**: `codex/spec-kit-claude-code` | **Date**: 2026-07-16 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-azure-api-control-plane/spec.md`

## Summary

Move the canonical REST/domain service from AWS API Gateway/Lambda to an Entra-protected Azure Container App. Preserve the existing OpenAPI contract, lifecycle semantics, DynamoDB registry, versioned S3 quarantine/artifacts, malware gate, AWS processors, and pinned headless IDP workflow. The Azure service validates user tokens itself, obtains a dedicated managed-identity access token, exchanges it through AWS STS `AssumeRoleWithWebIdentity`, caches the short-lived credentials, and uses narrowly scoped AWS SDK adapters. The browser continues to upload/download through Azure-authorized presigned S3 grants and never calls AppSync, Jobs REST, or a custom AWS Loan API.

The migration deliberately keeps one registry in DynamoDB. Moving the API runtime does not move pipeline state: both retained AWS event processors already transact the registry, and introducing Cosmos DB would create a dual-write/callback problem outside this feature.

## Technical Context

**Language/Version**: Python 3.13 for the Azure API and retained AWS processors; PowerShell 7 for provisioning; Bicep for Azure and SAM/CloudFormation for AWS

**Primary Dependencies**: FastAPI, Uvicorn, PyJWT with `cryptography`, Azure Identity, boto3/botocore, Pydantic settings; existing PDF/IDP processor dependencies remain pinned

**Storage**: Existing DynamoDB single-table registry/idempotency/outbox and versioned SSE-KMS S3 quarantine/artifact/IDP buckets; Azure Log Analytics/Application Insights for telemetry only; no second product registry

**Testing**: pytest, FastAPI `TestClient`/HTTPX, botocore stubs/fakes, coverage.py per-file and aggregate gates, Ruff, OpenAPI validator, `az bicep build`, cfn-lint, PowerShell parser tests, and conditional Vitest/Playwright for browser changes

**Target Platform**: Linux container on Azure Container Apps with a user-assigned managed identity and Azure-managed custom-domain certificate; retained AWS workload in `us-west-2`; Azure region is an environment input with `westus2` as the non-binding example

**Project Type**: Cross-cloud web service plus event-driven document-processing data plane

**Performance Goals**: Upload initialization, status, and small JSON reads below two seconds p95 at the initial baseline of 100 concurrent API requests; PDF bytes never transit the API; container startup/readiness below the configured ingress timeout

**Constraints**: No static AWS key, Azure application secret, browser AWS credential, Cognito service user, public AppSync, Jobs REST dependency, or duplicate registry; exact issuer/audience/subject federation; exact S3 version/checksum processing; sanitized telemetry; 100 MiB PDF and 200-page defaults; 80% line coverage for every service file and aggregate

**Scale/Scope**: One Entra tenant and one AWS account per environment initially; independently scalable API replicas; one active loan incarnation per tenant/business key; retained asynchronous processors and bounded IDP concurrency; production, staging, and development have separate Azure identities/resources and AWS stacks

## Constitution Check

*GATE: PASS before research; PASS after design.*

| Principle | Design evidence | Result |
|---|---|---|
| Specification and Contract First | This `002` packet is active before code changes; the root OpenAPI remains authoritative and will be updated only for provider/issuer descriptions. | PASS |
| Stable Identity and Lifecycle Semantics | The Azure API issues the existing distinct platform IDs; IDP object/job/execution IDs and S3 versions are explicit mappings; archive transactions and idempotency remain unchanged. | PASS |
| Privacy and Zero-Trust Boundaries | Entra user-token validation stays at the public API; a separate managed workload token is exchanged for scoped temporary AWS credentials; document bytes bypass the API. | PASS |
| Deterministic Processing and Provenance | The existing clean-version gate, two-pass configs, deterministic selection, and exact artifact provenance remain in AWS. | PASS |
| Testable, Reviewable Changes | Runtime, auth, federation, contract, IaC, migration, and negative paths receive automated tests plus per-file coverage. | PASS |
| Scripted, Observable, Cost-Aware Operations | Azure and AWS provisioning use IaC/scripts and workload OIDC; alarms, logs, backup, restore, and budget controls remain explicit. | PASS |
| Exact-Head Copilot Review | Delivery remains pull-request based with exact-current-head Copilot review after every push. | PASS |
| Coverage and Browser Integration | Each new service file is included in the 80% gate; runtime/UI changes retain deterministic synthetic browser tests. | PASS |

The constitution will be amended from 1.2.0 to 1.3.0 to make the Azure control-plane and Entra-to-AWS federation boundary normative. This expands an operational principle without removing an existing safety gate.

## Architecture Decisions

### Runtime and public edge

Use Azure Container Apps rather than Functions because the existing domain service is a long-lived Python HTTP application with a sizeable dependency set, request-local AWS sessions, and explicit concurrency needs. Container Apps provides managed identity, autoscaling, revision rollout, built-in ingress authentication as defense in depth, Log Analytics integration, and managed custom-domain certificates without adapting the domain to a functions trigger model. Code-level JWT validation remains authoritative for exact claims and permissions.

Azure Static Web Apps is reserved for the React SPA. The browser calls only `https://api.loans.<domain>/v1`. The deployable state contains no AWS CloudFront API distribution, Regional API Gateway, Lambda Loan API, origin-verification secret, or `origin-api` hostname.

### Cross-cloud workload identity

Create a dedicated single-tenant Entra resource application for AWS federation; do not reuse the browser API audience. It issues a v1 app-only token to the Azure API user-assigned managed identity for its Application ID URI. AWS uses the documented tenant OIDC issuer `https://sts.windows.net/<tenant-id>/` and a role trust policy matching both:

```text
sts.windows.net/<tenant-id>/:aud = <dedicated federation Application ID URI>
sts.windows.net/<tenant-id>/:sub = <Azure API managed identity principal ID>
```

The API calls unsigned STS `AssumeRoleWithWebIdentity`, caches the returned credentials behind a concurrency-safe provider, refreshes before the five-minute safety window, and creates request-scoped DynamoDB/S3/Lambda clients. The original browser bearer token is never forwarded to STS or AWS.

### Runtime composition

Refactor the existing lifecycle behavior in `services/loan_api/app.py` behind a host-callable dispatch entry point and configurable AWS session, preserving its already tested state transitions during the hosting move. The dispatch retains a normalized Lambda-compatible envelope and AWS-aware DynamoDB/S3/Lambda operations; before each serialized call, Azure rebinds those clients to the current refreshable session. `services/azure_api/` supplies the FastAPI transport, CORS, correlation IDs, bounded bodies, health/readiness, cryptographic JWT verification, and refreshable managed-identity/STS session. Long-lived boto3 clients use botocore refreshable credentials with synchronized early refresh; they are never initialized from a one-time static credential dictionary. The legacy Lambda adapter may remain temporarily for rollback testing, but no AWS infrastructure deploys it.

The initial IDP adapter is intentionally an S3/event adapter:

1. Azure creates registry identities and a constrained quarantine upload grant.
2. Completion pins the exact S3 version and updates shared state.
3. Retained GuardDuty/upload-processor events validate and copy the exact clean version to the pinned headless IDP input bucket.
4. Retained postprocessing writes status/provenance/artifact references to DynamoDB/S3.
5. Azure reads those shared records and issues exact-version result/download grants.

The headless deployment removes AppSync, and the optional private Jobs REST API is not enabled. Neither is present in runtime configuration or IAM policy.

### Infrastructure split and migration

Keep the current AWS stack name and stateful logical IDs while changing `infra/api/template.yaml` into a private data/processing runtime plus the Azure workload OIDC provider/role. This avoids replacing existing buckets, table, KMS key, processors, or backup resources. A clean deployment never creates an AWS product API. Operators migrating a legacy deployed stack must stage Azure acceptance and legacy-resource cleanup as separately reviewed releases; the one-shot clean-deployment orchestrator does not claim application rollback to an endpoint that this repository cannot deploy.

Add `infra/azure/main.bicep` for the resource group deployment: user-assigned runtime identity, registry, Log Analytics, Container Apps environment/application, autoscaling, ingress auth, diagnostics/alerts, Static Web App placeholder, and safe outputs. Image build/push and custom-hostname managed-certificate binding are scripted because they require created-resource/DNS ordering. Deployment automation uses separate GitHub-to-Azure and GitHub-to-AWS OIDC identities.

## Project Structure

### Documentation (this feature)

```text
specs/002-azure-api-control-plane/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── README.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
services/
├── azure_api/
│   ├── __init__.py
│   ├── main.py                  # FastAPI transport and health endpoints
│   ├── auth.py                  # Entra JWT verification and route authorization
│   ├── settings.py              # validated non-secret runtime configuration
│   ├── aws_credentials.py       # managed identity -> STS cached credentials
│   ├── requirements.txt
│   └── Dockerfile
├── loan_api/
│   └── app.py                   # host-callable AWS-aware lifecycle dispatch + rollback adapter
├── upload_processor/            # retained private AWS malware/validation stage
└── idp_postprocessor/           # retained private AWS IDP result stage

infra/
├── azure/
│   └── main.bicep
├── api/
│   └── template.yaml            # retained AWS data/processing runtime; no public API
└── bootstrap/
    └── template.yaml            # AWS deployment identity permissions

scripts/
├── provision-entra.ps1          # API/SPA/federation apps and managed-identity role grant
├── deploy-azure.ps1             # Azure foundation, image, revision, domain/cert
├── deploy-platform.ps1          # private AWS runtime/federation stack
├── deploy-idp.ps1               # pinned headless upstream deployment
├── deploy-web.ps1               # Azure Static Web Apps deployment
└── deploy-all.ps1               # ordered cross-cloud orchestration

tests/
├── test_azure_api_auth.py
├── test_azure_api_domain.py
├── test_azure_api_http.py
├── test_aws_federation.py
├── test_aws_adapters.py
├── test_processors.py
└── test_repository_validator.py

contracts/openapi/loan-api.yaml   # canonical public contract
config/environments/prod.example.json
.github/workflows/validate.yml
.github/workflows/deploy-prod.yml
```

**Structure Decision**: Add a provider-explicit Azure API package while retaining the two AWS event processors. Keep the existing AWS template path and stack identity for stateful migration safety, but change its responsibility from public API to private runtime. The source tree contains one product HTTP service and one shared AWS data plane, not two public APIs.

## Phase 0: Research Outcome

The decisions and rejected alternatives are recorded in [research.md](research.md). All technical unknowns are resolved before implementation: Azure runtime, token validation, workload federation issuer/audience/subject, credential lifetime, registry ownership, headless IDP interface, deployment order, and cutover strategy.

## Phase 1: Design and Contracts

- [data-model.md](data-model.md) defines identifier ownership, DynamoDB continuity, state transitions, artifact pinning, and upstream mappings.
- [contracts/README.md](contracts/README.md) keeps the root OpenAPI as the only public interface and defines the private adapter boundary.
- [quickstart.md](quickstart.md) describes deterministic local validation, IaC checks, negative federation tests, staged deployment, smoke, DNS cutover, and rollback.

Post-design constitution re-check: **PASS**. The selected approach adds one necessary Azure runtime and one trust boundary while explicitly removing the duplicate AWS public API and avoiding a second data store. No complexity exception is required.

## Complexity Tracking

No constitution violation requires an exception. The cross-cloud boundary is mandated by the feature; retaining DynamoDB/S3 and the existing processors is the smallest migration that avoids dual writes and a new AWS-to-Azure callback service.
