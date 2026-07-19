---

description: "Implementation tasks for moving the product API to Azure while retaining the headless AWS IDP data plane"
---

# Tasks: Azure API Control Plane

**Input**: Design documents from `specs/002-azure-api-control-plane/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/README.md`, `quickstart.md`

**Tests**: Tests are mandatory. Write or update the named tests before the corresponding production behavior, then keep every service file and the aggregate service suite at or above 80% line coverage.

**Organization**: Tasks are grouped by independently testable user story. Live cloud acceptance is intentionally not represented as a completed repository task; the runbook records the environment-gated evidence still required after deployment inputs are supplied.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel because it owns different files and has no dependency on an incomplete task in the same phase.
- **[Story]**: Maps implementation to the prioritized story in `spec.md`.

## Phase 1: Setup and Governance

**Purpose**: Make the Azure control-plane feature authoritative before executable behavior changes.

- [x] T001 Amend the control-plane, workload-federation, test, and delivery invariants in `.specify/memory/constitution.md`, `CLAUDE.md`, and `.github/copilot-instructions.md`
- [x] T002 [P] Update feature discovery and historical-baseline wording in `.specify/feature.json`, `specs/README.md`, and `README.md`
- [x] T003 [P] Add Azure API runtime dependencies and container/build exclusions in `services/azure_api/requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `.gitignore`, and `.dockerignore`
- [x] T004 [P] Add Azure subscription/runtime/federation inputs and remove AWS API-origin inputs in `config/environments/prod.example.json`, while verifying `contracts/runtime-config.schema.json` exposes only the provider-neutral public API URL and Entra SPA values
- [x] T005 Extend repository invariants for the active `002` packet, headless/no-AppSync mode, no deployable AWS Loan API, Azure Bicep, and new service coverage in `scripts/validate-repository.py` and `tests/test_repository_validator.py`

**Checkpoint**: Repository guidance and validation describe one Azure public API and one private AWS processing data plane.

---

## Phase 2: Foundational Security and Runtime

**Purpose**: Build the validated configuration, caller identity, and refreshable cross-cloud credential foundation that blocks every public route.

**⚠️ CRITICAL**: No user-story endpoint may call AWS until this phase passes its negative tests.

- [x] T006 [P] Write fail-closed configuration tests for exact Entra issuers/audiences, allowed clients/origins, federation audience/role, AWS resources, TTL bounds, and headless mode in `tests/test_azure_api_settings.py`
- [x] T007 Implement immutable validated runtime configuration with no secret/static-key fields in `services/azure_api/settings.py`
- [x] T008 [P] Write cryptographic JWT tests for signature, algorithm, key rotation, issuer, audience, lifetime, tenant, token type, actor, client allow/deny lists, app-only `azpacr=2` certificate proof, scopes, and matching roles in `tests/test_azure_api_auth.py`
- [x] T009 Implement cached Entra discovery/JWKS verification and caller-principal construction in `services/azure_api/auth.py`
- [x] T010 [P] Write managed-identity-to-STS tests for exact token resource, unsigned role exchange, `aud`/`sub` assumptions, synchronized early refresh, sanitized failures, and proof that caller tokens never reach AWS in `tests/test_aws_federation.py`
- [x] T011 Implement Azure managed-identity token acquisition and botocore refreshable `AssumeRoleWithWebIdentity` sessions in `services/azure_api/aws_credentials.py`
- [x] T012 Add package metadata and a secretless non-root production container with health checks and pinned Python base in `services/azure_api/__init__.py` and `services/azure_api/Dockerfile`

**Checkpoint**: Unit tests prove invalid callers and invalid federation fail before a DynamoDB/S3/Lambda client can be used.

---

## Phase 3: User Story 1 - Use One Azure Product API (Priority: P1) 🎯 MVP

**Goal**: Serve the existing loan/document/archive contract from Azure with unchanged identity, idempotency, concurrency, and archive semantics.

**Independent Test**: Run synthetic contract/lifecycle tests through the ASGI application and verify all successful and denied operations without any API Gateway or Lambda deployment.

### Tests for User Story 1

- [x] T013 [P] [US1] Port lifecycle, idempotency, concurrent create, document replacement, `_001`/`_002` archive, and permission-before-AWS tests to `tests/test_azure_api_domain.py`
- [x] T014 [P] [US1] Add FastAPI contract tests for health, production custom-host enforcement, CORS, correlation IDs, bounded bodies, problem responses, every canonical route, zero trust in forwarded identity headers, and log/error redaction of tokens/grants/content in `tests/test_azure_api_http.py`

### Implementation for User Story 1

- [x] T015 [US1] Refactor the existing lifecycle service to expose host-callable request dispatch and refreshable AWS-session rebinding while retaining its normalized Lambda-compatible envelope and a non-deployed rollback adapter in `services/loan_api/app.py`
- [x] T016 [US1] Implement the FastAPI lifespan, bearer validation, route dispatch, CORS, correlation, response translation, liveness, and dependency readiness in `services/azure_api/main.py`
- [x] T017 [US1] Update public descriptions and platform-issued identifier wording without changing paths/DTOs in `contracts/openapi/loan-api.yaml` and `tests/test_loan_api.py`
- [x] T018 [US1] Remove the Loan API Lambda role/function, API Gateway, AWS API custom domain, origin certificate/DNS, and API alarms while preserving stateful logical resources in `infra/api/template.yaml`

**Checkpoint**: User Story 1 passes through the Azure ASGI app, and the AWS template has no deployable public Loan API.

---

## Phase 4: User Story 2 - Upload and Process Through Headless IDP (Priority: P1)

**Goal**: Preserve direct constrained uploads, exact-version completion, malware/PDF validation, and the S3/event headless IDP path.

**Independent Test**: Initialize and complete a synthetic upload through Azure, replay clean/threat/unknown/reordered scan fixtures, and verify only the exact clean version is copied to the pinned screen input.

### Tests for User Story 2

- [x] T019 [P] [US2] Add Azure-hosted upload initialization/completion tests for presigned conditions, KMS, exact `VersionId`, size/checksum/type mismatch, expiry, idempotent replay, and no PDF bytes through the API in `tests/test_aws_adapters.py`
- [x] T020 [P] [US2] Extend retained processor tests for deterministic `screen/{run}/{document}/{upload}.pdf` and `full/{run}/{document}/{upload}.pdf` mappings, headless routing metadata, and duplicate/reordered malware events in `tests/test_processors.py`, `tests/test_upload_processor_coverage.py`, and `tests/test_idp_postprocessor_coverage.py`; T024 separately enforces the absence of AppSync/Jobs runtime dependencies

### Implementation for User Story 2

- [x] T021 [US2] Cap upload/download grants by both configured policy and remaining refreshable STS lifetime and sanitize dependency errors in `services/loan_api/app.py` and `services/azure_api/aws_credentials.py`
- [x] T022 [US2] Preserve exact clean-version staging and make platform-to-IDP object mappings explicit in `services/upload_processor/app.py` and `services/idp_postprocessor/app.py`
- [x] T023 [US2] Add the Entra tenant OIDC provider and exact audience/managed-identity-subject runtime role with DynamoDB/S3/KMS/processor-only permissions in `infra/api/template.yaml`
- [x] T024 [US2] Enforce the pinned headless deployment mode and absence of AppSync/Jobs runtime configuration in `vendor/idp.lock.json`, `scripts/deploy-idp.ps1`, and `scripts/validate-repository.py`

**Checkpoint**: The same exact-version malware/IDP workflow operates behind Azure and no optional upstream UI/API interface is required.

---

## Phase 5: User Story 3 - Read Status and Exact Artifacts (Priority: P1)

**Goal**: Authorize status, JSON, and artifact grants in Azure and resolve only exact current/archive records and object versions.

**Independent Test**: Seed synthetic current, document-archive, and loan-archive records; verify allowed reads/grants and cross-tenant/missing-permission denials through Azure.

### Tests for User Story 3

- [x] T025 [P] [US3] Add current/archive status, bounded JSON, exact-version source/selected/data-point grant, stale pointer, oversize result, and cross-tenant denial tests in `tests/test_azure_api_domain.py` and `tests/test_aws_adapters.py`
- [x] T026 [P] [US3] Add HTTP contract tests for all current/document-archive/loan-archive data-point and download variants in `tests/test_azure_api_http.py`

### Implementation for User Story 3

- [x] T027 [US3] Enforce ownership-first registry resolution, bounded inline JSON, exact-version download grants, and sanitized not-ready/not-found behavior in `services/loan_api/app.py`
- [x] T028 [US3] Verify the Azure runtime role has only the exact versioned object and table/index read permissions required by the implemented routes in `infra/api/template.yaml` and `tests/test_repository_validator.py`

**Checkpoint**: All public reads pass through Azure authorization, while S3 remains a byte-transfer target only through ephemeral grants.

---

## Phase 6: User Story 4 - Reproduce and Operate the Cross-Cloud Boundary (Priority: P2)

**Goal**: Script Azure hosting, managed identity, exact AWS trust, custom HTTPS names, GitHub OIDC delivery, staged migration, monitoring, and rollback.

**Independent Test**: Validate Bicep/CloudFormation/PowerShell/workflows locally, then use the environment-gated quickstart to prove positive/negative federation and reversible DNS cutover in a synthetic environment.

### Tests for User Story 4

- [x] T029 [P] [US4] Extend PowerShell parser tests for every new/changed Azure, Entra federation, AWS runtime, web, and orchestration script in `scripts/test-powershell-syntax.ps1`
- [x] T030 [P] [US4] Add declarative checks for Container Apps identity/ingress/auth/probes/scaling/diagnostics, exact OIDC trust, no AWS API resources, and safe outputs in `tests/test_repository_validator.py`

### Implementation for User Story 4

- [x] T031 [P] [US4] Define the user-assigned runtime identity, ACR, Log Analytics, Container Apps environment/app, Easy Auth defense in depth, autoscaling, diagnostics/alerts, and Static Web App resource in `infra/azure/main.bicep`
- [x] T032 [US4] Extend Entra provisioning with a dedicated v1 AWS federation resource app/role and exact assignment to the created managed identity in `scripts/provision-entra.ps1` and `scripts/provision-entra-federation.ps1`
- [x] T033 [US4] Implement idempotent Azure foundation/image/revision/default-host/custom-domain managed-certificate deployment in `scripts/deploy-azure.ps1`
- [x] T034 [US4] Refactor the AWS runtime deployment to pass only Azure federation, data-plane, processor, IDP, backup, alert, and budget inputs in `scripts/deploy-platform.ps1`
- [x] T035 [US4] Reorder cross-cloud deployment as Entra → Azure identity/foundation → federation assignment → AWS runtime → headless IDP → AWS wiring → Azure revision/domain → web in `scripts/deploy-all.ps1`
- [x] T036 [P] [US4] Replace CloudFront/S3 web publication with Azure Static Web Apps runtime configuration and deployment, failing production publication until API custom-domain binding and DNS cutover are recorded, in `scripts/deploy-web.ps1` and `apps/web/README.md`
- [x] T037 [US4] Remove the obsolete AWS edge deployment source in `scripts/deploy-edge.ps1` and `infra/edge/template.yaml`
- [x] T038 [US4] Add AWS CloudFormation execution permissions for the Entra OIDC provider/role and remove obsolete UI/API role assumptions in `infra/bootstrap/template.yaml`
- [x] T039 [US4] Add GitHub-to-Azure workload-federation provisioning and safe repository/environment variables with no client secret in `scripts/provision-github-azure.ps1`, `scripts/provision-github.ps1`, and `scripts/sync-github-entra.ps1`
- [x] T040 [US4] Update production delivery to use separately scoped `azure/login` and AWS OIDC sessions, build/deploy the API image and Azure resources, and remove origin-secret/edge inputs in `.github/workflows/deploy-prod.yml`
- [x] T041 [US4] Add Bicep build, container build, ASGI tests, coverage, retained CloudFormation lint, and conditional synthetic Playwright gates in `.github/workflows/validate.yml`

**Checkpoint**: All deployment artifacts are reproducible and contain no static cross-cloud credential or deployable AWS public Loan API.

---

## Phase 7: Documentation, Migration Safety, and Quality Convergence

**Purpose**: Make every canonical document truthful, preserve rollback evidence, and pass the complete repository gates.

- [x] T042 [P] Replace the AWS-hosted public API diagram, trust boundary, identity issuer, public names, upload/status path, and headless interface description in `docs/architecture.md`
- [x] T043 [P] Document Entra caller validation, managed-identity-to-STS trust, temporary-credential/grant lifetimes, Azure custom certificate, and AWS data-plane controls in `docs/security.md`
- [x] T044 [P] Document staged deployment, positive/negative federation, DNS cutover/rollback, trust revocation, cert renewal, alarms, backup restore, and live evidence in `docs/runbook.md`
- [x] T045 [P] Update dual-cloud GitHub OIDC delivery and exact-head Copilot requirements in `docs/github-delivery.md`, `CONTRIBUTING.md`, and `.github/pull_request_template.md`
- [x] T046 [P] Update Azure Static Web Apps/API runtime handoff and platform-issued IDs in `docs/ui-handoff.md`, `apps/web/CLAUDE.md`, and `CLAUDE.md`
- [x] T047 Mark the historical `001` feature as superseded without rewriting its completed record, and link migration decisions from `specs/001-loan-document-platform/spec.md` and `specs/README.md`
- [ ] T048 Add and run a sanitized synthetic API load/smoke check for the two-second p95 upload-initialization/status targets in `scripts/test-azure-api-load.ps1`, then run repository validation, Ruff, compileall, all pytest suites, per-file/aggregate coverage, OpenAPI validation, PowerShell parsing, cfn-lint, Bicep build, and container build using `specs/002-azure-api-control-plane/quickstart.md`
- [x] T049 Update every task checkbox truthfully, record only environment-gated live acceptance as pending in `specs/002-azure-api-control-plane/tasks.md` and `specs/002-azure-api-control-plane/checklists/production-readiness.md`
- [x] T050 Push a reviewed branch, request the `copilot-pull-request-reviewer[bot]` reviewer for the exact head, wait for review/check completion, address every sound comment, re-run validation, and repeat after any new push according to `.specify/memory/constitution.md`

---

## Phase 8: Windows Azure CLI Invocation Remediation

**Purpose**: Preserve literal Graph query and JSON arguments when PowerShell provisioning runs with the Windows MSI Azure CLI.

- [x] T051 [P] [US4] Add cross-platform regression tests for literal Azure CLI arguments and Windows `az.cmd` launch resolution in `tests/test_powershell_deployment_helpers.py`
- [x] T052 [US4] Add the shared safe Azure CLI launcher in `scripts/common.psm1`, route Entra Graph and Azure role REST calls through it, and document the Windows MSI behavior in `specs/002-azure-api-control-plane/quickstart.md`
- [x] T053 [P] [US4] Add a PowerShell helper regression proving a new Entra application accepts an empty existing-permission collection in `tests/test_powershell_deployment_helpers.py`
- [x] T054 [US4] Permit empty existing delegated-scope and application-role collections in `scripts/provision-entra.ps1` without weakening idempotent identifier reuse

---

## Phase 9: Entra Permission-Value Collision Remediation

**Purpose**: Use collision-free external Entra claim values while preserving canonical permissions at the private domain boundary.

- [x] T055 [P] [US4] Add provisioning and JWT regressions for scope `P`, app role `P.Role`, canonical role normalization, and rejection of raw unsuffixed roles in `tests/test_powershell_deployment_helpers.py` and `tests/test_azure_api_auth.py`
- [x] T056 [US4] Namespace Entra app-role values, normalize them at the Azure JWT boundary, update the synthetic load validator, and document the exact scope/role mapping in the OpenAPI and UI/security handoffs

---

## Phase 10: AWS Deployment Compatibility

**Purpose**: Preserve fail-closed first-install stack discovery with current AWS CLI service-error formatting and allow both split execution roles to expand their reviewed SAM templates.

- [x] T057 [P] [US4] Add prefixed positive and strict negative CloudFormation not-found classifier regressions in `tests/test_powershell_deployment_helpers.py`
- [x] T058 [US4] Normalize only the exact AWS CLI service-error prefix before anchored missing-stack classification and sanitize shared AWS failure context in `scripts/common.psm1`
- [x] T059 [US4] Grant both split CloudFormation execution roles access only to the regional AWS-managed Serverless transform in `infra/bootstrap/template.yaml`, enforce the exact two-role invariant in `scripts/validate-repository.py`, and add mutation coverage in `tests/test_repository_validator.py`

---

## Phase 11: AWS CloudFormation Handler Contract

**Purpose**: Keep create-time AWS resource identities and permission-only handler dependencies aligned with least-privilege bootstrap IAM.

- [x] T060 [P] [US4] Add deterministic registry/source-name, exact Backup mount-action, and service-linked-role permission mutations in `tests/test_repository_validator.py`
- [x] T061 [US4] Set the authorized registry table and source bucket names in `infra/api/template.yaml`, then grant only the active Backup handler actions and exact Backup service-linked-role creation in `infra/bootstrap/template.yaml`
- [x] T062 [US4] Parse and enforce the complete cross-template CloudFormation handler contract in `scripts/validate-repository.py`
- [x] T063 [US4] Reject global DynamoDB/S3 resource grants and standalone role-policy attachments, and preserve the stateful replacement gate for pre-existing generated resources
- [x] T064 [US4] Scope the stale cfn-lint Backup mount catalog exception to W3037 on the platform execution role and enforce that exact exception in repository validation

---

## Phase 12: AWS Deployment Artifact Boundary

**Purpose**: Let CloudFormation create packaged Lambda functions without granting document-runtime or cross-environment access to the CI artifact store.

- [x] T065 [P] [US4] Add artifact-prefix, artifact-key, and key-purpose mutation cases in `tests/test_repository_validator.py`
- [x] T066 [US4] Grant exact environment artifact reads/decrypt in `infra/bootstrap/template.yaml` and partition document/artifact KMS authorization with `KeyPurpose` in `infra/api/template.yaml` and `infra/bootstrap/template.yaml`
- [x] T067 [US4] Extend the structured cross-template handler contract in `scripts/validate-repository.py` to reject broader or relocated artifact access
- [x] T068 [US4] Validate the exact SAM artifact bucket, environment prefix, and KMS key producer coordinates in `scripts/deploy-platform.ps1`

---

## Phase 13: Lambda Event-Filter Deployment Contract

**Purpose**: Reject invalid or broadened DynamoDB stream filters before Lambda create-handler execution.

- [x] T069 [P] [US4] Add malformed-delimiter, duplicate-key, non-object, and semantic-drift filter regressions in `tests/test_repository_validator.py`
- [x] T070 [US4] Correct the upload-completion filter JSON and structurally enforce every Lambda filter plus the exact reviewed upload mapping in `infra/api/template.yaml` and `scripts/validate-repository.py`
- [x] T071 [US4] Record the filter grammar, exact event contract, and measurable deployment gate in the active Spec Kit artifacts and quickstart

---

## Phase 14: Cross-Platform IDP Digest Contract

**Purpose**: Keep reviewed IDP configuration identity stable across Git checkout line endings without weakening content verification.

- [x] T072 [P] [US4] Add LF, CRLF, CR, invalid-UTF-8, and deployment/generator adoption regressions in `tests/test_powershell_deployment_helpers.py`
- [x] T073 [US4] Export one strict UTF-8 LF-normalized SHA-256 helper from `scripts/common.psm1` and use it in `scripts/new-screen-config.ps1` and `scripts/deploy-idp.ps1`
- [x] T074 [US4] Record the cross-platform digest algorithm and fail-closed encoding gate in the active Spec Kit artifacts and quickstart

---

## Phase 15: Pinned IDP Python Runtime

**Purpose**: Keep IDP 0.5.16 on the Python minor supported by its reviewed NumPy pin without moving platform code off Python 3.13.

- [x] T075 [P] [US4] Add exact-minor resolver, path-restoration, stale-cache, workflow-order, and validator mutation regressions in `tests/test_powershell_deployment_helpers.py` and `tests/test_repository_validator.py`
- [x] T076 [US4] Pin `cliPythonVersion` in `vendor/idp.lock.json`, provision it in `scripts/bootstrap.ps1`, isolate and verify it in `scripts/deploy-idp.ps1`, and retain Python 3.13 as the platform/validation default in GitHub Actions
- [x] T077 [US4] Record the split Python runtime and supported recovery procedure in the active Spec Kit artifacts, README, and deployment runbook

---

## Phase 16: Native Windows IDP Child-Tool Bridge

**Purpose**: Let the pinned Python publisher launch Windows SAM/npm safely without a command shell or upstream patch.

- [x] T078 [P] [US4] Add argument-boundary, exit-code, self-target, activated-venv, layout-validation, scoped-environment, cache-contract, and validator-mutation regressions in `tests/test_idp_windows_cli_bridge.py`, `tests/test_powershell_deployment_helpers.py`, and `tests/test_repository_validator.py`
- [x] T079 [US4] Add the reviewed Python 3.12 native console-entry package and fail-closed Windows SAM/Node/npm resolver, bind its normalized digest to the IDP cache identity, and smoke both child tools before marking installation complete
- [x] T080 [US4] Record the Windows launcher boundary, recovery behavior, and no-shell/no-vendor-patch guarantees in the active Spec Kit artifacts, README, quickstart, and deployment runbook

---

## Phase 17: Pinned IDP Publisher Build Tools

**Purpose**: Make every direct Python publisher prerequisite reproducible inside the managed IDP environment.

- [x] T081 [P] [US4] Audit every upstream publisher child command and add lock/cache/order mutation coverage in `tests/test_powershell_deployment_helpers.py` and `tests/test_repository_validator.py`
- [x] T082 [US4] Pin cfn-lint, Ruff, and uv in `vendor/idp.lock.json`, install them into the Python 3.12 IDP environment, include them in cache invalidation, force-repair missing launchers without dependency drift, and require metadata/executable smoke checks before marking the cache complete
- [x] T083 [US4] Verify the pinned Ruff checks the exact IDP 0.5.16 source and record the publisher prerequisite and recovery contract in the active Spec Kit artifacts, README, quickstart, and runbook

---

## Phase 18: Repository-Owned Environment Configuration

**Purpose**: Replace the successful workstation-only environment setup block with a reviewed, idempotent, fail-closed operator script while keeping all populated values local.

- [x] T084 [P] [US4] Add helper, atomic-write, redaction, rerun, identity-mismatch, and repository-mutation coverage in `tests/test_environment_configuration.py`, `tests/test_azure_domain_deployment.py`, and `tests/test_repository_validator.py`
- [x] T085 [US4] Implement `scripts/configure-environment.ps1` with safe Azure/AWS invocation, public-zone selection, canonical host/contact/CA validation, ignored-target enforcement, mismatch protection, and validated atomic replacement; propagate the configured CA path to all three CLI trust variables in `scripts/bootstrap.ps1`
- [x] T086 [US4] Amend the scripted-operations governance rule and record the repository-owned setup command, local-only values, rerun behavior, and recovery boundary in `CLAUDE.md`, the active Spec Kit artifacts, README, and runbook

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup/Governance)**: Starts immediately and makes the active architecture unambiguous.
- **Phase 2 (Security/Runtime Foundation)**: Depends on Phase 1 configuration decisions and blocks every route.
- **US1 (Phase 3)**: Depends on Phase 2; establishes the Azure API MVP and removes deployable AWS public API resources.
- **US2 (Phase 4)**: Depends on the US1 dispatch/session boundary; retained processor test work can begin in parallel after Phase 2.
- **US3 (Phase 5)**: Depends on US1 authorization/dispatch; read/grant tests may be authored in parallel with US2.
- **US4 (Phase 6)**: Declarative Azure/AWS work may begin after Phase 2 settings are stable; deployment orchestration waits for US1–US3 runtime inputs.
- **Phase 7 (Convergence)**: Documentation tasks can run after the relevant design stabilizes; full validation and review wait for all implementation phases.
- **Phase 18 (Repository-Owned Environment Configuration)**: Depends on the shared safe CLI launchers and canonical environment validation; it can be reviewed independently of live cloud mutation and must complete before the configurator is a supported runbook step.

### User Story Dependencies

- **US1**: Independent MVP after foundational authentication and federation.
- **US2**: Uses US1 transport/domain clients but is independently proven with synthetic upload/scan/IDP fixtures.
- **US3**: Uses US1 authorization/registry access but is independently proven with seeded exact-version records.
- **US4**: Deploys US1–US3 and can be statically validated without a live account; environment acceptance follows when ignored inputs are supplied.

### Parallel Opportunities

- T003–T004 can proceed in parallel; T005 follows their final names.
- T006/T008/T010 are independent test-first tracks; T007/T009/T011 implement them in pairs.
- T013 and T014 are parallel test tracks before T015/T016.
- T019 and T020 can proceed in parallel; T025 and T026 can proceed in parallel.
- T029/T030/T031 and T036 can proceed in parallel once settings/output names stabilize.
- T042–T046 are disjoint documentation files and can proceed in parallel.

## Parallel Example: Foundational Security

```text
Task T006: Write validated settings tests.
Task T008: Write Entra JWT verification tests.
Task T010: Write managed-identity-to-STS refresh tests.
```

## Parallel Example: User Stories 2 and 3

```text
Task T019: Write upload/completion/presign tests.
Task T020: Write retained malware/IDP mapping tests.
Task T025: Write exact current/archive read and grant tests.
Task T026: Write HTTP contract tests for read/download variants.
```

## Implementation Strategy

### MVP First

1. Complete governance and foundational security.
2. Deliver US1 through the Azure ASGI test harness.
3. Prove that `infra/api/template.yaml` can no longer deploy an AWS public Loan API.
4. Stop and run the full lifecycle/authorization/coverage gates before adding processing or cloud delivery.

### Incremental Delivery

1. Add US2 without changing the public contract or headless processor boundary.
2. Add US3 exact-version reads/grants independently.
3. Add US4 IaC and deployment scripts, first validated statically and then through the environment-gated quickstart.
4. Converge documentation, validation, coverage, and the mandatory Copilot review loop.

## Notes

- A checked task means repository work and deterministic tests are complete; it never implies Azure/AWS/Entra resources were deployed.
- Live tenant/account identifiers and deployment output remain ignored local inputs.
- No task may solve federation failure by adding a static AWS key, Entra client secret, Cognito user, direct browser AWS role, AppSync endpoint, or optional Jobs REST dependency.
- Do not replace the retained DynamoDB registry or stateful S3/KMS logical resources during this migration.
