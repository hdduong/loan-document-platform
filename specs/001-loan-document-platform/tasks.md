# Tasks: Loan Document Platform

- **Input**: [specification](spec.md), [plan](plan.md), [research](research.md), [data model](data-model.md), and [canonical contracts](contracts/README.md)
- **Status rule**: Checked items are verifiable in this repository. Live UI/cloud/operational work stays unchecked until acceptance evidence exists.

## Format

`- [ ] TNNN [P?] [US?] Action with exact path`

- `[P]` means the task can proceed in parallel without editing the same dependency.
- `[USn]` maps the task to a user story in [spec.md](spec.md).
- Setup/foundation/release tasks have no user-story label.

## Phase 1: Repository and Spec-Driven Foundation

**Purpose**: Establish reproducible source, contracts, generated integrations, and quality gates.

- [x] T001 Pin AWS IDP upstream version/commit in `vendor/idp.lock.json`.
- [x] T002 [P] Preserve complete reviewed screening/full configurations and digests in `config/idp/`.
- [x] T003 [P] Define the canonical HTTP API in `contracts/openapi/loan-api.yaml` and public SPA config in `contracts/runtime-config.schema.json`.
- [x] T004 Document architecture, security, operations, delivery, and UI behavior in `docs/`.
- [x] T005 Create repository validation and Python quality gates in `scripts/validate-repository.py`, `pyproject.toml`, and `.github/workflows/validate.yml`.
- [x] T006 Initialize pinned GitHub Spec Kit v0.12.15 Claude/PowerShell assets under `.specify/` and `.claude/skills/`.
- [x] T007 Write the governing constitution in `.specify/memory/constitution.md`.
- [x] T008 [P] Write the complete baseline feature packet in `specs/001-loan-document-platform/`.
- [x] T009 [P] Align root and UI Claude instructions in `CLAUDE.md`, `apps/web/CLAUDE.md`, and `.claude/README.md`.
- [x] T010 Pin/synchronize upstream Spec Kit provenance in `vendor/spec-kit.lock.json`, `scripts/sync-spec-kit.ps1`, and `THIRD_PARTY_NOTICES.md`.
- [x] T011 Add PowerShell syntax and Spec Kit integrity checks in `scripts/test-powershell-syntax.ps1` and `scripts/validate-repository.py`.

---

## Phase 2: Platform Foundations

**Purpose**: Implement shared identity, persistence, security, infrastructure, and async primitives required by every story.

- [x] T012 Define tenant/loan DynamoDB keys, conditional revisions, archive counters, idempotency records, and outbox entities in `services/loan_api/`.
- [x] T013 [P] Define private versioned S3/KMS/quarantine/result resources and lifecycle controls in `infra/api/template.yaml`.
- [x] T014 [P] Define queues, retries, DLQs, alarms, limits, DynamoDB PITR/deletion protection, and backup controls in `infra/api/template.yaml`.
- [x] T015 Implement Entra JWT claim extraction plus route scope/role/client/tenant enforcement in `services/loan_api/`.
- [x] T016 [P] Define the Regional API, Lambda permissions, route bindings, and disabled default endpoint in `infra/api/template.yaml`.
- [x] T017 [P] Define CloudFront/WAF/custom UI/API/origin resources in `infra/edge/template.yaml`.
- [x] T018 Implement request validation, problem responses, canonical idempotency hashing, and safe logging in `services/loan_api/`.
- [x] T019 Add focused shared authorization, idempotency, selection, and configuration tests in `tests/`.

**Checkpoint**: Source-level shared platform controls exist and validate; live deployment remains a later phase.

---

## Phase 3: User Story 1 — Manage Loan Lifecycle (Priority: P1)

**Goal**: Create, retrieve, archive, and recreate a stable business loan while preserving immutable incarnations.

**Independent Test**: Create/archive/recreate/archive synthetic `23051`; assert distinct instance IDs, `_001`/`_002`, current plus archive reads, and idempotent retry.

- [x] T020 [US1] Implement create-loan conditional transaction in `services/loan_api/app.py` and supporting modules.
- [x] T021 [US1] Implement current-plus-archives loan read in `services/loan_api/app.py` and supporting modules.
- [x] T022 [US1] Implement O(1) loan-instance archive transaction, counter, manifest reference, and outbox write in `services/loan_api/`.
- [x] T023 [US1] Implement immutable loan archive/document reads in `services/loan_api/`.
- [x] T024 [P] [US1] Cover create races, archive retries/conflicts, sequence formatting, and recreate behavior in `tests/`.

**Checkpoint**: US1 is independently implemented/testable at unit/contract level.

---

## Phase 4: User Story 2 — Upload and Process a Loan Package (Priority: P1)

**Goal**: Return AWS-owned identities/direct upload, validate an exact clean PDF, and run the two-pass CD workflow.

