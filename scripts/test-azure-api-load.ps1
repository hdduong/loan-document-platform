<#
.SYNOPSIS
Runs a sanitized synthetic latency check against the deployed Azure product API.

.DESCRIPTION
The script creates a uniquely named synthetic loan and unuploaded document intents,
measures concurrent document-upload initialization and document-status reads, then
archives the synthetic loan. It never uploads PDF bytes, prints response bodies,
prints bearer tokens, or persists signed S3 grants.

Pass a token in memory with `-AccessToken $secureToken`, or set the process-scoped
`AZURE_API_TEST_TOKEN` environment variable. The latter is cleared immediately
after copying. The token must belong to an isolated test identity with the required
delegated scopes and assigned roles.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [Security.SecureString]$AccessToken,
    [string]$BaseUrl = '',
    [string]$AzureDeploymentFile = '',
    [string]$EntraDeploymentFile = '',
    [string]$SyntheticLoanId = '',
    [ValidateRange(1, 10000)][int]$RequestCount = 100,
    [ValidateRange(1, 500)][int]$Concurrency = 20,
    [ValidateRange(0, 1000)][int]$WarmupCount = 5,
    [ValidateRange(1, 60000)][int]$MaximumP95Milliseconds = 2000,
    [switch]$AllowSyntheticMutations,
    [switch]$AllowProductionSyntheticTest,
    [switch]$KeepSyntheticLoan
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

if (-not $AllowSyntheticMutations) {
    throw 'This check creates and archives synthetic records. Re-run with -AllowSyntheticMutations after confirming the target environment.'
}
$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
if ([string]$config.environment -eq 'prod' -and -not $AllowProductionSyntheticTest) {
    throw 'Production synthetic load requires the explicit -AllowProductionSyntheticTest switch.'
}
if ($Concurrency -gt $RequestCount) { $Concurrency = $RequestCount }

if ([string]::IsNullOrWhiteSpace($AzureDeploymentFile)) {
    $AzureDeploymentFile = Join-Path $root ".local/azure-$($config.environment).json"
}
if ([string]::IsNullOrWhiteSpace($EntraDeploymentFile)) {
    $EntraDeploymentFile = Join-Path $root ".local/entra-$($config.environment).json"
}
if (-not (Test-Path -LiteralPath $EntraDeploymentFile)) {
    throw "Entra deployment identifiers are missing: $EntraDeploymentFile"
}
$entra = Get-Content -Raw -LiteralPath $EntraDeploymentFile | ConvertFrom-Json -Depth 30
if ([string]$entra.tenantId -ne [string]$config.entraTenantId) {
    throw 'Entra deployment output belongs to a different tenant.'
}

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    if (-not (Test-Path -LiteralPath $AzureDeploymentFile)) {
        throw "Azure deployment output is missing: $AzureDeploymentFile. Pass -BaseUrl explicitly or deploy the finalized Azure API."
    }
    $azure = Get-Content -Raw -LiteralPath $AzureDeploymentFile | ConvertFrom-Json -Depth 30
    if ([string]$azure.phase -ne 'finalized' -or $null -eq $azure.api) {
        throw 'Azure deployment output does not describe a finalized API.'
    }
    $BaseUrl = [string]$azure.api.defaultUrl
}
$BaseUrl = $BaseUrl.TrimEnd('/')
$baseUri = [uri]$BaseUrl
if (-not $baseUri.IsAbsoluteUri -or $baseUri.Scheme -ne 'https' -or $baseUri.AbsolutePath -ne '/' -or -not [string]::IsNullOrWhiteSpace($baseUri.Query) -or -not [string]::IsNullOrWhiteSpace($baseUri.Fragment)) {
    throw 'BaseUrl must be an absolute HTTPS origin without query or fragment.'
}
if (-not $baseUri.IsDefaultPort) { throw 'BaseUrl must use the default HTTPS port.' }
$allowedHosts = @([string]$config.apiHostName)
if (Test-Path -LiteralPath $AzureDeploymentFile) {
    $hostDeployment = Get-Content -Raw -LiteralPath $AzureDeploymentFile | ConvertFrom-Json -Depth 30
    if ([string]$hostDeployment.phase -eq 'finalized' -and $null -ne $hostDeployment.api) {
        $allowedHosts += [string]$hostDeployment.api.fqdn
    }
}
if (-not ($allowedHosts | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and [string]$_ -ieq $baseUri.Host })) {
    throw 'BaseUrl host is not the finalized Container App FQDN or the configured API custom hostname. Refusing to send the bearer token.'
}

