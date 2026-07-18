# Security and certificate policy

## Distinct certificate and token purposes

1. **Public HTTPS certificates** protect `loans.<domain>` on Azure Static Web
   Apps and `api.loans.<domain>` on Azure Container Apps. Use Azure-managed
   custom-domain certificates when eligible, or a separately managed Key Vault
   certificate when policy requires it. No TLS private key enters this repository
   or application configuration.
2. **External Entra machine-client certificates** belong to individual
   confidential callers that cannot use workload federation. Entra stores only
   the public certificate; the caller controls the private key.
3. **Entra token-signing certificates** are Microsoft-managed and discovered
   through the tenant's OpenID metadata and JWKS.
4. **Azure-to-AWS workload federation** uses a user-assigned managed identity
   token and short-lived AWS STS credentials. It does not use a client
   certificate, client secret, or AWS access key.
5. **GitHub deployment federation** uses GitHub OIDC independently with Azure
   and AWS. It is separate from both product callers and the Container App's
   runtime identity.

These purposes are never combined. The React SPA has no secret or certificate.
The Azure API runtime does not possess an external caller's private key, and its
managed identity cannot be reused as a browser or deployment identity.

## React and interactive users

The SPA uses single-tenant authorization code flow with PKCE, exact HTTPS
redirect URIs, and no wildcard, secret, or certificate. MSAL uses
`sessionStorage`. Production excludes localhost redirect URIs and applies the
tenant's Conditional Access/MFA policy.

The product API exposes each canonical permission as a delegated scope and a
distinct matching user/application role. Custom Entra applications require the
two claim values to be unique:

| Permission | Delegated `scp` | Assigned `roles` value |
|---|---|---|
| Loan create | `Loan.Create` | `Loan.Create.Role` |
| Loan read | `Loan.Read` | `Loan.Read.Role` |
| Loan archive | `Loan.Archive` | `Loan.Archive.Role` |
| Document upload | `Document.Upload` | `Document.Upload.Role` |
| Document read | `Document.Read` | `Document.Read.Role` |
| Document archive | `Document.Archive` | `Document.Archive.Role` |
| Data-point read | `DataPoints.Read` | `DataPoints.Read.Role` |
| Administrative purge | `Admin.Purge` | `Admin.Purge.Role` (not granted to UI v1) |

For canonical permission `P`, a delegated token requires scope `P` and assigned
app role `P.Role`. The Azure boundary accepts only that exact suffix and
normalizes it back to `P` for the private domain seam. For an app-only token,
it requires an exact allowlisted client ID, role `P.Role`, `idtyp=app`, and
`azpacr=2`. The last claim proves certificate authentication; a client-secret
token is rejected even if Entra issued it. This prevents tenant-wide consent
from granting every user every operation. Hiding a UI control is not
authorization; the Azure API enforces every route.

## Product access-token validation

The Azure API cryptographically validates the original bearer token before any
domain, STS, DynamoDB, S3, KMS, or IDP-related action. Validation includes:

- an allowlisted signing algorithm and a signature from the tenant-specific
  JWKS;
- exact tenant-specific v2 issuer and exact product API audience;
- `exp`, `nbf`, and bounded clock skew;
- exact `tid`;
- a delegated `scp` or application `roles` plus the expected token type;
- immutable actor identifier (`oid`);
- allowlisted `azp`/`appid` and optional emergency client denylist;
- for app-only calls, `idtyp=app` and `azpacr=2` certificate proof;
- the route's declared permission, including both scope and role for delegated
  calls.

A missing bearer token, malformed token, invalid signature, unsupported
algorithm, wrong issuer/audience/tenant, or invalid lifetime is an
authentication failure and returns `401`. A cryptographically valid token that
fails the exact client allowlist, token-type, certificate, scope, or role policy
is an authorization failure and returns `403`. Application-generated failures
use the canonical problem shape and correlation ID; ingress defense in depth
may reject an invalid token before the application receives it.

JWKS and metadata caches honor bounded lifetimes. An unknown `kid` permits one
synchronized refresh and otherwise fails closed. Container Apps ingress
authentication may add defense in depth, but forwarded identity headers are not
the API's sole proof of identity.

Email, UPN, display name, HTTP headers that claim a user, and tenant IDs supplied
in a request body are never authorization keys. Browser ID tokens are not API
access tokens. A product access token is never forwarded to AWS.

