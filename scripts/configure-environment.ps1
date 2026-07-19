#requires -Version 7.2

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$EnvironmentFile = (Join-Path (Split-Path -Parent $PSScriptRoot) 'config/environments/dev.json'),
    [string]$CorporateCaBundlePath = '',
    [string]$AwsProfile = '',
    [string]$HostedZoneId = '',
    [string]$UiHostName = '',
    [string]$ApiHostName = '',
    [string]$AlertEmail = '',
    [string]$BudgetEmail = '',
    [string]$AzureContainerRegistryName = '',
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

function Test-ConfiguredValue {
    [CmdletBinding()]
    param([AllowNull()][object]$Value)

    $text = [string]$Value
    $exampleSentinels = @('example.com', 'loans.example.com', 'api.loans.example.com')
    return -not [string]::IsNullOrWhiteSpace($text) -and
        $text -cnotmatch '^REPLACE_' -and $text -cnotin $exampleSentinels
}

function Assert-ExistingValueMatches {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object]$Config,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Expected,
        [ValidateSet('Ordinal', 'Guid')][string]$Comparison = 'Ordinal'
    )

    $property = $Config.PSObject.Properties[$Name]
    if ($null -eq $property -or -not (Test-ConfiguredValue -Value $property.Value)) { return }
    $matches = if ($Comparison -ceq 'Guid') {
        $existingGuid = [guid]::Empty
        $expectedGuid = [guid]::Empty
        [guid]::TryParse([string]$property.Value, [ref]$existingGuid) -and
            [guid]::TryParse($Expected, [ref]$expectedGuid) -and $existingGuid -eq $expectedGuid
    } else {
        [string]::Equals([string]$property.Value, $Expected, [StringComparison]::Ordinal)
    }
    if (-not $matches) {
        throw "The authenticated cloud identity does not match the existing '$Name' value. Sign in to the intended account or use a separate ignored environment file."
    }
}

function ConvertTo-CanonicalDnsName {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Value)

    return $Value.Trim().TrimEnd('.').ToLowerInvariant()
}

function Test-HostNameForDomain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$HostName,
        [Parameter(Mandatory)][string]$DomainName
    )

    $hostValue = ConvertTo-CanonicalDnsName -Value $HostName
    $domainValue = ConvertTo-CanonicalDnsName -Value $DomainName
    $validDnsName = '^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$'
    return $hostValue -cmatch $validDnsName -and
        $hostValue.EndsWith(".$domainValue", [StringComparison]::Ordinal)
}

function Assert-ValidEmailAddress {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Value,
        [Parameter(Mandatory)][string]$Name
    )

    if ($Value.IndexOfAny([char[]]"`r`n") -ge 0) {
        throw "'$Name' must be one email address without control characters."
    }
    try {
        $parsed = [System.Net.Mail.MailAddress]::new($Value)
    } catch {
        throw "'$Name' must be a valid email address."
    }
    if ($Value -cne $Value.Trim() -or $parsed.Address -cne $Value) {
        throw "'$Name' must contain exactly one canonical email address."
    }
}

function Get-AwsProfileNames {
    [CmdletBinding()]
    param()

    $command = Get-Command aws -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $exitCode = -1
    $output = @()
    try {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -WhatIf:$false
        }
        $output = @(& $command.Source 'configure' 'list-profiles' 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $nativePreference.Value -Scope Local -WhatIf:$false
        }
    }
    if ($exitCode -ne 0) { throw "AWS CLI failed while listing configured profiles." }
    return @($output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}

function Invoke-GitProbe {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $exitCode = -1
    try {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -WhatIf:$false
        }
        & git @Arguments *> $null
        $exitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $nativePreference.Value -Scope Local -WhatIf:$false
        }
    }
    return $exitCode
}

function ConvertFrom-CloudJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object[]]$InputObject,
        [Parameter(Mandatory)][string]$Operation
    )

    try {
        return ($InputObject | Out-String | ConvertFrom-Json -Depth 50)
    } catch {
        throw "$Operation returned invalid JSON."
    }
}

function New-ValidatedEnvironmentConfigFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object]$Config,
        [Parameter(Mandatory)][string]$Destination
    )

    $directory = Split-Path -Parent $Destination
    $leaf = Split-Path -Leaf $Destination
    $temporaryPath = Join-Path $directory ".$leaf.$([guid]::NewGuid().ToString('N')).json"
    $utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
    try {
        $json = $Config | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($temporaryPath, "$json`n", $utf8WithoutBom)
        Read-EnvironmentConfig -Path $temporaryPath | Out-Null
        return $temporaryPath
    } catch {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -WhatIf:$false
        }
        throw
    }
}

