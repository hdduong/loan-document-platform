<#
.SYNOPSIS
Deploys the Azure API foundation or final authenticated Container Apps revision.

.DESCRIPTION
Foundation mode creates identity, ACR, monitoring, the Container Apps environment,
and Static Web Apps, but deliberately creates no public Container App. The ignored
`.local/azure-{environment}.json` output contains only resource IDs, hostnames, and
the managed identity client/principal IDs needed by Entra and AWS federation.

Final mode requires `.local/entra-{environment}.json`,
`.local/entra-aws-federation-{environment}.json`, and the retained AWS stack
outputs. It builds the API image, resolves the tag to an immutable digest, and then
creates the authenticated Container App revision. `-BindCustomDomain` creates only
the Route 53 `asuid` TXT validation record and an Azure-managed certificate; it
does not cut the API CNAME over from the rollback endpoint.

.OUTPUTS
Writes ignored non-secret JSON with this stable shape:
`{schemaVersion,phase,environment,subscriptionId,resourceGroupName,location,
apiManagedIdentity:{resourceId,clientId,principalId},
containerRegistry:{resourceId,loginServer},
containerAppsEnvironment:{resourceId},
observability:{logAnalyticsWorkspaceResourceId,applicationInsightsResourceId},
costControls:{azureBudgetResourceId},staticWebApp:{resourceId,defaultHostname},api}`. `api` is null in foundation
mode. Final mode sets `{resourceId,fqdn,defaultUrl,immutableImage,
customDomain:{hostname,bound,dnsCutoverPerformed:false}}`.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [switch]$FoundationOnly,
    [switch]$SkipImageBuild,
    [switch]$SkipImageScan,
    [switch]$BindCustomDomain,
    [switch]$SkipDnsValidationRecord,
    [string]$ImageRepository = 'loan-document-api',
    [string]$ImageTag = '',
    [string]$EntraDeploymentFile = '',
    [string]$FederationDeploymentFile = '',
    [string]$OutputFile = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$template = Join-Path $root 'infra/azure/main.bicep'
Assert-Command -Name az -InstallHint 'Run scripts/bootstrap.ps1.'
if (-not $FoundationOnly -and [string]::IsNullOrWhiteSpace($ImageTag)) {
    Assert-Command -Name git -InstallHint 'Install Git or pass -ImageTag explicitly.'
}
if ($BindCustomDomain -and -not $SkipDnsValidationRecord) {
    Assert-Command -Name aws -InstallHint 'Install AWS CLI v2 to create the Route 53 validation TXT record.'
}
if ($SkipImageScan -and [string]$config.environment -eq 'prod') {
    throw 'Production deployment cannot skip the exact-image vulnerability and SBOM gate.'
}
if (-not (Test-Path -LiteralPath $template)) { throw "Azure Bicep template is missing: $template" }

if ($config.corporateCaBundlePath) {
    $caPath = (Resolve-Path -LiteralPath $config.corporateCaBundlePath).Path
    $env:REQUESTS_CA_BUNDLE = $caPath
    $env:SSL_CERT_FILE = $caPath
}

function Get-OptionalConfigValue {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][object]$Default
    )
    $property = $config.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value -or [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        return $Default
    }
    return $property.Value
}

function Assert-NonEmptyValue {
    param(
        [Parameter(Mandatory)][hashtable]$Values,
        [Parameter(Mandatory)][string[]]$Names,
        [Parameter(Mandatory)][string]$Context
    )
    foreach ($name in $Names) {
        if (-not $Values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace([string]$Values[$name])) {
            throw "$Context is missing required value '$name'."
        }
    }
}

function New-ArmParameterFile {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Values)
    $parameters = [ordered]@{}
    foreach ($entry in $Values.GetEnumerator()) {
        $parameters[$entry.Key] = @{ value = $entry.Value }
    }
    $document = [ordered]@{
        '$schema' = 'https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#'
        contentVersion = '1.0.0.0'
        parameters = $parameters
    }
    $path = Join-Path ([System.IO.Path]::GetTempPath()) "loan-azure-parameters-$([guid]::NewGuid().Guid).json"
    [System.IO.File]::WriteAllText($path, ($document | ConvertTo-Json -Depth 30), [System.Text.UTF8Encoding]::new($false))
    return $path
}

function Invoke-GroupDeployment {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Parameters
    )
    $parameterFile = New-ArmParameterFile -Values $Parameters
    try {
        $raw = & az deployment group create `
            --subscription $config.azureSubscriptionId `
            --resource-group $config.azureResourceGroupName `
            --name $Name `
            --mode Incremental `
            --template-file $template `
            --parameters "@$parameterFile" `
            --query properties.outputs `
            --output json
        if ($LASTEXITCODE -ne 0) { throw "Azure deployment '$Name' failed." }
        return ($raw | Out-String | ConvertFrom-Json -Depth 50)
    } finally {
        if (Test-Path -LiteralPath $parameterFile) { Remove-Item -LiteralPath $parameterFile -Force }
    }
}

function Get-DeploymentOutput {
    param(
        [Parameter(Mandatory)][object]$Outputs,
        [Parameter(Mandatory)][string]$Name
    )
    $property = $Outputs.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) { return '' }
    return [string]$property.Value.value
}

