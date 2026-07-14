[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$CommonName,
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\.local\certificates'),
    [ValidateSet(2048, 3072, 4096)][int]$KeyLength = 3072,
    [switch]$MachineContext
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not (Get-Command certreq.exe -ErrorAction SilentlyContinue)) {
    throw 'certreq.exe is required to create a Windows non-exportable certificate request.'
}

$safeName = $CommonName -replace '[^A-Za-z0-9_.-]', '-'
$outputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$infPath = Join-Path $outputDirectory "$safeName.inf"
$requestPath = Join-Path $outputDirectory "$safeName.req"
$context = if ($MachineContext) { 'Machine' } else { 'User' }

$inf = @"
[Version]
Signature=`"`$Windows NT`$`"

[NewRequest]
Subject = `"CN=$CommonName`"
KeyAlgorithm = RSA
KeyLength = $KeyLength
HashAlgorithm = SHA256
KeySpec = AT_SIGNATURE
KeyUsage = 0x80
MachineKeySet = $(if ($MachineContext) { 'TRUE' } else { 'FALSE' })
Exportable = FALSE
ExportableEncrypted = FALSE
ProviderName = `"Microsoft Software Key Storage Provider`"
RequestType = PKCS10
SMIME = FALSE

[EnhancedKeyUsageExtension]
OID=1.3.6.1.5.5.7.3.2
"@

[System.IO.File]::WriteAllText($infPath, $inf, [System.Text.Encoding]::ASCII)
& certreq.exe -new -q $infPath $requestPath
if ($LASTEXITCODE -ne 0) { throw 'certreq failed to generate the request.' }

Write-Host "Created certificate request: $requestPath"
Write-Host "Private key context: $context; non-exportable; RSA $KeyLength; SHA-256"
Write-Host 'Submit the .req to the approved enterprise/public CA, then accept the returned certificate with:'
Write-Host "  certreq.exe -accept <issued-certificate.cer>"
Write-Host 'Only the issued public .cer is registered with Entra. Never commit this directory or a private key.'
