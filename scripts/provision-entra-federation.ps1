<#
.SYNOPSIS
Creates the dedicated v1 Entra resource audience used only for Azure-to-AWS STS federation.

.OUTPUTS
Writes ignored non-secret JSON with
`{tenantId,issuer,audience,applicationObjectId,applicationClientId,
servicePrincipalObjectId,applicationRoleId,
managedIdentity:{resourceId,clientId,principalId}}`. The AWS runtime stack must
pin `audience` and `managedIdentity.principalId`; no credential is emitted.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$OutputFile = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
Assert-Command -Name az -InstallHint 'Run scripts/bootstrap.ps1.'

if ($config.corporateCaBundlePath) {
    $caPath = (Resolve-Path -LiteralPath $config.corporateCaBundlePath).Path
    $env:REQUESTS_CA_BUNDLE = $caPath
    $env:SSL_CERT_FILE = $caPath
}

& az account set --subscription $config.azureSubscriptionId
if ($LASTEXITCODE -ne 0) { throw "Cannot select Azure subscription '$($config.azureSubscriptionId)'." }
$account = & az account show --output json | ConvertFrom-Json -Depth 20
if ($LASTEXITCODE -ne 0) { throw 'Azure CLI is not signed in. Run az login for the target tenant.' }
if ([string]$account.tenantId -ne [string]$config.entraTenantId) {
    throw "Azure CLI tenant '$($account.tenantId)' does not match '$($config.entraTenantId)'."
}
if ([string]$account.id -ne [string]$config.azureSubscriptionId) {
    throw "Azure CLI subscription '$($account.id)' does not match '$($config.azureSubscriptionId)'."
}

$identityRaw = & az identity show `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureApiManagedIdentityName `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Azure API managed identity '$($config.azureApiManagedIdentityName)' is missing. Run deploy-azure.ps1 -FoundationOnly first."
}
$identity = $identityRaw | Out-String | ConvertFrom-Json -Depth 20
foreach ($property in @('id', 'clientId', 'principalId')) {
    if ([string]::IsNullOrWhiteSpace([string]$identity.$property)) {
        throw "Azure managed identity output '$property' is empty."
    }
}

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PATCH')][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [object]$Body
    )

    $arguments = @('rest', '--method', $Method, '--uri', $Uri, '--resource', 'https://graph.microsoft.com/', '--output', 'json')
    if ($PSBoundParameters.ContainsKey('Body')) {
        $json = $Body | ConvertTo-Json -Depth 30 -Compress
        $arguments += @('--headers', 'Content-Type=application/json', '--body', $json)
    }
    $raw = Invoke-AzureCli -Arguments $arguments
    if ([string]::IsNullOrWhiteSpace(($raw | Out-String))) { return $null }
    return ($raw | Out-String | ConvertFrom-Json -Depth 50)
}

function Get-GraphCollection {
    param([Parameter(Mandatory)][string]$Uri)
    $next = $Uri
    $pageCount = 0
    while (-not [string]::IsNullOrWhiteSpace($next)) {
        $pageCount++
        if ($pageCount -gt 100) { throw "Microsoft Graph pagination exceeded 100 pages: $Uri" }
        $response = Invoke-Graph -Method GET -Uri $next
        foreach ($item in @($response.value)) { Write-Output $item }
        $nextProperty = $response.PSObject.Properties['@odata.nextLink']
        $next = if ($null -eq $nextProperty) { '' } else { [string]$nextProperty.Value }
    }
}

function Get-GraphCollectionFirst {
    param([Parameter(Mandatory)][string]$Uri)
    $items = @(Get-GraphCollection -Uri $Uri)
    if ($items.Count -eq 0) { return $null }
    if ($items.Count -gt 1) { throw "Expected one Graph object, found $($items.Count): $Uri" }
    return $items[0]
}

function Get-ApplicationByName {
    param([Parameter(Mandatory)][string]$DisplayName)
    $escaped = $DisplayName.Replace("'", "''")
    $filter = [uri]::EscapeDataString("displayName eq '$escaped'")
    return Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=$filter&`$select=id,appId,displayName,api,appRoles,passwordCredentials,keyCredentials,tags"
}

function Ensure-ServicePrincipal {
    param([Parameter(Mandatory)][string]$AppId)

    $filter = [uri]::EscapeDataString("appId eq '$AppId'")
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $servicePrincipal = Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$filter&`$select=id,appId,displayName,servicePrincipalType"
        if ($null -ne $servicePrincipal) { return $servicePrincipal }
        if ($attempt -eq 1 -and $PSCmdlet.ShouldProcess("service principal for $AppId", 'Create')) {
            Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals' -Body @{ appId = $AppId } | Out-Null
        }
        if ($attempt -lt 12) { Start-Sleep -Seconds 3 }
    }
    throw "Entra service principal for application '$AppId' did not become available."
}

