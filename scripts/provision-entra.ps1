[CmdletBinding()]
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

$account = & az account show --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Azure CLI is not signed in. Run az login --tenant <tenant-id>.' }
if ($account.tenantId -ne $config.entraTenantId) {
    throw "Azure CLI tenant $($account.tenantId) does not match $($config.entraTenantId)."
}

function Invoke-Graph {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST', 'PATCH')][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [object]$Body
    )
    $arguments = @(
        'rest', '--method', $Method, '--uri', $Uri,
        '--resource', 'https://graph.microsoft.com/', '--output', 'json'
    )
    if ($PSBoundParameters.ContainsKey('Body')) {
        $json = $Body | ConvertTo-Json -Depth 30 -Compress
        $arguments += @('--headers', 'Content-Type=application/json', '--body', $json)
    }
    try {
        $raw = Invoke-AzureCli -Arguments $arguments
    } catch {
        throw "Microsoft Graph $Method request failed. $($_.Exception.Message)"
    }
    if ([string]::IsNullOrWhiteSpace(($raw | Out-String))) { return $null }
    return ($raw | Out-String | ConvertFrom-Json -Depth 50)
}

function Get-GraphCollectionFirst {
    param([Parameter(Mandatory)][string]$Uri)
    $response = Invoke-Graph -Method GET -Uri $Uri
    if ($null -eq $response.value -or $response.value.Count -eq 0) { return $null }
    if ($response.value.Count -gt 1) { throw "Expected one Graph object, found $($response.value.Count): $Uri" }
    return $response.value[0]
}