if ($null -eq $AccessToken) {
    if ([string]::IsNullOrWhiteSpace($env:AZURE_API_TEST_TOKEN)) {
        throw 'Supply an in-memory SecureString with -AccessToken or set process-scoped AZURE_API_TEST_TOKEN. The script never prompts for or persists a token.'
    }
    $environmentToken = $env:AZURE_API_TEST_TOKEN
    $env:AZURE_API_TEST_TOKEN = $null
    try {
        $AccessToken = ConvertTo-SecureString $environmentToken -AsPlainText -Force
    } finally {
        $environmentToken = $null
    }
}

$bstr = [IntPtr]::Zero
$token = $null
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AccessToken)
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
if ([string]::IsNullOrWhiteSpace($token) -or $token.Contains("`r") -or $token.Contains("`n")) {
    throw 'The supplied bearer token is empty or malformed.'
}

function ConvertFrom-Base64Url {
    param([Parameter(Mandatory)][string]$Value)
    $normalized = $Value.Replace('-', '+').Replace('_', '/')
    switch ($normalized.Length % 4) {
        2 { $normalized += '==' }
        3 { $normalized += '=' }
        0 { }
        default { throw 'Bearer token payload has invalid base64url length.' }
    }
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($normalized))
}

$tokenParts = $token.Split('.')
if ($tokenParts.Count -ne 3) { throw 'The supplied bearer token is not a JWT access token.' }
try {
    $claims = ConvertFrom-Base64Url -Value $tokenParts[1] | ConvertFrom-Json -Depth 20
} catch {
    throw 'The supplied bearer token payload cannot be decoded.'
}
if ([string]$claims.tid -ne [string]$config.entraTenantId) { throw 'Bearer token tenant does not match the target environment.' }
if ([string]$claims.aud -ne [string]$entra.api.audience) { throw 'Bearer token audience does not match the product API.' }
$nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
if ([long]$claims.exp -le ($nowEpoch + 60)) { throw 'Bearer token expires too soon for a load test.' }
$clientClaim = if ($claims.PSObject.Properties['azp']) { [string]$claims.azp } else { [string]$claims.appid }
$allowedTokenClients = @([string]$entra.spa.clientId)
if ($null -ne $entra.service -and -not [string]::IsNullOrWhiteSpace([string]$entra.service.clientId)) {
    $allowedTokenClients += [string]$entra.service.clientId
}
if ($allowedTokenClients -notcontains $clientClaim) { throw 'Bearer token client is not an approved SPA or service test client.' }

$requiredPermissions = @('Loan.Create', 'Loan.Archive', 'Document.Upload', 'Document.Read')
$roles = @($claims.roles)
$tokenType = if ($claims.PSObject.Properties['idtyp']) { [string]$claims.idtyp } else { 'user' }
if ($tokenType -eq 'app') {
    $missing = @($requiredPermissions | Where-Object { $roles -notcontains $_ })
} else {
    $scopes = @(([string]$claims.scp).Split(' ', [StringSplitOptions]::RemoveEmptyEntries))
    $missing = @($requiredPermissions | Where-Object { $roles -notcontains $_ -or $scopes -notcontains $_ })
}
if ($missing.Count -gt 0) { throw 'Bearer token lacks one or more required synthetic-test permissions.' }
$claims = $null

if ([string]::IsNullOrWhiteSpace($SyntheticLoanId)) {
    $SyntheticLoanId = "loadtest-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
}
if ($SyntheticLoanId -notmatch '^loadtest-[A-Za-z0-9_-]{1,55}$') {
    throw 'SyntheticLoanId must start with loadtest- and use only contract-safe characters.'
}
$encodedLoanId = [uri]::EscapeDataString($SyntheticLoanId)

$handler = [Net.Http.HttpClientHandler]::new()
$handler.AllowAutoRedirect = $false
$client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(30)
$client.DefaultRequestHeaders.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token)
$client.DefaultRequestHeaders.UserAgent.ParseAdd('loan-platform-synthetic-load/1.0')

function New-ApiRequest {
    param(
        [Parameter(Mandatory)][Net.Http.HttpMethod]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body,
        [switch]$Mutation
    )
    $request = [Net.Http.HttpRequestMessage]::new($Method, [uri]::new($baseUri, $Path))
    $request.Headers.TryAddWithoutValidation('X-Correlation-Id', [guid]::NewGuid().Guid) | Out-Null
    if ($Mutation) { $request.Headers.TryAddWithoutValidation('Idempotency-Key', [guid]::NewGuid().Guid) | Out-Null }
    if ($PSBoundParameters.ContainsKey('Body')) {
        $json = $Body | ConvertTo-Json -Depth 10 -Compress
        $request.Content = [Net.Http.StringContent]::new($json, [Text.Encoding]::UTF8, 'application/json')
    }
    return $request
}

