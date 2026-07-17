# React UI handoff specification

This document is the implementation brief for Claude Code. The OpenAPI file is authoritative if prose and schema differ. Azure Static Web Apps hosts the SPA, and the Entra-protected Azure Container Apps API is its only product API.

## Stack and structure

Build a TypeScript React/Vite SPA in `apps/web` using MSAL Browser/React, React Router, TanStack Query, React Hook Form, Zod, a generated OpenAPI client, Vitest, Testing Library, MSW, Playwright, and axe.

Suggested feature folders: `auth`, `api/generated`, `loans`, `documents`, `uploads`, `data-points`, and shared accessible components. Do not hand-code duplicate DTOs or scatter endpoint strings through components.

The browser never calls an AWS Loan API, AppSync, the optional IDP Jobs REST API, DynamoDB, or an IDP workflow. It calls the Azure API for every domain/status/data operation. The only browser-to-AWS request is an exact operation performed with a short-lived upload or download grant returned after Azure authorization.

## Runtime configuration

Load and validate `/runtime-config.json` before rendering:

```json
{
  "environment": "production",
  "apiBaseUrl": "https://api.loans.example.com",
  "entraTenantId": "<tenant-guid>",
  "entraSpaClientId": "<spa-client-guid>",
  "entraApiScopeBase": "api://<api-app-client-guid>",
  "redirectUri": "https://loans.example.com/auth/callback",
  "postLogoutRedirectUri": "https://loans.example.com/",
  "buildSha": "<commit-sha>",
  "maximumUploadBytes": 104857600
}
```

These values are public, not secrets. Fail with a clear configuration page rather than silently falling back to a different tenant/API.

## Authentication and permissions

Use single-tenant authorization code + PKCE, `loginRedirect`, `sessionStorage`, and silent token acquisition. On `interaction_required`, authenticate and return to the original route. After a `401`, refresh/retry once, then sign in again.

The browser registration has no client secret/certificate and never requests app-only tokens. Request delegated scopes needed for the current action. Production user tokens also carry matching assigned app roles. Show an action only when the required value is present in both `scp` and `roles`; the server performs the same check. UI capability mapping:

| Scope | Capability |
|---|---|
| `Loan.Read` | View current loan and archives |
| `Loan.Create` | Create/recreate a current loan |
| `Loan.Archive` | Archive the active loan |
| `Document.Upload` | Create document/upload/replacement/complete |
| `Document.Read` | View status and obtain PDF download grants |
| `Document.Archive` | Archive the current document version |
| `DataPoints.Read` | View/download extracted data |

`Admin.Purge` is excluded from UI v1. Decoding `scp` may hide actions, but the API remains authoritative.

## Routes

```text
/
/auth/callback
/loans
/loans/new
/loans/:loanId
/loans/:loanId/documents/new
/loans/:loanId/documents/:documentId
/loans/:loanId/documents/:documentId/archives/:archiveSequence
/loans/:loanId/archives/:archiveSequence
/unauthorized
/not-found
```

Do not assume a global loan-list endpoint. The initial experience is loan-ID lookup plus create. Treat archive display aliases as display-only; use numeric sequence and server links.

## Shared request behavior

Every API call sends bearer token, `Accept: application/json`, and a new `X-Correlation-Id`. JSON mutations also send `Content-Type` and an `Idempotency-Key` representing the user intent.

Create an idempotency key at intent start, disable double submission, and retain the same key across timeout, offline, 5xx, and explicit retry. A genuinely new user action gets a new key. Pending keys may be kept in `sessionStorage`; tokens and business data may not.

Render `application/problem+json` safely with correlation ID. Never show raw HTML/stack traces. Retry GET at most twice for `429/502/503/504`, honor `Retry-After`, and add jitter. Mutations retry only with the original key.

## End-to-end user flow

