[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$version = '0.72.0'
$expectedSha256 = 'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'
$assetName = "trivy_${version}_Linux-64bit.tar.gz"
$downloadUri = "https://github.com/aquasecurity/trivy/releases/download/v$version/$assetName"

if (-not $IsLinux -or
    [Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne
        [Runtime.InteropServices.Architecture]::X64) {
    throw 'The pinned CI Trivy installer supports only Linux x64 GitHub runners.'
}

$baseDirectory = if ([string]::IsNullOrWhiteSpace($env:RUNNER_TEMP)) {
    [IO.Path]::GetTempPath()
} else {
    $env:RUNNER_TEMP
}
$installDirectory = Join-Path $baseDirectory "trivy-$version-$([guid]::NewGuid().Guid)"
$archivePath = Join-Path $installDirectory $assetName
[IO.Directory]::CreateDirectory($installDirectory) | Out-Null

try {
    Invoke-WebRequest -Uri $downloadUri -OutFile $archivePath -MaximumRedirection 5
    $actualSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -cne $expectedSha256) {
        throw 'The downloaded Trivy archive does not match the repository-pinned SHA-256 digest.'
    }

    & tar -xzf $archivePath -C $installDirectory trivy
    if ($LASTEXITCODE -ne 0) { throw 'Failed to extract the pinned Trivy executable.' }
    $executable = Join-Path $installDirectory 'trivy'
    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw 'The pinned Trivy archive did not contain the expected executable.'
    }
    & chmod 0755 $executable
    if ($LASTEXITCODE -ne 0) { throw 'Failed to make the pinned Trivy executable runnable.' }

    if ([string]::IsNullOrWhiteSpace($env:GITHUB_PATH)) {
        $env:PATH = "$installDirectory$([IO.Path]::PathSeparator)$env:PATH"
    } else {
        [IO.File]::AppendAllText(
            $env:GITHUB_PATH,
            "$installDirectory$([Environment]::NewLine)",
            [Text.UTF8Encoding]::new($false)
        )
    }
    & $executable --version
    if ($LASTEXITCODE -ne 0) { throw 'The pinned Trivy executable failed its version smoke check.' }
} finally {
    if (Test-Path -LiteralPath $archivePath) {
        Remove-Item -LiteralPath $archivePath -Force
    }
}
