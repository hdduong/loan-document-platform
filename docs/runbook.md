# Deployment and operations runbook

## Prerequisites

- AWS account with administrator rights for initial bootstrap; ongoing delivery uses GitHub OIDC and a dedicated CloudFormation execution role.
- GitHub account with permission to create the public repository and GitHub Actions workflows.
- IAM Identity Center AWS CLI profile with MFA. Do not use long-lived access keys.
- Route 53 public hosted zone for the selected company domain.
- Microsoft Entra tenant with Application/Cloud Application Administrator rights and permission to grant tenant consent.
- PowerShell 7, Git, Python 3.12+, Node 22+, AWS CLI v2, AWS SAM CLI, Azure CLI, and WSL/Ubuntu for upstream IDP tooling.
- Corporate TLS root CA configured for Azure CLI/Graph if a proxy intercepts TLS. Never disable certificate validation.

## Initial order

1. Create an ignored `config/environments/prod.json` from the example.
2. Run `scripts/bootstrap.ps1 -InstallMissing`, then `scripts/bootstrap.ps1 -EnvironmentFile ...` to verify the selected AWS account and Entra tenant.
3. Run `scripts/provision-github.ps1 -EnvironmentFile ... -CreateRepository -GenerateInitialOriginVerifySecret` to create/configure the public GitHub repository, AWS OIDC bootstrap stack, non-secret variables, and initial protected origin secret. Review its trust policy before publishing code.
4. Commit the reviewed scaffold and push `main` to GitHub. Configure branch protection and the `prod` GitHub environment before enabling production deployment.
5. Run `scripts/provision-entra.ps1 -EnvironmentFile ...`, then `scripts/sync-github-entra.ps1 -EnvironmentFile ...`. Only non-secret tenant/app IDs are synchronized; no certificate or client secret is uploaded.
6. Deploy the platform stack once with its placeholder IDP bucket. This creates the postprocessor ARN.
7. Deploy the pinned headless IDP stack with the postprocessor ARN.
8. Upload named `cd-screen-v1` and `cd-full-v1` configurations.
9. Update the platform stack with actual IDP input/output/working bucket names.
10. Deploy the `us-east-1` edge stack for UI/API CloudFront, WAF, certificates, and DNS.
11. Deploy the SPA build and `runtime-config.json`.
12. Run authenticated synthetic smoke tests.

`scripts/deploy-all.ps1` orchestrates these phases but stops before external consent/certificate steps that require an administrator decision.

The GitHub deployment workflow is deliberately manual (`workflow_dispatch`) for production. Its OIDC trust accepts only this repository's configured environment subject; pull-request jobs receive no AWS deployment role.

## Production release gates

- Unit, API contract, IaC lint, dependency, secret, and synthetic integration tests pass.
- No PDFs, private keys, `.env`, OCR, or extraction output are present in Git.
- The same immutable build artifacts are promoted; production is not rebuilt.
- IDP configuration digests and upstream commit match the release manifest.
- CloudFormation change sets are reviewed and approved.
- Entra redirect URI, audience, scopes/roles, and client allowlists match the target environment.
- Certificate remaining validity exceeds 30 days.
- Smoke-test spend is bounded and uses synthetic data.

## Operational responses

### Upload stuck in `VALIDATING`

Check the upload item’s pinned S3 version, GuardDuty result, EventBridge delivery, processor DLQ, and PDF validation code. Do not manually copy unscanned content into IDP.

### Selection enters `HOLD`

Review candidate page IDs and safe reason codes. Do not edit model output in place. Record a manual selection as a new audited decision/rule version, then submit only the approved selected pages to the full pass.

### IDP failure

Inspect Step Functions and sanitized correlation metadata. Reprocessing creates a new `processingExecutionId` pinned to the same source version/config. It does not overwrite earlier results.

### Restore

DynamoDB PITR restores to a new table. S3 versions remain immutable. Perform a restore exercise before launch and at least annually; update stack parameters only after reconciliation and approval.

### Certificate rotation

Register next public certificate, deploy its private key only to the owning workload, canary, promote, observe, remove old Entra credential, and destroy old key. Test this procedure before the first production expiration.
