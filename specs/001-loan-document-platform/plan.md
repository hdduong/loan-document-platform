# Implementation Plan: Loan Document Platform

> **Historical baseline — do not execute.** This file records the superseded AWS-hosted product API. Use the current [Azure API control-plane specification](../002-azure-api-control-plane/spec.md) and its companion files for implementation.

- **Feature**: `001-loan-document-platform`
- **Date**: 2026-07-14
- **Specification**: [spec.md](spec.md)
- **Status**: Brownfield baseline implemented in source; UI, live deployment, and operational acceptance remain

## Summary

Deliver a production-oriented AWS loan document platform with a stable business `loanId`, AWS-generated immutable loan/document/upload identities, O(1) archives, direct quarantined S3 upload, Entra-protected APIs, and a two-pass AWS IDP path. Preserve the reviewed full extraction configuration while reducing package-wide OCR cost by using text-only OCR/classification across all unknown pages and Forms+Tables only on the selected CD.

## Technical Context

- **Languages/versions**: Python 3.13; PowerShell 7; TypeScript/React on Node.js 22 for the pending SPA
- **Infrastructure**: AWS SAM/CloudFormation, Regional API Gateway HTTP API, Lambda, DynamoDB, S3, KMS, SQS/EventBridge, GuardDuty Malware Protection, CloudFront, WAF, Route 53/ACM, AWS Budgets
- **Identity**: Microsoft Entra single-tenant OAuth/OIDC; SPA authorization code with PKCE; delegated scopes plus matching roles; certificate/federated app-only clients
- **Document processing**: AWS GenAI IDP accelerator v0.5.16; Textract; Bedrock US inference profiles; complete versioned JSON configurations
- **Storage**: DynamoDB single table for registry/idempotency/outbox; versioned SSE-KMS S3 buckets for quarantine, selected PDFs, and results
- **Testing**: pytest, Ruff, OpenAPI validator, cfn-lint, repository invariant validator, PowerShell parser; Vitest/Testing Library/MSW/Playwright/axe after the SPA is present
- **Target platform**: AWS `us-west-2` application stacks, with required CloudFront ACM certificates in `us-east-1`; GitHub-hosted Actions using OIDC
- **Performance/cost constraints**: Bound document/page/token sizes and workflow concurrency; all-page pass uses text-only OCR; $100 budget alerts supplement hard concurrency/size controls
- **Privacy constraints**: Mortgage documents and derived PII never enter Git or telemetry; exact object versions/checksums and short download grants are mandatory

## Constitution Check

| Gate | Status | Design evidence |
|---|---|---|
| Specification and contract first | PASS | This packet links to the canonical root OpenAPI; endpoint/schema duplication is prohibited. |
| Stable identity and lifecycle semantics | PASS | Business, instance, logical document, upload, execution, and storage identities remain separate; archives are monotonic/idempotent. |
| Privacy and zero-trust boundaries | PASS | Entra route permissions, CloudFront origin protection, direct quarantine upload, malware validation, KMS, and telemetry exclusion are explicit. |
| Deterministic processing and provenance | PASS | Every run pins versions/checksums/config/model data; ambiguity enters `HOLD`; no model side effects are allowed. |
| Testable, reviewable changes | PASS | Stories are independently testable, tasks identify files plus remaining live acceptance gates, and exact-head Copilot review is mandatory after every push. |
| Scripted, observable, cost-aware operations | PASS | AWS/Entra/GitHub provisioning is scripted; alarms, DLQs/reconciliation, concurrency, and budgets are part of the design. |

The post-design check also passes: [research decisions](research.md), [data relationships](data-model.md), [contract authority](contracts/README.md), and [tasks](tasks.md) retain every constitutional boundary. Production completion remains blocked on the explicitly unchecked deployment/acceptance tasks, not on a waived gate.

## Project Structure

```text
apps/web/                         # React SPA target; instruction/runtime-config assets exist
config/idp/                       # Versioned screening and full extraction snapshots
contracts/
  openapi/loan-api.yaml           # Canonical HTTP contract
  runtime-config.schema.json      # Canonical public SPA config contract
docs/                             # Architecture, security, runbook, UI, delivery
infra/
  api/                            # API/data/upload infrastructure
  bootstrap/                      # GitHub OIDC deployment bootstrap
  edge/                           # CloudFront/WAF/custom-domain edge
services/
  loan_api/                       # Entra-protected lifecycle/read/download API
  upload_processor/               # Scan/PDF validation and IDP submission
  idp_postprocessor/              # Deterministic selection and result materialization
scripts/                          # Repeatable bootstrap, provision, deploy, validation
specs/001-loan-document-platform/ # This feature packet
tests/                            # Python unit/invariant tests
```

## Architecture and Implementation Strategy

