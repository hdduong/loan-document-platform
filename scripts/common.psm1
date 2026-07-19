Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Get-NormalizedTextSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
    try {
        $text = $strictUtf8.GetString([System.IO.File]::ReadAllBytes($resolved))
    } catch [System.Text.DecoderFallbackException] {
        throw "Reviewed text file '$resolved' must contain valid UTF-8."
    }
    $normalized = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $algorithm.ComputeHash($strictUtf8.GetBytes($normalized))
    } finally {
        $algorithm.Dispose()
    }
    return ([System.BitConverter]::ToString($digest)).Replace('-', '').ToLowerInvariant()
}

function Assert-CertificateOnlyBundle {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'The configured corporate CA bundle does not exist.'
    }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $bundleInfo = Get-Item -LiteralPath $resolved
    if ($bundleInfo.Length -le 0 -or $bundleInfo.Length -gt 10MB) {
        throw 'The corporate CA bundle has an invalid size.'
    }
    $bundleText = [System.IO.File]::ReadAllText($resolved)
    $pemBoundaries = [regex]::Matches(
        $bundleText,
        '(?m)^[^\S\r\n]*-----(?<Boundary>BEGIN|END) (?<Label>[^\r\n]+)-----[^\S\r\n]*\r?$'
    )
    $beginLabels = @(
        $pemBoundaries | Where-Object { $_.Groups['Boundary'].Value -ceq 'BEGIN' }
    )
    $endLabels = @(
        $pemBoundaries | Where-Object { $_.Groups['Boundary'].Value -ceq 'END' }
    )
    if ($beginLabels.Count -eq 0 -or
        @($pemBoundaries | Where-Object { $_.Groups['Label'].Value -cne 'CERTIFICATE' }).Count -gt 0) {
        throw 'The corporate CA bundle must contain certificates and no private key or other PEM object.'
    }
    $certificateBlocks = [regex]::Matches(
        $bundleText,
        '(?ms)-----BEGIN CERTIFICATE-----\s*(?<Body>[A-Za-z0-9+/=\r\n]+?)\s*-----END CERTIFICATE-----'
    )
    if ($certificateBlocks.Count -ne $beginLabels.Count -or
        $endLabels.Count -ne $beginLabels.Count -or
        $pemBoundaries.Count -ne (2 * $certificateBlocks.Count)) {
        throw 'The corporate CA bundle contains an incomplete or invalid certificate block.'
    }
    foreach ($block in $certificateBlocks) {
        $certificate = $null
        try {
            $base64 = $block.Groups['Body'].Value -replace '\s', ''
            $rawCertificate = [Convert]::FromBase64String($base64)
            $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new(
                $rawCertificate
            )
            if ($certificate.RawData.Length -eq 0) { throw 'Empty certificate.' }
        } catch {
            throw 'The corporate CA bundle contains a certificate that cannot be parsed.'
        } finally {
            if ($null -ne $certificate) { $certificate.Dispose() }
        }
    }
    return $resolved
}

