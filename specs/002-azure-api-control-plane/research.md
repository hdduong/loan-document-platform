# Research: Azure API Control Plane

This record resolves the implementation choices for moving the public loan and
document API to Azure while retaining the existing AWS document-processing data
plane. The existing REST contract and business behavior remain authoritative.

## Azure application host

**Decision**: Run the API as a containerized FastAPI application on Azure
Container Apps. Assign a dedicated user-assigned managed identity to the app,
deploy immutable image revisions, expose health and readiness probes, and bind
the production API hostname to a managed or Key Vault-backed certificate. Keep
the identity independent of any one revision or Container App replacement.

**Rationale**: The API is a cohesive, middleware-heavy HTTP service rather than
a collection of independent event handlers. Container Apps runs a standard ASGI
process without a cloud-specific request adapter, supports revision-based
rollout and rollback, horizontal scaling, user-assigned managed identities, and
custom HTTPS domains. A user-assigned identity has an independent lifecycle,
which keeps the AWS trust subject stable when a revision or application resource
is replaced. Microsoft documents both user-assigned identities for Container
Apps and automatically renewed managed certificates for eligible custom domains:
[Container Apps security](https://learn.microsoft.com/en-us/azure/container-apps/security)
and [custom domains and managed certificates](https://learn.microsoft.com/en-us/azure/container-apps/custom-domains-managed-certificates).

**Alternatives considered**:

- **Azure Functions**: Suitable for short, independent triggers, but the current
  API has shared authorization, idempotency, transaction, and artifact-grant
  behavior across many routes. Adapting the domain service to the Functions
  programming model would add a second request abstraction and make local
  contract testing and middleware behavior less direct. Premium Functions could
  meet the runtime requirements, but offers no material benefit for this API.
- **Azure App Service**: A viable FastAPI host with mature custom-domain and
  authentication features. It was not selected because Container Apps provides
  an explicit immutable-container and revision model that matches the repository's
  scripted release and rollback requirements without an App Service-specific
  deployment package.
- **Azure Static Web Apps managed API**: Keep Static Web Apps for the SPA only.
  Its managed API integration is not the ownership boundary for this production
  service, cross-cloud credential lifecycle, or long-lived independent API domain.

## API framework and caller authentication

**Decision**: Implement a thin FastAPI/ASGI HTTP layer over the retained
host-callable lifecycle dispatch. The application cryptographically validates
every Entra bearer access token using the tenant-specific OpenID configuration
and JWKS before constructing the normalized Lambda-compatible envelope consumed
by that dispatch. Validation includes signature, algorithm allowlist, exact v2
issuer, exact API audience, `exp`, `nbf`, tenant, token type, immutable actor,
allowed/denied client, app-only certificate proof, and the route's `scp` and
`roles` requirements before an AWS SDK operation is possible.

JWKS values are cached according to bounded configuration/HTTP cache lifetimes.
An unknown `kid` permits one synchronized metadata/JWKS refresh and otherwise
fails closed. Container Apps authentication may be an additional ingress control,
but its injected principal headers are not the API's sole proof of identity.

**Rationale**: FastAPI preserves the existing Python investment, supports the
OpenAPI-first contract, and separates HTTP concerns from the loan/document state
machine. Application-level JWT verification has deterministic unit and contract
tests and does not make domain security depend on an Azure-specific forwarded
header. Only access tokens issued for this API are accepted; browser ID tokens
and an Azure workload token intended for AWS federation are different credentials
and are never interchangeable.

**Alternatives considered**:

- **Trust Container Apps authentication headers only**: Rejected because route
  scope/role policy, client allowlists, token-type rules, and negative tests must
  remain explicit and portable. A proxy configuration error must not turn an
  unsigned header into an authenticated principal.
- **Deploy or trust the API Gateway/Lambda adapter unchanged**: Rejected because
  it delegates cryptographic validation to API Gateway and preserves a public
  AWS API. The retained normalized envelope is only an in-process compatibility
  seam after Azure has independently authenticated the caller; no Lambda or API
  Gateway product endpoint is deployed.
- **Rewrite in a new language/framework**: Rejected for the initial migration.
  It would combine a hosting move with a semantic rewrite of archive,
  idempotency, and upload-integrity behavior.

## Authoritative registry

**Decision**: Retain the current DynamoDB registry as the only mutable source of
truth for loan heads, loan instances, documents, uploads, archive counters,
idempotency records, processing state, and pinned artifact references. The Azure
API accesses it through a repository adapter using temporary AWS credentials.
The existing AWS upload and IDP postprocessors continue to update the same table
with conditional and transactional operations.

**Rationale**: The processors already reconcile malware events, IDP workflow
events, and exact object versions against this registry. Keeping one table
preserves atomic counters, conditional concurrency, idempotent replay, and the
current completion-versus-malware ordering behavior. It also makes the hosting
migration a control-plane change rather than a distributed data migration. The
additional Azure-to-`us-west-2` latency is acceptable within the two-second API
target and is reduced with connection reuse and bounded SDK retries.

**Alternatives considered**:

- **Copy state to Cosmos DB, Azure SQL, or PostgreSQL**: Rejected because two
  writable registries would create ambiguous ownership and require a reliable,
  ordered cross-cloud outbox/callback protocol before cutover.
- **Move the registry and make AWS processors call Azure synchronously**:
  Rejected for this feature because it places malware and IDP event processing
  on a cross-cloud synchronous dependency and expands failure recovery.
- **Read model in Azure**: Deferred. A later, disposable read projection may be
  built from durable events, but it cannot allocate identities, advance state,
  authorize ownership, or act as an archive record.

## Azure workload federation into AWS

**Decision**: Use a dedicated user-assigned managed identity and a dedicated
Entra application audience for AWS federation. The federation resource app
issues a v1 app-only access token whose audience is its Application ID URI. The
API obtains that token from `ManagedIdentityCredential` and passes it to the
regional AWS STS `AssumeRoleWithWebIdentity` endpoint. AWS contains an OIDC
provider for the documented tenant issuer `https://sts.windows.net/<tenant-id>/`
and a runtime role whose trust policy uses `StringEquals` for both the
provider-qualified `aud` and `sub` claims. The trust contains no wildcard issuer,
audience, subject, or tenant value.

For this documented managed-identity token form, the exact `sub` is the Azure
managed identity's object/principal ID. Provisioning obtains that ID from the
created user-assigned identity, verifies the token claims during acceptance, and
requires an explicit trust update if either identity or audience is replaced.
The browser API audience is not reused as the workload-federation audience. The
role permissions are separately restricted to the exact registry, S3 buckets
and prefixes, KMS key, and integration action required by the Azure API.

**Rationale**: Managed identity removes reusable AWS keys and Entra client
secrets. The requested resource becomes the token's `aud`, and AWS supports
matching `aud` and `sub` together in an OIDC role trust. STS then returns bounded
temporary credentials whose effective permissions cannot exceed the role policy.
Relevant platform behavior is documented by Microsoft in [managed identities](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/overview)
and by AWS in its [Entra managed-identity STS pattern](https://aws.amazon.com/blogs/security/how-to-access-aws-resources-from-microsoft-entra-id-tenants-using-aws-security-token-service/),
[OIDC provider creation](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html),
and [`AssumeRoleWithWebIdentity`](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html).

**Alternatives considered**:

- **Static AWS access key in Azure configuration or Key Vault**: Prohibited. A
  vault reduces disclosure risk but does not remove a reusable long-lived AWS
  credential or its rotation burden.
- **Entra certificate credential followed by STS**: Rejected because the Azure
  host already has a managed workload identity. Certificates remain appropriate
  for external confidential callers that cannot use workload federation, not for
  this runtime.
- **Cognito service user/client credentials**: Rejected because Cognito is not
  the platform identity boundary and does not grant the stock AppSync upload
  mutation's required user-group authorization.
- **Broad tenant or audience-only AWS trust**: Rejected because another workload
  capable of obtaining a token for that audience could assume the role. Exact
  `aud` plus exact `sub` is mandatory.

## Temporary credential lifecycle

**Decision**: Cache the managed-identity token and assumed AWS credentials only
in memory per Container Apps replica. Use a single-flight/lock around refresh,
refresh AWS credentials before a fixed safety window (initially five minutes),
and never allow requests to start with credentials that cannot remain valid for
their bounded AWS operation. Rebuild AWS clients when the credential generation
changes, or use an SDK refreshable-credentials provider; do not construct
long-lived boto3 clients from a one-time static credential dictionary.

Upload and download grant expiration is capped at the smaller of the configured
grant lifetime and the remaining STS credential lifetime minus clock-skew and
safety margins. If that window is insufficient, credentials are refreshed before
the grant is signed. Expired credentials fail closed. A forced refresh may be
retried once for an operation known not to have been committed; mutation replay
continues to rely on the existing idempotency record rather than a blind SDK
retry after an indeterminate response.

**Rationale**: Per-replica caching avoids an Entra and STS exchange on every API
request while keeping credentials out of persistent or distributed storage.
Single-flight refresh prevents a scale replica from producing a refresh storm.
AWS documents that `AssumeRoleWithWebIdentity` returns expiring credentials and
that its requested duration is bounded by the role's maximum session duration.
Presigned URLs made with temporary credentials cannot safely be treated as valid
beyond those credentials.

**Alternatives considered**:

- **Exchange on every request**: Rejected because it adds avoidable latency and
  makes Entra/STS throttling part of every domain operation.
- **Redis, database, or Key Vault credential cache**: Rejected because it turns
  short-lived secrets into shared persisted material and adds coordination with
  no requirement to share sessions across replicas.
- **Refresh only after `ExpiredToken`**: Rejected because a request or presigned
  grant can cross the expiration boundary and create ambiguous failures.

## Headless IDP integration

**Decision**: Keep the public dispatch entry point provider-neutral while
retaining the existing lifecycle module's AWS SDK implementation during this
hosting migration. Before serialized dispatch, the Azure host rebinds the
module's DynamoDB, S3, and Lambda clients to a botocore refreshable session
obtained through managed identity and STS. This feature does not introduce a
separate `IdpGateway` object or claim that the retained lifecycle module is
boto3-free.

The effective IDP integration remains the pinned headless deployment's S3/event
boundary. After the exact `quarantine/tenants/.../source.pdf` version is
client-complete, malware-clean, and PDF validated, the retained AWS upload
processor copies that version to the IDP input bucket with the pinned screening
configuration metadata. The IDP postprocessor handles screening completion,
deterministic page selection, selected-PDF materialization, full-extraction
submission, and final pinned result recording outside the quarantine prefix.
The Azure API reads status from the shared registry and retrieves or grants only
the exact stored S3 artifact version.

Do not deploy or call AppSync for this feature. The repository's `--headless`
template transformation removes AppSync, and the stock `uploadDocument` mutation
does not provide the required IAM service-to-service upload path. Do not enable
the optional Jobs REST API: it is a private VPC/Cognito interface with a
ZIP-in/results-ZIP-out contract and is not the existing single-PDF two-pass
integration. Upstream object keys, S3 versions, and workflow ARNs are recorded as
explicit mappings and never substituted for the platform `documentId` or
`processingExecutionId`.

**Rationale**: The current direct S3 path is already the proven integration used
by both screening and full extraction. It preserves GuardDuty's event boundary,
exact-version validation, the supplied two-pass configuration, and postprocessor
provenance without adding another identity system or an upstream UI API. A
separately specified repository/gateway refactor can still add a future pinned
upstream API adapter without changing public domain routes.

**Alternatives considered**:

- **AppSync with IAM**: Rejected for the initial deployment. Headless mode removes
  it, its useful reads duplicate the registry, and its stock upload/review fields
  are Cognito-group authorized. Patching those fields would create a maintained
  upstream fork.
- **Optional Jobs REST API**: Rejected because its private networking, Cognito
  OAuth, ZIP contract, and job-level result archive do not match the direct PDF,
  exact-version, two-pass workflow.
- **Direct browser-to-AppSync or browser AWS federation**: Prohibited. It would
  bypass Azure's per-loan authorization and expose an AWS application surface.
- **Azure calls the IDP state machine directly**: Rejected because it bypasses
  the supported input trigger, validation ordering, and configuration metadata.

## Migration and cutover

**Decision**: Use one behavior-preserving registry and one Azure API; never run
permanent dual writes.

1. Add a host-callable dispatch seam around the existing loan/document/archive
   state machine, rebind its DynamoDB/S3/Lambda clients to the refreshable Azure
   workload session, and preserve identifiers, key layout, conditional
   expressions, idempotency records, response shapes, and exact-version rules.
2. Run the existing contract and state-machine tests against that retained
   lifecycle dispatch, then add FastAPI authorization and HTTP contract tests.
   Use only a synthetic tenant and isolated resources for integration tests.
3. Provision the user-assigned identity, exact Entra OIDC provider/trust, narrow
   AWS runtime role, Container App, observability, custom domain certificate, and
   deployment identity through reviewed scripts. Prove wrong issuer, `aud`, and
   `sub` assumptions fail before production traffic.
4. For a clean deployment, validate Azure against synthetic state before creating
   the public DNS record; no AWS product endpoint exists.
5. For a legacy deployed stack, use a separately reviewed bridge release to
   compare authorized reads, freeze old mutations, drain/reconcile in-flight
   work, and switch the stable hostname. Do not apply the clean-deployment
   private-only AWS template until the cutover is accepted, because doing so
   would remove legacy resources during the preparation phase.
6. Roll back the DNS change from the captured Route 53 snapshot if Azure
   acceptance fails. The current repository does not recreate or use the legacy
   AWS endpoint.
7. Preserve DynamoDB, S3/KMS, GuardDuty, upload/postprocessing functions, alarms,
   backups, and the pinned headless IDP stack through either path.

**Rationale**: Reusing the existing table and contract avoids a business-data
migration and preserves safe replay. Separating extraction, federation proof,
synthetic acceptance, DNS activation, and any legacy cleanup keeps each boundary
explicit without retaining a second deployable product API. The stable public
hostname and Entra API audience prevent an unnecessary browser contract change.

**Alternatives considered**:

- **Big-bang rewrite and data migration**: Rejected because it combines runtime,
  identity, persistence, domain semantics, and IDP integration changes in one
  irreversible event.
- **Dual-write Azure and AWS registries**: Rejected because transaction order,
  archive counters, and idempotency cannot be made atomic across both stores.
- **Permanent active/active Azure and AWS public APIs**: Rejected because it
  retains the unwanted AWS Loan API and doubles the exposed authorization and
  operational surface. A short, controlled rollback window is sufficient.