## Azure workload federation into AWS

The Container App uses a dedicated user-assigned managed identity and a
dedicated Entra resource application for AWS federation. The browser API
audience is not reused. The managed identity obtains a v1 app-only token whose
audience is the federation application's Application ID URI.

AWS configures an OIDC provider for:

```text
https://sts.windows.net/<tenant-id>/
```

The runtime role trust uses exact `StringEquals` conditions for both:

```text
aud = <dedicated federation Application ID URI>
sub = <Azure API managed identity principal/object ID>
```

There is no wildcard issuer, audience, subject, tenant, or repository. Replacing
the managed identity or federation audience requires an explicit reviewed trust
change and new negative/positive acceptance evidence.

The Azure API sends the workload token only to regional AWS STS
`AssumeRoleWithWebIdentity`. STS returns temporary credentials bounded by the
role's maximum session duration. Each Container Apps replica caches credentials
in memory, coordinates refresh through a single-flight lock, and refreshes before
the configured safety window. Access key ID, secret access key, session token,
and managed-identity token are never persisted or logged.

Presigned grant expiration is capped at the smaller of the contract lifetime
and the remaining STS credential lifetime after clock-skew/safety margins. The
service refreshes before signing when that window is insufficient. An
indeterminate mutation is recovered with the persisted idempotency result, not
blindly repeated after credential refresh.

## Least-privilege AWS role

The Azure runtime role is distinct from GitHub deployment and CloudFormation
execution roles. It is restricted to the exact resources and operations needed
by the public contract:

- conditional reads/writes/transactions on the named DynamoDB registry and
  queries on its named indexes;
- S3 head/get/put or presigning operations on the exact quarantine, artifact,
  manifest, and IDP-related prefixes required by the adapter;
- only the KMS usage required for those encrypted objects;
- a narrowly named private reconciliation action only if the implementation
  requires it.

It cannot mutate IAM, CloudFormation, Entra, Container Apps, or deployment
resources; list arbitrary buckets/tables; call a public AppSync product API;
create Cognito users; or invoke IDP state machines directly. Headless IDP
submission remains the validated S3/event path.

## AWS deployment-role containment

The GitHub AWS principal is not an account-wide infrastructure administrator.
It can address only the configured platform and IDP CloudFormation stack ARNs,
execute only the deployment CLI's named change sets, and pass only one of two
purpose-specific CloudFormation service roles. Platform provisioning is scoped
to named buckets, tables, functions, queues, topics, rules, alarms, IAM roles,
and tagged KMS keys. IDP provisioning is isolated in its own role; the pinned
upstream template requires broader service actions for nested stacks and custom
resources, so those remaining wildcards are accepted only inside that role and
are not shared with platform delivery.

Both execution roles enforce a bootstrap-owned permissions boundary when roles
are created or updated. They deny boundary removal, allow attachment of only the
reviewed AWS managed policies, and constrain `iam:PassRole` by role-name prefix
and destination service. The runtime boundaries do not grant IAM administration,
so attaching a broader identity policy cannot turn an IDP or platform runtime
role into an administrator. Headless delivery also omits AppSync, Cognito,
API Gateway, CloudFront, and WAF provisioning permissions.

A committed CloudFormation stack policy denies update-time deletion and
replacement of S3 buckets, DynamoDB tables, and KMS keys. Deployment applies the
policy before updates and verifies it afterward. An intentional protected-
resource migration is a separately approved break-glass operation with backup,
restore, change-set, and policy-restoration evidence.

## External machine-client lifecycle

Prefer managed identity/workload federation when a service runs in Azure or
another supported workload platform. Use a certificate only when federation is
unavailable and the calling workload is independently controlled.

Production certificate baseline:

- one certificate per workload and environment;
- RSA 3072 or stronger and SHA-256 signing usage;
- corporate/public CA issuance when suitable PKI exists;
- 180-day validity where policy permits;
- begin rotation 60 days before expiry and fail deployment below 30 days;
- register the new public certificate alongside the old one;
- canary token acquisition and a harmless authorized API read;
- promote, observe for 24–48 hours, then remove and destroy the old key;
- alert at 60, 30, 14, and 7 days.

The repository may create a non-exportable CSR and register its public
certificate. It does not generate a self-signed credential and label it
production-ready. Private keys stay with the calling workload and never enter
the resource API, GitHub, source control, or deployment output.

