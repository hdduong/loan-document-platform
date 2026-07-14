# React SPA workspace

This directory is reserved for the production React application being implemented with Claude Code.

Before generating code, read:

- `CLAUDE.md` in this directory
- `../../docs/ui-handoff.md`
- `../../contracts/openapi/loan-api.yaml`
- `../../contracts/runtime-config.schema.json`

Use `public/runtime-config.example.json` only as a shape/example. Deployment generates `runtime-config.json`; do not commit a live environment file.

The expected output is a TypeScript React/Vite SPA deployed as static assets behind the CloudFront stack in `infra/edge`.
