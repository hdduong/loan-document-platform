[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$EntraDeploymentFile = '',
    [string]$BuildSha = '',
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$appDirectory = Join-Path $root 'apps/web'
$distributionRegion = 'us-east-1'
Assert-Command -Name npm -InstallHint 'Install Node.js LTS and npm.'
Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'
Assert-Command -Name git -InstallHint 'Install Git.'
Assert-AwsIdentity -Profile $config.awsProfile -Region $distributionRegion -ExpectedAccountId $config.awsAccountId | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $appDirectory 'package.json'))) {
    throw "React application is not present in '$appDirectory'. Claude Code must finish the UI scaffold before deployment."
}
if (-not (Test-Path -LiteralPath (Join-Path $appDirectory 'package-lock.json'))) {
    throw 'apps/web/package-lock.json is required for a reproducible production build.'
}
if (-not $EntraDeploymentFile) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
if (Test-Path -LiteralPath $EntraDeploymentFile) {
    $entra = Get-Content -Raw -LiteralPath $EntraDeploymentFile | ConvertFrom-Json -Depth 20
    $tenantId = [string]$entra.tenantId
    $spaClientId = [string]$entra.spa.clientId
    $scopeBase = [string]$entra.api.scopeBase
} elseif ($env:ENTRA_API_CLIENT_ID -and $env:ENTRA_SPA_CLIENT_ID) {
    $tenantId = [string]$config.entraTenantId
    $spaClientId = $env:ENTRA_SPA_CLIENT_ID
    $scopeBase = "api://$($env:ENTRA_API_CLIENT_ID)"
} else {
    throw 'Entra non-secret deployment IDs are unavailable. Run provision-entra.ps1 or set ENTRA_API_CLIENT_ID and ENTRA_SPA_CLIENT_ID.'
}
if ($tenantId -ne [string]$config.entraTenantId) {
    throw "Entra tenant '$tenantId' does not match environment tenant '$($config.entraTenantId)'."
}

$edge = Get-StackOutputs -Profile $config.awsProfile -Region $distributionRegion -StackName $config.edgeStackName
if (-not $edge.UiBucketName -or -not $edge.UiDistributionId) {
    throw "Edge stack '$($config.edgeStackName)' does not expose UI deployment outputs."
}
if ($env:GITHUB_ACTIONS -eq 'true' -and -not $edge.UiDeploymentRoleArn) {
    throw "Edge stack '$($config.edgeStackName)' does not expose its narrowly scoped UI deployment role."
}
if (-not $BuildSha) {
    $BuildSha = if ($env:GITHUB_SHA) {
        $env:GITHUB_SHA
    } else {
        (& git -C $root rev-parse HEAD | Out-String).Trim()
    }
}
if (-not $BuildSha) { throw 'A build commit SHA is required.' }

$previousCi = $env:CI
$env:CI = 'true'
try {
    Push-Location $appDirectory
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        if (-not $SkipTests) {
            & npm run test --if-present
            if ($LASTEXITCODE -ne 0) { throw 'UI tests failed.' }
        }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'UI production build failed.' }
    } finally {
        Pop-Location
    }
} finally {
    $env:CI = $previousCi
}

$distDirectory = Join-Path $appDirectory 'dist'
$indexPath = Join-Path $distDirectory 'index.html'
if (-not (Test-Path -LiteralPath $indexPath)) {
    throw "UI build did not produce '$indexPath'."
}
$runtimeConfig = [ordered]@{
    environment = if ($config.environment -eq 'prod') { 'production' } else { [string]$config.environment }
    apiBaseUrl = "https://$($config.apiHostName)"
    entraTenantId = $tenantId
    entraSpaClientId = $spaClientId
    entraApiScopeBase = $scopeBase
    redirectUri = "https://$($config.uiHostName)/auth/callback"
    postLogoutRedirectUri = "https://$($config.uiHostName)/"
    buildSha = $BuildSha
    maximumUploadBytes = [long]$config.maximumUploadBytes
}
$runtimePath = Join-Path $distDirectory 'runtime-config.json'
[IO.File]::WriteAllText($runtimePath, ($runtimeConfig | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

$credentialBackup = $null
if ($env:GITHUB_ACTIONS -eq 'true') {
    $credentialBackup = @{
        AccessKeyId = $env:AWS_ACCESS_KEY_ID
        SecretAccessKey = $env:AWS_SECRET_ACCESS_KEY
        SessionToken = $env:AWS_SESSION_TOKEN
    }
    $assumed = Invoke-Aws -Profile $config.awsProfile -Region $distributionRegion -Arguments @(
        'sts', 'assume-role',
        '--role-arn', $edge.UiDeploymentRoleArn,
        '--role-session-name', "github-ui-$($config.environment)",
        '--duration-seconds', '3600'
    ) -CaptureJson
    $env:AWS_ACCESS_KEY_ID = $assumed.Credentials.AccessKeyId
    $env:AWS_SECRET_ACCESS_KEY = $assumed.Credentials.SecretAccessKey
    $env:AWS_SESSION_TOKEN = $assumed.Credentials.SessionToken
}

try {
    $awsBase = @('--region', $distributionRegion, '--no-cli-pager')
    if ($env:GITHUB_ACTIONS -ne 'true') { $awsBase = @('--profile', $config.awsProfile) + $awsBase }
    $destination = "s3://$($edge.UiBucketName)"
    & aws @awsBase s3 sync $distDirectory $destination --delete --exclude index.html --exclude runtime-config.json --cache-control 'public,max-age=300,must-revalidate'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to synchronize UI assets.' }
    $assetsDirectory = Join-Path $distDirectory 'assets'
    if (Test-Path -LiteralPath $assetsDirectory) {
        & aws @awsBase s3 cp $assetsDirectory "$destination/assets" --recursive --cache-control 'public,max-age=31536000,immutable'
        if ($LASTEXITCODE -ne 0) { throw 'Failed to publish immutable hashed UI assets.' }
    }
    & aws @awsBase s3 cp $indexPath "$destination/index.html" --content-type 'text/html; charset=utf-8' --cache-control 'no-store, max-age=0'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to publish index.html.' }
    & aws @awsBase s3 cp $runtimePath "$destination/runtime-config.json" --content-type 'application/json; charset=utf-8' --cache-control 'no-store, max-age=0'
    if ($LASTEXITCODE -ne 0) { throw 'Failed to publish runtime-config.json.' }
    & aws @awsBase cloudfront create-invalidation --distribution-id $edge.UiDistributionId --paths '/*' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the UI CloudFront invalidation.' }
} finally {
    if ($null -ne $credentialBackup) {
        $env:AWS_ACCESS_KEY_ID = $credentialBackup.AccessKeyId
        $env:AWS_SECRET_ACCESS_KEY = $credentialBackup.SecretAccessKey
        $env:AWS_SESSION_TOKEN = $credentialBackup.SessionToken
    }
}

Write-Host "UI build $BuildSha deployed to https://$($config.uiHostName)"
