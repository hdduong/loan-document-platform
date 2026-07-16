# Claude Code UI brief

Implement the UI in this directory only. Treat `../../contracts/openapi/loan-api.yaml` and `../../docs/ui-handoff.md` as authoritative. Generate the TypeScript API client from OpenAPI; do not hand-maintain duplicate DTOs or endpoint strings.

Before changing UI behavior, also read `../../.specify/memory/constitution.md` and the active feature's `spec.md`, `plan.md`, and `tasks.md`; resolve the active feature through `../../.specify/feature.json`. Implement only tasks assigned to the UI and keep the feature artifacts aligned with the authoritative OpenAPI contract and UI handoff. If a feature artifact conflicts with a production security rule or canonical contract, stop that implementation path until the artifacts and canonical source are deliberately reconciled.

Required stack: React, TypeScript, Vite, React Router, MSAL Browser/React, TanStack Query, React Hook Form, Zod, Vitest, Testing Library, MSW, Playwright, and axe.

Quality gates are mandatory: `npm run typecheck`, `npm run test:coverage`, the production build, and `npm run test:e2e:ci` must pass. Vitest must include all authored `src/**/*.ts` and `src/**/*.tsx` files and enforce per-file 80% statements, lines, functions, and branches. Generated client/type exclusions must be narrow and separately protected by generation-drift and compilation checks. Playwright must cover every affected journey with synthetic identity/API/S3 behavior, block unexpected external network calls, and use `msw/node` or Playwright routing rather than a production service worker.

Non-negotiable security rules:

- Use Entra authorization code + PKCE and `sessionStorage` for MSAL cache.
- Enable an action only when the delegated token contains both its scope and matching assigned app role.
- Never add a browser client secret, certificate, AWS credential, or service-client flow.
- Never log or persist tokens, presigned URLs, filenames, PDF content, loan data, or extracted data points.
- Do not add a service worker in v1.
- Load public deployment settings from `/runtime-config.json`, validate them against `../../contracts/runtime-config.schema.json`, and fail closed when invalid.
- Upload the PDF directly to the returned S3 presigned POST without an Entra token.
- Keep the same idempotency key across safe retries of the same user intent.
- Follow server links/sequence numbers for archives; display aliases are never parsed.
- Meet WCAG 2.2 AA and the acceptance tests in the handoff document.

Do not change infrastructure, backend authorization, archive semantics, or IDP configuration as part of UI work.