function Read-EnvironmentConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $config = Get-Content -Raw -LiteralPath $resolved | ConvertFrom-Json -Depth 20
    $required = @(
        'environment', 'azureSubscriptionId', 'azureLocation', 'azureResourceGroupName',
        'azureContainerRegistryName', 'azureContainerAppsEnvironmentName',
        'azureApiAppName', 'azureApiManagedIdentityName', 'azureStaticWebAppName',
        'azureApiMinReplicas', 'azureApiMaxReplicas',
        'azureApiConcurrentRequestsPerReplica', 'azureContainerAppsZoneRedundant',
        'azureContainerRegistrySku', 'azureStaticWebAppSku',
        'awsRegion', 'awsProfile', 'awsAccountId', 'repositoryName', 'githubOwner',
        'githubRepositoryVisibility', 'githubDeploymentReviewer',
        'githubDefaultBranch', 'githubEnvironment', 'domainName',
        'route53HostedZoneId', 'uiHostName', 'apiHostName',
        'entraTenantId', 'alertEmail', 'budgetEmail', 'platformStackName',
        'bootstrapStackName', 'idpStackName', 'entraApiAppDisplayName',
        'entraSpaAppDisplayName', 'entraAwsFederationAppDisplayName',
        'entraGitHubDeploymentAppDisplayName', 'monthlyBudgetUsd',
        'azureMonthlyBudgetUsd', 'azureBudgetStartDate',
        'maximumUploadBytes', 'maximumQueryItems', 'maximumLoanArchiveDocuments',
        'maximumLoanArchiveManifestBytes', 'maximumPdfPages', 'sourceRetentionDays',
        'logRetentionDays', 'idpVersion', 'idpCommit'
    )
    foreach ($name in $required) {
        $property = $config.PSObject.Properties[$name]
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value) -or [string]$property.Value -match '^REPLACE_') {
            throw "Environment file '$resolved' requires a real value for '$name'."
        }
    }
    $supportedEnvironments = @('dev', 'test', 'stage', 'prod')
    if ([string]$config.environment -cnotin $supportedEnvironments) {
        throw "Environment file '$resolved' requires environment to be one of: $($supportedEnvironments -join ', ')."
    }
    if ([string]$config.githubRepositoryVisibility -notin @('public', 'private')) {
        throw "Environment file '$resolved' requires githubRepositoryVisibility to be 'public' or 'private'."
    }
    foreach ($guidName in 'azureSubscriptionId', 'entraTenantId') {
        $parsedGuid = [guid]::Empty
        if (-not [guid]::TryParse([string]$config.$guidName, [ref]$parsedGuid)) {
            throw "Environment file '$resolved' requires '$guidName' to be a GUID."
        }
    }
    if ([string]$config.awsRegion -ne 'us-west-2') {
        throw "Environment file '$resolved' must keep the AWS data plane in us-west-2."
    }
    if ([string]$config.awsAccountId -notmatch '^\d{12}$') {
        throw "Environment file '$resolved' requires awsAccountId to contain 12 digits."
    }
    if ([string]$config.azureLocation -notmatch '^[a-z0-9]+$') {
        throw "Environment file '$resolved' contains an invalid Azure location."
    }
    if ([string]$config.azureContainerRegistryName -cnotmatch '^[a-z0-9]{5,50}$') {
        throw "Environment file '$resolved' requires a 5-50 character lowercase alphanumeric Azure Container Registry name."
    }
    if ([string]$config.uiHostName -eq [string]$config.apiHostName) {
        throw "Environment file '$resolved' requires distinct UI and API hostnames."
    }
    $domainSuffix = "." + ([string]$config.domainName).TrimEnd('.').ToLowerInvariant()
    foreach ($hostName in 'uiHostName', 'apiHostName') {
        $rawHostValue = [string]$config.$hostName
        $hostValue = $rawHostValue.TrimEnd('.').ToLowerInvariant()
        if ($rawHostValue -cne $hostValue) {
            throw "Environment file '$resolved' requires '$hostName' to be lowercase without a trailing dot."
        }
        if ($hostValue -notmatch '^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' -or
            -not $hostValue.EndsWith($domainSuffix, [StringComparison]::Ordinal)) {
            throw "Environment file '$resolved' requires '$hostName' to be a valid subdomain of domainName."
        }
    }
    foreach ($emailName in 'alertEmail', 'budgetEmail') {
        $emailValue = [string]$config.$emailName
        if ($emailValue.IndexOfAny([char[]]"`r`n") -ge 0) {
            throw "Environment file '$resolved' requires '$emailName' to contain one email address without control characters."
        }
        try {
            $parsedEmail = [System.Net.Mail.MailAddress]::new($emailValue)
        } catch {
            throw "Environment file '$resolved' requires '$emailName' to contain a valid email address."
        }
        if ($emailValue -cne $emailValue.Trim() -or $parsedEmail.Address -cne $emailValue) {
            throw "Environment file '$resolved' requires '$emailName' to contain exactly one canonical email address."
        }
    }
    $minimumReplicas = [int]$config.azureApiMinReplicas
    $maximumReplicas = [int]$config.azureApiMaxReplicas
    $concurrentRequests = [int]$config.azureApiConcurrentRequestsPerReplica
    if ($minimumReplicas -lt 1 -or $maximumReplicas -lt $minimumReplicas -or
        $maximumReplicas -gt 300 -or $concurrentRequests -ne 1) {
        throw "Environment file '$resolved' requires 1 <= azureApiMinReplicas <= azureApiMaxReplicas <= 300 and azureApiConcurrentRequestsPerReplica = 1 while domain calls are serialized."
    }
    if ([string]$config.azureContainerRegistrySku -notin @('Basic', 'Standard', 'Premium')) {
        throw "Environment file '$resolved' contains an unsupported Azure Container Registry SKU."
    }
    if ([string]$config.azureStaticWebAppSku -notin @('Free', 'Standard')) {
        throw "Environment file '$resolved' contains an unsupported Azure Static Web Apps SKU."
    }
    if ([int]$config.monthlyBudgetUsd -lt 1 -or [int]$config.azureMonthlyBudgetUsd -lt 1 -or
        [string]$config.azureBudgetStartDate -notmatch '^\d{4}-\d{2}-01$' -or
        [int64]$config.maximumUploadBytes -lt 1024 -or
        [int]$config.maximumPdfPages -lt 5 -or [int]$config.sourceRetentionDays -lt 365 -or
        [int]$config.logRetentionDays -lt 1) {
        throw "Environment file '$resolved' contains invalid budget, upload, page, or retention limits."
    }
    $maximumQueryItems = [int]$config.maximumQueryItems
    $maximumArchiveDocuments = [int]$config.maximumLoanArchiveDocuments
    $maximumArchiveManifestBytes = [int64]$config.maximumLoanArchiveManifestBytes
    if ($maximumQueryItems -lt 100 -or $maximumQueryItems -gt 100000 -or
        $maximumArchiveDocuments -lt 1 -or $maximumArchiveDocuments -gt 5000 -or
        $maximumArchiveDocuments -gt $maximumQueryItems -or
        $maximumArchiveManifestBytes -lt 1024 -or $maximumArchiveManifestBytes -gt 20971520) {
        throw "Environment file '$resolved' contains invalid query or loan archive limits."
    }
    return $config
}

