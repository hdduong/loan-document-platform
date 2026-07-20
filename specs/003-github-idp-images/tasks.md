# Tasks: GitHub-built AWS IDP Lambda Images

## Phase 1 - Contract and inventory

- [ ] T001 [US1] Confirm the 15-image inventory and build arguments against the pinned upstream `patterns/unified/buildspec.yml`; add `config/idp/images.json`.
- [ ] T002 [US1] Add `contracts/schemas/idp-image-release.schema.json` with exact-set, digest, ARM64, scan, SBOM, provenance, and source-lock constraints.
- [ ] T003 [US1] Add `vendor/patches/idp-v0.5.16-external-images.patch` and record its normalized checksum in `vendor/idp.lock.json`.
- [ ] T004 [US1] Add fixture tests for the inventory, schema, and transformed template under `tests/test_idp_images.py`.

## Phase 2 - Bootstrap and authorization

- [ ] T005 [US1] Add a retained KMS-encrypted immutable ECR repository, Lambda pull policy, and untagged-artifact lifecycle to `infra/bootstrap/template.yaml`.
- [ ] T006 [US1] Add a separate exact-environment GitHub OIDC publisher role with only repository-scoped ECR and manifest-artifact permissions to `infra/bootstrap/template.yaml`.
- [ ] T007 [US1] Export the publisher role and repository outputs and synchronize `AWS_IDP_IMAGE_BUILD_ROLE_ARN`, `AWS_IDP_IMAGE_REPOSITORY_URI`, and related non-secret variables in `scripts/provision-github.ps1`.
- [ ] T008 [US1] Add CloudFormation/IAM assertions for OIDC `aud`/`sub`, no CodeBuild/PassRole permissions, immutable repository settings, and scoped pull access.

## Phase 3 - Manifest tooling

- [ ] T009 [US1] Implement canonicalization and validation in `tooling/idp_images.py` using the shared schema and image lock.
- [ ] T010 [US1] Add `scripts/validate-idp-image-manifest.py` for CI and PowerShell deployment callers.
- [ ] T011 [US1] Add rejection tests for missing/extra/duplicate images, tag-only URIs, malformed digests, wrong account/region/repository/platform, stale lock, incomplete scans, and missing evidence.
- [ ] T012 [US3] Extend `scripts/check-python-coverage.py` and CI coverage targets to enforce 80% per `tooling/` file.

## Phase 4 - GitHub image workflow

- [ ] T013 [US1] Add `.github/workflows/build-idp-images.yml` with credential-free PR build/scan validation and protected main/environment publication.
- [ ] T014 [US1] Pin every action by commit SHA; use Buildx single-platform ARM64, immutable release tags, digest inspection, and exact matrix entries.
- [ ] T015 [US1] Add Trivy/ECR scan gates, SBOM generation, GitHub provenance/SBOM attestations, and immutable evidence artifacts.
- [ ] T016 [US1] Add all-or-nothing aggregation that writes the manifest to GitHub artifacts and the protected S3 prefix, with per-environment concurrency.
- [ ] T017 [US3] Add actionlint and fixture tests proving PR workflows cannot assume AWS or push to ECR.

## Phase 5 - IDP deployment adapter

- [ ] T018 [US2] Apply the reviewed overlay to a disposable pinned checkout in `scripts/deploy-idp.ps1`; preserve the pristine vendor checkout.
- [ ] T019 [US2] Remove the local Docker preflight and require/validate `-ImageManifestFile` for deployment and rollback.
- [ ] T020 [US2] Pass repository/digest parameters to every unified Lambda and remove CodeBuild/custom-resource dependencies from the transformed template.
- [ ] T021 [US2] Remove unused CodeBuild build and pass-role permissions from the IDP bootstrap boundary after transformed-template validation proves none remain.
- [ ] T022 [US2] Record manifest release ID/checksum and image digests in `.local/idp-<environment>.json` without secrets.

## Phase 6 - Verification and documentation

- [ ] T023 [US2] Add cfn-lint, transformed-template, digest-reference, and no-CodeBuild repository invariants.
- [ ] T024 [US2] Add synthetic dev integration coverage for publish, deploy, idempotent rerun, representative Lambda invocation, rollback, and unchanged CD configuration hashes.
- [ ] T025 [US3] Update README/runbook, architecture, threat model, cost notes, and deployment workflow documentation.
- [ ] T026 [US3] Run full validation, commit, push, request exact-head Copilot review, fix sound comments, and repeat until all review threads are resolved.