function New-SafeOutput {
    param(
        [Parameter(Mandatory)][ValidateSet('foundation', 'finalized')][string]$Phase,
        [Parameter(Mandatory)][object]$Outputs,
        [string]$ImmutableImage = '',
        [bool]$CustomDomainBound = $false
    )
    $apiResourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'apiResourceId'
    $apiFqdn = Get-DeploymentOutput -Outputs $Outputs -Name 'apiFqdn'
    return [ordered]@{
        schemaVersion = 1
        phase = $Phase
        environment = [string]$config.environment
        subscriptionId = [string]$config.azureSubscriptionId
        resourceGroupName = [string]$config.azureResourceGroupName
        location = [string]$config.azureLocation
        apiManagedIdentity = [ordered]@{
            resourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'apiManagedIdentityResourceId'
            clientId = Get-DeploymentOutput -Outputs $Outputs -Name 'apiManagedIdentityClientId'
            principalId = Get-DeploymentOutput -Outputs $Outputs -Name 'apiManagedIdentityPrincipalId'
        }
        containerRegistry = [ordered]@{
            resourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'containerRegistryResourceId'
            loginServer = Get-DeploymentOutput -Outputs $Outputs -Name 'containerRegistryLoginServer'
        }
        containerAppsEnvironment = [ordered]@{
            resourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'containerAppsEnvironmentResourceId'
        }
        observability = [ordered]@{
            logAnalyticsWorkspaceResourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'logAnalyticsWorkspaceResourceId'
            applicationInsightsResourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'applicationInsightsResourceId'
        }
        costControls = [ordered]@{
            azureBudgetResourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'azureBudgetResourceId'
        }
        staticWebApp = [ordered]@{
            resourceId = Get-DeploymentOutput -Outputs $Outputs -Name 'staticWebAppResourceId'
            defaultHostname = Get-DeploymentOutput -Outputs $Outputs -Name 'staticWebAppDefaultHostname'
        }
        api = if ([string]::IsNullOrWhiteSpace($apiResourceId)) {
            $null
        } else {
            [ordered]@{
                resourceId = $apiResourceId
                fqdn = $apiFqdn
                defaultUrl = "https://$apiFqdn"
                immutableImage = $ImmutableImage
                customDomain = [ordered]@{
                    hostname = [string]$config.apiHostName
                    bound = $CustomDomainBound
                    dnsCutoverPerformed = $false
                }
            }
        }
    }
}

function Write-SafeOutput {
    param([Parameter(Mandatory)][System.Collections.IDictionary]$Document)
    $directory = Split-Path -Parent $OutputFile
    if ($directory) { [System.IO.Directory]::CreateDirectory($directory) | Out-Null }
    $temporary = "$OutputFile.$([guid]::NewGuid().Guid).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, ($Document | ConvertTo-Json -Depth 30) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $OutputFile -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function ConvertTo-CanonicalDnsName {
    param([AllowEmptyString()][string]$Value)
    return $Value.Trim().TrimEnd('.').ToLowerInvariant()
}

function ConvertFrom-CertificateSubject {
    param([AllowEmptyString()][string]$Value)
    $subject = $Value.Trim()
    if ($subject.StartsWith('CN=', [StringComparison]::OrdinalIgnoreCase)) {
        $subject = $subject.Substring(3)
    }
    $separator = $subject.IndexOf(',')
    if ($separator -ge 0) { $subject = $subject.Substring(0, $separator) }
    return ConvertTo-CanonicalDnsName -Value $subject
}

