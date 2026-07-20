[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$EntraDeploymentFile = '',
    [string]$FederationDeploymentFile = '',
    [string]$ImageTag = '',
    [string]$IdpImageManifestFile = '',
    [switch]$SkipAzureFoundation,
    [switch]$SkipEntra,
    [switch]$SkipIdp,
    [switch]$SkipAzureApi,
    [switch]$SkipWeb,
    [switch]$SkipImageBuild,
    [switch]$BindApiCustomDomain,
    [switch]$CutoverApiDomain,
    [switch]$BindUiCustomDomain,
    [switch]$ReinstallIdpCli,
    [switch]$CleanIdpBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
if ([string]::IsNullOrWhiteSpace($EntraDeploymentFile)) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
if ([string]::IsNullOrWhiteSpace($FederationDeploymentFile)) {
    $FederationDeploymentFile = Join-Path $root ".local/entra-aws-federation-$($config.environment).json"
}
if ($CutoverApiDomain -and -not $BindApiCustomDomain) {
    throw '-CutoverApiDomain requires -BindApiCustomDomain so Azure has a validated hostname and certificate before DNS changes.'
}

Write-Host "Deploying Azure control plane and private AWS processing plane for '$($config.environment)'."

if (-not $SkipEntra) {
    & (Join-Path $PSScriptRoot 'provision-entra.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -OutputFile $EntraDeploymentFile
}

if (-not $SkipAzureFoundation) {
    & (Join-Path $PSScriptRoot 'deploy-azure.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -FoundationOnly
}

if (-not $SkipEntra) {
    & (Join-Path $PSScriptRoot 'provision-entra-federation.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -OutputFile $FederationDeploymentFile
} else {
    foreach ($requiredFile in @($EntraDeploymentFile, $FederationDeploymentFile)) {
        if (-not (Test-Path -LiteralPath $requiredFile)) {
            throw "Required non-secret Entra deployment state is missing: $requiredFile"
        }
    }
}

$platformArguments = @{
    EnvironmentFile = $EnvironmentFile
    FederationDeploymentFile = $FederationDeploymentFile
}
$initialPlatformArguments = @{}
foreach ($entry in $platformArguments.GetEnumerator()) {
    $initialPlatformArguments[$entry.Key] = $entry.Value
}
if (-not $SkipIdp) {
    # Only the orchestrated first-install pass may tolerate an IDP stack that
    # does not exist yet. Reuse and -SkipIdp deployments must resolve it.
    $initialPlatformArguments.AllowMissingIdp = $true
}
& (Join-Path $PSScriptRoot 'deploy-platform.ps1') @initialPlatformArguments

if (-not $SkipIdp) {
    if ([string]::IsNullOrWhiteSpace($IdpImageManifestFile)) {
        throw 'IdpImageManifestFile is required when IDP deployment is not skipped.'
    }
    $idpArguments = @{
        EnvironmentFile = $EnvironmentFile
        ImageManifestFile = $IdpImageManifestFile
        ReinstallCli = $ReinstallIdpCli
        CleanBuild = $CleanIdpBuild
    }
    & (Join-Path $PSScriptRoot 'deploy-idp.ps1') @idpArguments

    # The first private-runtime pass creates the postprocessor hook required by
    # IDP. The second binds exact IDP buckets/key/state-machine outputs without
    # creating a public AWS product API.
    & (Join-Path $PSScriptRoot 'deploy-platform.ps1') @platformArguments
}

if (-not $SkipAzureApi) {
    $azureArguments = @{
        EnvironmentFile = $EnvironmentFile
        EntraDeploymentFile = $EntraDeploymentFile
        FederationDeploymentFile = $FederationDeploymentFile
        SkipImageBuild = $SkipImageBuild
        BindCustomDomain = $BindApiCustomDomain
    }
    if (-not [string]::IsNullOrWhiteSpace($ImageTag)) { $azureArguments.ImageTag = $ImageTag }
    & (Join-Path $PSScriptRoot 'deploy-azure.ps1') @azureArguments
}

if ($CutoverApiDomain) {
    & (Join-Path $PSScriptRoot 'cutover-api-domain.ps1') `
        -EnvironmentFile $EnvironmentFile
}

if (-not $SkipWeb) {
    & (Join-Path $PSScriptRoot 'deploy-web.ps1') `
        -EnvironmentFile $EnvironmentFile `
        -AzureDeploymentFile (Join-Path $root ".local/azure-$($config.environment).json") `
        -EntraDeploymentFile $EntraDeploymentFile `
        -BindCustomDomain:$BindUiCustomDomain
}

Write-Host "Deployment sequence completed for '$($config.environment)'."
Write-Host 'Public product traffic terminates only at Azure; AWS remains a federated private processing and data plane.'