function Assert-Command {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$InstallHint = ''
    )
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        $message = "Required command '$Name' is not installed."
        if ($InstallHint) { $message += " $InstallHint" }
        throw $message
    }
}

function Resolve-PythonLaunch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidatePattern('^\d+\.\d+$')]
        [string]$Version,
        [switch]$AllowMissing
    )

    $candidates = [System.Collections.Generic.List[object]]::new()
    if ($IsWindows) {
        $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $launcher) {
            $candidates.Add([pscustomobject]@{
                FilePath = $launcher.Source
                PrefixArguments = @("-$Version")
            })
        }
    }

    foreach ($commandName in @("python$Version", 'python')) {
        $command = Get-Command $commandName -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -ne $command) {
            $candidates.Add([pscustomobject]@{
                FilePath = $command.Source
                PrefixArguments = @()
            })
        }
    }

    $probe = 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    foreach ($candidate in $candidates) {
        $probeArguments = @($candidate.PrefixArguments) + @('-c', $probe)
        $actual = (& $candidate.FilePath @probeArguments 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $actual -ceq $Version) {
            return [pscustomobject]@{
                FilePath = $candidate.FilePath
                PrefixArguments = @($candidate.PrefixArguments)
                Version = $actual
            }
        }
    }

    if ($AllowMissing) { return $null }
    throw "Python $Version is required. Install that exact minor version alongside the platform Python 3.13 and IDP Python 3.12 runtimes."
}