## Network and browser boundary

- Production product routes under `/v1` accept only the exact configured custom
  API `Host`. The Container Apps provider FQDN remains available solely for
  unauthenticated `/health` and `/ready` deployment probes; non-production may
  explicitly allow the provider hostname for synthetic testing.
- Production SPA deployment fails closed until deployment state proves that the
  API custom domain is bound and its DNS cutover completed. Entra
  redirect/logout URIs and SPA runtime configuration name only the approved
  custom hosts.
- The API applies an exact production CORS allowlist; credentials and wildcard
  origins are not combined.
- S3 CORS permits only the exact SPA origin, methods, and headers needed for the
  returned direct-upload form and exposes no bucket listing.
- The API rejects an unexpected product-route host, unknown route, or
  bounded-body violation before domain processing. The provider FQDN is not an
  alternate production product entry point even though its health/readiness
  probes intentionally remain callable.
- Static Web Apps serves strict CSP, HSTS, `nosniff`,
  `frame-ancestors 'none'`, restrictive referrer/permissions policies, and no
  service worker in v1.
- No public AWS API Gateway, AWS Loan API Lambda, AppSync endpoint, Cognito
  service user, or IDP Jobs REST endpoint is part of the product surface.

## Document and data controls

- All S3 buckets are private, versioned, TLS-only, Bucket Owner Enforced, Block
  Public Access, and SSE-KMS protected.
- Upload policies constrain the exact opaque key, content type, size, checksum,
  encryption fields, and short expiration.
- Quarantine bytes are not product-downloadable and never reach IDP before
  validation.
- GuardDuty `NO_THREATS_FOUND` for the exact completed version is mandatory.
  Threat, unsupported, failed, missing, or contradictory results fail closed.
- Every downstream process/read/download pins an exact
  bucket/key/version/checksum tuple.
- Signed download grants expire in one to five minutes and are created only
  after fresh Azure authorization and immutable ownership resolution.
- Large JSON and every PDF use grants; bounded small data-point JSON may be
  returned through the Azure API.
- Business `loanId` and filenames are sensitive metadata. Uvicorn request-line
  access logging is disabled because product URLs contain those identifiers;
  application telemetry uses correlation IDs, opaque platform IDs, status, and
  sanitized reason codes instead of raw request paths or filenames.
- Archive preserves data. `Admin.Purge` remains a separate legal-hold-aware
  workflow and is not implemented by archive endpoints.

## Model and IDP controls

The existing `us.anthropic...` model IDs are US geographic cross-Region
inference profiles. Production use requires explicit approval that mortgage
content may be processed in their documented US destination Regions. Global
profiles are prohibited.

Record the effective inference Region, model/profile ID, configuration digest,
prompt version, schema version, exact input key/version/checksum, selection
evidence, upstream workflow ARN, and output checksum. Model output is
schema-validated untrusted data and has no tools or direct side effects.

The pinned deployment is headless. AppSync is removed, the optional Jobs REST
API is not enabled, and stock Cognito-group mutations are not repurposed as the
product API.

## Telemetry and incident response

Logs, metrics, traces, alarms, and error responses exclude document content,
OCR, extracted values, tokens, temporary credentials, signed URLs, raw workflow
input/output, and sensitive filenames. Use safe correlation IDs, platform IDs,
provider request IDs where appropriate, status, duration, retry count, and
sanitized failure category.

On a caller compromise, disable the service principal, add its client ID to the
API denylist, remove the Entra credential/assignment, and rotate. Removing a
certificate blocks new acquisition but does not revoke already-issued tokens;
the denylist provides immediate API containment.

On an Azure runtime identity or federation-trust compromise:

1. disable ingress or route production traffic to a known-good revision;
2. disable the AWS runtime role or tighten its trust to prevent new STS sessions;
3. disable/detach the managed identity as appropriate;
4. inspect CloudTrail, Azure sign-in, Container Apps, DynamoDB, S3, and KMS audit
   evidence without copying sensitive payloads;
5. replace the identity/audience and update exact trust conditions;
6. rerun wrong-issuer/audience/subject tests and the synthetic end-to-end smoke
   before restoring traffic.

Already-issued STS sessions expire naturally and must be bounded. The runtime
role policy remains narrow enough to limit that residual window.
