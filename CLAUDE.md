# Instructions for coding agents

This public repository processes mortgage documents and extracted PII. Never commit, log, snapshot, or place real PDFs, OCR text, extracted values, real tenant/account/subscription identifiers, personal email/domain configuration, access tokens, presigned URLs, certificate private keys, `.env` files, or deployment output containing secrets in the repository.

Read these sources before changing behavior:

1. `.specify/memory/constitution.md` — project governance and non-negotiable quality gates.
2. The active feature's `spec.md`, `plan.md`, and `tasks.md` — approved scope, design, and execution order. Resolve it through `.specify/feature.json`; `specs/001-loan-document-platform/` is the historical AWS-hosted baseline and `specs/002-azure-api-control-plane/` is the active migration.
3. `contracts/openapi/loan-api.yaml` — authoritative HTTP contract.
4. `docs/architecture.md` — identities, archives, processing, and trust boundaries.
5. `docs/security.md` — mandatory production controls.
6. `docs/ui-handoff.md` — authoritative React behavior.
7. `docs/flows/README.md` — reviewed interactive and certificate-client sequence companion.

Use the Spec Kit skills under `.claude/skills/` for feature work. Invoke them as `/speckit-<command>`, not `/speckit.<command>`. Before implementation, make sure the active feature has a reviewed specification, plan, and task list. Treat the constitution, security controls, and existing API contract as constraints on feature artifacts; an intentional contract change must update the canonical contract and its tests in the same change. Do not hand-edit generated Spec Kit skills, scripts, or templates.

For every request that changes the repository, work through a pull request and complete the mandatory Copilot loop before reporting completion: ensure the GitHub Copilot pull request reviewer is requested for the exact current head, wait for the review, inspect every thread, implement and test every sound constitution-compatible suggestion, and document the reason for rejecting an inapplicable or harmful suggestion. Do not use an `@copilot` conversation command as a substitute; it starts a coding-agent task instead of submitting the required pull request review. Pushes made to address feedback require another Copilot review of the new head. Do not merge or call the work complete until `validate` and `copilot-review` pass and all actionable conversations are resolved.

When an operator confirms that a repeatable cloud setup, deployment, validation, recovery, or rotation command block worked, promote the generic procedure into `scripts/`, add synthetic regression coverage and runbook instructions, and publish it through the same review loop before treating it as a supported next step. Never copy local paths, filled environment values, certificates, identifiers, contacts, credentials, or command output into the repository.

Automated tests are mandatory for executable behavior. Every production Python file under `services/` must retain at least 80% line coverage individually and in aggregate. Every authored React/TypeScript production file must retain per-file 80% statements, lines, functions, and branches. Never lower a threshold, narrow source inclusion, or add an exclusion merely to pass. UI behavior changes also require deterministic synthetic Playwright integration coverage; real Entra/API/S3 smoke remains an environment-gated production-acceptance test.

Important invariants:

- Do not conflate `loanId` with `loanInstanceId`.
- Do not conflate `documentId` with `uploadId` or an IDP execution ID.
- Retrying an archive with the same idempotency key returns the same sequence.
- A loan archive includes all documents by freezing its immutable loan instance; do not copy or rename every object.
- Browser code uses Entra authorization-code flow with PKCE and has no client secret/certificate or AWS credentials.
- The Entra-protected Azure API is the only public domain API. Do not add or expose an AWS Loan API, direct AppSync client, Cognito service user, or optional IDP Jobs API dependency.
- Azure uses its dedicated managed identity token only for an exact issuer/audience/subject AWS STS trust. Never forward the browser bearer token to AWS and never add a static AWS key or Entra client secret fallback.
- DynamoDB remains the single mutable loan/document registry shared with the private AWS processors; do not introduce an Azure write-side replica or dual-write path.
- Upload bytes go directly to an S3 quarantine prefix with a short-lived presigned POST.
- Only an exact, malware-clean S3 version and checksum may reach IDP.
- `cd-full-v1.json` is the accuracy baseline; do not optimize it without regression evidence.
- GitHub is the source-code host. Do not add CodeCommit, long-lived cloud keys, client secrets, or cloud credentials to workflows; Azure and AWS deployment use separate exact-repository/environment OIDC identities.

For work inside `apps/web`, also read `apps/web/CLAUDE.md`.
