<#
.SYNOPSIS
Bootstraps exact GitHub-environment OIDC federation for Azure deployment.

.OUTPUTS
Writes ignored non-secret JSON with
`{tenantId,subscriptionId,resourceGroupId,applicationObjectId,clientId,
servicePrincipalObjectId,federatedCredential:{issuer,subject,audience},
roleDefinitionId}` and matching protected-environment variables. It never creates
an application secret or stores a GitHub/Azure token.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$OutputFile = '',
    [switch]$SkipGitHubVariables
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
Assert-Command -Name az -InstallHint 'Run scripts/bootstrap.ps1.'
if (-not $SkipGitHubVariables) { Assert-Command -Name gh -InstallHint 'Install GitHub CLI and run gh auth login.' }

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

$requiredProviders = @(
    'Microsoft.App',
    'Microsoft.ContainerRegistry',
    'Microsoft.Consumption',
    'Microsoft.Insights',
    'Microsoft.ManagedIdentity',
    'Microsoft.OperationalInsights',
    'Microsoft.Web'
)
foreach ($provider in $requiredProviders) {
    $state = & az provider show --namespace $provider --subscription $config.azureSubscriptionId --query registrationState --output tsv
    if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Azure resource provider '$provider'." }
    if ($state -ne 'Registered' -and $PSCmdlet.ShouldProcess($provider, 'Register Azure resource provider for OIDC deployments')) {
        & az provider register --namespace $provider --subscription $config.azureSubscriptionId --wait --output none
        if ($LASTEXITCODE -ne 0) { throw "Failed to register Azure resource provider '$provider'." }
    }
}

$groupExists = & az group exists --name $config.azureResourceGroupName --subscription $config.azureSubscriptionId --output tsv
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Azure resource group '$($config.azureResourceGroupName)'." }
if ($groupExists -ne 'true') {
    if (-not $PSCmdlet.ShouldProcess($config.azureResourceGroupName, 'Create Azure deployment resource group')) {
        Write-Host "WhatIf: would create resource group '$($config.azureResourceGroupName)'."
        return
    }
    & az group create `
        --name $config.azureResourceGroupName `
        --location $config.azureLocation `
        --subscription $config.azureSubscriptionId `
        --tags Application=loan-document-platform Environment=$($config.environment) ManagedBy=script `
        --output none
    if ($LASTEXITCODE -ne 0) { throw "Failed to create resource group '$($config.azureResourceGroupName)'." }
}
$resourceGroupId = "/subscriptions/$($config.azureSubscriptionId)/resourceGroups/$($config.azureResourceGroupName)"
$registryRaw = & az acr show `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureContainerRegistryName `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Azure Container Registry '$($config.azureContainerRegistryName)' is missing. Run deploy-azure.ps1 -FoundationOnly with an operator first."
}
$registry = $registryRaw | Out-String | ConvertFrom-Json -Depth 20
$identityRaw = & az identity show `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureApiManagedIdentityName `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Azure API managed identity '$($config.azureApiManagedIdentityName)' is missing. Run deploy-azure.ps1 -FoundationOnly with an operator first."
}
$apiIdentity = $identityRaw | Out-String | ConvertFrom-Json -Depth 20
if ([string]::IsNullOrWhiteSpace([string]$registry.id) -or [string]::IsNullOrWhiteSpace([string]$apiIdentity.principalId)) {
    throw 'Azure foundation registry or managed-identity identifiers are incomplete.'
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
    $raw = & az @arguments
    if ($LASTEXITCODE -ne 0) { throw "Microsoft Graph request failed: $Method $Uri" }
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

function Ensure-ServicePrincipal {
    param([Parameter(Mandatory)][string]$AppId)
    $filter = [uri]::EscapeDataString("appId eq '$AppId'")
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $sp = Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/servicePrincipals?`$filter=$filter&`$select=id,appId,displayName,servicePrincipalType"
        if ($null -ne $sp) { return $sp }
        if ($attempt -eq 1 -and $PSCmdlet.ShouldProcess("service principal for $AppId", 'Create')) {
            Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/servicePrincipals' -Body @{ appId = $AppId } | Out-Null
        }
        if ($attempt -lt 12) { Start-Sleep -Seconds 3 }
    }
    throw "Entra service principal for application '$AppId' did not become available."
}

$displayName = [string]$config.entraGitHubDeploymentAppDisplayName
$escapedName = $displayName.Replace("'", "''")
$filter = [uri]::EscapeDataString("displayName eq '$escapedName'")
$application = Get-GraphCollectionFirst -Uri "https://graph.microsoft.com/v1.0/applications?`$filter=$filter&`$select=id,appId,displayName,passwordCredentials,keyCredentials,tags"
if ($null -eq $application) {
    if (-not $PSCmdlet.ShouldProcess($displayName, 'Create dedicated GitHub-to-Azure deployment application')) {
        Write-Host "WhatIf: would create GitHub deployment application '$displayName'."
        return
    }
    $created = Invoke-Graph -Method POST -Uri 'https://graph.microsoft.com/v1.0/applications' -Body ([ordered]@{
        displayName = $displayName
        signInAudience = 'AzureADMyOrg'
        tags = @('loan-document-platform', "environment:$($config.environment)", 'kind:github-azure-deployment')
    })
    $application = Invoke-Graph -Method GET -Uri "https://graph.microsoft.com/v1.0/applications/$($created.id)?`$select=id,appId,displayName,passwordCredentials,keyCredentials,tags"
    Write-Host "Created dedicated GitHub-to-Azure deployment application '$displayName'."
} else {
    Write-Host "Using dedicated GitHub-to-Azure deployment application '$displayName'."
}