function Resolve-CommandSourceOutsidePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$ExcludedDirectory
    )

    $excluded = [IO.Path]::GetFullPath($ExcludedDirectory).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ) + [IO.Path]::DirectorySeparatorChar
    $pathComparison = if ($IsWindows) {
        [StringComparison]::OrdinalIgnoreCase
    } else {
        [StringComparison]::Ordinal
    }
    foreach ($command in @(Get-Command $Name -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ([string]::IsNullOrWhiteSpace([string]$command.Source) -or
            -not [IO.Path]::IsPathFullyQualified([string]$command.Source) -or
            -not (Test-Path -LiteralPath $command.Source -PathType Leaf)) {
            continue
        }
        $source = (Resolve-Path -LiteralPath $command.Source).Path
        if (-not $source.StartsWith($excluded, $pathComparison)) {
            return $source
        }
    }

    throw "Command '$Name' was not found outside the managed IDP CLI environment."
}

function Resolve-WindowsIdpCliBridge {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$SamCommandSource,
        [Parameter(Mandatory)][string]$NodeCommandSource,
        [Parameter(Mandatory)][string]$NpmCommandSource
    )

    foreach ($source in @($SamCommandSource, $NodeCommandSource, $NpmCommandSource)) {
        if (-not [IO.Path]::IsPathFullyQualified($source) -or
            -not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Windows IDP child-tool source '$source' must be an absolute file."
        }
    }

    $samNativeExecutablePath = $null
    $samPythonPath = $null
    if ([IO.Path]::GetExtension($SamCommandSource).Equals('.exe', [StringComparison]::OrdinalIgnoreCase)) {
        $samNativeExecutablePath = $SamCommandSource
    } else {
        if (-not [IO.Path]::GetExtension($SamCommandSource).Equals('.cmd', [StringComparison]::OrdinalIgnoreCase)) {
            throw "The Windows SAM CLI command '$SamCommandSource' is not a native executable or supported official wrapper."
        }
        $wrapper = (Get-Content -Raw -LiteralPath $SamCommandSource).Replace('\', '/')
        if ($wrapper -notmatch '"%~dp0/\.\./runtime/python\.exe"\s+-m\s+samcli\s+%\*') {
            throw "The Windows SAM CLI wrapper '$SamCommandSource' does not match the reviewed official launcher layout."
        }
        $samPythonPath = [IO.Path]::GetFullPath(
            (Join-Path (Split-Path -Parent $SamCommandSource) '../runtime/python.exe')
        )
        if (-not (Test-Path -LiteralPath $samPythonPath -PathType Leaf)) {
            throw "The Windows SAM CLI wrapper '$SamCommandSource' has no bundled Python runtime."
        }
        $samModulePath = Join-Path (Split-Path -Parent $samPythonPath) 'Lib/site-packages/samcli'
        if (-not (Test-Path -LiteralPath $samModulePath -PathType Container)) {
            throw "The Windows SAM CLI wrapper '$SamCommandSource' has no bundled samcli module."
        }
    }

    $npmNativeExecutablePath = $null
    $nodeExecutablePath = $null
    $npmCliPath = $null
    if ([IO.Path]::GetExtension($NpmCommandSource).Equals('.exe', [StringComparison]::OrdinalIgnoreCase)) {
        $npmNativeExecutablePath = $NpmCommandSource
    } else {
        if (-not [IO.Path]::GetExtension($NpmCommandSource).Equals('.cmd', [StringComparison]::OrdinalIgnoreCase)) {
            throw "The Windows npm command '$NpmCommandSource' is not a native executable or supported official wrapper."
        }
        if (-not [IO.Path]::GetExtension($NodeCommandSource).Equals('.exe', [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $NodeCommandSource -PathType Leaf)) {
            throw "The Windows npm bridge requires a native Node.js executable."
        }
        $nodeDirectory = [IO.Path]::GetFullPath((Split-Path -Parent $NodeCommandSource))
        $npmDirectory = [IO.Path]::GetFullPath((Split-Path -Parent $NpmCommandSource))
        if (-not $nodeDirectory.Equals($npmDirectory, [StringComparison]::OrdinalIgnoreCase)) {
            throw "The Windows npm wrapper and native Node.js executable must share an installation directory."
        }
        $npmCliPath = Join-Path (Split-Path -Parent $NodeCommandSource) 'node_modules/npm/bin/npm-cli.js'
        if (-not (Test-Path -LiteralPath $npmCliPath -PathType Leaf)) {
            throw "The Windows Node.js installation has no npm CLI module."
        }
        $nodeExecutablePath = $NodeCommandSource
    }

    return [pscustomobject]@{
        BridgeRequired = $null -ne $samPythonPath -or $null -ne $npmCliPath
        SamNativeExecutablePath = $samNativeExecutablePath
        SamPythonPath = $samPythonPath
        NpmNativeExecutablePath = $npmNativeExecutablePath
        NodeExecutablePath = $nodeExecutablePath
        NpmCliPath = $npmCliPath
    }
}

function Invoke-WithPrependedPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][scriptblock]$ScriptBlock,
        [hashtable]$Environment = @{}
    )

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $originalPath = $env:PATH
    $originalEnvironment = @{}
    try {
        $env:PATH = "$resolved$([IO.Path]::PathSeparator)$originalPath"
        foreach ($name in $Environment.Keys) {
            if ([string]::IsNullOrWhiteSpace([string]$name) -or
                [string]$name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$' -or
                [string]$name -ieq 'PATH') {
                throw "Invalid scoped process environment variable '$name'."
            }
            $originalEnvironment[$name] = @{
                Exists = Test-Path -LiteralPath "Env:$name"
                Value = [Environment]::GetEnvironmentVariable($name, 'Process')
            }
            [Environment]::SetEnvironmentVariable($name, [string]$Environment[$name], 'Process')
        }
        & $ScriptBlock
    } finally {
        foreach ($name in $originalEnvironment.Keys) {
            if ($originalEnvironment[$name].Exists) {
                [Environment]::SetEnvironmentVariable(
                    $name,
                    [string]$originalEnvironment[$name].Value,
                    'Process'
                )
            } else {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue -WhatIf:$false
            }
        }
        $env:PATH = $originalPath
    }
}

