[CmdletBinding()]
param(
    [switch]$SkipRepositoryValidation
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common.psm1") -Force

$repositoryRoot = Get-ProjectRoot
$lockPath = Join-Path $repositoryRoot "vendor/spec-kit.lock.json"
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json

Assert-Command -Name "uvx" -InstallHint "Install uv from https://docs.astral.sh/uv/."

if ($lock.commit -notmatch "^[0-9a-f]{40}$") {
    throw "Spec Kit lock must contain an immutable 40-character commit SHA."
}
if ($lock.integration -ne "claude" -or $lock.script -ne "ps") {
    throw "This repository requires the Claude integration and PowerShell scripts."
}

$source = "git+$($lock.repository).git@$($lock.commit)"
$arguments = @(
    "--system-certs",
    "--from", $source,
    "specify", "init",
    "--here",
    "--force",
    "--integration", $lock.integration,
    "--script", $lock.script,
    "--ignore-agent-tools"
)

Push-Location $repositoryRoot
try {
    & uvx @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Pinned Spec Kit refresh failed with exit code $LASTEXITCODE."
    }

    $initOptionsPath = Join-Path $repositoryRoot ".specify/init-options.json"
    $initOptions = Get-Content -LiteralPath $initOptionsPath -Raw | ConvertFrom-Json
    if (
        $initOptions.speckit_version -ne $lock.version -or
        $initOptions.integration -ne $lock.integration -or
        $initOptions.script -ne $lock.script -or
        $initOptions.ai_skills -ne $true
    ) {
        throw "Generated Spec Kit metadata does not match vendor/spec-kit.lock.json."
    }

    if (-not $SkipRepositoryValidation) {
        $venvPython = Join-Path $repositoryRoot ".venv/Scripts/python.exe"
        if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
            & $venvPython "scripts/validate-repository.py"
        } else {
            Assert-Command -Name "python" -InstallHint "Install Python 3.13 or create .venv."
            & python "scripts/validate-repository.py"
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Repository validation failed after refreshing Spec Kit."
        }
    }
} finally {
    Pop-Location
}

Write-Host "Spec Kit $($lock.version) Claude/PowerShell assets are synchronized."
