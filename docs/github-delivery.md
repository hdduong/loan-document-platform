# GitHub delivery model

GitHub is the public source host. Azure runs the SPA and product API; AWS runs
the private registry, object, malware, processor, and headless-IDP data plane.
Public repository visibility provides standard GitHub-hosted Actions and public-
repository deployment protection on GitHub Free. Public source does not make
mortgage data public: customer documents and every derived value are prohibited
from the repository and CI.

Claude Code works from this monorepo and follows `CLAUDE.md`,
`apps/web/CLAUDE.md`, `docs/ui-handoff.md`, and the canonical OpenAPI contract.

## One-time bootstrap

`scripts/provision-github.ps1` performs the privileged GitHub/AWS bootstrap:

1. optionally creates `<githubOwner>/aws-idp-custom-platform` with the configured
   visibility (`public` for this deployment);
2. creates or verifies GitHub's OIDC trust in AWS;
3. creates an AWS deployment role restricted to the exact repository and GitHub
   environment subject, a separate CloudFormation execution role, and an
   encrypted build-artifact bucket;
4. writes only non-secret AWS/GitHub environment variables such as role, region,
   account, stack, and artifact identifiers;
5. optionally adds the GitHub URL as `origin`; it never pushes code automatically.

After `deploy-azure.ps1 -FoundationOnly`,
`scripts/provision-github-azure.ps1` creates the separate Entra application and
exact GitHub environment federated credential, assigns the deployment role at
the target resource group, and assigns ACR Push/Pull only at the named registry.
The deployer cannot write role assignments, create credentials, or administer
the subscription or tenant.

No CloudFront-to-origin verification secret is created because the product API
is hosted on Azure Container Apps and there is no AWS public API origin.

After Entra provisioning, `scripts/sync-github-entra.ps1` publishes only public
tenant/API/SPA/federation application identifiers required by deployment. Entra
credentials, managed-identity tokens, TLS private keys, AWS credentials, and
certificate private keys are never GitHub variables or secrets.

After the reviewed initial commit is on `main`, configure repository protection:

```powershell
./scripts/configure-github-protection.ps1 `
  -RepositoryOwner hdduong `
  -RepositoryName aws-idp-custom-platform `
  -DefaultBranch main `
  -DeploymentEnvironment prod `
  -DeploymentReviewer hdduong
