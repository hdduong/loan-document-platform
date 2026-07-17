# Entra, Azure API, and AWS identity flows

These guides document the production target boundary for interactive users and
certificate-authenticated service callers. They correct the original draft
diagrams: automated callers do not use client secrets, product bearer tokens do
not enter AWS, and the Azure API—not an AWS product API—owns every loan,
document, archive, status, and data-point operation.

## Full-size visual guides

- [Target React UI flow](entra-ui-flow.html)
- [Certificate-client API testing flow](entra-certificate-api-testing-flow.html)

GitHub displays committed HTML as source. For the large, scrollable diagram,
download or clone the repository and open the HTML file locally in a browser.
Both files are static: they contain no script, remote asset, form, or network
dependency; each uses only the adjacent committed stylesheet. This Markdown
page is the accessible GitHub-rendered companion.

## Three separate identity boundaries

| Boundary | Credential | Required claims or trust | Where it stops |
|---|---|---|---|
| Human SPA to Azure API | Entra v2 delegated product access token obtained with authorization code + PKCE | Exact tenant issuer, bare product API GUID audience, lifetime, tenant, user `oid`, allowlisted SPA `azp`, route `scp`, and matching assigned `roles` | Azure API |
| Service caller to Azure API | Entra v2 app-only product access token obtained with a certificate-signed `private_key_jwt` assertion | Exact tenant issuer, bare product API GUID audience, lifetime, tenant, service-principal `oid`, allowlisted client, route `roles`, `idtyp=app`, `azpacr=2`, and no `scp` | Azure API |
| Azure API to AWS STS | Dedicated Entra v1 managed-identity workload token | Issuer `https://sts.windows.net/<tenant-guid>/`, audience `api://<federation-application-guid>`, and subject equal to the user-assigned managed identity principal/object ID | Regional AWS STS |

The common cryptographic checks do not make delegated and app-only
authorization equivalent. A certificate-client test proves the app-only route
and the managed-identity-to-AWS path; it does not prove PKCE, Conditional
Access, delegated scope-plus-role enforcement, UI CORS, or MSAL behavior.

Client-secret application tokens are intentionally rejected. The optional
service client is not created by the production example by default. When a
service integration is required, use a distinct principal and certificate per
workload and environment, register only the public certificate in Entra, keep
the private key in the caller's approved key store, and follow the rotation
policy in [Security and certificate policy](../security.md).

## Product request path

1. Create a loan with `POST /v1/loans`. The caller supplies `loanId`; the Azure
   API returns an immutable `loanInstanceId`.
2. Compute the PDF's Base64 SHA-256 digest and initialize the document with
   `POST /v1/loans/{loanId}/documents`. The Azure API returns the stable
   `documentId`, physical `uploadId`, and a constrained presigned S3 POST.
3. Send the PDF directly to the returned S3 URL with every returned form field
   and no Entra bearer token.
4. After S3 succeeds, call
   `POST /v1/loans/{loanId}/documents/{documentId}/uploads/{uploadId}/complete`
   with an empty body. The API pins one exact S3 `VersionId` and returns `202`.
5. Object creation and GuardDuty scanning run independently of completion. The
   processor advances only the same client-complete, malware-clean version after
   checksum, size, encryption, metadata, PDF, and page-limit validation.
6. Poll `GET /v1/loans/{loanId}/documents/{documentId}`. The normal persisted
   path is `AWAITING_UPLOAD -> VALIDATING -> QUEUED -> SCREENING -> EXTRACTING
   -> SUCCEEDED`; terminal alternatives are `HOLD`, `REJECTED`, and `FAILED`.
   Selection is committed atomically between screening and extraction.
7. Read bounded inline data points through Azure or request a fresh
   Azure-authorized grant for data points, source PDF, or selected PDF. The
   caller performs only that exact S3 GET and sends no Entra token to S3.

Every mutation has its own intent-specific `Idempotency-Key`. Retries of the
same operation reuse its key; a new user intent receives a new key. In
particular, document initialization and upload completion never share a key.

Presigned capabilities are short-lived but not one-time. Current defaults are
600 seconds for upload POSTs and 120 seconds for download URLs, capped by the
remaining AWS credential lifetime. Completion pins the accepted upload version;
a download URL can otherwise be reused until its `expiresAt` value.

## Archive semantics

- Archiving a terminal document creates an immutable logical snapshot. The
  stable `documentId` remains unchanged; display aliases increase from `_001`.
- A replacement receives a new `uploadId`, repeats the same processing path,
  and can later become `_002`.
- Archiving a loan creates an immutable manifest for all owned documents and
  exact artifact versions. It does not move, rename, or copy S3 objects.
- Recreating the same business `loanId` creates a new `loanInstanceId`; its next
  loan archive sequence continues at `_002`.

## AWS access and audit boundary

The Azure runtime role is limited to the named DynamoDB registry/indexes, exact
S3 quarantine/artifact/manifest prefixes, KMS use through S3 for those objects,
and the named upload-processor reconciliation function. It cannot invoke
AppSync, the optional Jobs REST API, an IDP state machine, IAM, or deployment
APIs.

AWS sees the Azure assumed-role session, not the human or service caller's
product token. Azure registry/audit fields use the validated immutable Entra
`oid`; a correlation ID joins sanitized request diagnostics but is not itself an
identity. STS assumption is a CloudTrail management event. S3 and DynamoDB data
event coverage must be separately enabled and verified when production audit
policy requires it.

## Authoritative sources

- [OpenAPI contract](../../contracts/openapi/loan-api.yaml)
- [Security and certificate policy](../security.md)
- [Claude UI handoff](../ui-handoff.md)
- [Architecture](../architecture.md)
- [Azure API control-plane specification](../../specs/002-azure-api-control-plane/spec.md)
