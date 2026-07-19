# Deployment and operations runbook

## Prerequisites

- AWS account with administrator rights for the initial bootstrap; ongoing AWS
  delivery uses GitHub OIDC and separate platform/IDP CloudFormation execution
  roles.
- Azure subscription with permission to create a resource group, Azure Container
  Registry, Log Analytics/Application Insights, a Container Apps environment and
  application, a user-assigned managed identity, and Azure Static Web Apps.
- Microsoft Entra tenant with Application/Cloud Application Administrator rights
  and permission to grant tenant consent and managed-identity assignments.
- DNS control for the selected UI and API custom hostnames.
- GitHub account with permission to configure the public repository, branch
  rules, Actions, federated deployment credentials, and protected environments.
- GitHub Copilot code-review entitlement and available premium-request quota;
  review failure blocks merge.
- IAM Identity Center AWS CLI profile with MFA. Do not create an access key.
- Azure CLI login using the intended tenant/subscription. Do not create a client
  secret or download a publish profile.
- PowerShell 7, Git, Python 3.13 for platform validation, a separate Python 3.12
  interpreter for the pinned IDP CLI, Node 22, AWS CLI v2, AWS SAM CLI, Azure
  CLI with Bicep, Docker, and WSL/Ubuntu for upstream IDP tooling.
- Corporate TLS root CA configured for Azure/AWS/Graph/GitHub tooling when a
  proxy intercepts TLS. Never disable certificate validation.

## Deployment identity separation

The deployment uses four separate trust paths:

1. operator to Azure/AWS for one-time bootstrap;
2. GitHub OIDC to Azure for reviewed Azure resource and revision deployment;
3. GitHub OIDC to AWS for reviewed private AWS/IDP stack deployment;
4. Azure Container Apps managed identity to AWS STS for product runtime access.

The runtime identity cannot deploy infrastructure. GitHub deployment identities
cannot act as the product runtime. The browser receives none of these
credentials.

AWS delivery is deliberately split again: GitHub may create or update only the
configured platform and IDP stack ARNs and may pass only the corresponding
execution role to CloudFormation. The platform execution role is resource-scoped
to the named platform resources. The pinned IDP role retains the upstream
service-level wildcards that cannot safely be reduced without breaking nested
stack/custom-resource deployment, but it is isolated from the platform role,
limited to the configured IDP stack-name family, and cannot create an unbounded
IAM role. Every created role must carry its stack-specific permissions boundary;
boundary removal is explicitly denied, AWS managed-policy attachment is
allowlisted, and `iam:PassRole` is limited to approved service principals.

## Clean-environment deployment order

1. Create an ignored `config/environments/prod.json` from the example and supply
   the AWS account/region/profile, Azure tenant/subscription/resource group/
   region, DNS names, Entra display names/administrators, alert contacts, limits,
   retention, and stack/resource names. Do not commit the filled file.
   `environment` is an exact enum (`dev`, `test`, `stage`, or `prod`); do not
   substitute `production` or an ad hoc suffix. UI and API hostnames must be
   canonical lowercase DNS names without a trailing dot.
   Keep `maximumQueryItems`, `maximumLoanArchiveDocuments`, and
   `maximumLoanArchiveManifestBytes` at reviewed values; deployment and runtime
   validation reject out-of-range or internally inconsistent limits.
2. Run `scripts/bootstrap.ps1 -InstallMissing`, then run it with the ignored
   environment file to verify the selected AWS account, Azure subscription, and
   Entra tenant.
3. Run `scripts/provision-github.ps1` to configure the public repository,
   protected environment, exact GitHub-to-AWS OIDC trust, AWS execution role,
   and encrypted artifact bucket.
4. Push the reviewed baseline to `main`, then run
   `scripts/configure-github-protection.ps1` to configure validation, exact-head
   Copilot review, resolved conversations, and the reviewer-gated `prod`
   environment.
5. Run `scripts/provision-entra.ps1` for the product API, SPA, and optional
   certificate-authenticated external machine client.
6. Run `scripts/deploy-azure.ps1 -FoundationOnly` for the Azure foundation. It creates the
   user-assigned runtime identity, registry, Container Apps environment,
   observability resources, Static Web App, Azure budget, and safe outputs; it
   deliberately creates no public Container App yet.
