# Feature Specification: GitHub-built AWS IDP Lambda Images

**Feature Branch**: `codex/github-idp-images`  
**Created**: 2026-07-19  
**Status**: Draft  
**Input**: User request: "I want AWS IDP Lambda container images built in this GitHub repository instead of CodeBuild."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Publish one complete, trusted IDP image release (Priority: P1)

As a platform release operator, I can start a reviewed build from the pinned AWS IDP source and receive one complete release containing every Lambda image required by the headless deployment, so a partial or mutable image set can never be promoted.

**Why this priority**: The IDP stack cannot be deployed safely until every required image exists and has a stable identity.

**Independent Test**: Start a release for the pinned source revision and verify that exactly the expected 15 images are built, checked, recorded by immutable digest, and represented by one complete release manifest. Force one image build to fail and verify that no complete manifest is published.

**Acceptance Scenarios**:

1. **Given** a reviewed revision on an allowed deployment branch, **When** an authorized operator starts an image release, **Then** all expected images are built from the pinned upstream source and a complete immutable release record is published.
2. **Given** any missing, failed, unscanned, wrong-architecture, or policy-violating image, **When** the release process evaluates the result, **Then** it fails closed and does not publish a deployable release manifest.
3. **Given** an untrusted pull-request context, **When** repository validation runs, **Then** it cannot obtain image-publishing credentials or alter the production image repository.

---

### User Story 2 - Deploy and roll back an exact image release (Priority: P2)

As a deployment operator, I can deploy or roll back AWS IDP by selecting a reviewed release manifest, so every Lambda runs the exact image that was built and checked without rebuilding images during deployment.

**Why this priority**: Separating image production from stack deployment removes mutable tags, CodeBuild timing, and workstation Docker from the production path.

**Independent Test**: Deploy a complete release, inspect every image-based Lambda reference, and verify each points to the digest in the selected manifest. Then deploy the preceding retained manifest and verify the stack rolls back without rebuilding images.

**Acceptance Scenarios**:

1. **Given** a complete manifest matching the locked IDP version, AWS account, region, repository, and expected image set, **When** deployment starts, **Then** the stack uses the exact digest recorded for each Lambda image.
2. **Given** a missing, duplicated, altered, stale, or context-mismatched manifest entry, **When** deployment validation runs, **Then** deployment stops before changing the stack.
3. **Given** a previously successful retained release, **When** an operator selects its manifest, **Then** the IDP stack can be restored without source compilation or image rebuilding.

---

### User Story 3 - Audit and operate the image supply chain (Priority: P3)

As a security or operations reviewer, I can trace every deployed IDP image to its source revision, reviewed workflow run, checks, and release manifest, while enough prior complete releases remain available for recovery.

**Why this priority**: Production support needs evidence, reproducibility, and safe retention after the core publish-and-deploy flow works.

**Independent Test**: Select a deployed Lambda and follow its digest to a retained release record containing the source and workflow identities, image checks, and attestations. Confirm that the current and at least two previous successful complete releases remain recoverable.

**Acceptance Scenarios**:

1. **Given** a deployed image digest, **When** a reviewer examines release evidence, **Then** the reviewer can identify the pinned upstream revision, repository revision, workflow run, architecture, scan result, and attestation for that image.
2. **Given** repeated execution for the same reviewed release identity, **When** the process is retried, **Then** it either safely resumes or reports the already completed immutable result without overwriting it.
3. **Given** retention processing, **When** older artifacts are retired, **Then** the active release and at least two prior successfully deployed complete releases remain available.

### Edge Cases

