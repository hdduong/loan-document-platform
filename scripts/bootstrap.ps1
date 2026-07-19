[CmdletBinding()]
param(
    [string]$EnvironmentFile,
    [switch]$InstallMissing
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force
$requiredTrivyVersion = '0.72.0'
$idpLock = Get-Content -Raw -LiteralPath (Join-Path (Get-ProjectRoot) 'vendor/idp.lock.json') |
    ConvertFrom-Json -Depth 10
$requiredIdpPythonVersion = [string]$idpLock.cliPythonVersion
if ($requiredIdpPythonVersion -cne '3.12') {
    throw 'The reviewed IDP 0.5.16 toolchain requires cliPythonVersion 3.12.'
}

$commands = @(
    @{ Name = 'git'; Package = 'Git.Git'; Hint = 'Install Git for Windows.' },
    @{ Name = 'gh'; Package = 'GitHub.cli'; Hint = 'Install GitHub CLI.' },
    @{ Name = 'aws'; Package = 'Amazon.AWSCLI'; Hint = 'Install AWS CLI v2.' },
    @{ Name = 'sam'; Package = 'Amazon.SAM-CLI'; Hint = 'Install AWS SAM CLI.' },
    @{ Name = 'az'; Package = 'Microsoft.AzureCLI'; Hint = 'Install Azure CLI.' },
    @{ Name = 'python'; Package = 'Python.Python.3.13'; Hint = 'Install Python 3.13.' },
    @{ Name = 'node'; Package = 'OpenJS.NodeJS.LTS'; Hint = 'Install Node.js 22 or later.' },
    @{ Name = 'trivy'; Package = 'AquaSecurity.Trivy'; Version = $requiredTrivyVersion; Hint = 'Install the pinned Trivy scanner for the immutable production image gate.' }
)

foreach ($command in $commands) {
    if (-not (Get-Command $command.Name -ErrorAction SilentlyContinue)) {
        if (-not $InstallMissing) {
            throw "Missing '$($command.Name)'. $($command.Hint) Re-run with -InstallMissing to use winget."
        }
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget is unavailable; install '$($command.Name)' manually."
        }
        $wingetArguments = @(
            'install', '--id', $command.Package, '--exact', '--source', 'winget',
            '--accept-package-agreements', '--accept-source-agreements'
        )
        if ($command.ContainsKey('Version')) { $wingetArguments += @('--version', $command.Version) }
        & winget @wingetArguments
        if ($LASTEXITCODE -ne 0) { throw "Failed to install $($command.Package)." }
    }
}

$idpPython = Resolve-PythonLaunch -Version $requiredIdpPythonVersion -AllowMissing
if ($null -eq $idpPython) {
    if (-not $InstallMissing) {
        throw "Missing Python $requiredIdpPythonVersion for the pinned IDP CLI. Re-run with -InstallMissing to use winget on Windows."
    }
    if (-not $IsWindows) {
        throw "Install Python $requiredIdpPythonVersion with the operating system package manager, then rerun bootstrap."
    }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is unavailable; install Python $requiredIdpPythonVersion manually."
    }
    & winget install --id Python.Python.3.12 --exact --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install Python.Python.3.12.' }
    $idpPython = Resolve-PythonLaunch -Version $requiredIdpPythonVersion
}

$trivyVersion = (& trivy --version | Select-Object -First 1 | Out-String).Trim()
if ($trivyVersion -ne "Version: $requiredTrivyVersion") {
    throw "Trivy $requiredTrivyVersion is required; found '$trivyVersion'."
}

$pythonVersion = & python --version
$nodeVersion = & node --version
Write-Host "Toolchain installed: $pythonVersion; IDP CLI Python $($idpPython.Version); Node $nodeVersion"

if ([string]::IsNullOrWhiteSpace($EnvironmentFile)) {
    Write-Host 'No environment file supplied; cloud identity and tenant checks were skipped.'
    Write-Host 'No certificate validation was disabled and no cloud credential was written.'
    return
}

$config = Read-EnvironmentConfig -Path $EnvironmentFile

if ($config.corporateCaBundlePath) {
    $caPath = (Resolve-Path -LiteralPath $config.corporateCaBundlePath).Path
    $env:REQUESTS_CA_BUNDLE = $caPath
    $env:SSL_CERT_FILE = $caPath
    Write-Host "Configured this process to trust corporate CA bundle: $caPath"
}

Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null

& gh auth status
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login.' }

& az account set --subscription $config.azureSubscriptionId
if ($LASTEXITCODE -ne 0) { throw "Cannot select Azure subscription '$($config.azureSubscriptionId)'." }
$azAccount = & az account show --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Azure CLI is not signed in. Run az login for the target tenant.' }
if ($azAccount.tenantId -ne $config.entraTenantId) {
    throw "Azure CLI tenant $($azAccount.tenantId) does not match $($config.entraTenantId)."
}
if ($azAccount.id -ne $config.azureSubscriptionId) {
    throw "Azure CLI subscription $($azAccount.id) does not match $($config.azureSubscriptionId)."
}
& az bicep build --file (Join-Path (Get-ProjectRoot) 'infra/azure/main.bicep') --stdout | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Azure Bicep compilation failed.' }

Write-Host "Toolchain ready: $pythonVersion; IDP CLI Python $($idpPython.Version); Node $nodeVersion"
Write-Host 'No certificate validation was disabled and no cloud credential was written.'
