# Production Readiness Checklist: Loan Document Platform

> **Historical baseline — do not execute.** This file records the superseded AWS-hosted product API. Use the current [Azure API control-plane specification](../../002-azure-api-control-plane/spec.md) and its companion files for implementation.

- **Purpose**: Separate repository readiness from real AWS/Entra/domain operational acceptance.
- **Created**: 2026-07-14
- **Feature**: [spec.md](../spec.md)

## Repository and Design Baseline

- [x] CHK001 Canonical OpenAPI and runtime configuration schemas are versioned and validated.
- [x] CHK002 API, upload processor, IDP postprocessor, infrastructure scaffolds, and Python tests are present.
- [x] CHK003 Screening/full IDP configurations and upstream IDP commit are pinned and digest validated.
- [x] CHK004 GitHub workflows pin third-party actions and PR validation has read-only repository permission.
- [x] CHK005 AWS/Entra/GitHub/bootstrap/deployment work is scripted and excludes long-lived AWS keys.
- [x] CHK006 Architecture, security, runbook, GitHub delivery, UI handoff, constitution, feature artifacts, and Claude skills are documented.
- [x] CHK007 Repository invariant, Python, OpenAPI, CloudFormation, and PowerShell syntax gates are defined.
- [x] CHK008 Main-branch protection, required `validate` check, and production environment gate are configured in GitHub.
- [x] CHK034 The canonical repository slug, automatic draft/every-push Copilot ruleset, exact-head review gate, and review-response governance are configured and scripted.

## Application Completion

- [ ] CHK009 Build the React/TypeScript SPA and generate its client/types from canonical OpenAPI.
- [ ] CHK010 Implement Entra PKCE/session handling and permission-aware loan/document/archive/download workflows.
- [ ] CHK011 Pass UI lint, unit/component, build, Playwright, WCAG 2.2 AA/axe, and security tests.
- [ ] CHK012 Validate failure/hold/retry/expiry UX with synthetic MSW and live test cases.

## AWS, Entra, DNS, and Certificates

- [ ] CHK013 Supply reviewed ignored environment values for the real AWS account/profile, Route 53 zone/domain, and Entra tenant/subscription.
- [ ] CHK014 Provision Entra API/SPA registrations, scopes/roles, groups/assignments, exact redirects, client allowlist, and Conditional Access/MFA.
- [ ] CHK015 Provision/verify GitHub OIDC bootstrap and least-privilege CloudFormation deployment/execution roles in the target AWS account.
- [ ] CHK016 Deploy API/data/upload/IDP resources in `us-west-2` and verify KMS, S3, DynamoDB PITR/deletion protection/backup, GuardDuty, queues, DLQs, and limits.
- [ ] CHK017 Issue/validate ACM certificates, Route 53 records, CloudFront/WAF distributions, API origin header, and disabled execute-api endpoint.
- [ ] CHK018 Publish/validate the SPA runtime configuration without secrets and deploy the SPA at the production custom hostname.
- [ ] CHK019 Register CA-issued workload certificates only where federation is unavailable; store private keys outside source and the resource API.

## End-to-End and Negative Acceptance

- [ ] CHK020 Run create/get/archive/recreate loan lifecycle, including concurrent/idempotent `_001`/`_002` behavior.
- [ ] CHK021 Run create/upload/complete/scan/process/read/download/archive/replace document lifecycle with a synthetic package.
- [ ] CHK022 Demonstrate clean, threat, unsupported, failed scan, checksum mismatch, invalid/encrypted PDF, page/size-limit, and duplicate/out-of-order event outcomes.
- [ ] CHK023 Demonstrate correct CD selection plus tie/conflict/missing-evidence `HOLD` behavior.
- [ ] CHK024 Demonstrate delegated and app-only positive/negative matrices for tenant, audience, issuer, expiry, token type, scope, role, client allowlist, and denylist.
- [ ] CHK025 Confirm signed grants expire in the configured one-to-five-minute window and cannot cross immutable ownership/archive boundaries.
- [ ] CHK026 Confirm WAF/origin bypass attempts and the default API Gateway hostname cannot reach the API.

## Operations, Recovery, and Cost

- [ ] CHK027 Deliver and acknowledge alarms for API/edge, auth denial, Lambda, queues/DLQs, IDP, malware, DynamoDB, certificate expiry, and budget.
- [ ] CHK028 Run the scheduled reconciliation/watchdog against intentionally delayed/missed events and drain a synthetic DLQ item.
- [ ] CHK029 Sample logs/traces/metrics and prove no document content, extracted values, token, signed URL, private key, or sensitive filename is present.
- [ ] CHK030 Restore DynamoDB/S3-backed state from the documented backup path and verify immutable archive references.
- [ ] CHK031 Rotate a confidential-client credential with overlap/canary/observation/removal and test emergency denylist containment.
- [ ] CHK032 Record synthetic accuracy/selection, latency, effective model region, and per-package cost evidence; verify hard concurrency/size controls and $100 budget alerts.
- [ ] CHK033 Obtain required mortgage-data, model-region, retention/legal-hold, and security approvals before production traffic.

Production readiness is not achieved until every unchecked item is completed with evidence in the release record.
