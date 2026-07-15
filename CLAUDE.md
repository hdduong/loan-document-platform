# Instructions for coding agents

This public repository processes mortgage documents and extracted PII. Never commit, log, snapshot, or place real PDFs, OCR text, extracted values, real tenant/account/subscription identifiers, personal email/domain configuration, access tokens, presigned URLs, certificate private keys, `.env` files, or deployment output containing secrets in the repository.

Read these sources before changing behavior:

1. `.specify/memory/constitution.md` — project governance and non-negotiable quality gates.
2. The active feature's `spec.md`, `plan.md`, and `tasks.md` — approved scope, design, and execution order. Resolve it through `.specify/feature.json`; the checked-in baseline is `specs/001-loan-document-platform/`.
3. `contracts/openapi/loan-api.yaml` — authoritative HTTP contract.
4. `docs/architecture.md` — identities, archives, processing, and trust boundaries.
5. `docs/security.md` — mandatory production controls.
6. `docs/ui-handoff.md` — authoritative React behavior.

Use the Spec Kit skills under `.claude/skills/` for feature work. Invoke them as `/speckit-<command>`, not `/speckit.<command>`. Before implementation, make sure the active feature has a reviewed specification, plan, and task list. Treat the constitution, security controls, and existing API contract as constraints on feature artifacts; an intentional contract change must update the canonical contract and its tests in the same change. Do not hand-edit generated Spec Kit skills, scripts, or templates.

Important invariants:

- Do not conflate `loanId` with `loanInstanceId`.
- Do not conflate `documentId` with `uploadId` or an IDP execution ID.
- Retrying an archive with the same idempotency key returns the same sequence.
- A loan archive includes all documents by freezing its immutable loan instance; do not copy or rename every object.
- Browser code uses Entra authorization-code flow with PKCE and has no client secret/certificate or AWS credentials.
- Upload bytes go directly to an S3 quarantine prefix with a short-lived presigned POST.
- Only an exact, malware-clean S3 version and checksum may reach IDP.
- `cd-full-v1.json` is the accuracy baseline; do not optimize it without regression evidence.
- GitHub is the source-code host. Do not add CodeCommit, long-lived AWS keys, or cloud credentials to workflows; AWS deployment uses the exact-repository OIDC role.

For work inside `apps/web`, also read `apps/web/CLAUDE.md`.