7. Run `scripts/provision-entra-federation.ps1` to create the dedicated v1
   federation audience and assign only the API managed identity. Then run
   `scripts/provision-github-azure.ps1` for exact GitHub environment OIDC and
   exact ACR/resource-group scopes, followed by `scripts/sync-github-entra.ps1`.
8. On a first installation only, run
   `scripts/deploy-platform.ps1 -AllowMissingIdp` to deploy the private AWS
   data/processing runtime and the
   exact Azure-workload OIDC provider/role trust before the IDP stack exists.
   Normal reruns omit `-AllowMissingIdp` and fail closed unless the IDP stack and
   every required output can be resolved. The platform retains DynamoDB,
   S3/KMS, GuardDuty, processors, queues, backups, and alarms but does not deploy
   API Gateway or a Loan API Lambda.
9. Run `scripts/deploy-idp.ps1` to deploy the pinned `--headless` IDP stack and
   upload/activate `cd-screen-v1` and `cd-full-v1`. The script creates an
   ABI-qualified Python 3.12 virtual environment because IDP 0.5.16 pins NumPy
   1.26.4; it rejects Python 3.13 rather than compiling or changing that upstream
   dependency. AppSync and Jobs REST are not enabled.
10. Re-run `scripts/deploy-platform.ps1` without `-AllowMissingIdp`. This pass is
    required: it binds processor environment/IAM values to the deployed IDP
    buckets, KMS key, and state machine, then verifies the stored CloudFormation
    parameters and conditional IDP event resources.

Both AWS deployment scripts apply and read back
`infra/stack-policies/protect-stateful-resources.json` before an existing stack
update and after deployment. The policy permits ordinary in-place changes but
blocks replacement or update-time deletion of every S3 bucket, DynamoDB table,
and KMS key. If a reviewed migration genuinely requires replacement, stop the
standard workflow, verify backups/restores and the exact change set, use a
separately approved temporary stack-policy override, and immediately restore
and re-verify the committed policy. Never weaken the committed policy as part of
an ordinary release.
11. Build the Azure API container, resolve the immutable digest, and deploy a
    Container Apps revision through `scripts/deploy-azure.ps1 -BindCustomDomain`.
    The script invokes the repository-owned `infra/azure/acr-build-api.yml`
    multi-step ACR task. That task explicitly enables BuildKit, builds from
    `services/azure_api/Dockerfile`, and performs a separate push because ACR
    multi-step builds do not push implicitly. CI enables the same BuildKit mode.
    Its `/ready` probe obtains a real managed-identity token,
    verifies exact `aud`/`sub`, and completes AWS STS federation before the
    revision is accepted. The validation TXT/certificate step does not change
    production traffic DNS. Before the revision update, deployment fails if it
    finds an unexpected custom hostname and preserves the exact existing SNI
    binding. A prior interrupted run may resume its exact `Disabled`/no-certificate
    binding only with `-BindCustomDomain`; managed-certificate issuance is
    bounded and must reach `Succeeded` before binding.
12. Probe `/health` and `/ready` on the default Container Apps hostname. Run
    negative federation/authorization tests and authenticated `/v1` synthetic
    smoke through the bound custom hostname using reviewed host-pinned TLS
    resolution before DNS changes. Production rejects product routes on the
    provider hostname. The deployment script's pinned Trivy gate scans the
    exact ACR digest, emits an ignored CycloneDX SBOM, and blocks the revision
    and traffic cutover on fixable HIGH/CRITICAL findings.
13. Run `scripts/cutover-api-domain.ps1`; it snapshots exact Route 53 records,
    changes the CNAME, tests HTTPS/deep readiness, and automatically restores
    prior records on failure.
14. After the React scaffold exists, run `scripts/deploy-web.ps1` to build/test
    and publish to Azure Static Web Apps. `-BindCustomDomain` performs an explicit
    UI CNAME cutover with rollback and writes a public `runtime-config.json` that
    points only to the accepted Azure API hostname. Every production UI publish
    re-reads the live Container Apps binding and certificate, verifies the exact
    Route 53 CNAME target, and passes `/health` plus `/ready` on the custom API
    hostname before uploading the build.

