<#
.SYNOPSIS
Builds, verifies, and publishes the React SPA to Azure Static Web Apps.

.DESCRIPTION
The deployment token is retrieved with the caller's short-lived Azure identity,
kept only in process memory, and cleared after `swa deploy`. The generated runtime
configuration points only at the Entra-protected Azure API. `-BindCustomDomain`
performs an explicit Route 53 CNAME cutover only after the default Azure hostname
has served the reviewed build; DNS is rolled back if binding or health fails.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$AzureDeploymentFile = '',
    [string]$EntraDeploymentFile = '',
    [string]$BuildSha = '',
    [switch]$BindCustomDomain
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$appDirectory = Join-Path $root 'apps/web'
Assert-Command -Name npm -InstallHint 'Install Node.js LTS and npm.'
Assert-Command -Name az -InstallHint 'Run scripts/bootstrap.ps1.'
Assert-Command -Name git -InstallHint 'Install Git.'
if ([string]$config.environment -eq 'prod' -or $BindCustomDomain) {
    Assert-Command -Name aws -InstallHint 'Install AWS CLI v2 for production API-domain verification and Route 53 cutover.'
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

function Assert-LiveProductionApiDomain {
    $appRaw = & az containerapp show `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect the live production Container App.' }
    $app = $appRaw | Out-String | ConvertFrom-Json -Depth 40
    if ([string]$app.id -ine [string]$azure.api.resourceId) {
        throw 'Live Container App resource ID does not match the reviewed Azure deployment state.'
    }
    $liveApiFqdn = [string]$app.properties.configuration.ingress.fqdn
    if ([string]::IsNullOrWhiteSpace($liveApiFqdn) -or
        (ConvertTo-CanonicalDnsName -Value $liveApiFqdn) -cne
        (ConvertTo-CanonicalDnsName -Value ([string]$azure.api.fqdn))) {
        throw 'Live Container App FQDN does not match the reviewed Azure deployment state.'
    }

    $hostnameRaw = & az containerapp hostname list `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureApiAppName `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect the live API custom-domain binding.' }
    $bindings = @($hostnameRaw | Out-String | ConvertFrom-Json -Depth 30)
    $canonicalApiHostname = ConvertTo-CanonicalDnsName -Value ([string]$config.apiHostName)
    $unexpectedBindings = @($bindings | Where-Object {
        (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -cne $canonicalApiHostname
    })
    if ($unexpectedBindings.Count -gt 0) {
        throw 'Live Container App has an unexpected custom-domain binding.'
    }
    $matchingBindings = @($bindings | Where-Object {
        (ConvertTo-CanonicalDnsName -Value ([string]$_.name)) -ceq $canonicalApiHostname
    })
    if ($matchingBindings.Count -ne 1 -or
        [string]$matchingBindings[0].bindingType -cne 'SniEnabled' -or
        [string]::IsNullOrWhiteSpace([string]$matchingBindings[0].certificateId)) {
        throw 'Live API custom hostname is not secured by exactly one SNI certificate binding.'
    }

    $certificateRaw = & az containerapp env certificate list `
        --subscription $config.azureSubscriptionId `
        --resource-group $config.azureResourceGroupName `
        --name $config.azureContainerAppsEnvironmentName `
        --managed-certificates-only `
        --output json
    if ($LASTEXITCODE -ne 0) { throw 'Failed to inspect the live API managed certificate.' }
    $certificates = @($certificateRaw | Out-String | ConvertFrom-Json -Depth 30)
    $certificateMatches = @($certificates | Where-Object {
        [string]$_.id -ieq [string]$matchingBindings[0].certificateId
    })
    if ($certificateMatches.Count -ne 1 -or
        [string]$certificateMatches[0].properties.provisioningState -cne 'Succeeded' -or
        (ConvertFrom-CertificateSubject -Value ([string]$certificateMatches[0].properties.subjectName)) -cne $canonicalApiHostname) {
        throw 'Live API certificate is missing, not ready, or issued for another hostname.'
    }

    Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null
    $trafficLookup = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'route53', 'list-resource-record-sets',
        '--hosted-zone-id', [string]$config.route53HostedZoneId,
        '--start-record-name', [string]$config.apiHostName,
        '--max-items', '10'
    ) -CaptureJson
    $trafficRecords = @($trafficLookup.ResourceRecordSets | Where-Object {
        (ConvertTo-CanonicalDnsName -Value ([string]$_.Name)) -ceq $canonicalApiHostname
    })
    if ($trafficRecords.Count -ne 1 -or [string]$trafficRecords[0].Type -cne 'CNAME') {
        throw 'Production API hostname must have exactly one Route 53 CNAME record.'
    }
    $trafficTarget = ConvertTo-CanonicalDnsName -Value ([string]$trafficRecords[0].ResourceRecords[0].Value)
    if ($trafficTarget -cne (ConvertTo-CanonicalDnsName -Value $liveApiFqdn)) {
        throw 'Production API CNAME does not target the live Container App FQDN.'
    }

    foreach ($path in @('/health', '/ready')) {
        $probe = Invoke-WebRequest `
            -Uri "https://$($config.apiHostName)$path" `
            -Method Get `
            -TimeoutSec 20 `
            -MaximumRedirection 0
        if ([int]$probe.StatusCode -ne 200) { throw "Production API custom hostname failed probe '$path'." }
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $appDirectory 'package.json'))) {
    throw "React application is not present in '$appDirectory'. Claude Code must finish the UI scaffold before deployment."
}
if (-not (Test-Path -LiteralPath (Join-Path $appDirectory 'package-lock.json'))) {
    throw 'apps/web/package-lock.json is required for a reproducible production build.'
}
if ([string]::IsNullOrWhiteSpace($AzureDeploymentFile)) {
    $AzureDeploymentFile = Join-Path $root ".local/azure-$($config.environment).json"
}
if ([string]::IsNullOrWhiteSpace($EntraDeploymentFile)) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
foreach ($path in @($AzureDeploymentFile, $EntraDeploymentFile)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required non-secret deployment state is missing: $path" }
}

