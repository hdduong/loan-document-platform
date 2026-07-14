[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$EnvironmentFile,
    [switch]$ReinstallCli,
    [switch]$CleanBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$FailureMessage
    )
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

$config = Read-EnvironmentConfig -Path $EnvironmentFile
$root = Get-ProjectRoot
$lockPath = Join-Path $root 'vendor/idp.lock.json'
$lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json -Depth 10
$manifest = Get-Content -Raw -LiteralPath (Join-Path $root 'config/idp/manifest.json') | ConvertFrom-Json -Depth 10

if ($lock.version -ne $config.idpVersion -or $lock.commit -ne $config.idpCommit) {
    throw 'Environment IDP version/commit does not match vendor/idp.lock.json.'
}
if ($lock.deploymentMode -ne 'headless') { throw 'The committed IDP lock must specify headless deployment mode.' }

foreach ($command in 'aws', 'git', 'python', 'sam', 'docker', 'node', 'npm') {
    Assert-Command -Name $command -InstallHint "Install '$command' before building the pinned IDP source."
}
Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null

$bootstrap = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.bootstrapStackName
foreach ($requiredOutput in 'ArtifactBucketName', 'CloudFormationExecutionRoleArn') {
    if (-not $bootstrap.ContainsKey($requiredOutput)) {
        throw "Bootstrap stack '$($config.bootstrapStackName)' is missing '$requiredOutput'."
    }
}
$artifactSuffix = "-$($config.awsRegion)"
if (-not $bootstrap.ArtifactBucketName.EndsWith($artifactSuffix, [StringComparison]::Ordinal)) {
    throw "Artifact bucket '$($bootstrap.ArtifactBucketName)' must end in '$artifactSuffix' for pinned idp-cli publishing. Re-deploy the bootstrap stack with the repository convention."
}
$artifactBucketBaseName = $bootstrap.ArtifactBucketName.Substring(0, $bootstrap.ArtifactBucketName.Length - $artifactSuffix.Length)

$platform = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.platformStackName
if (-not $platform.IdpPostprocessorFunctionArn) {
    throw "Deploy regional platform stack '$($config.platformStackName)' before the IDP stack."
}

$screenPath = Join-Path $root "config/idp/$($manifest.screen.file)"
$fullPath = Join-Path $root "config/idp/$($manifest.full.file)"
foreach ($entry in @(
    @{ Path = $screenPath; Expected = [string]$manifest.screen.sourceSha256; Name = $manifest.screen.name },
    @{ Path = $fullPath; Expected = [string]$manifest.full.sourceSha256; Name = $manifest.full.name }
)) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $entry.Path).Hash.ToLowerInvariant()
    if ($actual -ne $entry.Expected) {
        throw "IDP configuration '$($entry.Name)' hash is $actual, expected $($entry.Expected)."
    }
}

$vendorDirectory = Join-Path $root ".local/vendor/idp-$($lock.version)"
if (-not (Test-Path -LiteralPath (Join-Path $vendorDirectory '.git'))) {
    [IO.Directory]::CreateDirectory((Split-Path $vendorDirectory)) | Out-Null
    Invoke-Checked -Command git -Arguments @(
        'clone', '--branch', $lock.tag, '--depth', '1', '--single-branch', $lock.repository, $vendorDirectory
    ) -FailureMessage 'Failed to clone the pinned AWS IDP source.'
}
$origin = (& git -C $vendorDirectory remote get-url origin | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $origin -ne $lock.repository) {
    throw "IDP source remote '$origin' does not match lock '$($lock.repository)'."
}
$sourceCommit = (& git -C $vendorDirectory rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0) { throw 'Could not read the IDP source commit.' }
if ($sourceCommit -ne $lock.commit) {
    Invoke-Checked -Command git -Arguments @(
        '-C', $vendorDirectory, 'fetch', '--depth', '1', 'origin', "refs/tags/$($lock.tag):refs/tags/$($lock.tag)"
    ) -FailureMessage 'Could not fetch the locked IDP tag.'
    Invoke-Checked -Command git -Arguments @(
        '-C', $vendorDirectory, 'checkout', '--detach', $lock.commit
    ) -FailureMessage 'Could not check out the locked IDP commit.'
    $sourceCommit = (& git -C $vendorDirectory rev-parse HEAD | Out-String).Trim().ToLowerInvariant()
}
if ($sourceCommit -ne $lock.commit) { throw "Checked-out IDP commit '$sourceCommit' does not match lock '$($lock.commit)'." }
& git -C $vendorDirectory diff --quiet --exit-code
if ($LASTEXITCODE -ne 0) { throw 'Pinned IDP source has tracked local modifications; refusing a production build.' }