function Get-ManagedApiCertificates {
    $raw = & az containerapp env certificate list `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureContainerAppsEnvironmentName `
        --managed-certificates-only `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect Container Apps managed certificates.' }
    return @($raw | Out-String | ConvertFrom-Json -Depth 30)
}

function Wait-ManagedApiCertificate {
    param(
        [Parameter(Mandatory)][string]$CertificateId,
        [Parameter(Mandatory)][string]$Hostname,
        [ValidateRange(1, 60)][int]$MaximumAttempts = 30
    )
    $canonicalHostname = ConvertTo-CanonicalDnsName -Value $Hostname
    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        $certificates = @(Get-ManagedApiCertificates)
        $subjectMatches = @($certificates | Where-Object {
            (ConvertFrom-CertificateSubject -Value ([string]$_.properties.subjectName)) -ceq $canonicalHostname
        })
        if ($subjectMatches.Count -gt 1) {
            throw "Multiple managed certificates exist for '$Hostname'; select and remove duplicates before deployment."
        }
        $idMatches = @($certificates | Where-Object { [string]$_.id -ieq $CertificateId })
        if ($idMatches.Count -ne 1) {
            throw "Managed certificate '$CertificateId' was not returned exactly once by Azure."
        }
        $certificate = $idMatches[0]
        if ((ConvertFrom-CertificateSubject -Value ([string]$certificate.properties.subjectName)) -cne $canonicalHostname) {
            throw "Managed certificate '$CertificateId' does not belong to '$Hostname'."
        }
        $state = [string]$certificate.properties.provisioningState
        if ($state -ceq 'Succeeded') { return $certificate }
        if ($state -notin @('Pending', 'InProgress')) {
            throw "Managed certificate '$CertificateId' entered terminal state '$state'."
        }
        if ($attempt -lt $MaximumAttempts) { Start-Sleep -Seconds 10 }
    }
    throw "Managed certificate '$CertificateId' did not reach Succeeded within the bounded wait."
}

function Get-LiveApiCustomDomainBinding {
    param([bool]$AllowIncompleteResume = $false)
    $appRaw = & az containerapp show `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --only-show-errors `
        --output json 2>&1
    $appExitCode = $LASTEXITCODE
    if ($appExitCode -ne 0) {
        $message = ($appRaw | Out-String).Trim()
        if ($message -match '(?i)(ResourceNotFound|could not be found|was not found)') {
            return $null
        }
        throw "Failed to inspect the existing Container App before deployment: $message"
    }
    $app = $appRaw | Out-String | ConvertFrom-Json -Depth 40

    $hostnameRaw = & az containerapp hostname list `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --only-show-errors `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect existing Container App custom-domain bindings.' }
    $bindings = @($hostnameRaw | Out-String | ConvertFrom-Json -Depth 30)
    $canonicalHostname = ConvertTo-CanonicalDnsName -Value ([string]$config.apiHostName)
    $unexpected = @($bindings | Where-Object {
        (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -cne $canonicalHostname
    })
    if ($unexpected.Count -gt 0) {
        $names = @($unexpected | ForEach-Object { [string]$_.name }) -join ', '
        throw "Container App has unexpected custom hostname bindings ($names); refusing an ARM deployment that would remove them."
    }
    $matches = @($bindings | Where-Object {
        (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -ceq $canonicalHostname
    })
    if ($matches.Count -gt 1) {
        throw "Container App returned duplicate bindings for '$($config.apiHostName)'."
    }
    if ($matches.Count -eq 0) { return $null }
    $binding = $matches[0]
    $bindingType = [string]$binding.bindingType
    $certificateId = [string]$binding.certificateId
    if ($bindingType -ceq 'SniEnabled' -and -not [string]::IsNullOrWhiteSpace($certificateId)) {
        return [pscustomobject]@{
            hostname = [string]$binding.name
            certificateId = $certificateId
            fqdn = [string]$app.properties.configuration.ingress.fqdn
            state = 'SniEnabled'
        }
    }
    if ($bindingType -ceq 'Disabled' -and [string]::IsNullOrWhiteSpace($certificateId) -and $AllowIncompleteResume) {
        return [pscustomobject]@{
            hostname = [string]$binding.name
            certificateId = ''
            fqdn = [string]$app.properties.configuration.ingress.fqdn
            state = 'Incomplete'
        }
    }
    if ($bindingType -ceq 'Disabled' -and [string]::IsNullOrWhiteSpace($certificateId)) {
        throw "Custom hostname '$($config.apiHostName)' is incomplete; resume explicitly with -BindCustomDomain."
    }
    throw "Existing custom hostname '$($config.apiHostName)' has unsupported binding state '$bindingType'."
}

if ([string]::IsNullOrWhiteSpace($OutputFile)) {
    $OutputFile = Join-Path $root ".local/azure-$($config.environment).json"
}
if ([string]::IsNullOrWhiteSpace($EntraDeploymentFile)) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
if ([string]::IsNullOrWhiteSpace($FederationDeploymentFile)) {
    $FederationDeploymentFile = Join-Path $root ".local/entra-aws-federation-$($config.environment).json"
}
$previousFinalState = $null
if (Test-Path -LiteralPath $OutputFile) {
    try {
        $candidateState = Get-Content -Raw -LiteralPath $OutputFile | ConvertFrom-Json -Depth 40
        if ([string]$candidateState.phase -eq 'finalized' -and
            [string]$candidateState.subscriptionId -eq [string]$config.azureSubscriptionId -and
            [string]$candidateState.resourceGroupName -eq [string]$config.azureResourceGroupName) {
            $previousFinalState = $candidateState
        }
    } catch {
        throw "Existing Azure deployment state is invalid and will not be overwritten: $OutputFile"
    }
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

& az bicep build --file $template --stdout | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Azure Bicep compilation failed.' }
if ($WhatIfPreference) {
    Write-Host "WhatIf: Bicep compiled; would deploy Azure foundation to '$($config.azureResourceGroupName)'."
    if (-not $FoundationOnly) { Write-Host 'WhatIf: would resolve federation/AWS outputs, build an immutable image, and deploy the authenticated API revision.' }
    if ($BindCustomDomain) { Write-Host 'WhatIf: would create the validation TXT record and bind an Azure-managed certificate without changing the API traffic record.' }
    return
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
if ($env:GITHUB_ACTIONS -ne 'true') {
    foreach ($provider in $requiredProviders) {
        $state = & az provider show --namespace $provider --subscription $config.azureSubscriptionId --query registrationState --output tsv
        if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Azure resource provider '$provider'." }
        if ($state -ne 'Registered') {
            if ($PSCmdlet.ShouldProcess($provider, 'Register Azure resource provider')) {
                & az provider register --namespace $provider --subscription $config.azureSubscriptionId --wait --output none
                if ($LASTEXITCODE -ne 0) { throw "Failed to register Azure resource provider '$provider'." }
            }
        }
    }
}

$groupExists = & az group exists --name $config.azureResourceGroupName --subscription $config.azureSubscriptionId --output tsv
if ($LASTEXITCODE -ne 0) { throw "Cannot inspect Azure resource group '$($config.azureResourceGroupName)'." }
if ($groupExists -ne 'true') {
    if ($env:GITHUB_ACTIONS -eq 'true') { throw "Azure resource group '$($config.azureResourceGroupName)' must be bootstrapped before GitHub deployment." }
    if ($PSCmdlet.ShouldProcess($config.azureResourceGroupName, 'Create Azure resource group')) {
        & az group create `
            --subscription $config.azureSubscriptionId `
            --name $config.azureResourceGroupName `
            --location $config.azureLocation `
            --tags Application=loan-document-platform Environment=$($config.environment) ManagedBy=script `
            --output none
        if ($LASTEXITCODE -ne 0) { throw "Failed to create Azure resource group '$($config.azureResourceGroupName)'." }
    }
}