function Invoke-SetupRequest {
    param(
        [Parameter(Mandatory)][Net.Http.HttpRequestMessage]$Request,
        [Parameter(Mandatory)][int[]]$ExpectedStatus,
        [switch]$ParseJson
    )
    $response = $null
    $bytes = $null
    try {
        $response = $client.SendAsync($Request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $status = [int]$response.StatusCode
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        if ($ExpectedStatus -notcontains $status) { throw "Synthetic setup request failed with HTTP $status; response content was suppressed." }
        if ($ParseJson) {
            $json = [Text.Encoding]::UTF8.GetString($bytes)
            try { return ($json | ConvertFrom-Json -Depth 20) } finally { $json = $null }
        }
        return $null
    } finally {
        if ($null -ne $bytes -and $bytes.Length -gt 0) { [Array]::Clear($bytes, 0, $bytes.Length) }
        if ($null -ne $response) { $response.Dispose() }
        $Request.Dispose()
    }
}

function Complete-OneRequest {
    param(
        [Parameter(Mandatory)][System.Collections.ArrayList]$Pending,
        [Parameter(Mandatory)][System.Collections.Generic.List[double]]$Latencies,
        [Parameter(Mandatory)][int]$ExpectedStatus,
        [Parameter(Mandatory)][string]$PhaseName
    )
    $tasks = [System.Threading.Tasks.Task[]]@($Pending | ForEach-Object { $_.Task })
    $completedTask = [Threading.Tasks.Task]::WhenAny($tasks).GetAwaiter().GetResult()
    $record = $Pending | Where-Object { $_.Task -eq $completedTask } | Select-Object -First 1
    if ($null -eq $record) { throw "Internal load runner error in phase '$PhaseName'." }
    $response = $null
    $bytes = $null
    try {
        $response = $record.Task.GetAwaiter().GetResult()
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $record.Stopwatch.Stop()
        if ([int]$response.StatusCode -ne $ExpectedStatus) {
            throw "Phase '$PhaseName' request failed with HTTP $([int]$response.StatusCode); response content was suppressed."
        }
        $Latencies.Add([double]$record.Stopwatch.Elapsed.TotalMilliseconds)
    } finally {
        if ($null -ne $bytes -and $bytes.Length -gt 0) { [Array]::Clear($bytes, 0, $bytes.Length) }
        if ($null -ne $response) { $response.Dispose() }
        $record.Request.Dispose()
        $Pending.Remove($record) | Out-Null
    }
}

function Invoke-LoadPhase {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][int]$Count,
        [Parameter(Mandatory)][int]$MaximumConcurrency,
        [Parameter(Mandatory)][scriptblock]$RequestFactory,
        [Parameter(Mandatory)][int]$ExpectedStatus
    )
    $pending = [System.Collections.ArrayList]::new()
    $latencies = [System.Collections.Generic.List[double]]::new()
    try {
        for ($index = 0; $index -lt $Count; $index++) {
            $request = & $RequestFactory $index
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $task = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
            $pending.Add([pscustomobject]@{ Task = $task; Stopwatch = $watch; Request = $request }) | Out-Null
            if ($pending.Count -ge $MaximumConcurrency) {
                Complete-OneRequest -Pending $pending -Latencies $latencies -ExpectedStatus $ExpectedStatus -PhaseName $Name
            }
        }
        while ($pending.Count -gt 0) {
            Complete-OneRequest -Pending $pending -Latencies $latencies -ExpectedStatus $ExpectedStatus -PhaseName $Name
        }
    } finally {
        foreach ($record in @($pending)) {
            $remainingResponse = $null
            try {
                $remainingResponse = $record.Task.GetAwaiter().GetResult()
            } catch { }
            if ($null -ne $remainingResponse) { $remainingResponse.Dispose() }
            $record.Stopwatch.Stop()
            $record.Request.Dispose()
        }
        $pending.Clear()
    }
    return $latencies.ToArray()
}

function Get-LatencySummary {
    param([Parameter(Mandatory)][double[]]$Values)
    if ($Values.Count -eq 0) { throw 'Cannot summarize an empty latency sample.' }
    $sorted = @($Values | Sort-Object)
    $p50Index = [Math]::Max(0, [Math]::Ceiling($sorted.Count * 0.50) - 1)
    $p95Index = [Math]::Max(0, [Math]::Ceiling($sorted.Count * 0.95) - 1)
    return [ordered]@{
        count = $sorted.Count
        averageMilliseconds = [Math]::Round(($sorted | Measure-Object -Average).Average, 2)
        p50Milliseconds = [Math]::Round($sorted[$p50Index], 2)
        p95Milliseconds = [Math]::Round($sorted[$p95Index], 2)
        maximumMilliseconds = [Math]::Round($sorted[-1], 2)
    }
}

