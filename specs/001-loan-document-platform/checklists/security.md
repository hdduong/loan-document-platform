# Security Requirements Checklist: Loan Document Platform

- **Purpose**: Confirm that the feature specification states the controls required by the project constitution and [security policy](../../../docs/security.md).
- **Created**: 2026-07-14
- **Feature**: [spec.md](../spec.md)

## Identity and Authorization Requirements

- [x] CHK001 Single-tenant Entra issuer, audience, lifetime, tenant, token-type, actor, client allowlist, and denylist validation are required.
- [x] CHK002 Delegated calls require both route scope and matching assigned app role.
- [x] CHK003 App-only calls require `idtyp=app` and the matching application role.
- [x] CHK004 SPA authorization code with PKCE, exact production redirects, and `sessionStorage` are required.
- [x] CHK005 Browser secrets, private certificates, AWS credentials, and service-client flow are prohibited.
- [x] CHK006 Confidential client credentials are workload/environment-specific and federation is preferred where available.

## Network and Storage Requirements

- [x] CHK007 CloudFront/WAF is the public API boundary; the Regional origin requires origin verification and default execute-api is disabled.
- [x] CHK008 Direct upload policy constrains opaque key, content type, size, checksum, encryption, and expiry.
- [x] CHK009 S3 privacy, versioning, TLS, ownership enforcement, Block Public Access, and SSE-KMS are required.
- [x] CHK010 Every process/read/download pins exact object version and checksum.
- [x] CHK011 Download grants require fresh authorization and expire within five minutes.

## Content and Model Requirements

- [x] CHK012 Only `NO_THREATS_FOUND` plus deterministic PDF validation can reach IDP.
- [x] CHK013 All other scan/validation outcomes fail closed and quarantine is not downloadable.
- [x] CHK014 Model output has no tools/side effects, is schema validated, and ambiguous selection enters `HOLD`.
- [x] CHK015 Model/config/region/input/output provenance is mandatory.
- [x] CHK016 Real PDF/OCR/extracted content is prohibited from source control, fixtures, logs, traces, and alarms.

## Lifecycle and Operational Requirements

- [x] CHK017 Archive preserves immutable evidence and is separate from legal-hold-aware purge.
- [x] CHK018 All mutations are actor-bound, request-hash-bound, and idempotent.
- [x] CHK019 Event consumers use bounded retry, DLQ, deduplication, and reconciliation.
- [x] CHK020 Repository CI has no AWS permissions; deployment uses exact-repository/environment GitHub OIDC.
- [x] CHK021 Certificate expiry/rotation, authorization denial, malware/IDP failures, queues, database, and budget require monitoring.

## Live Verification Still Required

- [ ] CHK022 Validate the actual Entra registrations, assignments, Conditional Access/MFA, allowlist, and negative token matrix.
- [ ] CHK023 Validate actual CloudFront/WAF/origin isolation, certificate chains/renewal, TLS, and disabled execute-api endpoint.
- [ ] CHK024 Validate actual S3/KMS/GuardDuty policies and clean/threat/unknown event paths with synthetic files.
- [ ] CHK025 Sample production telemetry to prove it contains no token, URL, filename, OCR, PDF, or extracted payload data.
- [ ] CHK026 Perform restore and confidential-client certificate rotation/revocation exercises.

Unchecked items are deployment acceptance checks, not permission to weaken their stated requirement.