function Get-ApplicationByName {
    param([Parameter(Mandatory)][string]$DisplayName)
    $escapedName = $DisplayName.Replace("'", "''")
    $filter = [uri]::EscapeDataString("displayName eq '$escapedName'")
    return Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=$filter&`$select=id,appId,displayName,api,appRoles,keyCredentials"
}

function Ensure-Application {
    param(
        [Parameter(Mandatory)][string]$DisplayName,
        [Parameter(Mandatory)][ValidateSet('api', 'spa', 'service')][string]$Kind
    )
    $app = Get-ApplicationByName -DisplayName $DisplayName
    if ($null -eq $app) {
        $body = [ordered]@{
            displayName = $DisplayName
            signInAudience = 'AzureADMyOrg'
            tags = @('loan-document-platform', "environment:$($config.environment)", "kind:$Kind")
        }
        $app = Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/applications' -Body $body
        Write-Host "Created Entra application: $DisplayName ($($app.appId))"
    } else {
        Write-Host "Using Entra application: $DisplayName ($($app.appId))"
    }
    return $app
}

function Ensure-ServicePrincipal {
    param([Parameter(Mandatory)][string]$AppId)
    $filter = [uri]::EscapeDataString("appId eq '$AppId'")
    $sp = Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$filter"
    if ($null -eq $sp) {
        $sp = Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals' -Body @{ appId = $AppId }
        Write-Host "Created service principal for $AppId"
    }
    return $sp
}

function Get-OrCreatePermissionId {
    param(
        [Parameter(Mandatory)][object[]]$Existing,
        [Parameter(Mandatory)][string]$Value
    )
    $match = $Existing | Where-Object { $_.value -eq $Value } | Select-Object -First 1
    if ($null -ne $match) { return [string]$match.id }
    return [guid]::NewGuid().Guid
}

$permissions = @(
    @{ Value = 'Loan.Create'; Description = 'Create or recreate an active loan instance.' },
    @{ Value = 'Loan.Read'; Description = 'Read current and archived loan instances.' },
    @{ Value = 'Loan.Archive'; Description = 'Archive an active loan and all documents.' },
    @{ Value = 'Document.Upload'; Description = 'Initialize, upload, replace, and complete documents.' },
    @{ Value = 'Document.Read'; Description = 'Read document status and PDF artifacts.' },
    @{ Value = 'Document.Archive'; Description = 'Archive a current document version.' },
    @{ Value = 'DataPoints.Read'; Description = 'Read and download extracted data points.' },
    @{ Value = 'Admin.Purge'; Description = 'Permanently purge data subject to hold and retention policy.' }
)
$spaPermissions = $permissions | Where-Object { $_.Value -ne 'Admin.Purge' }

$apiApp = Ensure-Application -DisplayName $config.entraApiAppDisplayName -Kind api
$existingScopes = @($apiApp.api.oauth2PermissionScopes)
$existingRoles = @($apiApp.appRoles)
$scopeByValue = @{}
$roleByValue = @{}

$scopes = foreach ($permission in $permissions) {
    $id = Get-OrCreatePermissionId -Existing $existingScopes -Value $permission.Value
    $scopeByValue[$permission.Value] = $id
    [ordered]@{
        id = $id
        value = $permission.Value
        type = 'Admin'
        isEnabled = $true
        adminConsentDisplayName = $permission.Value
        adminConsentDescription = $permission.Description
        userConsentDisplayName = $permission.Value
        userConsentDescription = $permission.Description
    }
}

$roles = foreach ($permission in $permissions) {
    $id = Get-OrCreatePermissionId -Existing $existingRoles -Value $permission.Value
    $roleByValue[$permission.Value] = $id
    [ordered]@{
        id = $id
        value = $permission.Value
        displayName = $permission.Value
        description = $permission.Description
        isEnabled = $true
        allowedMemberTypes = @('User', 'Application')
    }
}

$apiPatch = [ordered]@{
    identifierUris = @("api://$($apiApp.appId)")
    api = [ordered]@{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @($scopes)
    }
    appRoles = @($roles)
    optionalClaims = [ordered]@{
        accessToken = @(
            @{ name = 'idtyp'; essential = $true; additionalProperties = @() }
        )
    }
}
Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($apiApp.id)" -Body $apiPatch | Out-Null
$apiApp = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/applications/$($apiApp.id)"
$apiSp = Ensure-ServicePrincipal -AppId $apiApp.appId
Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$($apiSp.id)" -Body @{ appRoleAssignmentRequired = $true } | Out-Null

$spaApp = Ensure-Application -DisplayName $config.entraSpaAppDisplayName -Kind spa
$redirectUris = @("https://$($config.uiHostName)/auth/callback")
if ($config.environment -ne 'prod') { $redirectUris += 'http://localhost:5173/auth/callback' }
$spaResourceAccess = foreach ($permission in $spaPermissions) {
    @{ id = $scopeByValue[$permission.Value]; type = 'Scope' }
}
$spaPatch = [ordered]@{
    spa = @{ redirectUris = $redirectUris }
    web = @{ redirectUris = @(); implicitGrantSettings = @{ enableAccessTokenIssuance = $false; enableIdTokenIssuance = $false } }
    requiredResourceAccess = @(
        @{ resourceAppId = $apiApp.appId; resourceAccess = @($spaResourceAccess) }
    )
}
Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($spaApp.id)" -Body $spaPatch | Out-Null
$spaSp = Ensure-ServicePrincipal -AppId $spaApp.appId

$preAuthorized = @(
    @{ appId = $spaApp.appId; delegatedPermissionIds = @($spaPermissions | ForEach-Object { $scopeByValue[$_.Value] }) }
)
Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($apiApp.id)" -Body @{
    api = @{ requestedAccessTokenVersion = 2; oauth2PermissionScopes = @($scopes); preAuthorizedApplications = $preAuthorized }
} | Out-Null

function Ensure-AppRoleAssignment {
    param(
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][string]$ResourceServicePrincipalId,
        [Parameter(Mandatory)][string]$AppRoleId
    )
    $assignments = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$ResourceServicePrincipalId/appRoleAssignedTo"
    $exists = $assignments.value | Where-Object { $_.principalId -eq $PrincipalId -and $_.appRoleId -eq $AppRoleId }
    if (-not $exists) {
        Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/servicePrincipals/$ResourceServicePrincipalId/appRoleAssignedTo" -Body @{
            principalId = $PrincipalId
            resourceId = $ResourceServicePrincipalId
            appRoleId = $AppRoleId
        } | Out-Null
    }
}

$initialAdminId = [string]$config.entraInitialAdminUserObjectId
if ($initialAdminId -eq 'SIGNED_IN_USER') {
    $me = Invoke-Graph -Method GET -Uri 'https://graph.microsoft.com/v1.0/me?$select=id,userPrincipalName'
    $initialAdminId = $me.id
    Write-Host "Assigning API roles to signed-in user $($me.userPrincipalName) ($initialAdminId)"
}
foreach ($permission in $permissions) {
    Ensure-AppRoleAssignment -PrincipalId $initialAdminId -ResourceServicePrincipalId $apiSp.id -AppRoleId $roleByValue[$permission.Value]
}

$serviceApp = $null
$serviceSp = $null
if ([bool]$config.createServiceClient) {
    if ([string]::IsNullOrWhiteSpace([string]$config.serviceCertificatePublicPath)) {
        throw 'createServiceClient is true but serviceCertificatePublicPath is empty. Supply a CA-issued public .cer; no client secret will be created.'
    }
    $certificatePath = (Resolve-Path -LiteralPath $config.serviceCertificatePublicPath).Path
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath)
    if ($certificate.NotAfter.ToUniversalTime() -lt [DateTime]::UtcNow.AddDays(30)) {
        throw 'The service certificate expires in fewer than 30 days.'
    }

    $serviceApp = Ensure-Application -DisplayName $config.entraServiceAppDisplayName -Kind service
    # Certificate bytes are returned only for a single-object request with keyCredentials selected.
    # Preserve them so overlapping rotation credentials survive the collection-replacing PATCH.
    $serviceApp = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/applications/$($serviceApp.id)?`$select=id,appId,displayName,keyCredentials"
    $serviceResourceAccess = foreach ($permission in $spaPermissions) {
        @{ id = $roleByValue[$permission.Value]; type = 'Role' }
    }
    $keyCredentials = @($serviceApp.keyCredentials | Where-Object {
        [DateTime]$_.endDateTime -gt [DateTime]::UtcNow -and $_.customKeyIdentifier -ne [Convert]::ToBase64String($certificate.GetCertHash())
    })
    $keyCredentials += [ordered]@{
        customKeyIdentifier = [Convert]::ToBase64String($certificate.GetCertHash())
        displayName = "$($config.entraServiceAppDisplayName)-$($certificate.Thumbprint)"
        endDateTime = $certificate.NotAfter.ToUniversalTime().ToString('o')
        key = [Convert]::ToBase64String($certificate.RawData)
        keyId = [guid]::NewGuid().Guid
        startDateTime = $certificate.NotBefore.ToUniversalTime().ToString('o')
        type = 'AsymmetricX509Cert'
        usage = 'Verify'
    }
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($serviceApp.id)" -Body @{
        requiredResourceAccess = @(@{ resourceAppId = $apiApp.appId; resourceAccess = @($serviceResourceAccess) })
        keyCredentials = @($keyCredentials)
    } | Out-Null
    $serviceSp = Ensure-ServicePrincipal -AppId $serviceApp.appId
    foreach ($permission in $spaPermissions) {
        Ensure-AppRoleAssignment -PrincipalId $serviceSp.id -ResourceServicePrincipalId $apiSp.id -AppRoleId $roleByValue[$permission.Value]
    }
    Write-Host "Registered public certificate $($certificate.Thumbprint); private key was not read or uploaded."
}

