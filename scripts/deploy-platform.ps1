[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$FederationDeploymentFile = '',
    [switch]$SkipBuild,
    [switch]$AllowMissingIdp
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

function Get-StackOutputMap {
    param([Parameter(Mandatory)][object]$Stack)

    $outputs = @{}
    foreach ($output in @($Stack.Outputs)) {
        $outputs[[string]$output.OutputKey] = [string]$output.OutputValue
    }
    return $outputs
}

function Get-StackParameterMap {
    param([Parameter(Mandatory)][object]$Stack)

    $parameters = @{}
    foreach ($parameter in @($Stack.Parameters)) {
        $parameters[[string]$parameter.ParameterKey] = [string]$parameter.ParameterValue
    }
    return $parameters
}

function Get-OptionalConfigValue {
    param(
        [Parameter(Mandatory)][object]$Config,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Default
    )
    $property = $Config.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value -or
        [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        return $Default
    }
    return $property.Value
}

function Require-Output {
    param(
        [Parameter(Mandatory)][hashtable]$Outputs,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$StackName
    )
    if (-not $Outputs.ContainsKey($Name) -or [string]::IsNullOrWhiteSpace([string]$Outputs[$Name])) {
        throw "Stack '$StackName' is missing required output '$Name'."
    }
    return [string]$Outputs[$Name]
}

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'
Assert-Command -Name sam -InstallHint 'Install AWS SAM CLI.'
Assert-AwsIdentity `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -ExpectedAccountId $config.awsAccountId | Out-Null

$bootstrap = Get-StackOutputs `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.bootstrapStackName
foreach ($requiredOutput in @(
    'ArtifactBucketName',
    'ArtifactKeyArn',
    'PlatformCloudFormationExecutionRoleArn',
    'PlatformRolePermissionsBoundaryArn'
)) {
    Require-Output -Outputs $bootstrap -Name $requiredOutput -StackName $config.bootstrapStackName | Out-Null
}

if ([string]::IsNullOrWhiteSpace($FederationDeploymentFile)) {
    $FederationDeploymentFile = Join-Path $root ".local/entra-aws-federation-$($config.environment).json"
}
if (-not (Test-Path -LiteralPath $FederationDeploymentFile)) {
    throw "Federation deployment output is missing: $FederationDeploymentFile. Run deploy-azure.ps1 -FoundationOnly and provision-entra-federation.ps1 first."
}
$federation = Get-Content -Raw -LiteralPath $FederationDeploymentFile | ConvertFrom-Json -Depth 30
$expectedIssuer = "https://sts.windows.net/$($config.entraTenantId)/"
if ([string]$federation.tenantId -ne [string]$config.entraTenantId -or
    [string]$federation.issuer -ne $expectedIssuer) {
    throw 'Federation tenant or issuer does not match the environment.'
}
if ([string]$federation.audience -notmatch '^api://[0-9a-fA-F-]{36}$') {
    throw 'Federation audience must be a dedicated api:// application GUID.'
}
$federationSubject = [string]$federation.managedIdentity.principalId
$parsedSubject = [guid]::Empty
if (-not [guid]::TryParse($federationSubject, [ref]$parsedSubject)) {
    throw 'Federation managed-identity principalId must be a GUID.'
}

$idpInputBucket = ''
$idpWorkingBucket = ''
$idpOutputBucket = ''
$idpKeyArn = ''
$idpStateMachineArn = ''
$idpConnected = $false
$idpStack = Get-AwsCloudFormationStackDescription `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.idpStackName `
    -AllowMissing:$AllowMissingIdp
if ($null -eq $idpStack) {
    if (-not $AllowMissingIdp) {
        throw "Required IDP stack '$($config.idpStackName)' does not exist."
    }

    $existingPlatformStack = Get-AwsCloudFormationStackDescription `
        -Profile $config.awsProfile `
        -Region $config.awsRegion `
        -StackName $config.platformStackName `
        -AllowMissing
    if ($null -ne $existingPlatformStack) {
        $existingPlatformParameters = Get-StackParameterMap -Stack $existingPlatformStack
        $hasExistingIdpWiring = $false
        foreach ($parameterName in @(
            'IdpInputBucketName',
            'IdpWorkingBucketName',
            'IdpOutputBucketName',
            'IdpInputKeyArn',
            'IdpStateMachineArn'
        )) {
            if ($existingPlatformParameters.ContainsKey($parameterName) -and
                -not [string]::IsNullOrWhiteSpace([string]$existingPlatformParameters[$parameterName])) {
                $hasExistingIdpWiring = $true
                break
            }
        }
        if ($hasExistingIdpWiring) {
            throw "IDP stack '$($config.idpStackName)' is missing, but platform stack '$($config.platformStackName)' is connected. Refusing to erase the last known IDP wiring."
        }
    }
    Write-Host 'The explicit first-install bootstrap pass will deploy processors without IDP resources.'
} else {
    $idpOutputs = Get-StackOutputMap -Stack $idpStack
    $idpInputBucket = Require-Output -Outputs $idpOutputs -Name 'S3InputBucketName' -StackName $config.idpStackName
    $idpOutputBucket = Require-Output -Outputs $idpOutputs -Name 'S3OutputBucketName' -StackName $config.idpStackName
    $idpKeyArn = Require-Output -Outputs $idpOutputs -Name 'CustomerManagedEncryptionKeyArn' -StackName $config.idpStackName
    $idpStateMachineArn = Require-Output -Outputs $idpOutputs -Name 'StateMachineArn' -StackName $config.idpStackName
    $working = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'cloudformation', 'describe-stack-resource',
        '--stack-name', $config.idpStackName,
        '--logical-resource-id', 'WorkingBucket'
    ) -CaptureJson
    $idpWorkingBucket = [string]$working.StackResourceDetail.PhysicalResourceId
    if ([string]::IsNullOrWhiteSpace($idpWorkingBucket)) {
        throw "IDP stack '$($config.idpStackName)' has no physical WorkingBucket."
    }
    $idpConnected = $true
}

