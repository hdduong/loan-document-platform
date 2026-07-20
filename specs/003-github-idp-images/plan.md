# Implementation Plan: GitHub-built AWS IDP Lambda Images

## Summary

Move the 15 pinned headless IDP Lambda image builds to a protected GitHub
Actions workflow. Bootstrap a dedicated immutable ECR repository and least
privilege publisher role, publish a schema-validated digest manifest, transform
the upstream template to consume those digests, and make deployment consume the
manifest without CodeBuild or a local Docker daemon.

## Technical Context

- AWS IDP source: `vendor/idp.lock.json`, version 0.5.16, headless, `us-west-2`.
- Image platform: `linux/arm64` and Lambda `arm64`.
- Build context: pinned upstream repository root and `Dockerfile.optimized`.
- Build matrix: 15 logical image entries in `config/idp/images.json`.
- Image registry: bootstrap-owned KMS-encrypted immutable ECR repository.
- Artifact evidence: versioned bootstrap S3 bucket plus GitHub immutable workflow
  artifacts and attestations.
- Deployment: `scripts/deploy-idp.ps1` and the patched upstream CloudFormation
  templates, passed through the existing headless IDP CLI.
- Tests: Python unit/coverage, PowerShell syntax, actionlint, cfn-lint,
  repository invariants, and synthetic workflow/manifest fixtures.

## Constitution Check

- Specification and contract first: this feature has `spec.md`, schema, plan,
  tasks, quickstart, and tests before implementation.
- Privacy and zero trust: no document data or long-lived credentials enter the
  workflow; PRs cannot assume the publisher role.
- Reproducible supply chain: source, workflow actions, scanner, image set, and
  overlay are pinned; deployment uses digest-only complete manifests.
- Scripted operations: bootstrap, publication, validation, deployment, rollback,
  and runbook steps are repository-owned.
- Coverage/review gates: new tooling is under `tooling/` and must meet 80% per
  file; the required exact-head Copilot cycle remains mandatory.

## Project Structure

```text
config/idp/images.json                       # exact 15-image build contract
contracts/schemas/idp-image-release.schema.json # canonical manifest schema
tooling/idp_images.py                        # canonicalize and validate manifests
scripts/validate-idp-image-manifest.py       # CLI wrapper for deployment/CI
vendor/patches/idp-v0.5.16-external-images.patch
vendor/idp.lock.json                         # upstream source lock + overlay hash
infra/bootstrap/template.yaml                # ECR, publisher role, outputs
scripts/provision-github.ps1                 # sync publisher role variable
scripts/deploy-idp.ps1                       # manifest-driven deployment
.github/workflows/build-idp-images.yml       # PR validation and protected publish
tests/test_idp_images.py                     # tooling and rejection fixtures
```

## Phases

1. Inventory and lock the 15 upstream image contexts and the current CodeBuild
   coupling. Add the image contract, manifest schema, and checksum-locked
   overlay.
2. Add bootstrap ECR and publisher OIDC resources. Keep deployment OIDC and
   publisher OIDC roles separate; expose only non-secret GitHub variables.
3. Implement shared manifest tooling and tests, including exact-set, digest,
   context, platform, scan, attestation, and rollback validation.
4. Implement the protected GitHub matrix workflow. Keep pull-request jobs
   credential-free; make the publish job environment-gated and all-or-nothing.
5. Update IDP deployment to apply the overlay, require a manifest, remove the
   Docker preflight, and pass exact digest parameters. Remove CodeBuild actions
   and permissions from the transformed headless template and IDP boundary.
6. Add repository/workflow/static/IAM/CloudFormation validation and update the
   runbook, architecture, cost, and security documentation.
7. Run the full validation suite, commit on the feature branch, push, request
   exact-head Copilot review, fix sound comments, and stop before merge.

## Risks and Mitigations

- Upstream build side effects beyond containers: inventory and transformed
  template tests fail if any required resource disappears unexpectedly.
- Registry eventual consistency: digest resolution and ECR scan polling are
  explicit aggregation gates.
- Partial release: a manifest is written only by the all-15 aggregation job;
  incomplete attempts are not deployable.
- Rollback expiry: tagged image retention is explicit and referenced manifests
  are checked before cleanup.
- Buildx attestations creating OCI indexes: post-push media-type/platform
  inspection rejects anything other than a single ARM64 image manifest.
- Environment confusion: manifest and OIDC subject must match account, region,
  repository, and protected GitHub environment.