if ([string]::IsNullOrWhiteSpace($OutputFile)) {
    $OutputFile = Join-Path $root ".local/entra-$($config.environment).json"
}
$outputDirectory = Split-Path -Parent $OutputFile
if ($outputDirectory) { [System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null }
$output = [ordered]@{
    tenantId = $config.entraTenantId
    issuer = "https://login.microsoftonline.com/$($config.entraTenantId)/v2.0"
    api = @{ applicationObjectId = $apiApp.id; clientId = $apiApp.appId; servicePrincipalObjectId = $apiSp.id; audience = $apiApp.appId; scopeBase = "api://$($apiApp.appId)" }
    spa = @{ applicationObjectId = $spaApp.id; clientId = $spaApp.appId; servicePrincipalObjectId = $spaSp.id; redirectUris = $redirectUris }
    service = if ($null -eq $serviceApp) { $null } else { @{ applicationObjectId = $serviceApp.id; clientId = $serviceApp.appId; servicePrincipalObjectId = $serviceSp.id } }
    permissionRoleIds = $roleByValue
    permissionScopeIds = $scopeByValue
}
[System.IO.File]::WriteAllText($OutputFile, ($output | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
Write-Host "Wrote non-secret Entra deployment IDs to $OutputFile"
Write-Host 'Production authorization requires the delegated scope and matching assigned app role.'