1. Create `loanId`. Display the returned immutable `loanInstanceId`.
2. Initialize a document. The Azure API returns platform-issued stable `documentId`, physical `uploadId`, and presigned POST before bytes move.
3. Validate one PDF client-side for usability and compute Base64 SHA-256. The server repeats all checks.
4. Build `FormData` with every returned S3 field, append the PDF last, upload with progress, and do not send an Entra token to S3.
5. After S3 success, call the upload `complete` endpoint. It sends no PDF. A timeout offers **Retry completion**, never **Upload again**.
6. Poll document status starting near two seconds and back off to 15 seconds. Pause while hidden, resume on focus, abort on navigation, and stop on terminal state.
7. On success, request fresh Azure-authorized grants for source PDF, selected PDF, and data-point download. Signed URLs are never logged or persisted.
8. Archive a terminal document version. `_001` is read-only; a replacement retains `documentId`, gets a new `uploadId`, and later becomes `_002`.
9. Archive an active loan in one operation. Explain that every document is included and the snapshot becomes read-only. A processing/incomplete document blocks archive with `409`.
10. Recreating the base `loanId` yields a new `loanInstanceId`; its later archive becomes the next sequence.

Canonical processing progression:

```text
AWAITING_UPLOAD -> VALIDATING -> QUEUED -> SCREENING -> EXTRACTING -> SUCCEEDED
```

Selection is committed atomically between `SCREENING` and `EXTRACTING`; the
current processor does not persist `SELECTED` as a guaranteed polling state.
`SELECTED` remains a recognized contract value, so the UI must still tolerate
it. Terminal alternatives are `HOLD`, `REJECTED`, and `FAILED`. `ARCHIVING`
disables mutations; `ARCHIVED` is read-only. An unknown future state renders as
Processing and disables destructive actions.

## Data and download safety

Render configuration-dependent data points with a safe recursive read-only viewer and raw JSON mode. Escape all strings and never use `dangerouslySetInnerHTML`. Do not place data points in local storage, service workers, analytics, or error telemetry.

Download endpoints return a short-lived grant. Open it with `noopener,noreferrer`; request a new grant after expiration. Do not reveal internal bucket/key names.

## UX and accessibility

- Always distinguish stable and generated IDs; provide copy buttons for support IDs/correlation IDs.
- Preserve UTC server timestamps and show localized text with UTC detail.
- Archive confirmation explains read-only semantics and, for a loan, that all documents are included.
- Refreshing a document route reconstructs state from the API.
- Status is not communicated by color alone.
- Meet WCAG 2.2 AA: keyboard operation, visible focus, correct labels/table headers, focus management, reduced motion, accessible progress, and polite/assertive live regions.
- Production security headers require strict CSP, HSTS, `nosniff`, `frame-ancestors 'none'`, restrictive referrer/permissions policies, no `unsafe-eval`, and no service worker.

## Required automated acceptance cases

1. Sign-in returns to the requested route; read-only users see no mutation actions.
2. Double-click create produces one intent/resource.
3. `LOAN_ALREADY_ACTIVE` navigates to the existing current loan.
4. Azure API-generated platform `documentId` appears before upload and is never replaced by an IDP object key, workflow ARN, or S3 version.
5. Direct S3 POST has no Entra token and preserves returned fields.
6. Completion timeout retries with the same key and does not reupload.
7. Refresh during processing restores polling.
8. All known states render; polling stops on terminal states.
9. Downloads always request fresh grants.
10. First document archive is `_001`; same-key retry remains `_001`; replacement later becomes `_002`.
11. Loan archive is blocked while work is active, includes every document when successful, and is read-only.
12. Recreated loan has a new `loanInstanceId` and later archives to the next sequence.
13. Deterministic handling exists for 401, 403, 404, 409, 410, 413, 415, 422, 429, offline, and 5xx.
14. No token, signed URL, PDF, filename, or data-point value reaches logs or persistent browser storage.
15. Generated client compiles against the checked-in OpenAPI contract; Playwright/axe tests pass with MSW and a staging smoke suite covers real Entra/Azure API/S3/headless-IDP behavior.