function Resolve-AzureCliLaunch {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$CommandSource)

    if ([System.IO.Path]::GetExtension($CommandSource).Equals('.cmd', [StringComparison]::OrdinalIgnoreCase)) {
        $wrapperDirectory = Split-Path -Parent $CommandSource
        $installationDirectory = Split-Path -Parent $wrapperDirectory
        $pythonPath = [System.IO.Path]::GetFullPath((Join-Path $installationDirectory 'python.exe'))
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            throw "Azure CLI command wrapper '$CommandSource' does not have the expected bundled Python engine. Repair the Azure CLI installation."
        }
        return [pscustomobject]@{
            FilePath = $pythonPath
            PrefixArguments = @('-IBm', 'azure.cli')
            Installer = 'MSI'
        }
    }

    return [pscustomobject]@{
        FilePath = $CommandSource
        PrefixArguments = @()
        Installer = ''
    }
}

function Get-AzureCliFailureContext {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    if ($Arguments.Count -eq 0) { return 'unknown operation' }
    if ($Arguments[0] -ne 'rest') {
        return ($Arguments | Select-Object -First 2) -join ' '
    }

    $method = 'UNKNOWN'
    $target = ''
    for ($index = 1; $index -lt $Arguments.Count - 1; $index++) {
        if ($Arguments[$index] -eq '--method') { $method = $Arguments[$index + 1].ToUpperInvariant() }
        if ($Arguments[$index] -in @('--uri', '--url')) { $target = $Arguments[$index + 1] }
    }
    if ([string]::IsNullOrWhiteSpace($target)) { return "rest $method" }

    $parsedTarget = $null
    if (-not [uri]::TryCreate($target, [UriKind]::Absolute, [ref]$parsedTarget)) {
        return "rest $method remote-endpoint"
    }
    $safeHost = if ($parsedTarget.Host -in @('graph.microsoft.com', 'management.azure.com')) {
        $parsedTarget.Host
    } else {
        'remote-endpoint'
    }
    $safePath = $parsedTarget.AbsolutePath
    $safePath = $safePath -replace '(?i)(/subscriptions|/resourceGroups)/[^/]+', '$1/{id}'
    $safePath = $safePath -replace '(?i)(?<=/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?=/|$)', '{id}'
    return "rest $method $safeHost$safePath"
}