$azure = Get-Content -Raw -LiteralPath $AzureDeploymentFile | ConvertFrom-Json -Depth 30
$entra = Get-Content -Raw -LiteralPath $EntraDeploymentFile | ConvertFrom-Json -Depth 30
if ([string]$azure.phase -ne 'finalized' -or $null -eq $azure.api) {
    throw 'Azure deployment state does not describe a finalized API.'
}
if ([string]$azure.subscriptionId -ne [string]$config.azureSubscriptionId -or
    [string]$azure.resourceGroupName -ne [string]$config.azureResourceGroupName) {
    throw 'Azure deployment state belongs to a different subscription or resource group.'
}
if ([string]$entra.tenantId -ne [string]$config.entraTenantId) {
    throw 'Entra deployment state belongs to a different tenant.'
}
foreach ($required in @(
    [string]$azure.staticWebApp.defaultHostname,
    [string]$azure.api.resourceId,
    [string]$azure.api.fqdn,
    [string]$entra.api.clientId,
    [string]$entra.api.scopeBase,
    [string]$entra.spa.clientId
)) {
    if ([string]::IsNullOrWhiteSpace($required)) { throw 'Azure or Entra deployment state is incomplete.' }
}

& az account set --subscription $config.azureSubscriptionId
if ($LASTEXITCODE -ne 0) { throw "Cannot select Azure subscription '$($config.azureSubscriptionId)'." }
$account = & az account show --output json | ConvertFrom-Json -Depth 20
if ($LASTEXITCODE -ne 0 -or [string]$account.tenantId -ne [string]$config.entraTenantId) {
    throw 'Azure CLI is not signed into the configured tenant.'
}
$liveHostname = & az staticwebapp show `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureStaticWebAppName `
    --query defaultHostname `
    --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($liveHostname | Out-String))) {
    throw "Azure Static Web App '$($config.azureStaticWebAppName)' is unavailable."
}
$liveHostname = ($liveHostname | Out-String).Trim()
if ($liveHostname -cne [string]$azure.staticWebApp.defaultHostname) {
    throw 'Live Static Web App hostname does not match the reviewed Azure deployment state.'
}

if ([string]::IsNullOrWhiteSpace($BuildSha)) {
    $BuildSha = if ($env:GITHUB_SHA) { [string]$env:GITHUB_SHA } else { (& git -C $root rev-parse HEAD | Out-String).Trim() }
}
if ($BuildSha -notmatch '^[0-9a-fA-F]{7,64}$') { throw 'BuildSha must be a Git commit identifier.' }

$previousCi = $env:CI
$env:CI = 'true'
try {
    Push-Location $appDirectory
    try {
        & npm ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        & npm audit --audit-level=high
        if ($LASTEXITCODE -ne 0) { throw 'UI dependency vulnerability gate failed.' }
        & npm run lint
        if ($LASTEXITCODE -ne 0) { throw 'UI lint failed.' }
        & npm run typecheck
        if ($LASTEXITCODE -ne 0) { throw 'UI type checking failed.' }
        & npm run test:coverage
        if ($LASTEXITCODE -ne 0) { throw 'UI coverage tests failed.' }
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'UI production build failed.' }
        & npx playwright install chromium
        if ($LASTEXITCODE -ne 0) { throw 'Playwright Chromium installation failed.' }
        & npm run test:e2e:ci
        if ($LASTEXITCODE -ne 0) { throw 'Playwright integration tests failed.' }
    } finally {
        Pop-Location
    }
} finally {
    $env:CI = $previousCi
}

$distDirectory = Join-Path $appDirectory 'dist'
if (-not (Test-Path -LiteralPath (Join-Path $distDirectory 'index.html'))) {
    throw "UI build did not produce '$distDirectory/index.html'."
}
$apiCustomDomainReady = (
    $null -ne $azure.api.customDomain -and
    [bool]$azure.api.customDomain.bound -and
    [bool]$azure.api.customDomain.dnsCutoverPerformed
)
if ([string]$config.environment -eq 'prod' -and -not $apiCustomDomainReady) {
    throw 'Production UI publication requires the API custom-domain certificate and verified Route 53 cutover.'
}
$apiBaseUrl = if ($apiCustomDomainReady) { "https://$($config.apiHostName)" } else { [string]$azure.api.defaultUrl }
$runtimeConfig = [ordered]@{
    environment = if ($config.environment -eq 'prod') { 'production' } else { [string]$config.environment }
    apiBaseUrl = $apiBaseUrl.TrimEnd('/')
    entraTenantId = [string]$entra.tenantId
    entraSpaClientId = [string]$entra.spa.clientId
    entraApiScopeBase = [string]$entra.api.scopeBase
    redirectUri = "https://$($config.uiHostName)/auth/callback"
    postLogoutRedirectUri = "https://$($config.uiHostName)/"
    buildSha = $BuildSha
    maximumUploadBytes = [long]$config.maximumUploadBytes
}
$runtimePath = Join-Path $distDirectory 'runtime-config.json'
[IO.File]::WriteAllText($runtimePath, ($runtimeConfig | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

if ([string]$config.environment -eq 'prod') {
    Assert-LiveProductionApiDomain
}

if (-not $PSCmdlet.ShouldProcess($config.azureStaticWebAppName, "Publish reviewed UI build $BuildSha")) {
    return
}
$deploymentToken = & az staticwebapp secrets list `
    --subscription $config.azureSubscriptionId `
    --resource-group $config.azureResourceGroupName `
    --name $config.azureStaticWebAppName `
    --query properties.apiKey `
    --output tsv
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($deploymentToken | Out-String))) {
    throw 'The caller cannot obtain the Static Web Apps deployment token.'
}
$deploymentToken = ($deploymentToken | Out-String).Trim()
$previousDeploymentToken = $env:SWA_CLI_DEPLOYMENT_TOKEN
try {
    $env:SWA_CLI_DEPLOYMENT_TOKEN = $deploymentToken
    Push-Location $appDirectory
    try {
        & npx --no-install swa deploy $distDirectory --env production --yes
        if ($LASTEXITCODE -ne 0) { throw 'Azure Static Web Apps deployment failed.' }
    } finally {
        Pop-Location
    }
} finally {
    $env:SWA_CLI_DEPLOYMENT_TOKEN = $previousDeploymentToken
    $deploymentToken = $null
}