While the React scaffold is absent, run `scripts/deploy-all.ps1` with
`-SkipAzureFoundation`, `-SkipEntra`, `-SkipWeb`, and
`-BindApiCustomDomain` to orchestrate steps 8–13 after the one-time identity
bootstrap, then invoke
`scripts/cutover-api-domain.ps1` only after the acceptance pause. Once the UI
exists, omit `-SkipWeb` and use `-BindUiCustomDomain` only for its separately
reviewed cutover. The orchestration must stop on
missing administrator consent, DNS validation, trust evidence, or production
approval rather than inventing or printing sensitive values.

## Cross-cloud federation acceptance

Before production traffic, prove all of the following:

- the Container App's user-assigned identity can request the dedicated
  federation-audience token;
- exact issuer, audience, and managed-identity subject can assume only the named
  AWS runtime role;
- wrong issuer, audience, subject, tenant, token version, and expired token are
  rejected by AWS;
- the assumed role can access only the named DynamoDB table/indexes, approved S3
  prefixes, and required KMS operations;
- arbitrary table/bucket listing, IAM/CloudFormation mutation, AppSync, Cognito,
  state-machine direct invocation, and unrelated KMS use are denied;
- temporary credentials expire, refresh before the safety window, and are absent
  from configuration, logs, traces, crash output, and deployment output;
- a presigned grant cannot outlive the configured grant window or effective STS
  credential lifetime.

Record pass/fail evidence and safe identifiers only. Never record a token,
temporary credential, signed URL, document, filename, or extracted value.

## In-place migration from the AWS public API

The migration reuses DynamoDB and S3; it does not copy business data or run
Azure/AWS dual writes. The steps below describe a legacy migration only; a clean
deployment has no prior AWS product endpoint and begins at the production release
gates after completing the staged deployment above.

1. Back up the DynamoDB table and verify S3 versioning/retention before changing
   traffic.
2. Use a separately reviewed bridge release to deploy Azure with mutations
   disabled or restricted to a synthetic tenant while leaving the historical
   AWS application stack unchanged.
3. Run the canonical contract and authorization suites against Azure. Compare
   authorized reads and safe status metadata with the existing endpoint.
4. Prove upload initialization/completion, exact-version malware processing,
   IDP status, artifact grants, archive/recreate, concurrency, and idempotent
   retry with synthetic data.
5. Lower DNS TTL in advance. Freeze new mutations at the former AWS endpoint,
   drain requests, and reconcile pending upload completion, malware, outbox, and
   IDP workflow records.
6. Switch `api.loans.<domain>` DNS and the SPA runtime configuration to Azure.
   Never expose a second product hostname to users.
7. Observe Azure API, federation, DynamoDB, S3, GuardDuty, processors, IDP,
   certificate, latency, and denial signals through the approved rollback
   window. Baseline IaC supplies Azure 5xx/latency and AWS processor/DLQ/
   DynamoDB alarms; cutover remains blocked until the additional environment-
   specific alert rules and on-call delivery listed in the readiness checklist
   are configured and tested.
8. If rollback is necessary, stop Azure mutations before restoring the captured
   historical DNS target. Both releases use the same registry; no data restore
   or merge occurs.
9. After acceptance, remove the historical AWS Loan API Lambda, API Gateway, authorizer,
   CloudFront API/UI distributions, API origin/custom domain, origin secret, and
   obsolete runtime role through a separately reviewed cleanup, then adopt the
   current private-only template. Retain the stateful/private AWS data plane and
   headless IDP resources. The current repository cannot recreate the historical
   public endpoint.

Permanent active/active Azure and AWS public APIs are prohibited.

## Production release gates

- Repository validation, unit/contract/auth/federation tests, per-file and
  aggregate coverage, OpenAPI validation, Bicep build, CloudFormation lint,
  PowerShell parsing, container build/scan, and applicable Playwright tests pass.
- Copilot has reviewed the exact current pull-request head; every sound finding
  is fixed, rejected suggestions have evidence, feedback commits are re-reviewed,
  and every actionable conversation is resolved.
- No PDFs, private keys, `.env`, tenant/account deployment files, OCR, extraction
  output, token, credential, signed URL, or deployment output is in Git or CI
  artifacts.
- The same tested image digest and SPA artifact are promoted; production is not
  rebuilt from different source.
- IDP configuration digests and the upstream commit match the release manifest.
- Bicep what-if and CloudFormation change sets are reviewed. Stateful replacement,
  wildcard trust, or public AWS API creation blocks deployment.
- The platform and IDP stacks use different CloudFormation execution roles, all
  created runtime roles show the expected permissions-boundary ARN, and both
  stacks return the committed stateful-resource stack policy.
