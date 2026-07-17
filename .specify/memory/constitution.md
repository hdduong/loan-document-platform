# Loan Document Platform Constitution

## Core Principles

### I. Specification and Contract First

Every behavior change MUST begin in `specs/<NNN-feature>/spec.md` and MUST be
traceable through `plan.md` and `tasks.md` before implementation. The OpenAPI
contract in `contracts/openapi/loan-api.yaml` is authoritative for HTTP paths,
payloads, status codes, scopes, and roles. A pull request that changes behavior
MUST update the applicable specification and contract in the same change.

### II. Stable Identity and Lifecycle Semantics

`loanId`, `loanInstanceId`, `documentId`, `uploadId`, `processingExecutionId`,
and S3 `VersionId` are distinct identifiers and MUST never be substituted for
one another. Archive aliases are server-issued, monotonic (`_001`, `_002`, ...),
and idempotent. Archiving a loan freezes its immutable loan instance and all of
its documents; implementations MUST NOT simulate this by renaming or copying
every object. Destructive purge is an explicitly privileged operation, never an
alias for archive.

### III. Privacy and Zero-Trust Boundaries

No real mortgage document, OCR text, extracted value, tenant/account ID,
personal email/domain configuration, credential, token, signed URL, private
key, or deployment output may enter public source control, tests, logs, or
agent context. Browser authentication uses Entra authorization code with PKCE
and has no secret, certificate, AWS credential, or service-client flow. API
authorization MUST enforce both the declared OAuth scope and matching app role.
Uploads go directly to versioned S3 quarantine storage and only an exact,
checksum-verified, malware-clean version may enter IDP processing.

### IV. Deterministic Document Processing and Provenance

The reviewed `cd-full-v1` configuration is the extraction accuracy baseline and
MUST remain immutable unless regression evidence justifies a new version. The
screening pass inspects every package page using text-only OCR, then full Forms
and Tables extraction runs only on the deterministically selected Closing
Disclosure pages. Selection, input versions, configuration versions/digests,
model evidence, execution ARNs, and output artifacts MUST be recorded. Missing,
ambiguous, or contradictory evidence fails closed to a review/hold state.

### V. Testable, Reviewable Changes

Each prioritized user story MUST have an independent acceptance test. Contract,
authorization, idempotency, archive sequencing, exact-version processing, and
failure-reconciliation changes require automated regression coverage. All
changes MUST pass repository invariants, Python lint/compile, unit tests,
OpenAPI validation, PowerShell parsing, and CloudFormation lint before merge.
Generated or third-party assets MUST be pinned and their provenance retained.

### VI. Scripted, Observable, and Cost-Aware Operations

Production infrastructure and identity configuration MUST be reproducible from
reviewed scripts and infrastructure as code. GitHub deploys with short-lived
OIDC credentials restricted to the exact repository and environment; long-lived
AWS keys are prohibited. Production changes require the protected `main` branch,
the `prod` environment reviewer, observable failure paths, bounded retries/DLQs,
execution reconciliation, backups, and cost alerts. The USD 100 monthly budget
is an alerting guardrail, not a hard service stop.

### VII. Mandatory Exact-Head Copilot Review

Every pull request MUST request GitHub Copilot code review, including drafts,
and MUST request a new review after every pushed commit. Merge MUST remain
blocked until Copilot has submitted a review for the exact current head SHA.
All review comments MUST be evaluated: actionable comments consistent with
this constitution MUST be implemented and verified; an inapplicable,
incorrect, or harmful suggestion MUST be answered with concrete rationale
before its conversation is resolved. Any resulting push restarts the
review-wait-fix cycle. Copilot is advisory and never substitutes for automated
validation, security review, or human judgment. Quota exhaustion, timeout, or
service failure MUST fail closed and MUST NOT be treated as a completed review.

## Technology and Compliance Constraints

- AWS workload region is `us-west-2`; CloudFront-scoped resources use the AWS
  regions required by that service.
- Microsoft Entra ID is the identity provider for SPA SSO and OAuth API access.
- AWS IDP is pinned by `vendor/idp.lock.json`; Spec Kit is pinned by
  `vendor/spec-kit.lock.json`.
- Customer data is encrypted in transit and at rest, retained only by explicit
  lifecycle policy, and excluded from public repositories and CI artifacts.
- The React client consumes generated OpenAPI types and runtime configuration;
  it MUST NOT maintain a second backend contract.
- Simplicity is preferred. New services, stores, identities, or trust boundaries
  require a documented decision in `research.md` and a constitution check.

## Development Workflow and Quality Gates

1. Use `/speckit-specify` for a new behavior and `/speckit-clarify` when any
   material requirement is ambiguous.
2. Use `/speckit-plan`; record decisions in `research.md`, entities in
   `data-model.md`, operator validation in `quickstart.md`, and interface changes
   in `contracts/`.
3. Use `/speckit-tasks`; every task needs an ID, exact path, user-story label
   when applicable, and an honest completion state.
4. Use `/speckit-analyze` before implementation when artifacts change, and
   `/speckit-converge` after implementation to append any remaining work.
5. Work on a non-protected branch and open a pull request. Ensure Copilot is
   requested for the exact head SHA, wait for its review, address every sound
   comment, push fixes, and repeat until the latest head is reviewed with no
   unresolved actionable feedback.
6. Merge only after the required `validate` and `copilot-review` checks pass and
   every review conversation is resolved.
7. Deployment is a separate reviewed action. Passing CI does not imply that AWS
   or Entra resources have been provisioned or operationally accepted.

## Governance

This constitution supersedes conflicting project guidance. Amendments require
a pull request that explains the reason, migration impact, affected specs, and
version change. Major versions remove or redefine a principle; minor versions
add a principle or materially expand a gate; patch versions clarify wording.
Every plan MUST include a constitution check, and every review MUST reject
unjustified violations. `CLAUDE.md` supplies agent-specific operating context
but cannot weaken this constitution.

**Version**: 1.1.0 | **Ratified**: 2026-07-14 | **Last Amended**: 2026-07-14