### 1. Identity and transactional lifecycle

Use a tenant/`loanId` DynamoDB partition. A `HEAD` item points to one current AWS-generated `loanInstanceId` and holds monotonic archive counters. Immutable instance, logical document, upload, archive, outbox, and artifact-reference items share the partition. Conditional transactions make create/archive operations atomic and make retries return a stored result. Idempotency intents use a separate actor/route/key-derived partition and canonical request hash.

Loan archive changes only the current pointer/counter and writes an immutable reference/manifest/outbox item. Because every document is owned by an immutable instance, no per-document copy is needed. Document archive freezes its current `uploadId`; replacement generates another upload while retaining `documentId`.

### 2. API, edge, and authorization

The canonical OpenAPI drives routes and generated UI types. CloudFront/WAF exposes `api.loans.<domain>` and adds a deployment-generated origin-verification header to the Regional API origin. The default API Gateway endpoint is disabled. API Gateway validates the tenant-specific JWT signature/issuer/audience/lifetime; Lambda enforces exact tenant, token type, immutable actor, allowlisted client, denylist, and route permission.

The SPA is a public Entra client using PKCE and `sessionStorage`. A delegated call needs both its scope and matching assigned role. Confidential callers use federation when possible or a workload-specific CA-issued certificate whose private key never reaches this API or repository.

### 3. Safe upload and two-pass processing

The API creates metadata and returns a constrained presigned POST. Bytes land in an opaque, versioned, SSE-KMS quarantine key. Completion records the exact version/integrity metadata. GuardDuty events are reconciled by opaque object-key lookup; only `NO_THREATS_FOUND` plus deterministic PDF validation can submit that version to IDP.

`cd-screen-v1` uses Textract DetectDocumentText with no Forms/Tables features over every page, page-level multimodal classification, `maxPagesForClassification: ALL`, `sectionSplitting: llm_determined`, and one context page. Deterministic selection materializes a winning CD PDF or places ambiguous cases on hold. `cd-full-v1` then preserves the supplied Forms+Tables/Opus extraction on only that selected artifact.

### 4. Immutable reads, downloads, and provenance

All reads resolve current/archive ownership through DynamoDB references and exact S3 versions. Results store checksums and run/config/model provenance. Download endpoints authorize afresh and mint one-to-five-minute grants; no durable signed URL is persisted or logged.

### 5. Delivery and operations

GitHub is public source hosting at `hdduong/aws-idp-custom-platform`, but mortgage content and deployment identity values are prohibited. Pull-request validation has no AWS permission. A native branch ruleset requests Copilot review for drafts and every push; the required metadata-only gate accepts only a review whose commit matches the current head, while resolved-conversation protection holds actionable findings. Production deployment uses a manually dispatched, environment-gated GitHub workflow and short-lived OIDC credentials restricted to the exact repository/environment. AWS and Entra bootstraps remain scripted and separate application runtime from deployment roles.

Asynchronous integrations use bounded retry, DLQs, idempotent consumers, and scheduled reconciliation. Alarms cover edge/API denial and availability, functions, queues, IDP, malware results, DynamoDB, certificates, and budget. Production acceptance requires synthetic smoke, restore, alarm, and certificate-rotation evidence.

## Delivery Phases

1. **Repository baseline**: contracts, services, tests, IDP configurations, CloudFormation, security/operations docs, and delivery scripts — implemented.
2. **Spec Kit/Claude workflow**: pinned v0.12.15 PowerShell scaffold, constitution, feature packet, Claude skills/instructions, and CI integrity checks — implemented by this change.
3. **React SPA**: build the OpenAPI-generated client, Entra PKCE, loan/document/archive workflows, download UX, accessibility, and automated UI tests — pending.
4. **Cloud provisioning**: supply real ignored environment inputs; provision Entra, AWS bootstrap/data/API/IDP/edge stacks, DNS/certificates, runtime config, and GitHub environment variables — pending.
5. **Production acceptance**: run synthetic end-to-end/negative tests, log sampling, alarms, restore and certificate rotation drills, and record cost/latency baseline — pending.

## External Deployment Inputs

The following are intentionally not guessed or committed: AWS account and IAM Identity Center profile, Route 53 hosted zone/root domain, final UI/API/origin hostnames, Entra tenant/subscription, administrator/group assignments, production redirect/logout URLs, and CA-issued confidential-client public certificates. They are parameters to the existing scripts and release runbook.

## Complexity Tracking

No constitutional violation is accepted. The single-table design and separate edge/origin hostnames add operational complexity, but they directly enforce atomic archive semantics, immutable ownership, least privilege, and origin isolation. No second API contract, object-copy archive workflow, browser secret, CodeCommit mirror, or long-lived AWS key is introduced.