$allowedOrigins = @("https://$($config.uiHostName)")
if ([string]$config.environment -ne 'prod') { $allowedOrigins += 'http://localhost:5173' }
$baseParameters = [ordered]@{
    environmentName = [string]$config.environment
    location = [string]$config.azureLocation
    containerRegistryName = [string]$config.azureContainerRegistryName
    containerAppsEnvironmentName = [string]$config.azureContainerAppsEnvironmentName
    apiAppName = [string]$config.azureApiAppName
    apiManagedIdentityName = [string]$config.azureApiManagedIdentityName
    staticWebAppName = [string]$config.azureStaticWebAppName
    deployApi = $false
    apiMinReplicas = [int]$config.azureApiMinReplicas
    apiMaxReplicas = [int]$config.azureApiMaxReplicas
    concurrentRequestsPerReplica = [int]$config.azureApiConcurrentRequestsPerReplica
    containerAppsZoneRedundant = [bool]$config.azureContainerAppsZoneRedundant
    logRetentionDays = [int](Get-OptionalConfigValue -Name 'logRetentionDays' -Default 90)
    containerRegistrySku = [string]$config.azureContainerRegistrySku
    staticWebAppSku = [string]$config.azureStaticWebAppSku
    allowedOrigins = $allowedOrigins
    apiHostName = [string]$config.apiHostName
    maximumUploadBytes = [int](Get-OptionalConfigValue -Name 'maximumUploadBytes' -Default 104857600)
    maximumInlineDataPointsBytes = [int](Get-OptionalConfigValue -Name 'maximumInlineDataPointsBytes' -Default 5242880)
    maximumQueryItems = [int](Get-OptionalConfigValue -Name 'maximumQueryItems' -Default 5000)
    maximumLoanArchiveDocuments = [int](Get-OptionalConfigValue -Name 'maximumLoanArchiveDocuments' -Default 500)
    maximumLoanArchiveManifestBytes = [int](Get-OptionalConfigValue -Name 'maximumLoanArchiveManifestBytes' -Default 4194304)
    alertEmail = [string]$config.alertEmail
    budgetEmail = [string]$config.budgetEmail
    monthlyBudgetUsd = [int]$config.azureMonthlyBudgetUsd
    budgetStartDate = [string]$config.azureBudgetStartDate
    enableAlerts = $true
    tags = [ordered]@{
        Repository = "$($config.githubOwner)/$($config.repositoryName)"
    }
}
$foundationName = "loan-azure-$($config.environment)-foundation"
if ($foundationName.Length -gt 64) { $foundationName = $foundationName.Substring(0, 64) }
if (-not $PSCmdlet.ShouldProcess($config.azureResourceGroupName, 'Deploy Azure identity and hosting foundation')) { return }
$foundationOutputs = Invoke-GroupDeployment -Name $foundationName -Parameters $baseParameters

$foundationRequired = @{
    apiManagedIdentityResourceId = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'apiManagedIdentityResourceId'
    apiManagedIdentityClientId = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'apiManagedIdentityClientId'
    apiManagedIdentityPrincipalId = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'apiManagedIdentityPrincipalId'
    containerRegistryResourceId = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'containerRegistryResourceId'
    containerRegistryLoginServer = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'containerRegistryLoginServer'
    azureBudgetResourceId = Get-DeploymentOutput -Outputs $foundationOutputs -Name 'azureBudgetResourceId'
}
Assert-NonEmptyValue -Values $foundationRequired -Names @(
    'apiManagedIdentityResourceId',
    'apiManagedIdentityClientId',
    'apiManagedIdentityPrincipalId',
    'containerRegistryResourceId',
    'containerRegistryLoginServer',
    'azureBudgetResourceId'
) -Context 'Azure foundation output'

$acrPullRoleId = "/subscriptions/$($config.azureSubscriptionId)/providers/Microsoft.Authorization/roleDefinitions/7f951dda-4ed3-4680-a7ca-43fe172d538d"
$acrPullReady = $false
$acrPullCreateSucceeded = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
    $assignmentRaw = & az role assignment list `
        --assignee $foundationRequired.apiManagedIdentityPrincipalId `
        --scope $foundationRequired.containerRegistryResourceId `
        --output json
    if ($LASTEXITCODE -ne 0) {
        if ($attempt -eq 12) { throw 'Failed to inspect the API managed identity ACR Pull assignment after bounded propagation retries.' }
        Start-Sleep -Seconds 5
        continue
    }
    $assignments = @($assignmentRaw | Out-String | ConvertFrom-Json -Depth 30)
    $matches = @($assignments | Where-Object {
        [string]$_.roleDefinitionId -ieq $acrPullRoleId -and [string]$_.scope -ieq [string]$foundationRequired.containerRegistryResourceId
    })
    if ($matches.Count -gt 1) { throw 'Multiple exact ACR Pull assignments exist for the API managed identity.' }
    if ($matches.Count -eq 1) { $acrPullReady = $true; break }
    if ($env:GITHUB_ACTIONS -ne 'true' -and -not $acrPullCreateSucceeded -and $PSCmdlet.ShouldProcess($foundationRequired.apiManagedIdentityPrincipalId, 'Assign ACR Pull on the exact API registry')) {
        & az role assignment create `
            --assignee-object-id $foundationRequired.apiManagedIdentityPrincipalId `
            --assignee-principal-type ServicePrincipal `
            --role $acrPullRoleId `
            --scope $foundationRequired.containerRegistryResourceId `
            --output none
        if ($LASTEXITCODE -eq 0) {
            $acrPullCreateSucceeded = $true
        } elseif ($attempt -eq 12) {
            throw 'Failed to assign ACR Pull to the API managed identity after bounded propagation retries.'
        }
    }
    if ($attempt -lt 12) { Start-Sleep -Seconds 5 }
}
if (-not $acrPullReady) {
    throw 'The exact API managed-identity ACR Pull assignment is missing. Run the foundation phase once with an operator before GitHub finalization.'
}

