[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$searchRoots = @(
    (Join-Path $repositoryRoot "scripts"),
    (Join-Path $repositoryRoot ".specify/scripts/powershell")
)

$allErrors = [System.Collections.Generic.List[string]]::new()
$files = foreach ($root in $searchRoots) {
    if (Test-Path -LiteralPath $root) {
        Get-ChildItem -LiteralPath $root -Recurse -File |
            Where-Object { $_.Extension -in ".ps1", ".psm1" }
    }
}

foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $file.FullName,
        [ref]$tokens,
        [ref]$parseErrors
    ) | Out-Null

    foreach ($parseError in $parseErrors) {
        $allErrors.Add("$($file.FullName):$($parseError.Extent.StartLineNumber): $($parseError.Message)")
    }
}

if ($allErrors.Count -gt 0) {
    throw "PowerShell syntax validation failed:`n$($allErrors -join "`n")"
}

Write-Host "PowerShell syntax passed for $($files.Count) files."
