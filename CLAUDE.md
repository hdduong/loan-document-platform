# Instructions for coding agents

This public repository processes mortgage documents and extracted PII. Never commit, log, snapshot, or place real PDFs, OCR text, extracted values, real tenant/account/subscription identifiers, personal email/domain configuration, access tokens, presigned URLs, certificate private keys, `.env` files, or deployment output containing secrets in the repository.

Read these sources before changing behavior:

1. `contracts/openapi/loan-api.yaml` — authoritative HTTP contract.
2. `docs/architecture.md` — identities, archives, processing, and trust boundaries.
3. `docs/security.md` — mandatory production controls.
4. `docs/ui-handoff.md` — authoritative React behavior.

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