```

The script configures read-only default workflow permissions, selected Actions,
squash-only merges, required pull requests, validation plus exact-head Copilot
review gates, resolved conversations, private vulnerability reporting,
vulnerability alerts, and a reviewer-gated production environment restricted to
the exact `main` branch.

A single-owner repository uses zero required human approvals and does not require
CODEOWNER or last-push approval because an author cannot approve their own pull
request. Add a second maintainer before raising the approval count and enabling
required CODEOWNER review.

## Mandatory exact-head Copilot loop

The active `Mandatory Copilot review` ruleset requests Copilot review for draft
pull requests and again after every push. The `copilot-review` workflow waits for
a Copilot `COMMENTED` review whose `commit_id` matches the current head.

Copilot is advisory and never counts as an approving reviewer. Every actionable
comment consistent with the constitution must be fixed and tested; an
inapplicable or harmful suggestion must receive concrete evidence before its
conversation is resolved. A feedback fix creates a new SHA and restarts the
review loop. Quota exhaustion, timeout, or service outage fails closed.

## Required repository controls

- Protect `main`; require `validate`, exact-head `copilot-review`, and resolved
  conversations.
- Keep automatic Copilot review active for drafts and every new push.
- Restrict production deployment to the exact `main` branch through the custom
  `prod` environment branch policy. A generic protected-branch condition is not
  sufficient because deployment OIDC subjects use the environment.
- Require an explicit production-environment reviewer.
- Let fork pull requests run only read-only validation, with no protected
  environment, secret, Azure permission, or AWS permission.
- Keep workflow permissions read-only by default. Only production deployment
  jobs request `id-token: write` and the minimum package permission needed for
  immutable container artifacts.
- Pin every third-party action to a reviewed immutable revision and let
  Dependabot propose updates.
- Enable dependency vulnerability alerts and private vulnerability reporting;
  never request security disclosures in public issues.
- Never upload a PDF, OCR/extraction fixture, signed URL, token, temporary
  credential, cloud output, or filled environment file as an artifact.

## GitHub-to-AWS trust

The AWS deployment role requires both:

```text
aud = sts.amazonaws.com
sub = repo:<owner>/<repository>:environment:<environment>
```

GitHub can deliver reviewed private AWS/IDP CloudFormation changes and pass only
the named execution role. CloudFormation owns application-resource mutations.
The GitHub role is not the Azure API runtime role and cannot satisfy the
Azure-managed-identity `sub` condition.

## GitHub-to-Azure trust

The Entra federated identity credential requires:

```text
issuer   = https://token.actions.githubusercontent.com
audience = api://AzureADTokenExchange
subject  = repo:<owner>/<repository>:environment:<environment>
```

The workflow exchanges its GitHub OIDC token for short-lived Azure credentials.
GitHub stores no Azure client secret, certificate, or publish profile. The
deployment principal is scoped to the target subscription/resource group and
only the identity-assignment permissions explicitly needed by Bicep and the
release scripts.

Production, staging, and development use different federated credentials and
resource scopes even when they share the repository.

## Runtime trust is separate

The Container App's user-assigned managed identity uses a different Entra token
and AWS trust:

```text
issuer = https://sts.windows.net/<tenant-id>/
aud    = <dedicated AWS-federation Application ID URI>
sub    = <Azure API managed identity principal/object ID>
```

This runtime relationship is provisioned by Azure/Entra/AWS IaC, not GitHub
environment secrets. A GitHub deployment token cannot assume the runtime role,
and a Container App runtime token cannot deploy infrastructure.

## Production delivery sequence

1. Read-only pull-request validation runs Python/service tests, per-file and
   aggregate coverage, OpenAPI validation, PowerShell parsing, Bicep build,
   CloudFormation lint, UI build/unit/Playwright tests when present, repository
   invariants, and secret/dependency checks.
2. Exact-head Copilot review and every required check complete before merge.
3. The manual production workflow verifies it is running from `main`, enters the
   reviewer-protected `prod` environment, and validates the sanitized deployment
   configuration.
4. GitHub obtains independent short-lived Azure and AWS credentials through the
   two exact environment OIDC trusts.
5. The workflow installs the repository-pinned Trivy version, builds the Azure
   API image in ACR, resolves its immutable digest, and scans that exact digest.
   Fixable HIGH/CRITICAL findings block the revision, and digest-matched scan/SBOM
   evidence is required again before production traffic cutover. The SPA is
   built and tested when present.
6. Reviewed AWS changes update only the private data/processing runtime and
   pinned headless IDP. They do not create API Gateway, an AWS Loan API Lambda,
   public AppSync, Jobs REST, or CloudFront UI/API distributions.
7. Reviewed Azure changes deploy the user-assigned identity, Container Apps
   environment/revision, observability, custom API hostname/certificate, Static
   Web App, custom UI hostname, and public runtime configuration.
8. Container readiness proves a live managed-identity-to-STS exchange. Synthetic
   negative federation tests and the authenticated Entra/Azure/S3/IDP smoke pass
   before the explicit rollback-capable DNS cutover is accepted.

## Repository rename and trust updates

Renaming the repository changes both Azure and AWS GitHub OIDC subjects. If a
trust was deployed under an earlier slug, update the ignored environment
configuration and rerun the bootstrap before another deployment. Do not leave
the old subject trusted. The canonical repository is
`hdduong/aws-idp-custom-platform`.

## React UI location

Claude Code owns `apps/web`. It must not invent a second backend contract or
embed deployment-specific identifiers at build time. Azure Static Web Apps
serves a generated `runtime-config.json` containing only public values such as
the Azure API base URL, Entra tenant/client IDs, requested delegated scopes,
build SHA, and upload limit.