function Invoke-AzureCliLaunch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object]$Launch,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $hadInstaller = Test-Path Env:AZ_INSTALLER
    $previousInstaller = $env:AZ_INSTALLER
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    $exitCode = -1
    try {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -WhatIf:$false
        }
        if ($launch.Installer) { $env:AZ_INSTALLER = $launch.Installer }
        $allArguments = @($launch.PrefixArguments) + $Arguments
        $output = @(& $launch.FilePath @allArguments 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $nativePreference.Value -Scope Local -WhatIf:$false
        }
        if ($hadInstaller) {
            $env:AZ_INSTALLER = $previousInstaller
        } else {
            Remove-Item Env:AZ_INSTALLER -ErrorAction SilentlyContinue -WhatIf:$false
        }
    }
    if ($exitCode -ne 0) {
        $operation = Get-AzureCliFailureContext -Arguments $Arguments
        throw "Azure CLI failed while running 'az $operation'."
    }
    return $output
}

function Invoke-AzureCli {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    $command = Get-Command az -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $launch = Resolve-AzureCliLaunch -CommandSource $command.Source
    return Invoke-AzureCliLaunch -Launch $launch -Arguments $Arguments
}

function Get-AwsCliFailureContext {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string[]]$Arguments)

    if ($Arguments.Count -lt 2) { return 'unknown operation' }
    $safeParts = @($Arguments[0], $Arguments[1])
    if (@($safeParts | Where-Object { $_ -cnotmatch '^[a-z0-9][a-z0-9-]*$' }).Count -gt 0) {
        return 'unknown operation'
    }
    return ($safeParts -join ' ')
}

function Invoke-Aws {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$CaptureJson,
        [switch]$ForceProfile
    )
    $allArguments = @('--region', $Region, '--no-cli-pager') + $Arguments
    if ($ForceProfile -or $env:GITHUB_ACTIONS -ne 'true') {
        $allArguments = @('--profile', $Profile) + $allArguments
    }
    if ($CaptureJson) { $allArguments += @('--output', 'json') }
    $output = @()
    $exitCode = -1
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    try {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -WhatIf:$false
        }
        $output = @(& aws @allArguments 2>$null)
        $exitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $nativePreference.Value -Scope Local -WhatIf:$false
        }
    }
    if ($exitCode -ne 0) {
        $operation = Get-AwsCliFailureContext -Arguments $Arguments
        throw "AWS CLI failed while running 'aws $operation'."
    }
    if ($CaptureJson) {
        return ($output | Out-String | ConvertFrom-Json -Depth 50)
    }
    return $output
}

function Assert-AwsIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [string]$ExpectedAccountId = ''
    )
    $identity = Invoke-Aws -Profile $Profile -Region $Region -Arguments @('sts', 'get-caller-identity') -CaptureJson
    if ($ExpectedAccountId -and $identity.Account -ne $ExpectedAccountId) {
        throw 'The authenticated AWS identity does not match the configured account.'
    }
    Write-Host 'AWS identity and region verified.'
    return $identity
}