$projectRoot = Get-ProjectRoot
$environmentDirectory = [System.IO.Path]::GetFullPath((Join-Path $projectRoot 'config/environments'))
$resolvedEnvironmentFile = (Resolve-Path -LiteralPath $EnvironmentFile).Path
$environmentPrefix = $environmentDirectory.TrimEnd([System.IO.Path]::DirectorySeparatorChar) +
    [System.IO.Path]::DirectorySeparatorChar
if (-not $resolvedEnvironmentFile.StartsWith($environmentPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $resolvedEnvironmentFile.EndsWith('.example.json', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'EnvironmentFile must be a non-example JSON file under config/environments.'
}

Assert-Command -Name git -InstallHint 'Install Git for Windows.'
Assert-Command -Name az -InstallHint 'Install Azure CLI.'
Assert-Command -Name aws -InstallHint 'Install AWS CLI v2.'

$relativeEnvironmentFile = [System.IO.Path]::GetRelativePath($projectRoot, $resolvedEnvironmentFile).Replace('\', '/')
$trackedExitCode = Invoke-GitProbe -Arguments @(
    '-C', $projectRoot, 'ls-files', '--error-unmatch', '--', $relativeEnvironmentFile
)
if ($trackedExitCode -eq 0) {
    throw 'The environment file is tracked by Git. Use an ignored non-example environment file.'
}
if ($trackedExitCode -ne 1) { throw 'Git could not verify whether the environment file is tracked.' }
$ignoredExitCode = Invoke-GitProbe -Arguments @(
    '-C', $projectRoot, 'check-ignore', '--quiet', '--', $relativeEnvironmentFile
)
if ($ignoredExitCode -ne 0) {
    throw 'The environment file is not ignored by Git. Refusing to write cloud identifiers.'
}

try {
    $config = Get-Content -Raw -LiteralPath $resolvedEnvironmentFile | ConvertFrom-Json -Depth 20
} catch {
    throw 'The environment file is not valid JSON.'
}
if ($config -isnot [pscustomobject]) { throw 'The environment file must contain one JSON object.' }
if ([string]$config.environment -cnotin @('dev', 'test', 'stage', 'prod')) {
    throw "The environment file requires environment to be one of: dev, test, stage, prod."
}
if ([string]$config.awsRegion -cne 'us-west-2') {
    throw 'The AWS data plane must remain in us-west-2.'
}

$bundleCandidate = $CorporateCaBundlePath
if ([string]::IsNullOrWhiteSpace($bundleCandidate) -and
    (Test-ConfiguredValue -Value $config.corporateCaBundlePath)) {
    $bundleCandidate = [string]$config.corporateCaBundlePath
}
if ([string]::IsNullOrWhiteSpace($bundleCandidate)) {
    $defaultBundle = Join-Path $HOME '.certs/cloud-ca-bundle.pem'
    if (Test-Path -LiteralPath $defaultBundle -PathType Leaf) { $bundleCandidate = $defaultBundle }
}
if (-not [string]::IsNullOrWhiteSpace($bundleCandidate)) {
    $resolvedBundle = Assert-CertificateOnlyBundle -Path $bundleCandidate
    $env:REQUESTS_CA_BUNDLE = $resolvedBundle
    $env:SSL_CERT_FILE = $resolvedBundle
    $env:AWS_CA_BUNDLE = $resolvedBundle
    $config.corporateCaBundlePath = $resolvedBundle.Replace('\', '/')
}

$azureRaw = @(Invoke-AzureCli -Arguments @('account', 'show', '--output', 'json'))
$azureAccount = ConvertFrom-CloudJson -InputObject $azureRaw -Operation 'Azure account lookup'
$azureIdentity = Assert-AzureIdentity -Account $azureAccount
$subscriptionId = $azureIdentity.SubscriptionId
$tenantId = $azureIdentity.TenantId
Assert-ExistingValueMatches -Config $config -Name 'azureSubscriptionId' -Expected $subscriptionId.ToString() -Comparison Guid
Assert-ExistingValueMatches -Config $config -Name 'entraTenantId' -Expected $tenantId.ToString() -Comparison Guid

$selectedProfile = $AwsProfile.Trim()
if ([string]::IsNullOrWhiteSpace($selectedProfile) -and (Test-ConfiguredValue -Value $config.awsProfile)) {
    $selectedProfile = [string]$config.awsProfile
}
if ([string]::IsNullOrWhiteSpace($selectedProfile)) {
    $profiles = @(Get-AwsProfileNames)
    if ($profiles.Count -ne 1) {
        throw 'Specify -AwsProfile when the environment file has no configured profile and AWS CLI does not have exactly one profile.'
    }
    $selectedProfile = $profiles[0]
}
if ($selectedProfile.IndexOfAny([char[]]"`r`n") -ge 0) { throw 'AwsProfile contains control characters.' }
Assert-ExistingValueMatches -Config $config -Name 'awsProfile' -Expected $selectedProfile

$awsIdentity = Invoke-Aws -Profile $selectedProfile -Region ([string]$config.awsRegion) -Arguments @(
    'sts', 'get-caller-identity'
) -CaptureJson -ForceProfile
if ([string]$awsIdentity.Account -cnotmatch '^\d{12}$') {
    throw 'AWS identity lookup did not return a valid account identifier.'
}
Assert-ExistingValueMatches -Config $config -Name 'awsAccountId' -Expected ([string]$awsIdentity.Account)

$zoneResponse = Invoke-Aws -Profile $selectedProfile -Region ([string]$config.awsRegion) -Arguments @(
    'route53', 'list-hosted-zones'
) -CaptureJson -ForceProfile
$publicZones = @(
    @($zoneResponse.HostedZones) |
        Where-Object { -not [bool]$_.Config.PrivateZone } |
        Sort-Object @{ Expression = { ConvertTo-CanonicalDnsName -Value ([string]$_.Name) } }, Id
)
if ($publicZones.Count -eq 0) { throw 'No public Route 53 hosted zone exists in the authenticated AWS account.' }

$selectedZoneId = $HostedZoneId.Trim() -replace '^/hostedzone/', ''
if ([string]::IsNullOrWhiteSpace($selectedZoneId) -and
    (Test-ConfiguredValue -Value $config.route53HostedZoneId)) {
    $selectedZoneId = ([string]$config.route53HostedZoneId) -replace '^/hostedzone/', ''
}
$zone = $null
if (-not [string]::IsNullOrWhiteSpace($selectedZoneId)) {
    $matches = @($publicZones | Where-Object { (([string]$_.Id) -replace '^/hostedzone/', '') -ceq $selectedZoneId })
    if ($matches.Count -ne 1) { throw 'The selected hosted-zone ID does not identify exactly one public Route 53 zone.' }
    $zone = $matches[0]
} elseif ($publicZones.Count -eq 1) {
    $zone = $publicZones[0]
} elseif ($NonInteractive) {
    throw 'NonInteractive mode requires -HostedZoneId when more than one public zone exists.'
} else {
    $selectedZoneId = (Read-Host 'Route 53 hosted-zone ID' -MaskInput).Trim() -replace '^/hostedzone/', ''
    $matches = @($publicZones | Where-Object { (([string]$_.Id) -replace '^/hostedzone/', '') -ceq $selectedZoneId })
    if ($matches.Count -ne 1) { throw 'The supplied hosted-zone ID does not identify exactly one public Route 53 zone.' }
    $zone = $matches[0]
}

$domainName = ConvertTo-CanonicalDnsName -Value ([string]$zone.Name)
if ($domainName -cnotmatch '^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$') {
    throw 'The selected public hosted zone does not contain a valid DNS domain.'
}
Assert-ExistingValueMatches -Config $config -Name 'domainName' -Expected $domainName

$environmentName = [string]$config.environment
$defaultUiHost = if ((Test-ConfiguredValue -Value $config.uiHostName) -and
    (Test-HostNameForDomain -HostName ([string]$config.uiHostName) -DomainName $domainName)) {
    ConvertTo-CanonicalDnsName -Value ([string]$config.uiHostName)
} else {
    "idp-$environmentName.$domainName"
}
$defaultApiHost = if ((Test-ConfiguredValue -Value $config.apiHostName) -and
    (Test-HostNameForDomain -HostName ([string]$config.apiHostName) -DomainName $domainName)) {
    ConvertTo-CanonicalDnsName -Value ([string]$config.apiHostName)
} else {
    "api-$environmentName.$domainName"
}

$selectedUiHost = $UiHostName.Trim()
if ([string]::IsNullOrWhiteSpace($selectedUiHost)) {
    if ($NonInteractive -or (Test-ConfiguredValue -Value $config.uiHostName)) {
        $selectedUiHost = $defaultUiHost
    } else {
        $selectedUiHost = Read-Host 'UI hostname (leave blank for the deterministic default)' -MaskInput
        if ([string]::IsNullOrWhiteSpace($selectedUiHost)) { $selectedUiHost = $defaultUiHost }
    }
}
$selectedApiHost = $ApiHostName.Trim()
if ([string]::IsNullOrWhiteSpace($selectedApiHost)) {
    if ($NonInteractive -or (Test-ConfiguredValue -Value $config.apiHostName)) {
        $selectedApiHost = $defaultApiHost
    } else {
        $selectedApiHost = Read-Host 'API hostname (leave blank for the deterministic default)' -MaskInput
        if ([string]::IsNullOrWhiteSpace($selectedApiHost)) { $selectedApiHost = $defaultApiHost }
    }
}
$selectedUiHost = ConvertTo-CanonicalDnsName -Value $selectedUiHost
$selectedApiHost = ConvertTo-CanonicalDnsName -Value $selectedApiHost
if (-not (Test-HostNameForDomain -HostName $selectedUiHost -DomainName $domainName) -or
    -not (Test-HostNameForDomain -HostName $selectedApiHost -DomainName $domainName) -or
    $selectedUiHost -ceq $selectedApiHost) {
    throw 'UI and API hostnames must be distinct valid subdomains of the selected public domain.'
}

$selectedAlertEmail = $AlertEmail
if ([string]::IsNullOrWhiteSpace($selectedAlertEmail) -and (Test-ConfiguredValue -Value $config.alertEmail)) {
    $selectedAlertEmail = [string]$config.alertEmail
}
if ([string]::IsNullOrWhiteSpace($selectedAlertEmail)) {
    if ($NonInteractive) { throw 'NonInteractive mode requires -AlertEmail for an unconfigured environment.' }
    $selectedAlertEmail = Read-Host 'Email for operational alerts' -MaskInput
}
Assert-ValidEmailAddress -Value $selectedAlertEmail -Name 'AlertEmail'

$selectedBudgetEmail = $BudgetEmail
if ([string]::IsNullOrWhiteSpace($selectedBudgetEmail) -and (Test-ConfiguredValue -Value $config.budgetEmail)) {
    $selectedBudgetEmail = [string]$config.budgetEmail
}
if ([string]::IsNullOrWhiteSpace($selectedBudgetEmail)) { $selectedBudgetEmail = $selectedAlertEmail }
Assert-ValidEmailAddress -Value $selectedBudgetEmail -Name 'BudgetEmail'

$registryName = $AzureContainerRegistryName.Trim()
if ([string]::IsNullOrWhiteSpace($registryName) -and
    (Test-ConfiguredValue -Value $config.azureContainerRegistryName)) {
    $registryName = [string]$config.azureContainerRegistryName
}
if ([string]::IsNullOrWhiteSpace($registryName)) {
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $registryName = "loanidp$environmentName$suffix"
}
if ($registryName -cnotmatch '^[a-z0-9]{5,50}$') {
    throw 'AzureContainerRegistryName must contain 5-50 lowercase alphanumeric characters.'
}

$config.azureSubscriptionId = $subscriptionId.ToString()
$config.entraTenantId = $tenantId.ToString()
$config.awsProfile = $selectedProfile
$config.awsAccountId = [string]$awsIdentity.Account
$config.domainName = $domainName
$config.route53HostedZoneId = (([string]$zone.Id) -replace '^/hostedzone/', '')
$config.uiHostName = $selectedUiHost
$config.apiHostName = $selectedApiHost
$config.alertEmail = $selectedAlertEmail
$config.budgetEmail = $selectedBudgetEmail
$config.azureContainerRegistryName = $registryName

$commitConfiguration = $PSCmdlet.ShouldProcess(
    $relativeEnvironmentFile,
    'Validate and atomically replace ignored environment configuration'
)
$validatedConfigPath = New-ValidatedEnvironmentConfigFile -Config $config -Destination $resolvedEnvironmentFile
$environmentLeaf = Split-Path -Leaf $resolvedEnvironmentFile
$backupConfigPath = Join-Path (Split-Path -Parent $resolvedEnvironmentFile) ".${environmentLeaf}.$([guid]::NewGuid().ToString('N')).backup.json"
try {
    if ($commitConfiguration) {
        [System.IO.File]::Replace($validatedConfigPath, $resolvedEnvironmentFile, $backupConfigPath, $true)
        Write-Host 'Environment configuration saved. Cloud identifiers, contacts, profile names, and the complete configuration were not displayed.' -ForegroundColor Green
    } else {
        Write-Host 'Environment configuration validated; no file was changed.'
    }
} finally {
    if (Test-Path -LiteralPath $validatedConfigPath) {
        Remove-Item -LiteralPath $validatedConfigPath -Force -WhatIf:$false
    }
    if (Test-Path -LiteralPath $backupConfigPath) {
        Remove-Item -LiteralPath $backupConfigPath -Force -WhatIf:$false
    }
}
