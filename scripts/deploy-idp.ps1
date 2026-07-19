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
if ([string]$lock.cliPythonVersion -cne '3.12') {
    throw 'The reviewed IDP 0.5.16 CLI dependency set requires Python 3.12.'
}
$reviewedBuildTools = @{
    cfnLint = '1.53.0'
    ruff = '0.15.21'
    uv = '0.9.6'
}
foreach ($name in $reviewedBuildTools.Keys) {
    if ([string]$lock.cliBuildTools.$name -cne $reviewedBuildTools[$name]) {
        throw "The pinned IDP publisher requires reviewed $name version $($reviewedBuildTools[$name])."
    }
}
$pythonRuntimeTag = ([string]$lock.cliPythonVersion).Replace('.', '')
$venvDirectory = Join-Path $root ".local/tools/idp-cli-$($lock.version)-py$pythonRuntimeTag"
$venvExecutableDirectory = if ($IsWindows) {
    Join-Path $venvDirectory 'Scripts'
} else {
    Join-Path $venvDirectory 'bin'
}

foreach ($command in 'aws', 'git', 'sam', 'docker', 'node', 'npm') {
    Assert-Command -Name $command -InstallHint "Install '$command' before building the pinned IDP source."
}
$idpPython = Resolve-PythonLaunch -Version ([string]$lock.cliPythonVersion)
$windowsCliBridge = $null
if ($IsWindows) {
    $samCommandSource = Resolve-CommandSourceOutsidePath -Name sam -ExcludedDirectory $venvExecutableDirectory
    $nodeCommandSource = Resolve-CommandSourceOutsidePath -Name node -ExcludedDirectory $venvExecutableDirectory
    $npmCommandSource = Resolve-CommandSourceOutsidePath -Name npm -ExcludedDirectory $venvExecutableDirectory
    $windowsCliBridge = Resolve-WindowsIdpCliBridge `
        -SamCommandSource $samCommandSource `
        -NodeCommandSource $nodeCommandSource `
        -NpmCommandSource $npmCommandSource
}
Assert-AwsIdentity -Profile $config.awsProfile -Region $config.awsRegion -ExpectedAccountId $config.awsAccountId | Out-Null

$bootstrap = Get-StackOutputs -Profile $config.awsProfile -Region $config.awsRegion -StackName $config.bootstrapStackName
foreach ($requiredOutput in @(
    'ArtifactBucketName',
    'IdpCloudFormationExecutionRoleArn',
    'IdpRolePermissionsBoundaryArn'
)) {
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
    $actual = Get-NormalizedTextSha256 -Path $entry.Path
    if ($actual -ne $entry.Expected) {
        throw "IDP configuration '$($entry.Name)' hash is $actual, expected $($entry.Expected)."
    }
}

$vendorDirectory = Join-Path $root ".local/vendor/idp-$($lock.version)"
$stackPolicyPath = Join-Path $root 'infra/stack-policies/protect-stateful-resources.json'
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

$pythonExecutable = if ($IsWindows) {
    Join-Path $venvDirectory 'Scripts/python.exe'
} else {
    Join-Path $venvDirectory 'bin/python'
}
$bridgePackageDirectory = Join-Path $root 'scripts/idp_windows_cli_bridge'
$bridgeIdentity = 'native'
$bridgeExecutables = @()
if ($IsWindows) {
    $bridgeSources = @(
        (Join-Path $bridgePackageDirectory 'pyproject.toml'),
        (Join-Path $bridgePackageDirectory 'idp_windows_cli_bridge.py')
    )
    $bridgeIdentity = ($bridgeSources | ForEach-Object { Get-NormalizedTextSha256 -Path $_ }) -join ':'
    $bridgeExecutables = @(
        (Join-Path $venvDirectory 'Scripts/sam.exe'),
        (Join-Path $venvDirectory 'Scripts/npm.exe')
    )
}
$buildToolIdentity = "cfn-lint=$($lock.cliBuildTools.cfnLint):ruff=$($lock.cliBuildTools.ruff):uv=$($lock.cliBuildTools.uv)"
$executableSuffix = if ($IsWindows) { '.exe' } else { '' }
$buildToolExecutables = @(
    (Join-Path $venvExecutableDirectory "cfn-lint$executableSuffix"),
    (Join-Path $venvExecutableDirectory "ruff$executableSuffix"),
    (Join-Path $venvExecutableDirectory "uv$executableSuffix")
)
$installMarker = Join-Path $venvDirectory ".installed-$($lock.commit)-py$pythonRuntimeTag"
$expectedInstallIdentity = "$($lock.commit)|python=$($lock.cliPythonVersion)|bridge=$bridgeIdentity|tools=$buildToolIdentity"
$installedIdentity = if (Test-Path -LiteralPath $installMarker -PathType Leaf) {
    (Get-Content -Raw -LiteralPath $installMarker).Trim()
} else {
    ''
}
$installRequired = $ReinstallCli -or
    -not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf) -or
    $installedIdentity -cne $expectedInstallIdentity -or
    @(($bridgeExecutables + $buildToolExecutables) | Where-Object {
        -not (Test-Path -LiteralPath $_ -PathType Leaf)
    }).Count -gt 0
if ($installRequired) {
    if (Test-Path -LiteralPath $installMarker -PathType Leaf) {
        [IO.File]::Delete($installMarker)
    }
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        [IO.Directory]::CreateDirectory((Split-Path $venvDirectory)) | Out-Null
        Invoke-Checked `
            -Command $idpPython.FilePath `
            -Arguments (@($idpPython.PrefixArguments) + @('-m', 'venv', $venvDirectory)) `
            -FailureMessage 'Failed to create the pinned Python 3.12 IDP CLI virtual environment.'
    }
}
$venvProbe = 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
$venvVersion = (& $pythonExecutable -c $venvProbe 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $venvVersion -cne [string]$lock.cliPythonVersion) {
    throw "Pinned IDP CLI environment must use Python $($lock.cliPythonVersion); remove only the local cache '$venvDirectory' and rerun."
}
if ($installRequired) {
    Invoke-Checked -Command $pythonExecutable -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip') -FailureMessage 'Failed to update pip in the IDP virtual environment.'
    Invoke-Checked -Command $pythonExecutable -Arguments @(
        '-m', 'pip', 'install',
        '-e', "$(Join-Path $vendorDirectory 'lib/idp_common_pkg')[all]",
        '-e', (Join-Path $vendorDirectory 'lib/idp_sdk'),
        '-e', (Join-Path $vendorDirectory 'lib/idp_cli_pkg'),
        "cfn-lint==$($lock.cliBuildTools.cfnLint)",
        "ruff==$($lock.cliBuildTools.ruff)",
        "uv==$($lock.cliBuildTools.uv)"
    ) -FailureMessage 'Failed to install the pinned IDP CLI and build dependencies.'
    Invoke-Checked -Command $pythonExecutable -Arguments @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--force-reinstall', '--no-deps',
        "cfn-lint==$($lock.cliBuildTools.cfnLint)",
        "ruff==$($lock.cliBuildTools.ruff)",
        "uv==$($lock.cliBuildTools.uv)"
    ) -FailureMessage 'Failed to repair the pinned IDP publisher child-tool executables.'
    if ($IsWindows) {
        Invoke-Checked -Command $pythonExecutable -Arguments @(
            '-m', 'pip', 'install', '--disable-pip-version-check',
            '--force-reinstall', '--no-deps',
            '--editable', $bridgePackageDirectory
        ) -FailureMessage 'Failed to install the reviewed Windows IDP child-tool bridge.'
    }
}
Invoke-Checked -Command $pythonExecutable -Arguments @('-m', 'pip', 'check') -FailureMessage 'Pinned IDP CLI dependencies are inconsistent.'
$dependencySmoke = 'import importlib.metadata as m; import idp_common, idp_sdk, idp_cli; assert m.version("numpy") == "1.26.4"'
Invoke-Checked -Command $pythonExecutable -Arguments @('-c', $dependencySmoke) -FailureMessage 'Pinned IDP CLI dependency smoke test failed.'
$buildToolSmoke = "import importlib.metadata as m; assert m.version('cfn-lint') == '$($lock.cliBuildTools.cfnLint)'; assert m.version('ruff') == '$($lock.cliBuildTools.ruff)'; assert m.version('uv') == '$($lock.cliBuildTools.uv)'"
Invoke-Checked -Command $pythonExecutable -Arguments @('-c', $buildToolSmoke) -FailureMessage 'Pinned IDP publisher build-tool versions are inconsistent.'
foreach ($requiredExecutable in ($bridgeExecutables + $buildToolExecutables)) {
    if (-not (Test-Path -LiteralPath $requiredExecutable -PathType Leaf)) {
        throw "Reviewed IDP child-tool executable was not installed: $requiredExecutable"
    }
}

