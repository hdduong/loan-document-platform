# Security and certificate policy

## Three unrelated certificate types

1. **Public HTTPS certificates** are issued and renewed by AWS Certificate Manager. The CloudFront certificates for `loans.<domain>` and `api.loans.<domain>` live in `us-east-1`; the Regional API origin certificate for `origin-api.loans.<domain>` lives in `us-west-2`.
2. **Entra machine-client certificates** belong to individual confidential callers. Entra stores only the public certificate. The caller holds the private key and uses it to obtain an OAuth token.
3. **Entra token-signing certificates** are Microsoft-managed and discovered through the tenant JWKS endpoint.

These certificates are never reused across purposes. API Gateway and the React SPA do not possess a machine-client private key.

## React and interactive users

The SPA uses single-tenant authorization code flow with PKCE. It has exact HTTPS redirect URIs, no wildcard, secret, or certificate. MSAL uses `sessionStorage`. Production excludes localhost redirect URIs and uses Conditional Access/MFA according to tenant policy.

The API exposes delegated scopes and matching user/application roles:

- `Loan.Create`
- `Loan.Read`
- `Loan.Archive`
- `Document.Upload`
- `Document.Read`
- `Document.Archive`
- `DataPoints.Read`
- `Admin.Purge` (not granted to UI v1)

For a delegated token, production requires both the requested scope and the matching app role assigned to that user/group. For an app-only token, the matching application role is required. This prevents tenant-wide admin consent from granting every user every operation. The provisioning script initially assigns all roles directly to the selected administrator; production groups can replace those direct assignments later. The API enforces permissions. Hiding a UI button is not authorization.

## Machine-client certificate lifecycle

Create one certificate per workload and environment, for example `loan-document-service-prod-servicing`. Never share a production certificate with staging or another client.

Production baseline:

- RSA 3072 or stronger, SHA-256 signing usage.
- Corporate/public CA-issued certificate when a suitable PKI exists.
- 180-day validity where policy permits.
- Begin rotation 60 days before expiry and fail deployment below 30 days.
- Register the new public certificate alongside the old certificate.
- Canary token acquisition and a harmless authorized API read with the new key.
- Promote the new private key, observe at least 24–48 hours, then remove and destroy the old key.
- Alert at 60, 30, 14, and 7 days.

If a calling workload runs in Azure, managed identity/workload federation is preferable to a certificate. If it runs in AWS and federation is unavailable, store its private key in a dedicated Secrets Manager secret encrypted by a dedicated KMS key. Only that workload role may read/decrypt it. The resource API role receives no access.

The repository includes scripts to create a non-exportable CSR and to register a CA-issued public certificate. It intentionally does not generate a self-signed certificate and call it production-ready. A self-signed credential can be enabled explicitly for a constrained non-production client only.

On suspected compromise, immediately disable the service principal, add its client ID to the API emergency denylist, remove the Entra key credential, and rotate. Removing a certificate blocks new token acquisition but does not revoke already-issued access tokens; the API denylist provides immediate containment.

## Token validation

API Gateway validates the tenant-specific v2 issuer, exact resource application audience, signature, and time claims. Lambda then validates:

- exact `tid`;
- `scp` for a delegated user or `roles` plus `idtyp=app` for a service principal;
- immutable actor identifier (`oid`);
- allowlisted `azp`/`appid`;
- required permission for the route;
- optional emergency client denylist.

Tokens with neither valid delegated scopes nor valid application roles fail closed. Email, UPN, display name, headers, and request-body tenant fields are never authorization keys.

## Document and data controls

- All S3 buckets are private, versioned, TLS-only, Bucket Owner Enforced, Block Public Access, and SSE-KMS protected.
- Upload policies constrain exact opaque key, size, PDF content type, checksum, encryption headers, and short expiration.
- Quarantine bytes are inaccessible to product downloads and IDP.
- GuardDuty scan result `NO_THREATS_FOUND` is mandatory. Every other result is fail-closed.
- Every downstream operation pins an exact bucket/key/version/checksum tuple.
- Signed download grants expire in 1–5 minutes and are issued only after fresh authorization.
- Logs and telemetry exclude document content, OCR, extracted values, tokens, signed URLs, and potentially sensitive filenames.
- Business `loanId` is treated as sensitive metadata in URLs and access logs.
- Archive preserves data. `Admin.Purge` is a separate legal-hold-aware process that must remove all S3 versions and database references according to policy.

## Model controls

The existing `us.anthropic...` model IDs are US geographic cross-Region inference profiles. Production use requires explicit approval that mortgage content may be processed in documented US destination Regions. Global profiles are prohibited. Capture the effective inference Region, model/profile ID, config digest, prompt version, schema version, exact input version/checksum, and output checksum in audit metadata.