if (@($application.passwordCredentials).Count -gt 0 -or @($application.keyCredentials).Count -gt 0) {
    throw "GitHub deployment application '$displayName' contains a reusable credential. Remove it after review; OIDC must be the only credential."
}
if ($PSCmdlet.ShouldProcess($displayName, 'Enforce dedicated deployment application metadata')) {
    Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)" -Body @{
        tags = @('loan-document-platform', "environment:$($config.environment)", 'kind:github-azure-deployment')
    } | Out-Null
}

$credentialName = "github-environment-$($config.githubEnvironment)"
$expectedIssuer = 'https://token.actions.githubusercontent.com'
$expectedAudience = 'api://AzureADTokenExchange'
$expectedSubject = "repo:$($config.githubOwner)/$($config.repositoryName):environment:$($config.githubEnvironment)"
$allCredentials = @(Get-GraphCollection -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/federatedIdentityCredentials")
$unexpectedCredentials = @($allCredentials | Where-Object { [string]$_.name -ne $credentialName })
if ($unexpectedCredentials.Count -gt 0) {
    throw "Dedicated GitHub deployment application '$displayName' has unexpected federated credentials. Refusing to preserve a broader subject."
}
$credential = $allCredentials | Where-Object { [string]$_.name -eq $credentialName } | Select-Object -First 1
$credentialBody = [ordered]@{
    name = $credentialName
    description = "Exact GitHub environment trust for $($config.githubOwner)/$($config.repositoryName)."
    issuer = $expectedIssuer
    subject = $expectedSubject
    audiences = @($expectedAudience)
}
if ($null -eq $credential) {
    if ($PSCmdlet.ShouldProcess($expectedSubject, 'Create exact GitHub OIDC federated credential')) {
        Invoke-Graph -Method POST -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/federatedIdentityCredentials" -Body $credentialBody | Out-Null
    }
} else {
    $matches = (
        [string]$credential.issuer -ceq $expectedIssuer -and
        [string]$credential.subject -ceq $expectedSubject -and
        @($credential.audiences).Count -eq 1 -and
        [string](@($credential.audiences)[0]) -ceq $expectedAudience
    )
    if (-not $matches -and $PSCmdlet.ShouldProcess($expectedSubject, 'Tighten GitHub OIDC federated credential')) {
        Invoke-Graph -Method PATCH -Uri "https://graph.microsoft.com/v1.0/applications/$($application.id)/federatedIdentityCredentials/$($credential.id)" -Body $credentialBody | Out-Null
    }
}

$servicePrincipal = Ensure-ServicePrincipal -AppId ([string]$application.appId)
$roleName = "loan-document-azure-deployer-$($config.environment)"
$roleRaw = & az role definition list --name $roleName --scope $resourceGroupId --output json
if ($LASTEXITCODE -ne 0) { throw "Failed to inspect custom Azure role '$roleName'." }
$roles = @($roleRaw | Out-String | ConvertFrom-Json -Depth 50)
if ($roles.Count -gt 1) { throw "More than one Azure role is named '$roleName'." }
$roleDefinitionGuid = if ($roles.Count -eq 1) { [string]$roles[0].name } else { [guid]::NewGuid().Guid }
$roleDefinitionId = "/subscriptions/$($config.azureSubscriptionId)/providers/Microsoft.Authorization/roleDefinitions/$roleDefinitionGuid"
$roleBody = [ordered]@{
    properties = [ordered]@{
        roleName = $roleName
        description = 'Deploy only the Azure loan-platform resources in the environment resource group; no tenant or subscription administration.'
        type = 'CustomRole'
        assignableScopes = @($resourceGroupId)
        permissions = @(
            [ordered]@{
                actions = @(
                    'Microsoft.Resources/subscriptions/resourceGroups/read',
                    'Microsoft.Resources/deployments/*',
                    'Microsoft.ManagedIdentity/userAssignedIdentities/*',
                    'Microsoft.ContainerRegistry/registries/*',
                    'Microsoft.Consumption/budgets/*',
                    'Microsoft.OperationalInsights/workspaces/*',
                    'Microsoft.Insights/components/*',
                    'Microsoft.Insights/diagnosticSettings/*',
                    'Microsoft.Insights/actionGroups/*',
                    'Microsoft.Insights/metricAlerts/*',
                    'Microsoft.App/*',
                    'Microsoft.Web/staticSites/*',
                    'Microsoft.Authorization/roleAssignments/read',
                    'Microsoft.Authorization/roleDefinitions/read'
                )
                notActions = @()
                dataActions = @()
                notDataActions = @()
            }
        )
    }
}
if ($PSCmdlet.ShouldProcess($roleName, 'Create or update resource-group deployment role')) {
    $roleJson = $roleBody | ConvertTo-Json -Depth 30 -Compress
    & az rest `
        --method PUT `
        --url "https://management.azure.com${roleDefinitionId}?api-version=2022-04-01" `
        --headers Content-Type=application/json `
        --body $roleJson `
        --output none
    if ($LASTEXITCODE -ne 0) { throw "Failed to create or update custom Azure role '$roleName'." }
}

function Ensure-RoleAssignment {
    param(
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][string]$RoleId,
        [Parameter(Mandatory)][string]$Scope,
        [Parameter(Mandatory)][string]$Description
    )
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        $assignmentRaw = & az role assignment list `
            --assignee $PrincipalId `
            --scope $Scope `
            --output json
        if ($LASTEXITCODE -eq 0) {
            $assignments = @($assignmentRaw | Out-String | ConvertFrom-Json -Depth 30)
            $matches = @($assignments | Where-Object {
                [string]$_.roleDefinitionId -ieq $RoleId -and [string]$_.scope -ieq $Scope
            })
            if ($matches.Count -gt 1) { throw "Multiple $Description role assignments exist at the exact scope '$Scope'." }
            if ($matches.Count -eq 1) { return }
        } elseif ($attempt -eq 12) {
            throw "Failed to inspect $Description role assignments after bounded retries."
        }

        if ($PSCmdlet.ShouldProcess($PrincipalId, "Assign $Description at exact scope $Scope")) {
            & az role assignment create `
                --assignee-object-id $PrincipalId `
                --assignee-principal-type ServicePrincipal `
                --role $RoleId `
                --scope $Scope `
                --output none
            if ($LASTEXITCODE -eq 0) { continue }
        } else {
            return
        }
        if ($attempt -lt 12) { Start-Sleep -Seconds 5 }
    }
    throw "Failed to assign $Description at exact scope '$Scope' after bounded propagation retries."
}

Ensure-RoleAssignment -PrincipalId ([string]$servicePrincipal.id) -RoleId $roleDefinitionId -Scope $resourceGroupId -Description $roleName
$acrPushRoleId = '/subscriptions/' + $config.azureSubscriptionId + '/providers/Microsoft.Authorization/roleDefinitions/8311e382-0749-4cb8-b61a-304f252e45ec'
Ensure-RoleAssignment -PrincipalId ([string]$servicePrincipal.id) -RoleId $acrPushRoleId -Scope ([string]$registry.id) -Description 'AcrPush'
$acrPullRoleId = '/subscriptions/' + $config.azureSubscriptionId + '/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d'
Ensure-RoleAssignment -PrincipalId ([string]$apiIdentity.principalId) -RoleId $acrPullRoleId -Scope ([string]$registry.id) -Description 'API managed-identity AcrPull'

if (-not $SkipGitHubVariables) {
    & gh auth status | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }
    $repository = "$($config.githubOwner)/$($config.repositoryName)"
    $repositoryJson = & gh api "repos/$repository"
    if ($LASTEXITCODE -ne 0) { throw "Cannot read GitHub repository '$repository'." }
    $repositoryInfo = $repositoryJson | ConvertFrom-Json -Depth 20
    if ([string]$repositoryInfo.visibility -ne [string]$config.githubRepositoryVisibility) {
        throw "GitHub repository visibility '$($repositoryInfo.visibility)' does not match configuration '$($config.githubRepositoryVisibility)'."
    }

    & gh api "repos/$repository/environments/$($config.githubEnvironment)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Protected GitHub environment '$($config.githubEnvironment)' is missing. Run scripts/provision-github.ps1 before Azure federation bootstrap."
    }

    $variables = [ordered]@{
        AZURE_CLIENT_ID = [string]$application.appId
        AZURE_TENANT_ID = [string]$config.entraTenantId
        AZURE_SUBSCRIPTION_ID = [string]$config.azureSubscriptionId
        AZURE_RESOURCE_GROUP = [string]$config.azureResourceGroupName
        AZURE_LOCATION = [string]$config.azureLocation
        AZURE_CONTAINER_REGISTRY = [string]$config.azureContainerRegistryName
        AZURE_CONTAINER_APPS_ENVIRONMENT = [string]$config.azureContainerAppsEnvironmentName
        AZURE_API_APP_NAME = [string]$config.azureApiAppName
        AZURE_API_MANAGED_IDENTITY_NAME = [string]$config.azureApiManagedIdentityName
        AZURE_STATIC_WEB_APP_NAME = [string]$config.azureStaticWebAppName
    }
    foreach ($entry in $variables.GetEnumerator()) {
        if ($PSCmdlet.ShouldProcess("$repository/$($config.githubEnvironment)", "Set non-secret variable $($entry.Key)")) {
            $entry.Value | & gh variable set $entry.Key --repo $repository --env $config.githubEnvironment
            if ($LASTEXITCODE -ne 0) { throw "Failed to set GitHub environment variable '$($entry.Key)'." }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($OutputFile)) {
    $OutputFile = Join-Path $root ".local/github-azure-$($config.environment).json"
}
$outputDirectory = Split-Path -Parent $OutputFile
if ($outputDirectory) { [System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null }
$output = [ordered]@{
    tenantId = [string]$config.entraTenantId
    subscriptionId = [string]$config.azureSubscriptionId
    resourceGroupId = $resourceGroupId
    applicationObjectId = [string]$application.id
    clientId = [string]$application.appId
    servicePrincipalObjectId = [string]$servicePrincipal.id
    federatedCredential = [ordered]@{
        issuer = $expectedIssuer
        subject = $expectedSubject
        audience = $expectedAudience
    }
    roleDefinitionId = $roleDefinitionId
}
[System.IO.File]::WriteAllText($OutputFile, ($output | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Wrote non-secret GitHub/Azure federation identifiers to $OutputFile"
Write-Host 'No GitHub secret, Entra client secret, certificate private key, Azure deployment token, or AWS credential was created or persisted.'