if ($FoundationOnly) {
    if ($null -ne $previousFinalState) {
        if ([string]$previousFinalState.apiManagedIdentity.principalId -ne [string]$foundationRequired.apiManagedIdentityPrincipalId) {
            throw 'Live Azure foundation identity differs from the prior finalized handoff.'
        }
        Write-Host 'Azure foundation is ready; the prior finalized deployment handoff was preserved.'
    } else {
        $foundationDocument = New-SafeOutput -Phase foundation -Outputs $foundationOutputs
        Write-SafeOutput -Document $foundationDocument
        Write-Host "Azure foundation ready; non-secret identifiers were written to $OutputFile"
    }
    Write-Host 'No public Container App was created. Use the managed identity output to provision Entra/AWS federation before finalization.'
    return
}
Write-Host 'Azure foundation prerequisites are ready; the existing deployment handoff will be replaced only after finalization succeeds.'

if (-not (Test-Path -LiteralPath $EntraDeploymentFile)) {
    throw "Entra API deployment output is missing: $EntraDeploymentFile. Run provision-entra.ps1 first."
}
if (-not (Test-Path -LiteralPath $FederationDeploymentFile)) {
    throw "Entra/AWS federation output is missing: $FederationDeploymentFile. Run provision-entra-federation.ps1 after the foundation phase."
}
$entra = Get-Content -Raw -LiteralPath $EntraDeploymentFile | ConvertFrom-Json -Depth 30
$federation = Get-Content -Raw -LiteralPath $FederationDeploymentFile | ConvertFrom-Json -Depth 30
if ([string]$entra.tenantId -ne [string]$config.entraTenantId -or [string]$federation.tenantId -ne [string]$config.entraTenantId) {
    throw 'Entra output tenant does not match the environment tenant.'
}
if ([string]$federation.managedIdentity.clientId -ne [string]$foundationRequired.apiManagedIdentityClientId -or
    [string]$federation.managedIdentity.principalId -ne [string]$foundationRequired.apiManagedIdentityPrincipalId) {
    throw 'Federation output is bound to a different Azure managed identity. Refusing to deploy the API.'
}

$allowedClientIds = @([string]$entra.spa.clientId)
if ($null -ne $entra.service -and -not [string]::IsNullOrWhiteSpace([string]$entra.service.clientId)) {
    $allowedClientIds += [string]$entra.service.clientId
}
if ($allowedClientIds | Where-Object { [string]::IsNullOrWhiteSpace($_) }) {
    throw 'Entra output contains an empty allowed client ID.'
}

Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null
$awsOutputs = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.platformStackName
Assert-NonEmptyValue -Values $awsOutputs -Names @(
    'AzureApiRuntimeRoleArn',
    'RegistryTableName',
    'SourceBucketName',
    'DataKeyArn',
    'UploadProcessorFunctionArn'
) -Context "AWS stack '$($config.platformStackName)'"