- An upstream release adds, removes, or renames an image while the committed expected-image contract still describes 15 images.
- An image push succeeds but the workflow stops before all images or the release manifest are published.
- Registry scanning is delayed, unavailable, or returns an unapproved finding at the enforced severity threshold.
- A build reports a platform other than Linux ARM64 or publishes multiple architectures under one digest.
- Concurrent runs target the same source revision or environment.
- A manifest has valid syntax but its upstream revision, repository revision, account, region, repository, or environment does not match deployment context.
- A manifest contains a duplicate image name, an unexpected image name, a tag-only reference, or a malformed digest.
- The reviewed upstream overlay no longer applies cleanly or its checksum differs from the lock file.
- A prior release manifest exists but one or more referenced images have expired.
- The registry or artifact store is temporarily consistent after publication.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The platform MUST provide one repository-owned, reviewed workflow that builds AWS IDP Lambda images in GitHub rather than AWS CodeBuild.
- **FR-002**: For pinned AWS IDP version 0.5.16, a release MUST contain exactly the committed set of 15 headless unified-pattern images; missing or unexpected entries MUST fail the release.
- **FR-003**: Every image MUST be built from the exact upstream commit recorded in `vendor/idp.lock.json`, together with the exact reviewed platform revision that defines the build and deployment overlay.
- **FR-004**: Image publishing MUST use short-lived workload identity constrained to the configured GitHub repository and protected deployment environment; long-lived AWS access keys MUST NOT be used.
- **FR-005**: Pull-request validation and other untrusted contexts MUST NOT receive permissions that can push, replace, delete, or promote production images.
- **FR-006**: The production image repository MUST be private, encrypted, deny tag replacement, scan images, and deny insecure or unauthorized publication paths.
- **FR-007**: Every Lambda image MUST target exactly Linux ARM64 and MUST satisfy the Lambda container-image contract before it can enter a complete release.
- **FR-008**: Every successfully built image MUST be identified by an immutable content digest; tags MAY aid discovery but MUST NOT be the deployment identity.
- **FR-009**: A canonical, schema-validated release manifest MUST record the release identity, environment, AWS account and region, image repository, upstream version and commit, platform revision, workflow run, build time, and the exact name, digest, platform, scan status, and attestation evidence for all expected images.
- **FR-010**: A release manifest MUST be published only after all 15 images pass required checks. A partial build MUST leave no manifest that deployment accepts as complete.
- **FR-011**: The complete manifest MUST be retained in the protected deployment artifact store and also exposed as immutable workflow evidence suitable for review.
- **FR-012**: The image build MUST produce verifiable build provenance and a software-bill-of-materials attestation for each published image.
- **FR-013**: Security scanning MUST complete for every image, and images with findings at or above the enforced threshold MUST fail unless a reviewed, time-bounded exception is explicitly represented in release evidence.
- **FR-014**: Before deployment, the platform MUST validate the manifest schema, completeness, immutable digest syntax, upstream lock, platform revision, environment, account, region, repository, architecture, scan state, and evidence references.
- **FR-015**: Every image-based Lambda in the deployed headless IDP stack MUST reference its selected manifest entry by repository and digest.
- **FR-016**: The deployed headless IDP template MUST NOT create or invoke CodeBuild projects, build-trigger custom resources, or stack-owned build repositories.
- **FR-017**: Production IDP deployment MUST NOT require a Docker daemon or rebuild an image on the operator workstation or deployment runner.
- **FR-018**: The repository MUST keep the pinned upstream checkout pristine and MUST apply a reviewed, checksum-locked overlay to a disposable build/deployment copy; a drifted or inapplicable overlay MUST fail closed.
- **FR-019**: Release publication and deployment MUST be safe to retry and MUST prevent concurrent runs from overwriting an immutable release or racing an environment deployment.
- **FR-020**: Retention MUST preserve the active complete release and at least two prior successfully deployed complete releases, including their manifests and referenced images.
- **FR-021**: Operators MUST be able to roll back by selecting a retained complete manifest without rebuilding images.
- **FR-022**: Build, publication, deployment, rejection, and rollback outcomes MUST retain enough audit metadata to identify the actor or workload, source revisions, workflow run, release identity, and result.
- **FR-023**: Repository validation MUST detect image-contract drift, mutable deployment references, forbidden CodeBuild resources in the transformed headless path, and missing or invalid release-manifest tests.
- **FR-024**: This feature MUST preserve the existing IDP configuration files and document-extraction behavior; changing Closing Disclosure extraction is outside this feature.

### Key Entities

- **Expected Image Contract**: The reviewed mapping of the 15 logical AWS IDP Lambda image names to their source build definitions.
- **Image Artifact**: One private Linux ARM64 Lambda-compatible container image, addressed by a content digest and accompanied by checks and attestations.
- **Image Release**: An all-or-nothing collection of the exact expected image set built from one upstream revision and one platform revision for one deployment environment.
- **Release Manifest**: The canonical immutable record binding an image release to its context, exact digests, checks, and evidence.
- **Deployment Selection**: The reviewed choice of one complete release manifest for deploy or rollback.
- **Upstream Overlay**: The checksum-locked repository change that removes upstream build-time resources and replaces mutable image inputs with externally published immutable digests.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every accepted release contains exactly 15 of 15 expected images, with zero missing, duplicate, or unexpected image entries.
- **SC-002**: 100% of deployed image-based Lambda functions use immutable content digests from one selected complete manifest; zero use tag-only references.
- **SC-003**: The production headless deployment path contains zero CodeBuild projects or build-trigger resources and completes without access to a local Docker daemon.
- **SC-004**: Automated negative tests reject 100% of the defined invalid cases: partial sets, unknown or duplicate names, malformed digests, wrong architecture, incomplete or failed scans, mismatched source/context, altered overlay, and mutable image references.
- **SC-005**: 100% of image publication and production deployment sessions use short-lived workload credentials scoped to the reviewed repository environment; zero long-lived AWS credentials are stored in GitHub.
- **SC-006**: A reviewer can trace any deployed digest to its upstream revision, platform revision, workflow run, scan result, provenance, SBOM, and complete manifest using retained evidence.
- **SC-007**: An operator can redeploy either of the two preceding successful complete releases without rebuilding images and can begin the rollback from one documented command or workflow selection.
- **SC-008**: A failed or cancelled image build publishes zero deployment-acceptable complete manifests.

## Assumptions

- The active deployment remains the pinned, headless AWS IDP v0.5.16 unified-pattern path in `us-west-2`.
- Lambda functions in this path run on ARM64, and the expected image contract changes only through reviewed repository updates.
- GitHub protected environments and AWS workload federation remain the authorization boundary for releases and deployments.
- The bootstrap stack may own the shared image repository and protected release-manifest storage independently of the replaceable IDP stack.
- The current and two previous complete releases provide the minimum rollback window; longer retention may be configured where cost and policy allow.

## Out of Scope

- Closing Disclosure extraction prompts, schemas, model selection, and accuracy tuning.
- The upstream web UI build and the non-headless multi-document-discovery build path.
- Building application images unrelated to the pinned AWS IDP Lambda functions.
- Automatic production deployment immediately after an image build; deployment remains a separately reviewed action.