**Independent Test**: Create/upload/complete a synthetic document, simulate scan/validation, and assert exact-version screening/selection/full-extraction provenance.

- [x] T025 [US2] Implement document creation with generated `documentId`/`uploadId` and constrained presigned POST in `services/loan_api/`.
- [x] T026 [US2] Implement metadata-only upload completion with exact S3 version/size/checksum capture in `services/loan_api/`.
- [x] T027 [US2] Implement GuardDuty event reconciliation, deduplication, and fail-closed exact-version handling in `services/upload_processor/`.
- [x] T028 [US2] Implement deterministic PDF magic/parser/encryption/page/size/checksum validation in `services/upload_processor/`.
- [x] T029 [US2] Submit only clean valid versions to screening with `cd-screen-v1` in `services/upload_processor/`.
- [x] T030 [US2] Implement evidence-based CD candidate selection and tie/conflict `HOLD` behavior in `services/idp_postprocessor/`.
- [x] T031 [US2] Materialize the selected-page PDF and submit it with `cd-full-v1` in `services/idp_postprocessor/`.
- [x] T032 [US2] Validate/store final output and exact config/model/input/output provenance in `services/idp_postprocessor/`.
- [x] T033 [P] [US2] Cover configuration invariants, selector outcomes, and event safety in `tests/` and `scripts/validate-repository.py`.

**Checkpoint**: US2 is implemented/testable with synthetic unit fixtures; live GuardDuty/IDP acceptance is pending.

---

## Phase 5: User Story 3 — Read and Download (Priority: P1)

**Goal**: Read current/archived metadata and data points and mint fresh grants for exact authorized artifacts.

**Independent Test**: Read one successful current result and archived result; assert ownership/version resolution, permission checks, and expiring grants.

- [x] T034 [US3] Implement current and archived document/data-points reads in `services/loan_api/`.
- [x] T035 [US3] Implement exact-version source/selected/data-points download grant endpoints in `services/loan_api/`.
- [x] T036 [US3] Enforce fresh permission, immutable ownership, safe response, and one-to-five-minute expiry in `services/loan_api/`.
- [x] T037 [P] [US3] Cover active/archive path resolution, missing permission, wrong ownership, and grant selection in `tests/`.

**Checkpoint**: US3 is independently implemented/testable at unit/contract level.

---

## Phase 6: User Story 4 — Archive and Replace Document (Priority: P2)

**Goal**: Freeze each physical version while retaining the AWS-generated logical document identity.

**Independent Test**: Archive/upload/archive two versions and assert stable `documentId`, distinct upload IDs, `_001`/`_002`, and replay safety.

- [x] T038 [US4] Implement document archive transaction/counter/idempotent result in `services/loan_api/`.
- [x] T039 [US4] Implement replacement upload creation under the stable `documentId` in `services/loan_api/`.
- [x] T040 [US4] Implement current and archived document-version reads/downloads in `services/loan_api/`.
- [x] T041 [P] [US4] Cover sequence/replacement/archive ownership and retry/conflict cases in `tests/`.

**Checkpoint**: US4 is independently implemented/testable at unit/contract level.

---

## Phase 7: User Story 5 — Entra SSO and OAuth (Priority: P1)

**Goal**: Protect interactive and machine operations with single-tenant Entra permissions and no browser secret.

**Independent Test**: Run delegated/app-only positive and negative token matrices for every permission category.

- [x] T042 [US5] Script API/SPA/optional machine registration, scopes, roles, assignments, and certificate registration in `scripts/provision-entra.ps1`.
- [x] T043 [US5] Implement fail-closed API permission checks in `services/loan_api/`.
- [x] T044 [P] [US5] Document certificate/token lifecycle and emergency containment in `docs/security.md` and `docs/runbook.md`.
- [x] T045 [P] [US5] Cover token claim/permission combinations in `tests/`.

**Checkpoint**: Backend/provisioning behavior is source-complete; real tenant and SPA integration remain pending.

---

## Phase 8: React SPA (All User Stories)

**Purpose**: Deliver the production UI against the canonical contract.