$venvDirectory = Join-Path $root ".local/tools/idp-cli-$($lock.version)"
$pythonExecutable = if ($IsWindows) {
    Join-Path $venvDirectory 'Scripts/python.exe'
} else {
    Join-Path $venvDirectory 'bin/python'
}
$installMarker = Join-Path $venvDirectory ".installed-$($lock.commit)"
if ($ReinstallCli -or -not (Test-Path -LiteralPath $installMarker)) {
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        [IO.Directory]::CreateDirectory((Split-Path $venvDirectory)) | Out-Null
        Invoke-Checked -Command python -Arguments @('-m', 'venv', $venvDirectory) -FailureMessage 'Failed to create the pinned IDP CLI virtual environment.'
    }
    Invoke-Checked -Command $pythonExecutable -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') -FailureMessage 'Failed to update pip in the IDP virtual environment.'
    Invoke-Checked -Command $pythonExecutable -Arguments @(
        '-m', 'pip', 'install',
        '-e', "$(Join-Path $vendorDirectory 'lib/idp_common_pkg')[all]",
        '-e', (Join-Path $vendorDirectory 'lib/idp_sdk'),
        '-e', (Join-Path $vendorDirectory 'lib/idp_cli_pkg'),
        'cfn-lint'
    ) -FailureMessage 'Failed to install the pinned IDP CLI and build dependencies.'
    [IO.File]::WriteAllText($installMarker, $lock.commit + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

Invoke-Checked -Command docker -Arguments @('info') -FailureMessage 'Docker is required and must be running for a pinned source IDP build.'
$cliPrefix = @('-m', 'idp_cli.cli')
if ($env:GITHUB_ACTIONS -ne 'true') { $cliPrefix += @('--profile', $config.awsProfile) }
$deployArguments = $cliPrefix + @(
    'deploy',
    '--stack-name', $config.idpStackName,
    '--region', $config.awsRegion,
    '--from-code', $vendorDirectory,
    '--headless',
    '--admin-email', $config.alertEmail,
    '--custom-config', $screenPath,
    '--max-concurrent', '10',
    '--log-level', 'INFO',
    '--parameters', "PostProcessingLambdaHookFunctionArn=$($platform.IdpPostprocessorFunctionArn)",
    '--role-arn', $bootstrap.CloudFormationExecutionRoleArn,
    '--bucket-basename', $artifactBucketBaseName,
    '--prefix', "idp/$($lock.version)",
    '--wait'
)
if ($CleanBuild) { $deployArguments += '--clean-build' }
Invoke-Checked -Command $pythonExecutable -Arguments $deployArguments -FailureMessage 'Pinned headless IDP deployment failed.'

foreach ($entry in @(
    @{ Path = $screenPath; Version = [string]$manifest.screen.name; Description = [string]$manifest.screen.purpose },
    @{ Path = $fullPath; Version = [string]$manifest.full.name; Description = [string]$manifest.full.purpose }
)) {
    $uploadArguments = $cliPrefix + @(
        'config-upload',
        '--stack-name', $config.idpStackName,
        '--region', $config.awsRegion,
        '--config-file', $entry.Path,
        '--config-version', $entry.Version,
        '--version-description', $entry.Description
    )
    Invoke-Checked -Command $pythonExecutable -Arguments $uploadArguments -FailureMessage "Failed to upload IDP configuration '$($entry.Version)'."
}
$activateArguments = $cliPrefix + @(
    'config-activate',
    '--stack-name', $config.idpStackName,
    '--region', $config.awsRegion,
    '--config-version', [string]$manifest.screen.name
)
Invoke-Checked -Command $pythonExecutable -Arguments $activateArguments -FailureMessage 'Failed to activate the inexpensive screening configuration as the safe IDP default.'

$outputs = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.idpStackName
$working = Invoke-Aws -Profile $config.awsProfile -Region $config.awsRegion -Arguments @(
    'cloudformation', 'describe-stack-resource',
    '--stack-name', $config.idpStackName,
    '--logical-resource-id', 'WorkingBucket'
) -CaptureJson
$localDirectory = Join-Path $root '.local'
[IO.Directory]::CreateDirectory($localDirectory) | Out-Null
$outputPath = Join-Path $localDirectory "idp-$($config.environment).json"
$safeOutput = [ordered]@{
    stackName = $config.idpStackName
    region = $config.awsRegion
    version = $lock.version
    commit = $lock.commit
    deploymentMode = 'headless'
    inputBucketName = $outputs.S3InputBucketName
    workingBucketName = $working.StackResourceDetail.PhysicalResourceId
    outputBucketName = $outputs.S3OutputBucketName
    encryptionKeyArn = $outputs.CustomerManagedEncryptionKeyArn
    stateMachineArn = $outputs.StateMachineArn
    screenConfigVersion = $manifest.screen.name
    fullConfigVersion = $manifest.full.name
}
[IO.File]::WriteAllText($outputPath, ($safeOutput | ConvertTo-Json -Depth 10) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Host "Pinned headless IDP ready. Non-secret outputs: $outputPath"
Write-Host 'Run deploy-platform.ps1 again so processor IAM/environment values point at the deployed IDP buckets and KMS key.'
