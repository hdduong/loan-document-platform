<#
.SYNOPSIS
Cuts the configured API hostname over to a validated Azure Container App.

.DESCRIPTION
This is intentionally separate from deployment. It verifies the Azure default
hostname and deep readiness first, snapshots the exact Route 53 record sets,
changes DNS in one batch, verifies HTTPS/readiness at the custom hostname, and
automatically restores the prior records if validation fails.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [string]$AzureDeploymentFile = '',
    [ValidateRange(1, 30)][int]$HealthAttempts = 12
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
if ([string]::IsNullOrWhiteSpace($AzureDeploymentFile)) {
    $AzureDeploymentFile = Join-Path $root ".local/azure-$($config.environment).json"
}
if (-not (Test-Path -LiteralPath $AzureDeploymentFile)) {
    throw "Azure deployment state is missing: $AzureDeploymentFile"
}
$azure = Get-Content -Raw -LiteralPath $AzureDeploymentFile | ConvertFrom-Json -Depth 40
if ([string]$azure.phase -ne 'finalized' -or $null -eq $azure.api) {
    throw 'Azure deployment state does not describe a finalized API.'
}
if ([string]$azure.subscriptionId -ne [string]$config.azureSubscriptionId -or
    [string]$azure.resourceGroupName -ne [string]$config.azureResourceGroupName) {
    throw 'Azure deployment state belongs to a different subscription or resource group.'
}
if (-not [bool]$azure.api.customDomain.bound -or
    [string]$azure.api.customDomain.hostname -cne [string]$config.apiHostName) {
    throw 'The API custom hostname and managed certificate must be bound before DNS cutover.'
}
if ($null -eq $azure.api.imageScan -or -not [bool]$azure.api.imageScan.passed -or
    [string]$azure.api.imageScan.immutableImage -cne [string]$azure.api.immutableImage) {
    throw 'API DNS cutover requires passing vulnerability evidence for the exact deployed image digest.'
}
$targetHostname = [string]$azure.api.fqdn
if ($targetHostname -notmatch '^[A-Za-z0-9.-]+\.azurecontainerapps\.io$') {
    throw 'Azure deployment state contains an invalid Container Apps target hostname.'
}

Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'
Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null

foreach ($path in @('/health', '/ready')) {
    $probe = Invoke-WebRequest -Uri "https://$targetHostname$path" -Method Get -TimeoutSec 20 -MaximumRedirection 0
    if ([int]$probe.StatusCode -ne 200) { throw "Azure default hostname failed pre-cutover probe '$path'." }
}

$lookup = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
    'route53', 'list-resource-record-sets',
    '--hosted-zone-id', [string]$config.route53HostedZoneId,
    '--start-record-name', [string]$config.apiHostName,
    '--max-items', '20'
) -CaptureJson
$priorRecords = @($lookup.ResourceRecordSets | Where-Object {
    ([string]$_.Name).TrimEnd('.') -ceq [string]$config.apiHostName
})
$unsupported = @($priorRecords | Where-Object { [string]$_.Type -notin @('A', 'AAAA', 'CNAME') })
if ($unsupported.Count -gt 0) {
    throw "API hostname has non-address DNS records ($(@($unsupported.Type) -join ', ')); refusing an implicit migration."
}

$newRecord = [ordered]@{
    Name = [string]$config.apiHostName
    Type = 'CNAME'
    TTL = 60
    ResourceRecords = @(@{ Value = $targetHostname })
}
$changes = @()
foreach ($record in $priorRecords) { $changes += [ordered]@{ Action = 'DELETE'; ResourceRecordSet = $record } }
$changes += [ordered]@{ Action = 'CREATE'; ResourceRecordSet = $newRecord }
$batch = [ordered]@{
    Comment = "Explicit Azure API cutover to $targetHostname"
    Changes = $changes
}