$cliEnvironment = @{ PYTHONUTF8 = '1' }
if ($null -ne $windowsCliBridge) {
    foreach ($entry in @(
        @{ Name = 'IDP_SAM_NATIVE_EXECUTABLE'; Value = $windowsCliBridge.SamNativeExecutablePath },
        @{ Name = 'IDP_SAM_CLI_PYTHON'; Value = $windowsCliBridge.SamPythonPath },
        @{ Name = 'IDP_NPM_NATIVE_EXECUTABLE'; Value = $windowsCliBridge.NpmNativeExecutablePath },
        @{ Name = 'IDP_NODE_EXECUTABLE'; Value = $windowsCliBridge.NodeExecutablePath },
        @{ Name = 'IDP_NPM_CLI_JS'; Value = $windowsCliBridge.NpmCliPath }
    )) {
        $cliEnvironment[$entry.Name] = [string]$entry.Value
    }
}
Invoke-WithPrependedPath -Path $venvExecutableDirectory -Environment $cliEnvironment -ScriptBlock {
    Invoke-Checked -Command sam -Arguments @('--version') -FailureMessage 'The native SAM CLI child-tool path is not executable.'
    Invoke-Checked -Command npm -Arguments @('--version') -FailureMessage 'The native npm child-tool path is not executable.'
    Invoke-Checked -Command ruff -Arguments @('--version') -FailureMessage 'The pinned Ruff publisher prerequisite is not executable.'
    Invoke-Checked -Command cfn-lint -Arguments @('--version') -FailureMessage 'The pinned cfn-lint publisher prerequisite is not executable.'
    Invoke-Checked -Command uv -Arguments @('--version') -FailureMessage 'The pinned uv publisher prerequisite is not executable.'
}
if ($installRequired) {
    [IO.File]::WriteAllText($installMarker, $expectedInstallIdentity + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

Invoke-WithPrependedPath -Path $venvExecutableDirectory -Environment $cliEnvironment -ScriptBlock {
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
    '--parameters', "PostProcessingLambdaHookFunctionArn=$($platform.IdpPostprocessorFunctionArn),PermissionsBoundaryArn=$($bootstrap.IdpRolePermissionsBoundaryArn)",
    '--role-arn', $bootstrap.IdpCloudFormationExecutionRoleArn,
    '--bucket-basename', $artifactBucketBaseName,
    '--prefix', "idp/$($lock.version)",
    '--wait'
)
if ($CleanBuild) { $deployArguments += '--clean-build' }
$idpBeforeDeployment = Get-AwsCloudFormationStackDescription `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.idpStackName `
    -AllowMissing
if ($null -ne $idpBeforeDeployment) {
    Set-AwsStatefulStackPolicy `
        -Profile $config.awsProfile `
        -Region $config.awsRegion `
        -StackName $config.idpStackName `
        -PolicyPath $stackPolicyPath
}
Invoke-Checked -Command $pythonExecutable -Arguments $deployArguments -FailureMessage 'Pinned headless IDP deployment failed.'

Set-AwsStatefulStackPolicy `
    -Profile $config.awsProfile `
    -Region $config.awsRegion `
    -StackName $config.idpStackName `
    -PolicyPath $stackPolicyPath

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
}

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
