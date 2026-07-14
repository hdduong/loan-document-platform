[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [Security.SecureString]$OriginVerifySecret,
    [switch]$SkipEntra,
    [switch]$SkipIdp,
    [switch]$SkipEdge,
    [switch]$SkipWeb,
    [switch]$SkipUiTests,
    [switch]$ReinstallIdpCli,
    [switch]$CleanIdpBuild,
    [switch]$AllowCoordinatedOriginSecretRotation
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
if ($SkipEdge -and -not $SkipWeb) {
    throw '-SkipEdge requires -SkipWeb because the UI publication role and bucket are edge-stack outputs.'
}
if ($null -eq $OriginVerifySecret) {
    if ($env:LOAN_API_ORIGIN_VERIFY_SECRET) {
        $OriginVerifySecret = ConvertTo-SecureString $env:LOAN_API_ORIGIN_VERIFY_SECRET -AsPlainText -Force
    } elseif ($env:GITHUB_ACTIONS -eq 'true') {
        throw 'Protected GitHub Environment secret LOAN_API_ORIGIN_VERIFY_SECRET is required.'
    } else {
        $OriginVerifySecret = Read-Host 'CloudFront origin verification secret (32+ high-entropy characters)' -AsSecureString
    }
}

Write-Host "Deploying loan document platform environment '$($config.environment)' to AWS account $($config.awsAccountId)."

if (-not $SkipEntra) {
    & (Join-Path $PSScriptRoot 'provision-entra.ps1') -EnvironmentFile $EnvironmentFile
}

$platformArguments = @{
    EnvironmentFile = $EnvironmentFile
    OriginVerifySecret = $OriginVerifySecret
    AllowCoordinatedOriginSecretRotation = $AllowCoordinatedOriginSecretRotation
}
& (Join-Path $PSScriptRoot 'deploy-platform.ps1') @platformArguments

if (-not $SkipIdp) {
    $idpArguments = @{
        EnvironmentFile = $EnvironmentFile
        ReinstallCli = $ReinstallIdpCli
        CleanBuild = $CleanIdpBuild
    }
    & (Join-Path $PSScriptRoot 'deploy-idp.ps1') @idpArguments

    # The first platform pass creates the postprocessor ARN needed by IDP. The
    # second pass grants the processors access to the now-known IDP buckets/key.
    & (Join-Path $PSScriptRoot 'deploy-platform.ps1') @platformArguments
}

if (-not $SkipEdge) {
    $edgeArguments = @{
        EnvironmentFile = $EnvironmentFile
        OriginVerifySecret = $OriginVerifySecret
        AllowCoordinatedOriginSecretRotation = $AllowCoordinatedOriginSecretRotation
    }
    & (Join-Path $PSScriptRoot 'deploy-edge.ps1') @edgeArguments
}

if (-not $SkipWeb) {
    & (Join-Path $PSScriptRoot 'deploy-web.ps1') -EnvironmentFile $EnvironmentFile -SkipTests:$SkipUiTests
}

Write-Host "Deployment sequence completed for '$($config.environment)'."
