# React SPA workspace

This directory is reserved for the production React application being implemented with Claude Code.

Before generating code, read:

- `CLAUDE.md` in this directory
- `../../docs/ui-handoff.md`
- `../../contracts/openapi/loan-api.yaml`
- `../../contracts/runtime-config.schema.json`

Use `public/runtime-config.example.json` only as a shape/example. Deployment generates `runtime-config.json`; do not commit a live environment file.

The expected output is a TypeScript React/Vite SPA deployed to Azure Static Web Apps at the configured custom hostname. It calls only the Entra-protected Azure Container Apps API from `runtime-config.json`; it never calls AppSync, IDP Jobs REST, an AWS Loan API, or AWS management services. Direct S3 traffic is limited to the short-lived upload/download grant returned after Azure authorization.

The scaffold must commit a lockfile, `staticwebapp.config.json`, and a pinned
`@azure/static-web-apps-cli` development dependency so `scripts/deploy-web.ps1`
can use `npx --no-install swa deploy`. It must also provide `lint`, `typecheck`,
`test:coverage`, `build`, and `test:e2e:ci` scripts. Unit coverage is at least
80% per file/metric; Playwright covers the Entra-bound API integration using
mock/test identities and never production mortgage data.
The lockfile must also pass `npm audit --audit-level=high` in validation and
deployment.
