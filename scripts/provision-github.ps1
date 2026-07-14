[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [switch]$CreateRepository,
    [switch]$ConfigureOrigin,
    [switch]$GenerateInitialOriginVerifySecret
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force
$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$repository = "$($config.githubOwner)/$($config.repositoryName)"

Assert-Command -Name gh -InstallHint 'Run scripts/bootstrap.ps1.'
Assert-Command -Name git -InstallHint 'Run scripts/bootstrap.ps1.'
Assert-Command -Name aws -InstallHint 'Run scripts/bootstrap.ps1.'
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }

$identity = Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId

$repoStateJson = & gh repo view $repository --json nameWithOwner,visibility 2>$null
if ($LASTEXITCODE -ne 0) {
    if (-not $CreateRepository) {
        throw "GitHub repository '$repository' does not exist or is inaccessible. Re-run with -CreateRepository after reviewing the owner and name."
    }
    if ($PSCmdlet.ShouldProcess($repository, "Create $($config.githubRepositoryVisibility) GitHub repository")) {
        $visibilityFlag = "--$($config.githubRepositoryVisibility)"
        & gh repo create $repository $visibilityFlag --description 'Entra-protected loan document platform with headless AWS IDP integration.'
        if ($LASTEXITCODE -ne 0) { throw "Failed to create GitHub repository '$repository'." }
    }
} else {
    $repoState = $repoStateJson | ConvertFrom-Json
    if ([string]$repoState.visibility -cne ([string]$config.githubRepositoryVisibility).ToUpperInvariant()) {
        throw "GitHub repository '$repository' is $($repoState.visibility), but the environment requires $($config.githubRepositoryVisibility)."
    }
}

if ($WhatIfPreference) {
    Write-Host "WhatIf complete for GitHub repository and AWS bootstrap stack '$($config.bootstrapStackName)'."
    return
}

$providerArn = "arn:aws:iam::$($identity.Account):oidc-provider/token.actions.githubusercontent.com"
$providerOutput = & aws --profile $config.awsProfile --region $config.awsRegion --no-cli-pager --output json iam get-open-id-connect-provider --open-id-connect-provider-arn $providerArn 2>$null
if ($LASTEXITCODE -ne 0) {
    $providerArn = ''
} else {
    $provider = $providerOutput | Out-String | ConvertFrom-Json
    if ($provider.ClientIDList -notcontains 'sts.amazonaws.com') {
        throw "The existing GitHub OIDC provider does not trust audience 'sts.amazonaws.com'. Review it before continuing."
    }
}

$template = Join-Path $root 'infra\bootstrap\template.yaml'
$parameters = @(
    "GitHubOwner=$($config.githubOwner)",
    "GitHubRepository=$($config.repositoryName)",
    "GitHubEnvironment=$($config.githubEnvironment)",
    "EnvironmentName=$($config.environment)",
    "IdpStackName=$($config.idpStackName)",
    "Route53HostedZoneId=$($config.route53HostedZoneId)"
)
if ($providerArn) { $parameters += "ExistingGitHubOidcProviderArn=$providerArn" }

if ($PSCmdlet.ShouldProcess($config.bootstrapStackName, 'Deploy GitHub OIDC bootstrap stack')) {
    Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments (@(
        'cloudformation', 'deploy',
        '--stack-name', $config.bootstrapStackName,
        '--template-file', $template,
        '--capabilities', 'CAPABILITY_NAMED_IAM',
        '--parameter-overrides'
    ) + $parameters + @(
        '--tags',
        'Application=loan-document-platform',
        "Environment=$($config.environment)"
    )) | Out-Host
}

$outputs = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.bootstrapStackName
if (-not $outputs.GitHubDeploymentRoleArn -or -not $outputs.CloudFormationExecutionRoleArn -or -not $outputs.ArtifactBucketName) {
    throw 'Bootstrap stack outputs are incomplete.'
}