- Entra redirect URIs, audience, scopes/roles, client allow/deny lists, federation
  application, managed-identity assignment, and exact AWS trust conditions match
  the target environment.
- Azure custom domains/certificate states and DNS records are healthy, with no
  production localhost or provider-default redirect URI. Production `/v1`
  rejects the provider hostname; only `/health` and `/ready` remain available
  there for deployment probes.
- `azureApiConcurrentRequestsPerReplica` remains exactly `1`; configuration and
  Bicep reject any other value while the module-global AWS domain seam is
  protected by `domain_lock`. This HTTP threshold prompts earlier scale-out and
  minimizes head-of-line waiting, but it is not a hard admission cap. Confirm
  queued-request latency and replica growth under synthetic load, retain the
  reviewed `azureApiMaxReplicas`, and rely on the lock for serialization.
- Synthetic smoke-test spend is bounded. The configured AWS and Azure USD budget
  notifications are active; budgets alert after cost is recorded and are not a
  hard spending stop.

The GitHub production workflow is manual and uses the protected `prod`
environment. Pull requests and fork validation receive no production cloud role.

## Operational responses

### Azure API unavailable or unhealthy

Inspect Container Apps revision health, readiness, replica count, recent rollout,
resource limits, dependencies, and sanitized Application Insights telemetry.
The HTTP concurrency target of `1` is only a scale-out signal; it does not prove
that a replica admitted at most one request. Inspect head-of-line latency at the
serialized domain lock together with replica growth and the configured maximum.
Route traffic only to an already accepted immutable revision. Do not enable the
former AWS public API while Azure still accepts mutations.

### AWS federation failure

Compare safe configuration fingerprints for Azure tenant, issuer, federation
audience, managed-identity principal, OIDC provider, role ARN, and trust-policy
version. Check Entra sign-in logs, CloudTrail denial reason, regional STS health,
and credential-refresh alarms. Never print the workload token or fall back to an
access key. A wrong claim or disabled trust fails closed.

### Upload stuck in `AWAITING_UPLOAD` or `VALIDATING`

Check the upload expiration, exact source bucket/key/version, client-complete
record, GuardDuty version-specific result, EventBridge delivery, processor DLQ,
validation lease, and reconciliation state. Do not manually copy unscanned
content into IDP. A clean result for another `VersionId` cannot advance.

### IDP status stuck in `QUEUED`, `SCREENING`, or `EXTRACTING`

Inspect the version-pinned screen/full input mapping, Step Functions execution
ARN, active-workflow marker, postprocessor events/DLQ, and reconciliation alarm.
The platform `processingExecutionId`, IDP object key, execution ARN, and S3
version must remain distinct. Reprocessing creates attributable upstream
evidence and does not overwrite prior provenance.

### Selection enters `HOLD`

Review candidate page IDs and safe reason codes through an approved workflow.
Do not edit model output in place. A manual decision is a new audited rule/
decision version and only its approved selected pages may enter the full pass.

### Restore

DynamoDB PITR restores to a new table; S3 versions remain immutable. Restore the
table, reconcile its configured table ARN/name and role permissions, validate
archive/object references, then switch configuration only after approval. Run a
restore exercise before launch and at least annually.

Azure infrastructure is recreated from Bicep and immutable images; it is not the
business-data backup. Replacing the user-assigned identity changes the AWS trust
subject and requires an explicit trust migration.

### Public certificate renewal

Monitor Azure-managed certificate and DNS validation state. Repair DNS or domain
ownership evidence before renewal becomes critical. If policy uses a Key Vault
certificate, rotate with overlap and bind the new version to a canary revision
before promotion. Do not export a private key into the repository or workflow.

### Managed identity or trust rotation

Provision a new user-assigned identity and federation audience/trust in parallel,
verify exact token claims, deploy a canary revision, run allowed/denied AWS and
end-to-end smoke tests, then shift traffic. Disable the old role trust and
identity after the observation window. Confirm old sessions expire and remove
old configuration. An emergency response may disable the AWS role immediately
to stop new sessions.

### External machine-client certificate rotation

Register the next public certificate, canary token acquisition and a harmless
authorized read, promote the new private key only in the owning workload,
observe, remove the old Entra credential, and destroy the old key. Test this
procedure before the first production expiration.
