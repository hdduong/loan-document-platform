[CmdletBinding()]
param(
    [string]$EnvironmentFile,
    [switch]$InstallMissing
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

$commands = @(
    @{ Name = 'git'; Package = 'Git.Git'; Hint = 'Install Git for Windows.' },
    @{ Name = 'gh'; Package = 'GitHub.cli'; Hint = 'Install GitHub CLI.' },
    @{ Name = 'aws'; Package = 'Amazon.AWSCLI'; Hint = 'Install AWS CLI v2.' },
    @{ Name = 'sam'; Package = 'Amazon.SAM-CLI'; Hint = 'Install AWS SAM CLI.' },
    @{ Name = 'az'; Package = 'Microsoft.AzureCLI'; Hint = 'Install Azure CLI.' },
    @{ Name = 'python'; Package = 'Python.Python.3.13'; Hint = 'Install Python 3.12 or later.' },
    @{ Name = 'node'; Package = 'OpenJS.NodeJS.LTS'; Hint = 'Install Node.js 22 or later.' }
)

foreach ($command in $commands) {
    if (-not (Get-Command $command.Name -ErrorAction SilentlyContinue)) {
        if (-not $InstallMissing) {
            throw "Missing '$($command.Name)'. $($command.Hint) Re-run with -InstallMissing to use winget."
        }
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "winget is unavailable; install '$($command.Name)' manually."
        }
        & winget install --id $command.Package --exact --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "Failed to install $($command.Package)." }
    }
}

$pythonVersion = & python --version
$nodeVersion = & node --version
Write-Host "Toolchain installed: $pythonVersion; Node $nodeVersion"

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

$azAccount = & az account show --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Azure CLI is not signed in. Run az login for the target tenant.' }
if ($azAccount.tenantId -ne $config.entraTenantId) {
    throw "Azure CLI tenant $($azAccount.tenantId) does not match $($config.entraTenantId)."
}

Write-Host "Toolchain ready: $pythonVersion; Node $nodeVersion"
Write-Host 'No certificate validation was disabled and no cloud credential was written.'
