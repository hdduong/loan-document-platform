[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$EnvironmentFile,
    [string]$RepositoryOwner,
    [string]$RepositoryName,
    [string]$DefaultBranch = 'main',
    [string]$DeploymentEnvironment = 'prod',
    [string]$DeploymentReviewer
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

if ($EnvironmentFile) {
    $config = Read-EnvironmentConfig -Path $EnvironmentFile
    $RepositoryOwner = $config.githubOwner
    $RepositoryName = $config.repositoryName
    $DefaultBranch = $config.githubDefaultBranch
    $DeploymentEnvironment = $config.githubEnvironment
    $DeploymentReviewer = $config.githubDeploymentReviewer
}

foreach ($required in @{
        RepositoryOwner = $RepositoryOwner
        RepositoryName = $RepositoryName
        DefaultBranch = $DefaultBranch
        DeploymentEnvironment = $DeploymentEnvironment
    }.GetEnumerator()) {
    if ([string]::IsNullOrWhiteSpace([string]$required.Value)) {
        throw "Missing $($required.Key). Supply -EnvironmentFile or the direct GitHub parameters."
    }
}
if ([string]::IsNullOrWhiteSpace($DeploymentReviewer)) {
    $DeploymentReviewer = $RepositoryOwner
}

$repository = "$RepositoryOwner/$RepositoryName"

Assert-Command -Name gh -InstallHint 'Run scripts/bootstrap.ps1 -InstallMissing.'
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }

function Invoke-GitHubRequest {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'PUT', 'PATCH', 'POST', 'DELETE')][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [hashtable]$Body
    )

    if ($PSBoundParameters.ContainsKey('Body')) {
        $json = $Body | ConvertTo-Json -Depth 20 -Compress
        $output = $json | & gh api -H 'X-GitHub-Api-Version: 2026-03-10' --method $Method $Path --input -
    } else {
        $output = & gh api -H 'X-GitHub-Api-Version: 2026-03-10' --method $Method $Path
    }
    if ($LASTEXITCODE -ne 0) { throw "GitHub API request failed: $Method $Path" }
    return $output
}

$repositoryState = Invoke-GitHubRequest -Method GET -Path "repos/$repository" | ConvertFrom-Json
if ($repositoryState.visibility -cne 'public') {
    throw "Repository '$repository' must be public for this reviewed delivery model; received '$($repositoryState.visibility)'."
}

Invoke-GitHubRequest -Method GET -Path "repos/$repository/branches/$DefaultBranch" | Out-Null
$reviewer = Invoke-GitHubRequest -Method GET -Path "users/$DeploymentReviewer" | ConvertFrom-Json

if ($PSCmdlet.ShouldProcess($repository, 'Harden repository, Actions, branch, Copilot review, security reporting, and deployment environment settings')) {
    Invoke-GitHubRequest -Method PATCH -Path "repos/$repository" -Body @{
        allow_merge_commit = $false
        allow_rebase_merge = $false
        allow_squash_merge = $true
        allow_update_branch = $true
        delete_branch_on_merge = $true
        has_discussions = $false
        has_issues = $false
        has_projects = $false
        has_wiki = $false
    } | Out-Null

    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/actions/permissions/workflow" -Body @{
        default_workflow_permissions = 'read'
        can_approve_pull_request_reviews = $false
    } | Out-Null
    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/actions/permissions" -Body @{
        enabled = $true
        allowed_actions = 'selected'
    } | Out-Null
    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/actions/permissions/selected-actions" -Body @{
        github_owned_allowed = $true
        verified_allowed = $false
        patterns_allowed = @(
            'aws-actions/*',
            'azure/login@a457da9ea143d694b1b9c7c869ebb04ebe844ef5'
        )
    } | Out-Null

    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/environments/$DeploymentEnvironment" -Body @{
        reviewers = @(@{ type = 'User'; id = $reviewer.id })
        prevent_self_review = $false
        wait_timer = 0
        deployment_branch_policy = @{
            protected_branches = $false
            custom_branch_policies = $true
        }
    } | Out-Null

    $policyPath = "repos/$repository/environments/$DeploymentEnvironment/deployment-branch-policies"
    $policyResponse = Invoke-GitHubRequest -Method GET -Path $policyPath | ConvertFrom-Json
    $matchingPolicy = $null
    foreach ($policy in @($policyResponse.branch_policies)) {
        if ($policy.name -ceq $DefaultBranch -and $policy.type -ceq 'branch') {
            $matchingPolicy = $policy
            continue
        }
        Invoke-GitHubRequest -Method DELETE -Path "$policyPath/$($policy.id)" | Out-Null
    }
    if ($null -eq $matchingPolicy) {
        Invoke-GitHubRequest -Method POST -Path $policyPath -Body @{
            name = $DefaultBranch
            type = 'branch'
        } | Out-Null
    }

    $copilotRulesetName = 'Mandatory Copilot review'
    $copilotRuleset = @{
        name = $copilotRulesetName
        target = 'branch'
        enforcement = 'active'
        bypass_actors = @()
        conditions = @{
            ref_name = @{
                include = @("refs/heads/$DefaultBranch")
                exclude = @()
            }
        }
        rules = @(
            @{
                type = 'copilot_code_review'
                parameters = @{
                    review_draft_pull_requests = $true
                    review_on_push = $true
                }
            }
        )
    }
    $repositoryRulesets = @(
        Invoke-GitHubRequest -Method GET -Path "repos/$repository/rulesets" |
            ConvertFrom-Json
    )
    $matchingRulesets = @($repositoryRulesets | Where-Object { $_.name -ceq $copilotRulesetName })
    if ($matchingRulesets.Count -gt 1) {
        throw "Multiple '$copilotRulesetName' rulesets exist for $repository; reconcile them manually."
    }
    if ($matchingRulesets.Count -eq 1) {
        Invoke-GitHubRequest -Method PUT -Path "repos/$repository/rulesets/$($matchingRulesets[0].id)" -Body $copilotRuleset | Out-Null
    } else {
        Invoke-GitHubRequest -Method POST -Path "repos/$repository/rulesets" -Body $copilotRuleset | Out-Null
    }

    $protection = @{
        required_status_checks = @{
            strict = $true
            contexts = @('validate', 'copilot-review')
        }
        enforce_admins = $true
        required_pull_request_reviews = @{
            dismiss_stale_reviews = $true
            require_code_owner_reviews = $false
            require_last_push_approval = $false
            required_approving_review_count = 0
        }
        restrictions = $null
        required_conversation_resolution = $true
        required_linear_history = $true
        allow_force_pushes = $false
        allow_deletions = $false
        block_creations = $false
        lock_branch = $false
        allow_fork_syncing = $true
    }
    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/branches/$DefaultBranch/protection" -Body $protection | Out-Null

    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/vulnerability-alerts" | Out-Null
    Invoke-GitHubRequest -Method PUT -Path "repos/$repository/private-vulnerability-reporting" | Out-Null

    $forkPolicyJson = @{ approval_policy = 'all_external_contributors' } |
        ConvertTo-Json -Depth 5 -Compress
    $forkPolicyOutput = $forkPolicyJson |
        & gh api --method PUT "repos/$repository/actions/permissions/fork-pr-contributor-approval" --input - 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "GitHub did not accept the external-fork approval policy. Configure 'Require approval for all outside collaborators' manually. Response: $forkPolicyOutput"
    }
}

Write-Host "GitHub protection configured for $repository."
Write-Host "Production is reviewer-gated and restricted to the exact '$DefaultBranch' branch; pull requests require validation and an exact-head Copilot review without a self-approval deadlock."