$localDirectory = Join-Path $root '.local'
[IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$timestamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$rollbackPath = Join-Path $localDirectory "api-dns-rollback-$($config.environment)-$timestamp.json"
$rollbackEvidence = [ordered]@{
    schemaVersion = 1
    timestampUtc = [DateTime]::UtcNow.ToString('o')
    hostedZoneId = [string]$config.route53HostedZoneId
    hostname = [string]$config.apiHostName
    azureTargetHostname = $targetHostname
    priorRecordSets = $priorRecords
}
[IO.File]::WriteAllText($rollbackPath, ($rollbackEvidence | ConvertTo-Json -Depth 30) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

if (-not $PSCmdlet.ShouldProcess($config.apiHostName, "Replace exact DNS records with CNAME $targetHostname")) {
    Write-Host "WhatIf: rollback evidence prepared at $rollbackPath"
    return
}

$batchFile = Join-Path ([IO.Path]::GetTempPath()) "loan-api-cutover-$([guid]::NewGuid().Guid).json"
try {
    [IO.File]::WriteAllText($batchFile, ($batch | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    $result = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'route53', 'change-resource-record-sets',
        '--hosted-zone-id', [string]$config.route53HostedZoneId,
        '--change-batch', "file://$batchFile"
    ) -CaptureJson
    Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'route53', 'wait', 'resource-record-sets-changed', '--id', [string]$result.ChangeInfo.Id
    ) | Out-Null

    $healthy = $false
    for ($attempt = 1; $attempt -le $HealthAttempts; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri "https://$($config.apiHostName)/ready" -Method Get -TimeoutSec 20 -MaximumRedirection 0
            if ([int]$response.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
        if ($attempt -lt $HealthAttempts) { Start-Sleep -Seconds 10 }
    }
    if (-not $healthy) { throw 'Custom API hostname did not pass deep readiness after DNS cutover.' }
} catch {
    $rollbackChanges = @([ordered]@{ Action = 'DELETE'; ResourceRecordSet = $newRecord })
    foreach ($record in $priorRecords) { $rollbackChanges += [ordered]@{ Action = 'CREATE'; ResourceRecordSet = $record } }
    $rollbackBatch = [ordered]@{
        Comment = 'Automatic rollback after Azure API cutover verification failure'
        Changes = $rollbackChanges
    }
    [IO.File]::WriteAllText($batchFile, ($rollbackBatch | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
    Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
        'route53', 'change-resource-record-sets',
        '--hosted-zone-id', [string]$config.route53HostedZoneId,
        '--change-batch', "file://$batchFile"
    ) | Out-Null
    throw
} finally {
    if (Test-Path -LiteralPath $batchFile) { Remove-Item -LiteralPath $batchFile -Force }
}

$azure.api.customDomain.dnsCutoverPerformed = $true
if ($null -eq $azure.api.customDomain.PSObject.Properties['cutoverUtc']) {
    $azure.api.customDomain | Add-Member -NotePropertyName cutoverUtc -NotePropertyValue ([DateTime]::UtcNow.ToString('o'))
} else {
    $azure.api.customDomain.cutoverUtc = [DateTime]::UtcNow.ToString('o')
}
if ($null -eq $azure.api.customDomain.PSObject.Properties['rollbackEvidenceFile']) {
    $azure.api.customDomain | Add-Member -NotePropertyName rollbackEvidenceFile -NotePropertyValue $rollbackPath
} else {
    $azure.api.customDomain.rollbackEvidenceFile = $rollbackPath
}
$temporaryState = "$AzureDeploymentFile.$([guid]::NewGuid().Guid).tmp"
try {
    [IO.File]::WriteAllText($temporaryState, ($azure | ConvertTo-Json -Depth 40) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporaryState -Destination $AzureDeploymentFile -Force
} finally {
    if (Test-Path -LiteralPath $temporaryState) { Remove-Item -LiteralPath $temporaryState -Force }
}

Write-Host "API DNS cut over to https://$($config.apiHostName); deep readiness passed."
Write-Host "Rollback record snapshot: $rollbackPath"