if ($PSCmdlet.ShouldProcess("$repository environment $($config.githubEnvironment)", 'Configure non-secret deployment variables')) {
    $reviewerJson = & gh api "users/$($config.githubDeploymentReviewer)"
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve GitHub deployment reviewer '$($config.githubDeploymentReviewer)'." }
    $reviewer = $reviewerJson | ConvertFrom-Json
    $environmentPolicy = @{
        reviewers = @(@{ type = 'User'; id = $reviewer.id })
        prevent_self_review = $false
        wait_timer = 0
        deployment_branch_policy = @{
            protected_branches = $false
            custom_branch_policies = $true
        }
    } | ConvertTo-Json -Depth 5 -Compress
    $environmentPolicy | & gh api --method PUT "repos/$repository/environments/$($config.githubEnvironment)" --input - | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create or update the GitHub deployment environment.' }

    $policyPath = "repos/$repository/environments/$($config.githubEnvironment)/deployment-branch-policies"
    $policyResponseJson = & gh api $policyPath
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect GitHub deployment branch policies.' }
    $policyResponse = $policyResponseJson | ConvertFrom-Json
    $matchingPolicy = $null
    foreach ($policy in @($policyResponse.branch_policies)) {
        if ($policy.name -ceq $config.githubDefaultBranch -and $policy.type -ceq 'branch') {
            $matchingPolicy = $policy
            continue
        }
        & gh api --method DELETE "$policyPath/$($policy.id)" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to remove stale deployment branch policy '$($policy.name)'." }
    }
    if ($null -eq $matchingPolicy) {
        $branchPolicy = @{
            name = $config.githubDefaultBranch
            type = 'branch'
        } | ConvertTo-Json -Depth 5 -Compress
        $branchPolicy | & gh api --method POST $policyPath --input - | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Failed to restrict deployment to branch '$($config.githubDefaultBranch)'." }
    }

    $ciConfig = Get-Content -Raw -LiteralPath (Resolve-Path -LiteralPath $EnvironmentFile).Path | ConvertFrom-Json -Depth 20
    $ciConfig.awsProfile = 'GITHUB_ACTIONS'
    $ciConfig.corporateCaBundlePath = ''
    $ciConfig.serviceCertificatePublicPath = ''
    $environmentJson = $ciConfig | ConvertTo-Json -Depth 20 -Compress
    $variables = @{
        AWS_REGION = $config.awsRegion
        AWS_ACCOUNT_ID = $config.awsAccountId
        AWS_DEPLOY_ROLE_ARN = $outputs.GitHubDeploymentRoleArn
        AWS_CLOUDFORMATION_EXECUTION_ROLE_ARN = $outputs.CloudFormationExecutionRoleArn
        AWS_ARTIFACT_BUCKET = $outputs.ArtifactBucketName
        DEPLOYMENT_CONFIG_JSON = $environmentJson
    }
    foreach ($entry in $variables.GetEnumerator()) {
        $entry.Value | & gh variable set $entry.Key --repo $repository --env $config.githubEnvironment
        if ($LASTEXITCODE -ne 0) { throw "Failed to set GitHub environment variable '$($entry.Key)'." }
    }
}

if ($GenerateInitialOriginVerifySecret) {
    $existingSecretNames = & gh secret list --repo $repository --env $config.githubEnvironment --json name |
        ConvertFrom-Json | ForEach-Object { $_.name }
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect GitHub environment secrets.' }
    if ($existingSecretNames -contains 'LOAN_API_ORIGIN_VERIFY_SECRET') {
        throw 'LOAN_API_ORIGIN_VERIFY_SECRET already exists. Initial provisioning refuses to rotate it; use a coordinated API/edge rotation workflow.'
    }
    if ($PSCmdlet.ShouldProcess("$repository environment $($config.githubEnvironment)", 'Generate initial origin-verification secret')) {
        $secretBytes = [byte[]]::new(48)
        try {
            [System.Security.Cryptography.RandomNumberGenerator]::Fill($secretBytes)
            $originSecret = [Convert]::ToBase64String($secretBytes)
            $originSecret | & gh secret set LOAN_API_ORIGIN_VERIFY_SECRET --repo $repository --env $config.githubEnvironment
            if ($LASTEXITCODE -ne 0) { throw 'Failed to store the origin-verification secret in the GitHub environment.' }
        } finally {
            [Array]::Clear($secretBytes, 0, $secretBytes.Length)
            $originSecret = $null
        }
        Write-Host 'Generated the initial origin-verification secret without printing or writing it locally.'
    }
}

if ($ConfigureOrigin) {
    $remoteUrl = "https://github.com/$repository.git"
    Push-Location $root
    try {
        $existing = & git remote get-url origin 2>$null
        if ($LASTEXITCODE -eq 0 -and $existing -ne $remoteUrl) {
            throw "Remote 'origin' already points to '$existing'. Refusing to replace it with '$remoteUrl'."
        }
        if ($LASTEXITCODE -ne 0) {
            & git remote add origin $remoteUrl
            if ($LASTEXITCODE -ne 0) { throw 'Failed to add the GitHub origin remote.' }
        }
    } finally {
        Pop-Location
    }
}

Write-Host "GitHub/AWS bootstrap ready for $repository."
Write-Host 'No AWS access key, GitHub token, client secret, or private key was written to the repository.'