- [ ] T046 Generate/scaffold the React/TypeScript/Vite application and lockfile in `apps/web/` using the stack required by `apps/web/CLAUDE.md`.
- [ ] T047 [P] [US5] Generate the typed API client from `contracts/openapi/loan-api.yaml` into `apps/web/src/api/` with no hand-maintained duplicate DTOs/routes.
- [ ] T048 [P] [US5] Implement validated runtime configuration and Entra PKCE/MSAL `sessionStorage` integration in `apps/web/src/`.
- [ ] T049 [US5] Implement scope-plus-role-aware route/action guards and safe error/session behavior in `apps/web/src/`.
- [ ] T050 [US1] Implement create/get/archive/recreate loan views with current and ordered archive navigation in `apps/web/src/`.
- [ ] T051 [US2] Implement create/direct-S3-upload/complete/status/hold/retry document UX in `apps/web/src/`.
- [ ] T052 [US3] Implement current/archive data-point display and fresh source/selected/data download actions in `apps/web/src/`.
- [ ] T053 [US4] Implement document archive/replacement/history UX without parsing aliases in `apps/web/src/`.
- [ ] T054 [P] Add Vitest, Testing Library, MSW, Playwright, and axe coverage for all UI acceptance/negative cases in `apps/web/`.
- [ ] T055 Pass `npm ci`, lint, unit tests, production build, browser tests, and WCAG 2.2 AA evidence in `apps/web/` and CI.

---

## Phase 9: Real AWS, Entra, Domain, and Operations Acceptance

**Purpose**: Convert a validated source baseline into a verified production deployment.

- [ ] T056 Supply reviewed non-secret values in an ignored file derived from `config/environments/prod.example.json`.
- [ ] T057 Provision/verify Entra registrations, permissions, assignments, Conditional Access/MFA, allowlist, and exact production redirects with `scripts/provision-entra.ps1`.
- [ ] T058 Provision/verify exact-repository/environment GitHub OIDC and AWS roles with `scripts/provision-github.ps1` and `infra/bootstrap/template.yaml`.
- [ ] T059 Deploy and review API/data/upload/IDP stacks in `us-west-2` with `scripts/deploy-platform.ps1` and `scripts/deploy-idp.ps1`.
- [ ] T060 Deploy ACM/Route 53/CloudFront/WAF/custom UI/API/origin resources with `scripts/deploy-edge.ps1` and verify origin isolation.
- [ ] T061 Deploy the validated SPA/runtime config with `scripts/deploy-web.ps1`.
- [ ] T062 Run synthetic positive/negative lifecycle, upload, malware/PDF, IDP selection, archive, authorization, and download acceptance cases from `specs/001-loan-document-platform/quickstart.md`.
- [ ] T063 [P] Deliver/test alarms, DLQ/reconciliation, watchdog, certificate expiry, and $100 budget notifications using `docs/runbook.md`.
- [ ] T064 [P] Sample production telemetry and prove all prohibited content is absent using `docs/security.md` review criteria.
- [ ] T065 Perform and record DynamoDB/S3 restore plus confidential-client certificate overlap/rotation/revocation/emergency-denylist exercises using `docs/runbook.md`.
- [ ] T066 Record accuracy/selection, latency, effective inference region, and per-package cost evidence for synthetic/regression inputs without committing sensitive payloads.
- [ ] T067 Obtain required mortgage-data, retention/legal-hold, model-region, and security approvals and complete `checklists/production-readiness.md`.

---

## Phase 10: Repository Rename and Mandatory Copilot Review

**Purpose**: Keep repository identity/OIDC inputs canonical and require AI review of every exact pull-request head.

- [x] T068 Rename the GitHub repository and local origin to `hdduong/aws-idp-custom-platform`; update `README.md`, `config/environments/prod.example.json`, and `docs/github-delivery.md`.
- [x] T069 Add the active automatic draft/every-push Copilot ruleset and reproduce it in `scripts/configure-github-protection.ps1`.
- [x] T070 Add the exact-head metadata-only gate and review guidance in `.github/workflows/copilot-review.yml` and `.github/copilot-instructions.md`.
- [x] T071 Amend `.specify/memory/constitution.md`, `CLAUDE.md`, contributor/spec guidance, and repository invariants for the review-wait-fix-re-review loop.

---

## Dependencies and Execution Order

- Phase 1 precedes feature changes; Phase 2 precedes every user story.
- US1 loan instance ownership precedes US2/US3/US4 integration.
- US2 processing and US3 downloads can be validated independently once shared ownership/security primitives exist.
- US4 depends on logical document/upload separation from US2.
- Source-level US5 authorization applies across all stories; real tenant/UI integration is completed in Phases 8–9.
- Phase 8 can proceed against OpenAPI/MSW while cloud inputs are prepared, but Phase 9 acceptance depends on the completed SPA and deployed backend.

## Parallel Opportunities

- Tasks marked `[P]` use distinct files or test surfaces after their phase prerequisites.
- UI generated client/runtime auth and test harness work can begin in parallel before feature pages converge.
- Entra setup, AWS bootstrap review, and certificate/DNS preparation can proceed in parallel using separate control planes, then converge before edge/runtime deployment.

## Completion Rule

Do not mark the feature complete merely because the repository gates pass. Completion requires T046–T067 plus every item in [production readiness](checklists/production-readiness.md) and a final `/speckit-converge` showing no unexplained required work.
