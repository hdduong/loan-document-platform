Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProjectRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Read-EnvironmentConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $config = Get-Content -Raw -LiteralPath $resolved | ConvertFrom-Json -Depth 20
    $required = @(
        'environment', 'awsRegion', 'awsProfile', 'repositoryName', 'githubOwner',
        'githubRepositoryVisibility', 'githubDeploymentReviewer',
        'githubDefaultBranch', 'githubEnvironment', 'domainName',
        'route53HostedZoneId', 'uiHostName', 'apiHostName', 'apiOriginHostName',
        'entraTenantId', 'alertEmail', 'budgetEmail', 'platformStackName',
        'edgeStackName', 'bootstrapStackName', 'idpStackName'
    )
    foreach ($name in $required) {
        $property = $config.PSObject.Properties[$name]
        if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Value) -or [string]$property.Value -match '^REPLACE_') {
            throw "Environment file '$resolved' requires a real value for '$name'."
        }
    }
    if ([string]$config.githubRepositoryVisibility -notin @('public', 'private')) {
        throw "Environment file '$resolved' requires githubRepositoryVisibility to be 'public' or 'private'."
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

function Invoke-Aws {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$Profile,
        [Parameter(Mandatory)][string]$Region,
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$CaptureJson
    )
    $allArguments = @('--region', $Region, '--no-cli-pager') + $Arguments
    if ($env:GITHUB_ACTIONS -ne 'true') {
        $allArguments = @('--profile', $Profile) + $allArguments
    }
    if ($CaptureJson) { $allArguments += @('--output', 'json') }
    $output = & aws @allArguments
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI failed: aws $($Arguments -join ' ')"
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
        throw "AWS profile '$Profile' is account $($identity.Account), expected $ExpectedAccountId."
    }
    $credentialSource = if ($env:GITHUB_ACTIONS -eq 'true') { 'GitHub OIDC' } else { "profile $Profile" }
    Write-Host "AWS identity: $($identity.Arn) (account $($identity.Account), region $Region, source $credentialSource)"
    return $identity
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

Export-ModuleMember -Function Get-ProjectRoot, Read-EnvironmentConfig, Assert-Command, Invoke-Aws, Assert-AwsIdentity, Get-StackOutputs