$displayName = [string]$config.entraAwsFederationAppDisplayName
$application = Get-ApplicationByName -DisplayName $displayName
if ($null -eq $application) {
    if (-not $PSCmdlet.ShouldProcess($displayName, 'Create dedicated Entra AWS federation resource application')) {
        Write-Host "WhatIf: would create Entra federation application '$displayName'."
        return
    }
    $application = Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/applications' -Body ([ordered]@{
        displayName = $displayName
        signInAudience = 'AzureADMyOrg'
        tags = @('loan-document-platform', "environment:$($config.environment)", 'kind:aws-federation-resource')
    })
    $application = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)?`$select=id,appId,displayName,api,appRoles,passwordCredentials,keyCredentials,tags"
    Write-Host "Created dedicated Entra AWS federation resource application '$displayName'."
} else {
    Write-Host "Using dedicated Entra AWS federation resource application '$displayName'."
}

if (@($application.passwordCredentials).Count -gt 0 -or @($application.keyCredentials).Count -gt 0) {
    throw "Federation application '$displayName' contains a reusable credential. Remove it after review; this script will not use or rotate it."
}

$roleValue = 'AwsFederation.AssumeRole'
$existingRole = @($application.appRoles) | Where-Object { $_.value -eq $roleValue } | Select-Object -First 1
$roleId = if ($null -eq $existingRole) { [guid]::NewGuid().Guid } else { [string]$existingRole.id }
$audience = "api://$($application.appId)"
$applicationPatch = [ordered]@{
    identifierUris = @($audience)
    api = [ordered]@{
        requestedAccessTokenVersion = 1
        oauth2PermissionScopes = @()
    }
    appRoles = @(
        [ordered]@{
            id = $roleId
            value = $roleValue
            displayName = $roleValue
            description = 'Permit only the assigned Azure API managed identity to request the AWS federation audience.'
            isEnabled = $true
            allowedMemberTypes = @('Application')
        }
    )
    tags = @('loan-document-platform', "environment:$($config.environment)", 'kind:aws-federation-resource')
}
if ($PSCmdlet.ShouldProcess($displayName, 'Enforce v1 token audience and application role')) {
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Body $applicationPatch | Out-Null
}

$federationServicePrincipal = Ensure-ServicePrincipal -AppId ([string]$application.appId)
if ($PSCmdlet.ShouldProcess($federationServicePrincipal.id, 'Require explicit application-role assignment for federation tokens')) {
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($federationServicePrincipal.id)" -Body @{
        appRoleAssignmentRequired = $true
    } | Out-Null
}
$identityServicePrincipal = $null
for ($attempt = 1; $attempt -le 12; $attempt++) {
    try {
        $identityServicePrincipal = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($identity.principalId)?`$select=id,appId,displayName,servicePrincipalType"
        break
    } catch {
        if ($attempt -eq 12) { throw }
        Start-Sleep -Seconds 3
    }
}
if ([string]$identityServicePrincipal.id -ne [string]$identity.principalId) {
    throw 'The managed identity service principal does not match the user-assigned identity principal ID.'
}
if ([string]$identityServicePrincipal.servicePrincipalType -ne 'ManagedIdentity') {
    throw "Principal '$($identity.principalId)' is not an Entra managed identity service principal."
}

$assignmentReady = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
    $assignments = @(Get-GraphCollection -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($federationServicePrincipal.id)/appRoleAssignedTo")
    $unexpectedAssignments = @($assignments | Where-Object { [string]$_.principalId -ne [string]$identity.principalId })
    if ($unexpectedAssignments.Count -gt 0) {
        throw "Dedicated federation application '$displayName' is assigned to an unexpected principal. Refusing to preserve broader token access."
    }
    $assignment = $assignments | Where-Object {
        [string]$_.principalId -eq [string]$identity.principalId -and
        [string]$_.resourceId -eq [string]$federationServicePrincipal.id -and
        [string]$_.appRoleId -eq [string]$roleId
    } | Select-Object -First 1
    if ($null -ne $assignment) { $assignmentReady = $true; break }
    if (-not $PSCmdlet.ShouldProcess($identity.name, "Assign $roleValue")) { return }
    try {
        Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($federationServicePrincipal.id)/appRoleAssignedTo" -Body ([ordered]@{
            principalId = [string]$identity.principalId
            resourceId = [string]$federationServicePrincipal.id
            appRoleId = [string]$roleId
        }) | Out-Null
        $assignmentReady = $true
        Write-Host "Assigned the dedicated federation role to managed identity '$($identity.name)'."
        break
    } catch {
        if ($attempt -eq 12) { throw }
        Start-Sleep -Seconds 5
    }
}
if (-not $assignmentReady) {
    throw 'The managed-identity federation role assignment did not become available after bounded propagation retries.'
}

if ([string]::IsNullOrWhiteSpace($OutputFile)) {
    $OutputFile = Join-Path $root ".local/entra-aws-federation-$($config.environment).json"
}
$outputDirectory = Split-Path -Parent $OutputFile
if ($outputDirectory) { [System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null }
$output = [ordered]@{
    tenantId = [string]$config.entraTenantId
    issuer = "https://sts.windows.net/$($config.entraTenantId)/"
    audience = $audience
    applicationObjectId = [string]$application.id
    applicationClientId = [string]$application.appId
    servicePrincipalObjectId = [string]$federationServicePrincipal.id
    applicationRoleId = $roleId
    managedIdentity = [ordered]@{
        resourceId = [string]$identity.id
        clientId = [string]$identity.clientId
        principalId = [string]$identity.principalId
    }
}
[System.IO.File]::WriteAllText($OutputFile, ($output | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Wrote non-secret federation identifiers to $OutputFile"
Write-Host 'No client secret, certificate private key, managed-identity token, or AWS credential was created or persisted.'