$checksumBytes = [Security.Cryptography.SHA256]::HashData([byte[]]@(0))
$checksum = [Convert]::ToBase64String($checksumBytes)
[Array]::Clear($checksumBytes, 0, $checksumBytes.Length)
$loanCreated = $false
try {
    $createLoan = New-ApiRequest -Method ([Net.Http.HttpMethod]::Post) -Path '/v1/loans' -Body @{ loanId = $SyntheticLoanId } -Mutation
    Invoke-SetupRequest -Request $createLoan -ExpectedStatus @(201) | Out-Null
    $loanCreated = $true

    $baselineRequest = New-ApiRequest -Method ([Net.Http.HttpMethod]::Post) -Path "/v1/loans/$encodedLoanId/documents" -Body ([ordered]@{
        fileName = 'synthetic-load-baseline.pdf'
        contentType = 'application/pdf'
        sizeBytes = 1
        checksumSha256 = $checksum
    }) -Mutation
    $baseline = Invoke-SetupRequest -Request $baselineRequest -ExpectedStatus @(201) -ParseJson
    $documentId = [string]$baseline.documentId
    if ($documentId -notmatch '^doc_[0-9a-f-]{36}$') { throw 'Synthetic baseline response did not contain a valid platform document ID.' }
    $encodedDocumentId = [uri]::EscapeDataString($documentId)
    $baseline = $null

    $uploadFactory = {
        param($index)
        New-ApiRequest -Method ([Net.Http.HttpMethod]::Post) -Path "/v1/loans/$encodedLoanId/documents" -Body ([ordered]@{
            fileName = "synthetic-load-$index.pdf"
            contentType = 'application/pdf'
            sizeBytes = 1
            checksumSha256 = $checksum
        }) -Mutation
    }
    $statusFactory = {
        param($index)
        New-ApiRequest -Method ([Net.Http.HttpMethod]::Get) -Path "/v1/loans/$encodedLoanId/documents/$encodedDocumentId"
    }

    if ($WarmupCount -gt 0) {
        Invoke-LoadPhase -Name 'upload-initialization-warmup' -Count $WarmupCount -MaximumConcurrency ([Math]::Min($Concurrency, $WarmupCount)) -RequestFactory $uploadFactory -ExpectedStatus 201 | Out-Null
        Invoke-LoadPhase -Name 'status-read-warmup' -Count $WarmupCount -MaximumConcurrency ([Math]::Min($Concurrency, $WarmupCount)) -RequestFactory $statusFactory -ExpectedStatus 200 | Out-Null
    }

    $uploadLatencies = Invoke-LoadPhase -Name 'upload-initialization' -Count $RequestCount -MaximumConcurrency $Concurrency -RequestFactory $uploadFactory -ExpectedStatus 201
    $statusLatencies = Invoke-LoadPhase -Name 'status-read' -Count $RequestCount -MaximumConcurrency $Concurrency -RequestFactory $statusFactory -ExpectedStatus 200
    $uploadSummary = Get-LatencySummary -Values $uploadLatencies
    $statusSummary = Get-LatencySummary -Values $statusLatencies
    $passed = $uploadSummary.p95Milliseconds -le $MaximumP95Milliseconds -and $statusSummary.p95Milliseconds -le $MaximumP95Milliseconds
    $evidence = [ordered]@{
        schemaVersion = 1
        timestampUtc = [DateTime]::UtcNow.ToString('o')
        environment = [string]$config.environment
        targetHost = $baseUri.Host
        concurrency = $Concurrency
        p95TargetMilliseconds = $MaximumP95Milliseconds
        uploadInitialization = $uploadSummary
        statusRead = $statusSummary
        passed = $passed
    }
    $evidence | ConvertTo-Json -Depth 10
    if (-not $passed) { throw "Synthetic API load check exceeded the $MaximumP95Milliseconds ms p95 target." }
} finally {
    if ($loanCreated -and -not $KeepSyntheticLoan) {
        try {
            $archive = New-ApiRequest -Method ([Net.Http.HttpMethod]::Post) -Path "/v1/loans/$encodedLoanId/archive" -Mutation
            Invoke-SetupRequest -Request $archive -ExpectedStatus @(201) | Out-Null
        } catch {
            Write-Warning 'Synthetic loan cleanup failed; response content and identifiers were suppressed.'
        }
    }
    $client.DefaultRequestHeaders.Authorization = $null
    $client.Dispose()
    $handler.Dispose()
    $token = $null
    $checksum = $null
}