$manifest = Get-Content -Raw -LiteralPath (Join-Path $root 'config/idp/manifest.json') |
    ConvertFrom-Json -Depth 10
$lock = Get-Content -Raw -LiteralPath (Join-Path $root 'vendor/idp.lock.json') |
    ConvertFrom-Json -Depth 10
if ([string]$lock.deploymentMode -ne 'headless') {
    throw 'The private AWS runtime requires the pinned IDP deploymentMode to remain headless.'
}

$deployDirectory = Join-Path $root ".local/deploy/$($config.environment)/platform"
[System.IO.Directory]::CreateDirectory($deployDirectory) | Out-Null
$builtTemplate = Join-Path $deployDirectory 'built/template.yaml'
$packagedTemplate = Join-Path $deployDirectory 'packaged.yaml'
$stackPolicyPath = Join-Path $root 'infra/stack-policies/protect-stateful-resources.json'

if (-not $SkipBuild) {
    & sam build `
        --template-file (Join-Path $root 'infra/api/template.yaml') `
        --build-dir (Split-Path $builtTemplate) `
        --parallel
    if ($LASTEXITCODE -ne 0) { throw 'SAM build failed for the private AWS runtime.' }
}
if (-not (Test-Path -LiteralPath $builtTemplate)) {
    throw "Built SAM template not found at '$builtTemplate'. Remove -SkipBuild or provide a prior reviewed build."
}

$packageArguments = @(
    'package',
    '--template-file', $builtTemplate,
    '--s3-bucket', [string]$bootstrap.ArtifactBucketName,
    '--s3-prefix', "platform/$($config.environment)",
    '--kms-key-id', [string]$bootstrap.ArtifactKeyArn,
    '--region', [string]$config.awsRegion,
    '--output-template-file', $packagedTemplate
)
if ($env:GITHUB_ACTIONS -ne 'true') { $packageArguments += @('--profile', [string]$config.awsProfile) }
& sam @packageArguments
if ($LASTEXITCODE -ne 0) { throw 'SAM package failed for the private AWS runtime.' }

$expectedIdpParameters = [ordered]@{
    IdpInputBucketName = $idpInputBucket
    IdpWorkingBucketName = $idpWorkingBucket
    IdpOutputBucketName = $idpOutputBucket
    IdpInputKeyArn = $idpKeyArn
    IdpStateMachineArn = $idpStateMachineArn
}
$parameters = @(
    "EnvironmentName=$($config.environment)",
    "UiHostName=$($config.uiHostName)",
    "EntraTenantId=$($config.entraTenantId)",
    "AzureFederationAudience=$($federation.audience)",
    "AzureFederationSubject=$federationSubject",
    "AzureRuntimeRoleMaxSessionSeconds=$(Get-OptionalConfigValue -Config $config -Name 'azureAwsSessionDurationSeconds' -Default 3600)",
    "RolePermissionsBoundaryArn=$($bootstrap.PlatformRolePermissionsBoundaryArn)",
    "AlertEmail=$($config.alertEmail)",
    "BudgetEmail=$($config.budgetEmail)",
    "MonthlyBudgetUsd=$($config.monthlyBudgetUsd)",
    "MaximumUploadBytes=$($config.maximumUploadBytes)",
    "MaximumPdfPages=$($config.maximumPdfPages)",
    "SourceRetentionDays=$($config.sourceRetentionDays)",
    "LogRetentionDays=$($config.logRetentionDays)",
    "IdpInputBucketName=$($expectedIdpParameters['IdpInputBucketName'])",
    "IdpWorkingBucketName=$($expectedIdpParameters['IdpWorkingBucketName'])",
    "IdpOutputBucketName=$($expectedIdpParameters['IdpOutputBucketName'])",
    "IdpInputKeyArn=$($expectedIdpParameters['IdpInputKeyArn'])",
    "IdpStateMachineArn=$($expectedIdpParameters['IdpStateMachineArn'])",
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
    '--stack-name', [string]$config.platformStackName,
    '--role-arn', [string]$bootstrap.PlatformCloudFormationExecutionRoleArn,
    '--capabilities', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND',
    '--parameter-overrides'
) + $parameters + @(
    '--tags',
    'Application=loan-document-platform',
    "Environment=$($config.environment)",
    'ManagedBy=CloudFormation',
    '--no-fail-on-empty-changeset'
)
$platformBeforeDeployment = Get-AwsCloudFormationStackDescription `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.platformStackName `
    -AllowMissing
if ($null -ne $platformBeforeDeployment) {
    Set-AwsStatefulStackPolicy `
        -Profile $config.awsProfile `
        -Region $config.awsRegion `
        -StackName $config.platformStackName `
        -PolicyPath $stackPolicyPath
}

Invoke-Aws `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -Arguments $deployArguments | Out-Host

Set-AwsStatefulStackPolicy `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.platformStackName `
    -PolicyPath $stackPolicyPath

$deployedPlatformStack = Get-AwsCloudFormationStackDescription `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.platformStackName
$deployedPlatformParameters = Get-StackParameterMap -Stack $deployedPlatformStack
foreach ($parameterName in $expectedIdpParameters.Keys) {
    if (-not $deployedPlatformParameters.ContainsKey([string]$parameterName)) {
        throw "Deployed platform stack is missing required IDP parameter '$parameterName'."
    }
    if (-not [string]::Equals(
        [string]$deployedPlatformParameters[[string]$parameterName],
        [string]$expectedIdpParameters[$parameterName],
        [StringComparison]::Ordinal
    )) {
        throw "Deployed platform IDP parameter '$parameterName' does not match the discovered IDP stack value."
    }
}

$verifiedIdpConnected = $false
if ($idpConnected) {
    foreach ($logicalResourceId in 'IdpFailureRule', 'IdpExecutionWatchdogRule') {
        $resource = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
            'cloudformation', 'describe-stack-resource',
            '--stack-name', $config.platformStackName,
            '--logical-resource-id', $logicalResourceId
        ) -CaptureJson
        if ([string]::IsNullOrWhiteSpace([string]$resource.StackResourceDetail.PhysicalResourceId)) {
            throw "Deployed platform stack did not create required IDP integration resource '$logicalResourceId'."
        }
    }
    $verifiedIdpConnected = $true
}

$outputs = Get-StackOutputs `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.platformStackName
foreach ($requiredOutput in @(
    'SourceBucketName',
    'DataKeyArn',
    'RegistryTableName',
    'UploadProcessorFunctionArn',
    'IdpPostprocessorFunctionArn',
    'AzureApiRuntimeRoleArn',
    'EntraTenantOidcProviderArn'
)) {
    Require-Output -Outputs $outputs -Name $requiredOutput -StackName $config.platformStackName | Out-Null
}

$localDirectory = Join-Path $root '.local'
[System.IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$outputPath = Join-Path $localDirectory "aws-$($config.environment).json"
$safeOutput = [ordered]@{
    schemaVersion = 1
    region = [string]$config.awsRegion
    stackName = [string]$config.platformStackName
    sourceBucketName = [string]$outputs.SourceBucketName
    dataKeyArn = [string]$outputs.DataKeyArn
    registryTableName = [string]$outputs.RegistryTableName
    uploadProcessorFunctionArn = [string]$outputs.UploadProcessorFunctionArn
    idpPostprocessorFunctionArn = [string]$outputs.IdpPostprocessorFunctionArn
    azureApiRuntimeRoleArn = [string]$outputs.AzureApiRuntimeRoleArn
    entraTenantOidcProviderArn = [string]$outputs.EntraTenantOidcProviderArn
    federationAudience = [string]$federation.audience
    federationSubject = $federationSubject
    idpConnected = $verifiedIdpConnected
}
[System.IO.File]::WriteAllText(
    $outputPath,
    ($safeOutput | ConvertTo-Json -Depth 20) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Host "Private AWS runtime ready. Non-secret outputs: $outputPath"
