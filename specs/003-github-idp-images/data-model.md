# Data Model: GitHub-built AWS IDP Lambda Images

## Expected Image Contract

`config/idp/images.json` is the reviewed source of truth for the pinned
headless release. Each entry contains:

- `name`: logical image name used in the ECR tag and manifest.
- `sourcePath`: upstream Lambda source directory.
- `dockerfile`: repository-relative optimized Dockerfile.
- `buildArgs`: fixed build arguments; only the MLflow entry has `INSTALL_GIT`.
- `lambdaLogicalId`: CloudFormation logical function name.
- `imageParameter`: digest parameter consumed by the template adapter.
- `platform`: exactly `linux/arm64`.

The set is exact: bda-invoke-function, bda-completion-function,
bda-processresults-function, ocr-function, classification-function,
extraction-function, assessment-function, processresults-function,
summarization-function, evaluation-function, test-execution-aggregation-function,
rule-validation-function, rule-validation-orchestration-function,
mlflow-logger-function, and rule-validation-policy-classification-function.

## Image Release Manifest

The schema at `contracts/schemas/idp-image-release.schema.json` defines these fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Manifest contract version. |
| `releaseId` | Immutable workflow release identity. |
| `environment` | Protected GitHub/AWS deployment environment. |
| `aws.accountId` / `aws.region` | Registry and deployment context. |
| `repository` | Exact ECR repository URI. |
| `upstream.version` / `upstream.commit` | Locked AWS IDP source identity. |
| `platformRevision` | Repository commit containing the contract/overlay. |
| `overlaySha256` | Normalized patch checksum. |
| `workflow.runId` / `workflow.url` | Reproducible GitHub execution evidence. |
| `images[]` | Exactly 15 image records. |
| `images[].name` | Entry from the expected image contract. |
| `images[].digestUri` | Repository URI plus `@sha256:<64 hex>`; deployment identity. |
| `images[].platform` | Must equal `linux/arm64`. |
| `images[].scan` | Scanner, completion status, severity policy, and evidence digest. |
| `images[].sbom` / `images[].provenance` | Attestation subjects and immutable evidence references. |

The validator rejects unknown/duplicate names, missing entries, tag-only URIs,
malformed digests, wrong account/region/repository, wrong source or overlay,
incomplete scans, and absent attestations.

## Deployment Selection

A deployment selection is a manifest path and SHA-256 checksum recorded by the
IDP output under `.local/idp-<environment>.json`. The output contains no
credentials and is ignored by Git. Selecting a prior manifest is the rollback
operation; no image build is part of rollback.
