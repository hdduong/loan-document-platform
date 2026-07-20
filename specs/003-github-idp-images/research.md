# Research: GitHub-built AWS IDP Lambda Images

## Decision 1: Publish images outside the IDP stack

The pinned headless IDP publisher currently packages source into S3 and creates
an ECR repository, a privileged CodeBuild project, a trigger Lambda, and a
custom resource that waits for a build. That couples image compilation to every
CloudFormation update and gives the IDP execution role build privileges.

We will create one bootstrap-owned private ECR repository per environment and a
separate GitHub OIDC image-publisher role. The repository uses immutable tags,
scan-on-push, KMS encryption, and a Lambda pull policy scoped to the account and
region. The IDP stack will consume this repository but will not own it.

Alternatives rejected:

- Keeping the upstream ECR and CodeBuild resources: it violates the requested
  GitHub build boundary and makes deployment depend on a mutable build job.
- Fifteen repositories: the upstream unified pattern already uses one image
  repository and a logical-image-to-digest manifest. One repository keeps IAM,
  lifecycle, and rollback operations smaller without weakening digest identity.

## Decision 2: GitHub Buildx matrix, Linux ARM64, one image per logical function

The pinned `patterns/unified/buildspec.yml` defines the complete set of 15
functions and uses the repository root `Dockerfile.optimized`. The release
matrix will be committed, checked against the lock, and built as independent
single-platform `linux/arm64` images. The `mlflow_logger_function` entry keeps
the upstream `INSTALL_GIT=true` build argument.

Pull requests may build and scan without AWS credentials or registry writes.
Only a protected main/environment workflow may assume the image-publisher role.
The workflow records the exact upstream commit, repository revision, runner,
build arguments, image tag, digest, scan result, SBOM, and provenance evidence.

Alternatives rejected:

- A single sequential shell loop: one failure can hide which logical image is
  missing and wastes the runner; a matrix gives explicit, independently tested
  jobs and an all-or-nothing aggregation gate.
- Multi-architecture manifests: Lambda requires one architecture per image and
  an OCI index can be accepted by a registry while failing Lambda validation.

## Decision 3: Digest manifest is the deployment contract

The aggregation job writes a canonical JSON manifest containing exactly the
15 entries, the pinned source and overlay identities, AWS context, repository,
platform, content digests, scans, SBOMs, and attestations. It is uploaded as a
GitHub artifact and to the versioned KMS-protected deployment artifact bucket.

Deployment validates the manifest before CloudFormation changes anything, then
passes repository-plus-digest parameters to the reviewed IDP template adapter.
The resulting Lambda `ImageUri` values are direct digest references. Rollback
selects an earlier retained complete manifest and never rebuilds an image.

Tags remain human-readable release metadata only; they are not deployment
identity. The ECR lifecycle does not expire tagged releases automatically, so
the active and rollback manifests cannot be removed by a per-image count rule.

## Decision 4: Reviewed overlay, pristine vendor

The upstream checkout remains byte-for-byte pristine and is still pinned by
`vendor/idp.lock.json`. A checksum-locked repository patch is applied only to a
disposable deployment copy. The patch removes the unified CodeBuild/ECR trigger
resources, removes dangling build dependencies, adds repository/digest
parameters, and replaces every mutable `ImageVersion` reference with a digest
reference. The transformed headless template is statically checked for zero
CodeBuild resources and zero `codebuild:*` permissions before publication.

Maintaining a permanent fork was rejected because it makes upstream security
updates harder to review and obscures which source is actually deployed.

## Decision 5: Security evidence and failure policy

Trivy is reused at its existing pinned version for deterministic vulnerability
gates. GitHub artifact attestations provide build provenance and SBOM evidence
for the public repository; the manifest records their subjects and digests.
ECR scan-on-push remains an independent defense and is checked before release
aggregation. Any missing, pending, high-severity policy failure, wrong
architecture, altered source lock, or unverified evidence fails closed.

The publisher role has only ECR upload/read actions for the one repository,
manifest write access to its exact S3 prefix, and the minimum KMS data-key
operations. It cannot create stacks, assume roles, use CodeBuild, or access
documents. OIDC trust is restricted to the exact repository and GitHub
environment.

## Decision 6: Deployment runners do not need Docker

The existing PowerShell deployment wrapper will retain the pinned IDP CLI for
template/layer packaging but will stop asserting a Docker daemon. The wrapper
requires an image manifest, validates it with the shared tooling module, and
uses the disposable patched checkout. `-CleanBuild` remains available for
packaging caches but never triggers an image rebuild.