function Assert-AzureIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][object]$Account,
        [string]$ExpectedSubscriptionId = '',
        [string]$ExpectedTenantId = ''
    )

    $subscriptionProperty = $Account.PSObject.Properties['id']
    $tenantProperty = $Account.PSObject.Properties['tenantId']
    $subscriptionId = [guid]::Empty
    $tenantId = [guid]::Empty
    if ($null -eq $subscriptionProperty -or $null -eq $tenantProperty -or
        -not [guid]::TryParse([string]$subscriptionProperty.Value, [ref]$subscriptionId) -or
        -not [guid]::TryParse([string]$tenantProperty.Value, [ref]$tenantId)) {
        throw 'Azure account lookup returned invalid identity identifiers.'
    }

    if ($ExpectedSubscriptionId) {
        $expectedSubscription = [guid]::Empty
        if (-not [guid]::TryParse($ExpectedSubscriptionId, [ref]$expectedSubscription)) {
            throw 'The configured Azure subscription identifier is invalid.'
        }
        if ($subscriptionId -ne $expectedSubscription) {
            throw 'The authenticated Azure subscription does not match the configured subscription.'
        }
    }
    if ($ExpectedTenantId) {
        $expectedTenant = [guid]::Empty
        if (-not [guid]::TryParse($ExpectedTenantId, [ref]$expectedTenant)) {
            throw 'The configured Azure tenant identifier is invalid.'
        }
        if ($tenantId -ne $expectedTenant) {
            throw 'The authenticated Azure tenant does not match the configured tenant.'
        }
    }

    return [pscustomobject]@{
        SubscriptionId = $subscriptionId
        TenantId = $tenantId
    }
}

function Test-AwsCloudFormationStackNotFound {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$ErrorText
    )

    $normalized = $ErrorText.Trim()
    $awsCliServiceErrorPrefix = 'aws: [ERROR]: '
    if ($normalized.StartsWith($awsCliServiceErrorPrefix, [StringComparison]::Ordinal)) {
        $normalized = $normalized.Substring($awsCliServiceErrorPrefix.Length)
    }
    return [bool]($normalized -match '\AAn error occurred \(ValidationError\) when calling the DescribeStacks operation: Stack with id [^\r\n]+ does not exist\.?\z')
}

function Get-AwsCloudFormationStackDescription {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [Parameter(Mandatory)][string]$StackName,
        [switch]$AllowMissing
    )

    $arguments = @(
        '--region', $Region,
        '--no-cli-pager',
        '--output', 'json',
        'cloudformation', 'describe-stacks',
        '--stack-name', $StackName
    )
    if ($env:GITHUB_ACTIONS -ne 'true') {
        $arguments = @('--profile', $Profile) + $arguments
    }

    $raw = @()
    $exitCode = -1
    $nativePreference = Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue
    try {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $false -Scope Local -WhatIf:$false
        }
        $raw = @(& aws @arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        if ($null -ne $nativePreference) {
            Set-Variable -Name PSNativeCommandUseErrorActionPreference -Value $nativePreference.Value -Scope Local -WhatIf:$false
        }
    }

    $text = ($raw | Out-String).Trim()
    if ($exitCode -ne 0) {
        if ($AllowMissing -and (Test-AwsCloudFormationStackNotFound -ErrorText $text)) {
            Write-Host "CloudFormation stack '$StackName' does not exist yet."
            return $null
        }
        throw "AWS CLI failed while running 'aws cloudformation describe-stacks'."
    }

    try {
        $document = $text | ConvertFrom-Json -Depth 50
    } catch {
        throw "CloudFormation returned invalid JSON while describing stack '$StackName'."
    }
    $stacks = @($document.Stacks)
    if ($stacks.Count -ne 1) {
        throw "Expected one CloudFormation stack named '$StackName', found $($stacks.Count)."
    }
    return $stacks[0]
}

