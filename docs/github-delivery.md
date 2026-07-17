# GitHub delivery model

The source repository is public GitHub; AWS is the runtime, not the source-code host. Public visibility provides free standard GitHub-hosted Actions and public-repository deployment protection on GitHub Free. Claude Code works from the same monorepo and follows `CLAUDE.md`, `apps/web/CLAUDE.md`, `docs/ui-handoff.md`, and the OpenAPI contract.

## Bootstrap

`scripts/provision-github.ps1` performs the one-time privileged bootstrap:

1. Optionally creates `<githubOwner>/aws-idp-custom-platform` with the configured visibility (`public` for this deployment).
2. Reuses the account's GitHub OIDC provider when one exists, otherwise creates it.
3. Creates a GitHub deployment role whose trust is limited to the exact repository and GitHub environment subject.
4. Creates a separate CloudFormation execution role and encrypted build-artifact bucket.
5. Writes only non-secret GitHub environment variables, including role ARNs and the deployment configuration JSON.
6. With `-GenerateInitialOriginVerifySecret`, creates the protected CloudFront-to-origin verification value directly in GitHub without displaying or writing it locally.
7. Optionally adds the GitHub URL as `origin`; it never pushes code automatically.

After Entra provisioning, `scripts/sync-github-entra.ps1` publishes only the tenant/API/SPA/service application identifiers needed by deployment. Entra credentials and certificate private keys are never GitHub variables or secrets.

After the reviewed initial commit is on `main`, run `scripts/configure-github-protection.ps1`. It can use the complete environment file or only the GitHub identifiers, for example:

```powershell
./scripts/configure-github-protection.ps1 `
  -RepositoryOwner hdduong `
  -RepositoryName aws-idp-custom-platform `
  -DefaultBranch main `
  -DeploymentEnvironment prod `
  -DeploymentReviewer hdduong
```

The script configures read-only default workflow permissions, selected Actions, squash-only merges, required pull requests, validation plus exact-head Copilot review gates, resolved conversations, private vulnerability reporting, vulnerability alerts, and a reviewer-gated production environment restricted to the exact `main` branch. A single-owner repository uses zero required human approvals and does not require CODEOWNER or last-push approval, because GitHub does not allow an author to approve their own pull request. Add a second maintainer before raising the approval count to one and enabling required CODEOWNER review.

The active `Mandatory Copilot review` branch ruleset requests Copilot review for draft pull requests and again after every push. The `copilot-review` workflow check waits for a Copilot `COMMENTED` review whose `commit_id` matches the current pull-request head. Copilot never counts as an approving reviewer, so this is a required advisory review in addition to validation and human judgment. Every actionable comment that is consistent with the constitution MUST be fixed; an inapplicable or harmful suggestion MUST be answered with evidence and resolved. A fix creates a new head SHA, which starts the review loop again. Quota exhaustion or a Copilot outage fails closed rather than silently bypassing review.

The initial operator uses an IAM Identity Center profile. Subsequent GitHub jobs exchange an OIDC token for short-lived AWS credentials. Long-lived AWS access keys are not GitHub secrets.

## Required repository controls

- Protect `main`; require `validate`, the exact-head `copilot-review` check, and resolved review conversations.
- Keep the `Mandatory Copilot review` ruleset active for drafts and every new push. Do not merge until Copilot has reviewed the current head SHA and all actionable feedback is addressed.
- Restrict production deployment to the exact `main` branch through a custom `prod` environment branch policy. Using “protected branches” is insufficient because the OIDC subject contains the environment, not the source branch.
- Require explicit approval on the `prod` environment. Public repositories support environment secrets and deployment protection on GitHub Free.
- Let fork pull requests run only the read-only validation workflow, with no environment, secret, or AWS access. Require approval for every external contributor's fork workflow when the repository endpoint supports that policy.
- Keep workflow permissions read-only by default. Only the production deployment job requests `id-token: write`.
- Pin third-party actions to reviewed versions and let Dependabot propose updates.
- Enable dependency vulnerability alerts and private vulnerability reporting; never request security disclosures in public issues.

## Trust policy

The AWS role requires both:

```text
aud = sts.amazonaws.com
sub = repo:<owner>/<repository>:environment:<environment>
```

Pull requests and arbitrary repositories therefore cannot obtain the deployment role. AWS permissions are split: GitHub can deliver reviewed CloudFormation changes and pass only the named execution role; CloudFormation owns the application resource mutations.

Renaming the repository changes the OIDC `sub`. If an AWS bootstrap stack was deployed under an earlier slug, update the ignored environment configuration and rerun `scripts/provision-github.ps1` before attempting another deployment. The current canonical repository is `hdduong/aws-idp-custom-platform`.

## React UI location

Claude Code owns `apps/web`. It must not invent a second backend contract or embed deployment-specific identifiers at build time. The deployed UI reads a generated `runtime-config.json` containing only public values such as API base URL, Entra tenant/client IDs, and requested delegated scopes.