if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    $ImageTag = if ($env:GITHUB_SHA) { [string]$env:GITHUB_SHA } else { (& git -C $root rev-parse HEAD | Out-String).Trim() }
}
if ($ImageTag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') { throw "Image tag '$ImageTag' is invalid." }
if ($ImageRepository -notmatch '^[a-z0-9]+(?:[._/-][a-z0-9]+)*$') { throw "Image repository '$ImageRepository' is invalid." }

if (-not $SkipImageBuild) {
    if ($PSCmdlet.ShouldProcess("$($config.azureContainerRegistryName)/${ImageRepository}:$ImageTag", 'Build API image in ACR')) {
        & az acr build `
            --subscription $config.azureSubscriptionId `
            --registry $config.azureContainerRegistryName `
            --image "${ImageRepository}:$ImageTag" `
            --file (Join-Path $root 'services/azure_api/Dockerfile') `
            $root `
            --output none
        if ($LASTEXITCODE -ne 0) { throw 'Azure Container Registry image build failed.' }
    }
}
$digest = & az acr manifest show-metadata `
    --subscription $config.azureSubscriptionId `
    --registry $config.azureContainerRegistryName `
    --name "${ImageRepository}:$ImageTag" `
    --query digest `
    --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($digest | Out-String))) {
    throw "Cannot resolve ACR image '${ImageRepository}:$ImageTag' to an immutable digest."
}
$digest = ($digest | Out-String).Trim()
if ($digest -notmatch '^sha256:[0-9a-f]{64}$') { throw "ACR returned an invalid image digest for '${ImageRepository}:$ImageTag'." }
$immutableImage = "$($foundationRequired.containerRegistryLoginServer)/$ImageRepository@$digest"
$imageScanEvidence = $null
if (-not $SkipImageScan) {
    Assert-Command -Name trivy -InstallHint 'Install the pinned Trivy CLI or use the production GitHub workflow.'
    $requiredTrivyVersion = '0.72.0'
    $trivyVersion = (& trivy --version | Select-Object -First 1 | Out-String).Trim()
    if ($trivyVersion -ne "Version: $requiredTrivyVersion") {
        throw "Trivy $requiredTrivyVersion is required; found '$trivyVersion'."
    }
    & az acr login `
        --subscription $config.azureSubscriptionId `
        --name $config.azureContainerRegistryName `
        --output none
    if ($LASTEXITCODE -ne 0) { throw 'Azure Container Registry login for image scanning failed.' }
    & trivy image `
        --scanners vuln `
        --severity HIGH,CRITICAL `
        --ignore-unfixed `
        --exit-code 1 `
        --timeout 10m `
        --no-progress `
        $immutableImage
    if ($LASTEXITCODE -ne 0) { throw 'The immutable API image failed the actionable HIGH/CRITICAL vulnerability gate.' }

    $localDirectory = Join-Path $root '.local'
    [IO.Directory]::CreateDirectory($localDirectory) | Out-Null
    $safeDigest = $digest.Replace(':', '-')
    $sbomPath = Join-Path $localDirectory "api-sbom-$($config.environment)-$safeDigest.cdx.json"
    & trivy image `
        --format cyclonedx `
        --output $sbomPath `
        --timeout 10m `
        --no-progress `
        $immutableImage
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $sbomPath)) {
        throw 'CycloneDX SBOM generation failed for the immutable API image.'
    }
    $imageScanEvidence = [ordered]@{
        passed = $true
        immutableImage = $immutableImage
        scanner = $trivyVersion
        policy = 'no-fixable-high-or-critical-vulnerabilities'
        scannedAtUtc = [DateTime]::UtcNow.ToString('o')
        sbomFile = Split-Path -Leaf $sbomPath
    }
    $evidencePath = Join-Path $localDirectory "api-image-scan-$($config.environment).json"
    [IO.File]::WriteAllText($evidencePath, ($imageScanEvidence | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

$existingApiCustomDomain = Get-LiveApiCustomDomainBinding -AllowIncompleteResume ([bool]$BindCustomDomain)
$finalParameters = [ordered]@{}
foreach ($entry in $baseParameters.GetEnumerator()) { $finalParameters[$entry.Key] = $entry.Value }
$finalParameters.deployApi = $true
$finalParameters.containerImage = $immutableImage
$finalParameters.entraTenantId = [string]$config.entraTenantId
$finalParameters.entraApiClientId = [string]$entra.api.clientId
$finalParameters.entraApiAudience = [string]$entra.api.audience
$finalParameters.allowedClientIds = @($allowedClientIds)
$finalParameters.deniedClientIds = @()
$finalParameters.apiCustomDomainCertificateId = if (
    $null -ne $existingApiCustomDomain -and [string]$existingApiCustomDomain.state -ceq 'SniEnabled'
) { [string]$existingApiCustomDomain.certificateId } else { '' }
$finalParameters.awsRegion = [string]$config.awsRegion
$finalParameters.awsFederationAudience = [string]$federation.audience
$finalParameters.awsRoleArn = [string]$awsOutputs.AzureApiRuntimeRoleArn
$finalParameters.registryTableName = [string]$awsOutputs.RegistryTableName
$finalParameters.sourceBucketName = [string]$awsOutputs.SourceBucketName
$finalParameters.dataKeyArn = [string]$awsOutputs.DataKeyArn
$finalParameters.uploadProcessorArn = [string]$awsOutputs.UploadProcessorFunctionArn
$finalParameters.awsSessionDurationSeconds = [int](Get-OptionalConfigValue -Name 'azureAwsSessionDurationSeconds' -Default 3600)
$finalParameters.awsCredentialRefreshSeconds = [int](Get-OptionalConfigValue -Name 'azureAwsCredentialRefreshSeconds' -Default 300)
$finalName = "loan-azure-$($config.environment)-api-$($ImageTag.Substring(0, [Math]::Min(12, $ImageTag.Length)))"
if ($finalName.Length -gt 64) { $finalName = $finalName.Substring(0, 64) }
if (-not $PSCmdlet.ShouldProcess($config.azureApiAppName, "Deploy authenticated API revision $digest")) { return }
$finalOutputs = Invoke-GroupDeployment -Name $finalName -Parameters $finalParameters

if ((Get-DeploymentOutput -Outputs $finalOutputs -Name 'apiManagedIdentityPrincipalId') -ne [string]$federation.managedIdentity.principalId) {
    throw 'Final Azure deployment changed the managed identity principal. AWS trust was not updated; refusing to continue.'
}
$apiFqdn = Get-DeploymentOutput -Outputs $finalOutputs -Name 'apiFqdn'
if ([string]::IsNullOrWhiteSpace($apiFqdn)) { throw 'Final Azure deployment did not return the Container App FQDN.' }

$healthy = $false
for ($attempt = 1; $attempt -le 12; $attempt++) {
    try {
        $health = Invoke-WebRequest -Uri "https://$apiFqdn/health" -Method Get -TimeoutSec 15 -MaximumRedirection 0
        $ready = Invoke-WebRequest -Uri "https://$apiFqdn/ready" -Method Get -TimeoutSec 20 -MaximumRedirection 0
        if ([int]$health.StatusCode -eq 200 -and [int]$ready.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        if ($attempt -eq 12) { throw 'Azure API health or live UAMI-to-AWS federation readiness failed at the default hostname.' }
    }
    Start-Sleep -Seconds 5
}
if (-not $healthy) { throw 'Azure API health check did not succeed.' }

$customDomainBound = $false
if ($BindCustomDomain) {
    $verificationId = & az containerapp show `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --query properties.customDomainVerificationId `
        --output tsv
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($verificationId | Out-String))) {
        throw 'Container Apps custom-domain verification ID is unavailable.'
    }
    $verificationId = ($verificationId | Out-String).Trim()

    if (-not $SkipDnsValidationRecord) {
        Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null
        $recordName = "asuid.$($config.apiHostName)"
        $changeBatch = [ordered]@{
            Comment = 'Azure Container Apps ownership validation only; this does not cut over API traffic.'
            Changes = @(
                [ordered]@{
                    Action = 'UPSERT'
                    ResourceRecordSet = [ordered]@{
                        Name = $recordName
                        Type = 'TXT'
                        TTL = 300
                        ResourceRecords = @(@{ Value = "`"$verificationId`"" })
                    }
                }
            )
        }
        $changeFile = Join-Path ([System.IO.Path]::GetTempPath()) "loan-azure-domain-$([guid]::NewGuid().Guid).json"
        try {
            [System.IO.File]::WriteAllText($changeFile, ($changeBatch | ConvertTo-Json -Depth 20), [System.Text.UTF8Encoding]::new($false))
            if ($PSCmdlet.ShouldProcess($recordName, 'Upsert Azure ownership-validation TXT record in Route 53')) {
                Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
                    'route53', 'change-resource-record-sets',
                    '--hosted-zone-id', [string]$config.route53HostedZoneId,
                    '--change-batch', "file://${changeFile}"
                ) | Out-Null
            }
        } finally {
            if (Test-Path -LiteralPath $changeFile) { Remove-Item -LiteralPath $changeFile -Force }
        }
    }

    $hostnameList = & az containerapp hostname list `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect existing Container App hostnames.' }
    $hostnames = @($hostnameList | Out-String | ConvertFrom-Json -Depth 20)
    if (-not ($hostnames | Where-Object { [string]$_.name -ceq [string]$config.apiHostName })) {
        & az containerapp hostname add `
            --subscription $config.azureSubscriptionId `
            --resource-group $config.azureResourceGroupName `
            --name $config.azureApiAppName `
            --hostname $config.apiHostName `
            --output none
        if ($LASTEXITCODE -ne 0) { throw "Failed to add custom hostname '$($config.apiHostName)'. Confirm the TXT record has propagated." }
    }

    $certificateName = "api-$($config.environment)-managed"
    $certificates = @(Get-ManagedApiCertificates)
    $canonicalApiHostname = ConvertTo-CanonicalDnsName -Value ([string]$config.apiHostName)
    $subjectMatches = @($certificates | Where-Object {
        (ConvertFrom-CertificateSubject -Value ([string]$_.properties.subjectName)) -ceq $canonicalApiHostname
    })
    if ($subjectMatches.Count -gt 1) {
        throw "Multiple managed certificates exist for '$($config.apiHostName)'; select and remove duplicates before deployment."
    }
    $namedCertificates = @($certificates | Where-Object { [string]$_.name -ieq $certificateName })
    if ($namedCertificates.Count -gt 1) {
        throw "Azure returned duplicate managed certificate resources named '$certificateName'."
    }
    if ($subjectMatches.Count -eq 1) {
        $certificate = $subjectMatches[0]
    } elseif ($namedCertificates.Count -eq 1) {
        throw "Managed certificate name '$certificateName' belongs to a different hostname; refusing an implicit replacement."
    } else {
        $certificateRaw = & az containerapp env certificate create `
            --subscription $config.azureSubscriptionId `
            --resource-group $config.azureResourceGroupName `
            --name $config.azureContainerAppsEnvironmentName `
            --certificate-name $certificateName `
            --hostname $config.apiHostName `
            --validation-method TXT `
            --output json
        if ($LASTEXITCODE -ne 0) { throw "Failed to create managed certificate for '$($config.apiHostName)'. Confirm TXT propagation." }
        $certificate = $certificateRaw | Out-String | ConvertFrom-Json -Depth 30
    }
    if ([string]::IsNullOrWhiteSpace([string]$certificate.id)) {
        throw 'Managed certificate resource ID is unavailable.'
    }
    $certificate = Wait-ManagedApiCertificate `
        -CertificateId ([string]$certificate.id) `
        -Hostname ([string]$config.apiHostName)
    & az containerapp hostname bind `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --environment $config.azureContainerAppsEnvironmentName `
        --hostname $config.apiHostName `
        --certificate $certificate.id `
        --validation-method TXT `
        --output none
    if ($LASTEXITCODE -ne 0) { throw "Failed to bind managed certificate to '$($config.apiHostName)'." }
    $customDomainBound = $true
    Write-Host "Managed certificate bound to '$($config.apiHostName)'. The API traffic DNS record was not changed."
}

$boundHostnamesRaw = & az containerapp hostname list `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureApiAppName `
    --output json
if ($LASTEXITCODE -ne 0) { throw 'Failed to verify the final Container App custom-hostname state.' }
$boundHostnames = @($boundHostnamesRaw | Out-String | ConvertFrom-Json -Depth 20)
$canonicalApiHostname = ConvertTo-CanonicalDnsName -Value ([string]$config.apiHostName)
$unexpectedBoundHostnames = @($boundHostnames | Where-Object {
    (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -cne $canonicalApiHostname
})
if ($unexpectedBoundHostnames.Count -gt 0) {
    throw 'The final Container App contains an unexpected custom-domain binding.'
}
$matchingBoundHostnames = @($boundHostnames | Where-Object {
    (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -ceq $canonicalApiHostname
})
if ($matchingBoundHostnames.Count -gt 1) {
    throw 'The final Container App returned duplicate API custom-domain bindings.'
}
$boundApiHostname = $matchingBoundHostnames | Select-Object -First 1
$customDomainBound = $null -ne $boundApiHostname
if ($null -ne $existingApiCustomDomain -and
    [string]$existingApiCustomDomain.state -ceq 'SniEnabled' -and
    -not $customDomainBound) {
    throw 'The final deployment removed the pre-existing API custom-domain binding; refusing to report success.'
}
if ($customDomainBound -and (
    [string]$boundApiHostname.bindingType -cne 'SniEnabled' -or
    [string]::IsNullOrWhiteSpace([string]$boundApiHostname.certificateId)
)) {
    throw 'The final API custom-domain binding is not secured by an SNI certificate.'
}
if ($null -ne $existingApiCustomDomain -and
    [string]$existingApiCustomDomain.state -ceq 'SniEnabled' -and
    [string]$boundApiHostname.certificateId -ine [string]$existingApiCustomDomain.certificateId) {
    throw 'The final deployment replaced the pre-existing API custom-domain certificate unexpectedly.'
}

$trafficLookup = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
    'route53', 'list-resource-record-sets',
    '--hosted-zone-id', [string]$config.route53HostedZoneId,
    '--start-record-name', [string]$config.apiHostName,
    '--max-items', '10'
) -CaptureJson
$exactTrafficRecords = @($trafficLookup.ResourceRecordSets | Where-Object {
    ([string]$_.Name).TrimEnd('.') -ceq [string]$config.apiHostName
})
if (@($exactTrafficRecords | Where-Object { [string]$_.Type -ne 'CNAME' }).Count -gt 0) {
    throw "The API hostname '$($config.apiHostName)' has a non-CNAME traffic record; deployment state cannot be verified safely."
}
$trafficCname = @($exactTrafficRecords | Where-Object { [string]$_.Type -eq 'CNAME' }) | Select-Object -First 1
$dnsCutoverPerformed = $false
if ($null -ne $trafficCname) {
    $trafficTarget = ([string]$trafficCname.ResourceRecords[0].Value).TrimEnd('.')
    $dnsCutoverPerformed = $trafficTarget -ceq $apiFqdn.TrimEnd('.')
}
if ($dnsCutoverPerformed -and -not $customDomainBound) {
    throw 'Route 53 points API traffic at Azure but the Container App custom-domain certificate is not bound.'
}
if ($dnsCutoverPerformed) {
    $customHealthy = $false
    for ($attempt = 1; $attempt -le 12; $attempt++) {
        try {
            $customHealth = Invoke-WebRequest -Uri "https://$($config.apiHostName)/health" -Method Get -TimeoutSec 15 -MaximumRedirection 0
            $customReady = Invoke-WebRequest -Uri "https://$($config.apiHostName)/ready" -Method Get -TimeoutSec 20 -MaximumRedirection 0
            if ([int]$customHealth.StatusCode -eq 200 -and [int]$customReady.StatusCode -eq 200) {
                $customHealthy = $true
                break
            }
        } catch {
            if ($attempt -eq 12) { throw 'The custom API hostname failed health or live federation readiness after deployment.' }
        }
        Start-Sleep -Seconds 5
    }
    if (-not $customHealthy) { throw 'The custom API hostname health check did not succeed.' }
}

$finalDocument = New-SafeOutput -Phase finalized -Outputs $finalOutputs -ImmutableImage $immutableImage -CustomDomainBound $customDomainBound
$finalDocument.api['imageScan'] = if ($null -eq $imageScanEvidence) {
    [ordered]@{ passed = $false; policy = 'explicitly-skipped-nonproduction' }
} else {
    $imageScanEvidence
}
$finalDocument.api.customDomain.dnsCutoverPerformed = $dnsCutoverPerformed
if ($customDomainBound) {
    $finalDocument.api.customDomain.certificateResourceId = [string]$boundApiHostname.certificateId
}
if ($dnsCutoverPerformed) {
    $finalDocument.api.customDomain.dnsVerifiedUtc = [DateTime]::UtcNow.ToString('o')
}
if ($dnsCutoverPerformed -and $null -ne $previousFinalState -and
    $null -ne $previousFinalState.api.customDomain) {
    foreach ($name in @('cutoverUtc', 'rollbackEvidenceFile')) {
        $property = $previousFinalState.api.customDomain.PSObject.Properties[$name]
        if ($null -ne $property -and -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
            $finalDocument.api.customDomain[$name] = [string]$property.Value
        }
    }
}
Write-SafeOutput -Document $finalDocument
Write-Host "Azure API revision is healthy at https://$apiFqdn"
Write-Host "Wrote non-secret Azure deployment state to $OutputFile"
Write-Host 'No Entra secret, AWS access key, ACR password, Static Web Apps deployment token, bearer token, or signed object grant was created or persisted.'