function Assert-AwsStatefulStackPolicy {
    [CmdletBinding()]
    param([Parameter(Mandatory)][object]$Policy)

    $hasDefaultAllow = $false
    $hasStatefulDeny = $false
    $requiredTypes = @('AWS::DynamoDB::Table', 'AWS::KMS::Key', 'AWS::S3::Bucket')
    foreach ($statement in @($Policy.Statement)) {
        $actions = @($statement.Action)
        $resources = @($statement.Resource)
        if ([string]$statement.Effect -ceq 'Allow' -and
            $actions -ccontains 'Update:*' -and $resources -ccontains '*') {
            $hasDefaultAllow = $true
        }
        if ([string]$statement.Effect -cne 'Deny' -or
            -not ($actions -ccontains 'Update:Delete') -or
            -not ($actions -ccontains 'Update:Replace') -or
            -not ($resources -ccontains '*')) {
            continue
        }
        $conditionProperty = $statement.PSObject.Properties['Condition']
        if ($null -eq $conditionProperty) { continue }
        $equalsProperty = $conditionProperty.Value.PSObject.Properties['StringEquals']
        if ($null -eq $equalsProperty) { continue }
        $resourceTypeProperty = $equalsProperty.Value.PSObject.Properties['ResourceType']
        if ($null -eq $resourceTypeProperty) { continue }
        $protectedTypes = @($resourceTypeProperty.Value)
        if (@($requiredTypes | Where-Object { $protectedTypes -cnotcontains $_ }).Count -eq 0) {
            $hasStatefulDeny = $true
        }
    }
    if (-not $hasDefaultAllow -or -not $hasStatefulDeny) {
        throw 'Stack policy must allow normal updates while denying deletion and replacement of S3 buckets, DynamoDB tables, and KMS keys.'
    }
}

function Set-AwsStatefulStackPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [Parameter(Mandatory)][string]$StackName,
        [Parameter(Mandatory)][string]$PolicyPath
    )

    $resolvedPath = (Resolve-Path -LiteralPath $PolicyPath).Path
    $policyBody = Get-Content -Raw -LiteralPath $resolvedPath
    try {
        $policy = $policyBody | ConvertFrom-Json -Depth 30
    } catch {
        throw "Stack policy '$resolvedPath' is not valid JSON."
    }
    Assert-AwsStatefulStackPolicy -Policy $policy

    Invoke-Aws -Profile $Profile -Region $Region -Arguments @(
        'cloudformation', 'set-stack-policy',
        '--stack-name', $StackName,
        '--stack-policy-body', $policyBody
    ) | Out-Null
    $deployed = Invoke-Aws -Profile $Profile -Region $Region -Arguments @(
        'cloudformation', 'get-stack-policy',
        '--stack-name', $StackName
    ) -CaptureJson
    if ([string]::IsNullOrWhiteSpace([string]$deployed.StackPolicyBody)) {
        throw "CloudFormation stack '$StackName' has no stack policy after the release gate was applied."
    }
    try {
        $deployedPolicy = [string]$deployed.StackPolicyBody | ConvertFrom-Json -Depth 30
    } catch {
        throw "CloudFormation stack '$StackName' returned an invalid stack policy."
    }
    Assert-AwsStatefulStackPolicy -Policy $deployedPolicy
    Write-Host "Verified stateful-resource stack policy on '$StackName'."
}

function Get-StackOutputs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [Parameter(Mandatory)][string]$StackName
    )
    $response = Invoke-Aws -Profile $Profile -Region $Region -Arguments @(
        'cloudformation', 'describe-stacks', '--stack-name', $StackName
    ) -CaptureJson
    $result = @{}
    foreach ($output in $response.Stacks[0].Outputs) {
        $result[$output.OutputKey] = $output.OutputValue
    }
    return $result
}

Export-ModuleMember -Function Get-ProjectRoot, Get-NormalizedTextSha256, Assert-CertificateOnlyBundle, Read-EnvironmentConfig, Assert-Command, Resolve-PythonLaunch, Resolve-CommandSourceOutsidePath, Resolve-WindowsIdpCliBridge, Invoke-WithPrependedPath, Invoke-AzureCli, Invoke-Aws, Assert-AwsIdentity, Assert-AzureIdentity, Test-AwsCloudFormationStackNotFound, Get-AwsCloudFormationStackDescription, Assert-AwsStatefulStackPolicy, Set-AwsStatefulStackPolicy, Get-StackOutputs
