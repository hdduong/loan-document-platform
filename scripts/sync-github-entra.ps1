[CmdletBinding(SupportsShouldProcess)]
param([Parameter(Mandatory)][string]$EnvironmentFile)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force
$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$repository = "$($config.githubOwner)/$($config.repositoryName)"

Assert-Command -Name gh -InstallHint 'Run scripts/bootstrap.ps1.'
& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }

$entraPath = Join-Path $root ".local\entra-$($config.environment).json"
if (-not (Test-Path -LiteralPath $entraPath)) {
    throw "Missing '$entraPath'. Run scripts/provision-entra.ps1 first."
}
$entra = Get-Content -Raw -LiteralPath $entraPath | ConvertFrom-Json -Depth 20
if ($entra.tenantId -ne $config.entraTenantId) {
    throw "Entra state tenant '$($entra.tenantId)' does not match environment tenant '$($config.entraTenantId)'."
}

$variables = [ordered]@{
    ENTRA_TENANT_ID = $entra.tenantId
    ENTRA_API_CLIENT_ID = $entra.api.clientId
    ENTRA_API_SCOPE_BASE = $entra.api.scopeBase
    ENTRA_SPA_CLIENT_ID = $entra.spa.clientId
}
if ($null -ne $entra.service -and $entra.service.clientId) {
    $variables.ENTRA_SERVICE_CLIENT_ID = $entra.service.clientId
}

if ($PSCmdlet.ShouldProcess("$repository environment $($config.githubEnvironment)", 'Publish non-secret Entra application IDs')) {
    foreach ($entry in $variables.GetEnumerator()) {
        [string]$entry.Value | & gh variable set $entry.Key --repo $repository --env $config.githubEnvironment
        if ($LASTEXITCODE -ne 0) { throw "Failed to set GitHub environment variable '$($entry.Key)'." }
    }
}

Write-Host "Synchronized non-secret Entra IDs to GitHub environment '$($config.githubEnvironment)'."
