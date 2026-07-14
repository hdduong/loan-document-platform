[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [Security.SecureString]$OriginVerifySecret,
    [string]$EntraDeploymentFile = '',
    [switch]$SkipBuild,
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
            $Value = Read-Host 'CloudFront origin verification secret (32+ high-entropy characters)' -AsSecureString
        }
    }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Get-Sha256Hex {
    param([Parameter(Mandatory)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    try {
        return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Invoke-AwsRedacted {
    param(
        [Parameter(Mandatory)][string[]]$BaseArguments,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )
    & aws @BaseArguments @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Try-GetOutputs {
    param([string]$Profile, [string]$Region, [string]$StackName)
    try {
        return Get-StackOutputs -Profile $Profile -Region $Region -StackName $StackName
    } catch {
        return @{}
    }
}

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'
Assert-Command -Name sam -InstallHint 'Install AWS SAM CLI.'
Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null

$bootstrap = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.bootstrapStackName
foreach ($requiredOutput in 'ArtifactBucketName', 'ArtifactKeyArn', 'CloudFormationExecutionRoleArn') {
    if (-not $bootstrap.ContainsKey($requiredOutput)) {
        throw "Bootstrap stack '$($config.bootstrapStackName)' is missing output '$requiredOutput'."
    }
}

if (-not $EntraDeploymentFile) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
if (Test-Path -LiteralPath $EntraDeploymentFile) {
    $entra = Get-Content -Raw -LiteralPath $EntraDeploymentFile | ConvertFrom-Json -Depth 20
    $entraTenantId = [string]$entra.tenantId
    $entraAudience = [string]$entra.api.audience
    $allowedClientIds = @([string]$entra.spa.clientId)
    if ($null -ne $entra.service -and $entra.service.clientId) {
        $allowedClientIds += [string]$entra.service.clientId
    }
} elseif ($env:ENTRA_API_CLIENT_ID -and $env:ENTRA_SPA_CLIENT_ID) {
    $entraTenantId = [string]$config.entraTenantId
    $entraAudience = $env:ENTRA_API_CLIENT_ID
    $allowedClientIds = @($env:ENTRA_SPA_CLIENT_ID)
    if ($env:ENTRA_SERVICE_CLIENT_ID) { $allowedClientIds += $env:ENTRA_SERVICE_CLIENT_ID }
} else {
    throw "Run scripts/provision-entra.ps1 first, pass -EntraDeploymentFile, or set ENTRA_API_CLIENT_ID and ENTRA_SPA_CLIENT_ID."
}
if ($entraTenantId -ne [string]$config.entraTenantId) {
    throw "Entra deployment tenant '$entraTenantId' does not match environment tenant '$($config.entraTenantId)'."
}
$allowedClientIds = @($allowedClientIds | Where-Object { $_ } | Select-Object -Unique)
if ($allowedClientIds.Count -lt 1) { throw 'At least one Entra client application ID is required.' }

$secretPlain = Get-PlainSecret -Value $OriginVerifySecret
if ($secretPlain.Length -lt 32) { throw 'Origin verification secret must contain at least 32 characters.' }
$secretDigest = Get-Sha256Hex -Value $secretPlain

$edgeOutputs = Try-GetOutputs -Profile $config.awsProfile -Region 'us-east-1' -StackName $config.edgeStackName
if ($edgeOutputs.ContainsKey('OriginVerifySecretDigest') -and
    $edgeOutputs.OriginVerifySecretDigest -ne $secretDigest -and
    -not $AllowCoordinatedOriginSecretRotation) {
    throw 'The supplied origin secret differs from the deployed edge stack. Rotate through deploy-all.ps1 with -AllowCoordinatedOriginSecretRotation during a maintenance window.'
}

$idpInputBucket = ''
$idpWorkingBucket = ''
$idpOutputBucket = ''
$idpKeyArn = ''
$idpStateMachineArn = ''
$idpOutputs = Try-GetOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.idpStackName
if ($idpOutputs.Count -gt 0) {
    $idpInputBucket = [string]$idpOutputs.S3InputBucketName
    $idpOutputBucket = [string]$idpOutputs.S3OutputBucketName
    $idpKeyArn = [string]$idpOutputs.CustomerManagedEncryptionKeyArn
    $idpStateMachineArn = [string]$idpOutputs.StateMachineArn
    try {
        $working = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
            'cloudformation', 'describe-stack-resource',
            '--stack-name', $config.idpStackName,
            '--logical-resource-id', 'WorkingBucket'
        ) -CaptureJson
        $idpWorkingBucket = [string]$working.StackResourceDetail.PhysicalResourceId
    } catch {
        throw "IDP stack exists but WorkingBucket could not be resolved: $($_.Exception.Message)"
    }
}

$manifest = Get-Content -Raw -LiteralPath (Join-Path $root 'config/idp/manifest.json') | ConvertFrom-Json -Depth 10
$lock = Get-Content -Raw -LiteralPath (Join-Path $root 'vendor/idp.lock.json') | ConvertFrom-Json -Depth 10
$deployDirectory = Join-Path $root ".local/deploy/$($config.environment)/platform"
[IO.Directory]::CreateDirectory($deployDirectory) | Out-Null
$builtTemplate = Join-Path $deployDirectory 'built/template.yaml'
$packagedTemplate = Join-Path $deployDirectory 'packaged.yaml'

if (-not $SkipBuild) {
    & sam build --template-file (Join-Path $root 'infra/api/template.yaml') --build-dir (Split-Path $builtTemplate) --parallel
    if ($LASTEXITCODE -ne 0) { throw 'SAM build failed for the regional platform.' }
}
if (-not (Test-Path -LiteralPath $builtTemplate)) {
    throw "Built SAM template not found at '$builtTemplate'. Remove -SkipBuild or provide a previous build."
}

$samPackageArguments = @(
    'package',
    '--template-file', $builtTemplate,
    '--s3-bucket', $bootstrap.ArtifactBucketName,
    '--s3-prefix', "platform/$($config.environment)",
    '--kms-key-id', $bootstrap.ArtifactKeyArn,
    '--region', $config.awsRegion,
    '--output-template-file', $packagedTemplate
)
if ($env:GITHUB_ACTIONS -ne 'true') { $samPackageArguments += @('--profile', $config.awsProfile) }
& sam @samPackageArguments
if ($LASTEXITCODE -ne 0) { throw 'SAM package failed for the regional platform.' }

$awsBase = @('--region', $config.awsRegion, '--no-cli-pager')
if ($env:GITHUB_ACTIONS -ne 'true') { $awsBase = @('--profile', $config.awsProfile) + $awsBase }
$parameters = @(
    "EnvironmentName=$($config.environment)",
    "HostedZoneId=$($config.route53HostedZoneId)",
    "UiHostName=$($config.uiHostName)",
    "ApiOriginHostName=$($config.apiOriginHostName)",
    "EntraTenantId=$entraTenantId",
    "EntraApiAudience=$entraAudience",
    "AllowedClientIds=$($allowedClientIds -join ',')",
    'DeniedClientIds=',
    "OriginVerifySecret=$secretPlain",
    "OriginVerifySecretDigest=$secretDigest",
    "AlertEmail=$($config.alertEmail)",
    "BudgetEmail=$($config.budgetEmail)",
    "MonthlyBudgetUsd=$($config.monthlyBudgetUsd)",
    "MaximumUploadBytes=$($config.maximumUploadBytes)",
    "MaximumPdfPages=$($config.maximumPdfPages)",
    "SourceRetentionDays=$($config.sourceRetentionDays)",
    "LogRetentionDays=$($config.logRetentionDays)",
    "IdpInputBucketName=$idpInputBucket",
    "IdpWorkingBucketName=$idpWorkingBucket",
    "IdpOutputBucketName=$idpOutputBucket",
    "IdpInputKeyArn=$idpKeyArn",
    "IdpStateMachineArn=$idpStateMachineArn",
    "ScreenConfigVersion=$($manifest.screen.name)",
    "ScreenConfigSha256=$($manifest.screen.sourceSha256)",
    "FullConfigVersion=$($manifest.full.name)",
    "FullConfigSha256=$($manifest.full.sourceSha256)",
    "SelectorRuleVersion=$($manifest.selectorRuleVersion)",
    "IdpVersion=$($lock.version)",
    "IdpCommit=$($lock.commit)"
)
$deployArguments = @(
    'cloudformation', 'deploy',
    '--template-file', $packagedTemplate,
    '--stack-name', $config.platformStackName,
    '--role-arn', $bootstrap.CloudFormationExecutionRoleArn,
    '--capabilities', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND',
    '--parameter-overrides'
) + $parameters + @(
    '--tags',
    'Application=loan-document-platform',
    "Environment=$($config.environment)",
    'ManagedBy=CloudFormation',
    '--no-fail-on-empty-changeset'
)

try {
    Invoke-AwsRedacted -BaseArguments $awsBase -Arguments $deployArguments -FailureMessage 'Regional platform deployment failed; secret-bearing arguments were redacted.'
} finally {
    $secretPlain = $null
    $parameters = $null
    $deployArguments = $null
}

$outputs = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.platformStackName
if ($outputs.OriginVerifySecretDigest -ne $secretDigest) {
    throw 'Regional stack deployed, but its origin-secret digest does not match the requested value.'
}
$localDirectory = Join-Path $root '.local'
[IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$outputPath = Join-Path $localDirectory "aws-$($config.environment).json"
$safeOutput = [ordered]@{
    region = $config.awsRegion
    stackName = $config.platformStackName
    apiOriginUrl = $outputs.ApiOriginUrl
    sourceBucketName = $outputs.SourceBucketName
    registryTableName = $outputs.RegistryTableName
    uploadProcessorFunctionArn = $outputs.UploadProcessorFunctionArn
    idpPostprocessorFunctionArn = $outputs.IdpPostprocessorFunctionArn
    idpConnected = [bool]$idpInputBucket
    originVerifySecretDigest = $outputs.OriginVerifySecretDigest
}
[IO.File]::WriteAllText($outputPath, ($safeOutput | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Host "Regional platform ready. Non-secret outputs: $outputPath"
