[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [Security.SecureString]$OriginVerifySecret,
    [switch]$AllowCoordinatedOriginSecretRotation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

function Get-PlainSecret {
    param([Security.SecureString]$Value)
    if ($null -eq $Value) {
        if ($env:LOAN_API_ORIGIN_VERIFY_SECRET) {
            $Value = ConvertTo-SecureString $env:LOAN_API_ORIGIN_VERIFY_SECRET -AsPlainText -Force
        } elseif ($env:GITHUB_ACTIONS -eq 'true') {
            throw 'GitHub Environment secret LOAN_API_ORIGIN_VERIFY_SECRET is required.'
        } else {
            $Value = Read-Host 'CloudFront origin verification secret (must match the regional stack)' -AsSecureString
        }
    }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    try { return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant() }
    finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$edgeRegion = 'us-east-1'
Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'
Assert-AwsIdentity -Profile $config.awsProfile -Region $edgeRegion -ExpectedAccountId $config.awsAccountId | Out-Null

$bootstrap = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.bootstrapStackName
if (-not $bootstrap.CloudFormationExecutionRoleArn) {
    throw "Bootstrap stack '$($config.bootstrapStackName)' is missing CloudFormationExecutionRoleArn."
}
if (-not $bootstrap.GitHubDeploymentRoleArn) {
    throw "Bootstrap stack '$($config.bootstrapStackName)' is missing GitHubDeploymentRoleArn."
}
$platform = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.platformStackName
if (-not $platform.ApiOriginUrl -or -not $platform.OriginVerifySecretDigest) {
    throw "Deploy regional stack '$($config.platformStackName)' before the edge stack."
}

$secretPlain = Get-PlainSecret -Value $OriginVerifySecret
if ($secretPlain.Length -lt 32) { throw 'Origin verification secret must contain at least 32 characters.' }
$secretDigest = Get-Sha256Hex -Value $secretPlain
if ($platform.OriginVerifySecretDigest -ne $secretDigest -and -not $AllowCoordinatedOriginSecretRotation) {
    throw 'The supplied origin secret differs from the regional stack. Rotate through deploy-all.ps1 with -AllowCoordinatedOriginSecretRotation during a maintenance window.'
}

$awsBase = @('--region', $edgeRegion, '--no-cli-pager')
if ($env:GITHUB_ACTIONS -ne 'true') { $awsBase = @('--profile', $config.awsProfile) + $awsBase }
$parameters = @(
    "EnvironmentName=$($config.environment)",
    "HostedZoneId=$($config.route53HostedZoneId)",
    "UiHostName=$($config.uiHostName)",
    "ApiHostName=$($config.apiHostName)",
    "ApiOriginHostName=$($config.apiOriginHostName)",
    "GitHubDeploymentRoleArn=$($bootstrap.GitHubDeploymentRoleArn)",
    "OriginVerifySecret=$secretPlain",
    "OriginVerifySecretDigest=$secretDigest",
    "AlertEmail=$($config.alertEmail)",
    "LogRetentionDays=$($config.logRetentionDays)"
)
$arguments = @(
    'cloudformation', 'deploy',
    '--template-file', (Join-Path $root 'infra/edge/template.yaml'),
    '--stack-name', $config.edgeStackName,
    '--role-arn', $bootstrap.CloudFormationExecutionRoleArn,
    '--capabilities', 'CAPABILITY_NAMED_IAM',
    '--parameter-overrides'
) + $parameters + @(
    '--tags',
    'Application=loan-document-platform',
    "Environment=$($config.environment)",
    'ManagedBy=CloudFormation',
    '--no-fail-on-empty-changeset'
)

try {
    & aws @awsBase @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Edge deployment failed; secret-bearing arguments were redacted.' }
} finally {
    $secretPlain = $null
    $parameters = $null
    $arguments = $null
}

$outputs = Get-StackOutputs -Profile $config.awsProfile -Region $edgeRegion -StackName $config.edgeStackName
if ($outputs.OriginVerifySecretDigest -ne $secretDigest) {
    throw 'Edge stack deployed, but its origin-secret digest does not match the regional stack.'
}
$localDirectory = Join-Path $root '.local'
[IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$outputPath = Join-Path $localDirectory "edge-$($config.environment).json"
$safeOutput = [ordered]@{
    region = $edgeRegion
    stackName = $config.edgeStackName
    uiUrl = $outputs.UiUrl
    apiUrl = $outputs.ApiUrl
    uiBucketName = $outputs.UiBucketName
    uiDistributionId = $outputs.UiDistributionId
    apiDistributionId = $outputs.ApiDistributionId
    uiDeploymentRoleArn = $outputs.UiDeploymentRoleArn
    originVerifySecretDigest = $outputs.OriginVerifySecretDigest
}
[IO.File]::WriteAllText($outputPath, ($safeOutput | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Host "Edge ready. Non-secret outputs: $outputPath"