$deployedRuntime = Invoke-RestMethod -Uri "https://$liveHostname/runtime-config.json" -Method Get -TimeoutSec 30
if ([string]$deployedRuntime.buildSha -cne $BuildSha -or [string]$deployedRuntime.apiBaseUrl -cne $runtimeConfig.apiBaseUrl) {
    throw 'The default Static Web Apps hostname did not serve the reviewed runtime configuration.'
}

if ($BindCustomDomain) {
    Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null
    $lookup = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'route53', 'list-resource-record-sets',
        '--hosted-zone-id', [string]$config.route53HostedZoneId,
        '--start-record-name', [string]$config.uiHostName,
        '--max-items', '10'
    ) -CaptureJson
    $exactRecords = @($lookup.ResourceRecordSets | Where-Object {
        ([string]$_.Name).TrimEnd('.') -ceq [string]$config.uiHostName
    })
    if (@($exactRecords | Where-Object { [string]$_.Type -ne 'CNAME' }).Count -gt 0) {
        throw "A non-CNAME DNS record already exists for '$($config.uiHostName)'. Refusing an implicit migration."
    }
    $priorRecord = @($exactRecords | Where-Object { [string]$_.Type -eq 'CNAME' }) | Select-Object -First 1
    $newRecord = [ordered]@{
        Name = [string]$config.uiHostName
        Type = 'CNAME'
        TTL = 60
        ResourceRecords = @(@{ Value = $liveHostname })
    }
    $change = [ordered]@{
        Comment = "Explicit Azure Static Web Apps cutover for build $BuildSha"
        Changes = @(@{ Action = 'UPSERT'; ResourceRecordSet = $newRecord })
    }
    $changeFile = Join-Path ([IO.Path]::GetTempPath()) "loan-ui-cutover-$([guid]::NewGuid().Guid).json"
    try {
        [IO.File]::WriteAllText($changeFile, ($change | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
        if (-not $PSCmdlet.ShouldProcess($config.uiHostName, "Cut over UI DNS to $liveHostname")) { return }
        $changeResult = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
            'route53', 'change-resource-record-sets',
            '--hosted-zone-id', [string]$config.route53HostedZoneId,
            '--change-batch', "file://$changeFile"
        ) -CaptureJson
        Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
            'route53', 'wait', 'resource-record-sets-changed', '--id', [string]$changeResult.ChangeInfo.Id
        ) | Out-Null
        try {
            & az staticwebapp hostname set `
                --subscription $config.azureSubscriptionId `
                --resource-group $config.azureResourceGroupName `
                --name $config.azureStaticWebAppName `
                --hostname $config.uiHostName `
                --validation-method cname-delegation `
                --output none
            if ($LASTEXITCODE -ne 0) { throw 'Azure rejected the Static Web Apps custom hostname.' }
            $customRuntime = Invoke-RestMethod -Uri "https://$($config.uiHostName)/runtime-config.json" -Method Get -TimeoutSec 30
            if ([string]$customRuntime.buildSha -cne $BuildSha) { throw 'Custom UI hostname did not serve the reviewed build.' }
        } catch {
            $rollbackRecord = if ($null -ne $priorRecord) { $priorRecord } else { $newRecord }
            $rollbackAction = if ($null -ne $priorRecord) { 'UPSERT' } else { 'DELETE' }
            $rollback = [ordered]@{
                Comment = 'Automatic rollback after Azure Static Web Apps cutover failure'
                Changes = @(@{ Action = $rollbackAction; ResourceRecordSet = $rollbackRecord })
            }
            [IO.File]::WriteAllText($changeFile, ($rollback | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
            Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
                'route53', 'change-resource-record-sets',
                '--hosted-zone-id', [string]$config.route53HostedZoneId,
                '--change-batch', "file://$changeFile"
            ) | Out-Null
            throw
        }
    } finally {
        if (Test-Path -LiteralPath $changeFile) { Remove-Item -LiteralPath $changeFile -Force }
    }
}

Write-Host "UI build $BuildSha deployed to https://$liveHostname"
Write-Host 'No deployment token, bearer token, AWS credential, or signed object grant was written to disk.'
