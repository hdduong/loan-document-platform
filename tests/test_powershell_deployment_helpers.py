from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON_MODULE = ROOT / "scripts" / "common.psm1"


def run_powershell(script: str, *, environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


def normalized_text_sha256(path: Path) -> str:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$path = [Environment]::GetEnvironmentVariable('TEST_HASH_PATH'); "
        "$digest = Get-NormalizedTextSha256 -Path $path; "
        "[Console]::Out.Write($digest)"
    )
    environment = os.environ.copy()
    environment["TEST_HASH_PATH"] = str(path)
    return run_powershell(script, environment=environment)


@pytest.mark.parametrize("line_ending", ["\n", "\r\n", "\r"])
def test_normalized_text_sha256_is_stable_across_line_endings(
    tmp_path: Path, line_ending: str
) -> None:
    path = tmp_path / "reviewed.json"
    path.write_bytes(f'{{"label":"déjà vu"}}{line_ending}'.encode("utf-8"))

    expected = hashlib.sha256('{"label":"déjà vu"}\n'.encode("utf-8")).hexdigest()

    assert normalized_text_sha256(path) == expected


def test_normalized_text_sha256_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(b"{\xff}")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$path = [Environment]::GetEnvironmentVariable('TEST_HASH_PATH'); "
        "try { Get-NormalizedTextSha256 -Path $path | Out-Null; "
        "[Console]::Out.Write('accepted') } catch { "
        "[Console]::Out.Write($_.Exception.Message) }"
    )
    environment = os.environ.copy()
    environment["TEST_HASH_PATH"] = str(path)

    message = run_powershell(script, environment=environment)

    assert "must contain valid UTF-8" in message
    assert "accepted" not in message


def test_normalized_text_sha256_preserves_a_utf8_bom_as_significant(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviewed-with-bom.json"
    path.write_bytes(b'\xef\xbb\xbf{"label":"expected"}\r\n')

    with_bom = hashlib.sha256('\ufeff{"label":"expected"}\n'.encode("utf-8")).hexdigest()
    without_bom = hashlib.sha256('{"label":"expected"}\n'.encode("utf-8")).hexdigest()
    actual = normalized_text_sha256(path)

    assert actual == with_bom
    assert actual != without_bom


def test_idp_scripts_share_the_normalized_digest_helper() -> None:
    common = COMMON_MODULE.read_text(encoding="utf-8")
    deploy = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    generator = (ROOT / "scripts" / "new-screen-config.ps1").read_text(encoding="utf-8")
    module_import = "Import-Module (Join-Path $PSScriptRoot 'common.psm1') -Force"

    assert "[System.Security.Cryptography.SHA256]::Create()" in common
    assert "[System.Security.Cryptography.SHA256]::HashData" not in common
    assert "[System.Convert]::ToHexString" not in common
    assert module_import in deploy
    assert "$actual = Get-NormalizedTextSha256 -Path $entry.Path" in deploy
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $entry.Path" not in deploy
    assert module_import in generator
    assert "function Get-NormalizedTextSha256" not in generator


def test_python_launch_resolver_requires_the_exact_minor() -> None:
    module_path = str(COMMON_MODULE).replace("'", "''")
    expected = f"{sys.version_info.major}.{sys.version_info.minor}"
    script = (
        f"Import-Module -Force '{module_path}'; "
        f"$launch = Resolve-PythonLaunch -Version '{expected}'; "
        "$launch | ConvertTo-Json -Depth 5 -Compress"
    )

    launch = json.loads(run_powershell(script, environment=os.environ.copy()))

    assert launch["Version"] == expected
    assert launch["FilePath"]
    assert isinstance(launch["PrefixArguments"], list)


def test_python_launch_resolver_fails_closed_for_a_missing_minor() -> None:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$missing = Resolve-PythonLaunch -Version '99.99' -AllowMissing; "
        "$message = ''; "
        "try { Resolve-PythonLaunch -Version '99.99' | Out-Null } "
        "catch { $message = $_.Exception.Message }; "
        "$result = [pscustomobject]@{ Missing = ($null -eq $missing); Message = $message }; "
        "$result | ConvertTo-Json -Compress"
    )

    result = json.loads(run_powershell(script, environment=os.environ.copy()))

    assert result["Missing"] is True
    assert "Python 99.99 is required" in result["Message"]


@pytest.mark.parametrize("raise_inside", [False, True])
def test_prepend_path_helper_restores_process_path(tmp_path: Path, raise_inside: bool) -> None:
    module_path = str(COMMON_MODULE).replace("'", "''")
    prepend_path = str(tmp_path).replace("'", "''")
    should_raise = "$true" if raise_inside else "$false"
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$before = $env:PATH; "
        "$global:IDP_TEST_INSIDE = ''; $global:IDP_TEST_ENV_INSIDE = @{}; $caught = $false; "
        "try { Invoke-WithPrependedPath "
        f"-Path '{prepend_path}' -Environment @{{ IDP_TEST_EXISTING = 'inside-existing'; "
        "IDP_TEST_ABSENT = 'inside-absent'; IDP_TEST_CLEARED = '' } -ScriptBlock { "
        "$global:IDP_TEST_INSIDE = ($env:PATH -split [IO.Path]::PathSeparator)[0]; "
        "$global:IDP_TEST_ENV_INSIDE = @{ Existing = $env:IDP_TEST_EXISTING; "
        "Absent = $env:IDP_TEST_ABSENT; Cleared = $env:IDP_TEST_CLEARED }; "
        f"if ({should_raise}) {{ throw 'expected' }} }} }} "
        "catch { if ($_.Exception.Message -eq 'expected') { $caught = $true } else { throw } }; "
        "$result = [pscustomobject]@{ Before = $before; After = $env:PATH; "
        "Inside = $global:IDP_TEST_INSIDE; Caught = $caught; "
        "InsideExisting = $global:IDP_TEST_ENV_INSIDE.Existing; "
        "InsideAbsent = $global:IDP_TEST_ENV_INSIDE.Absent; "
        "InsideCleared = $global:IDP_TEST_ENV_INSIDE.Cleared; "
        "AfterExisting = $env:IDP_TEST_EXISTING; "
        "AfterCleared = $env:IDP_TEST_CLEARED; "
        "AfterAbsent = [Environment]::GetEnvironmentVariable('IDP_TEST_ABSENT', 'Process') }; "
        "$result | ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment["IDP_TEST_EXISTING"] = "before-existing"
    environment["IDP_TEST_CLEARED"] = "stale-value"
    environment.pop("IDP_TEST_ABSENT", None)

    result = json.loads(run_powershell(script, environment=environment))

    assert result["Before"] == result["After"]
    assert Path(result["Inside"]) == tmp_path
    assert result["Caught"] is raise_inside
    assert result["InsideExisting"] == "inside-existing"
    assert result["InsideAbsent"] == "inside-absent"
    assert result["InsideCleared"] == ""
    assert result["AfterExisting"] == "before-existing"
    assert result["AfterCleared"] == "stale-value"
    assert result["AfterAbsent"] is None


def test_command_source_resolver_excludes_an_activated_idp_venv(tmp_path: Path) -> None:
    managed = tmp_path / "managed" / "Scripts"
    external = tmp_path / "external"
    managed.mkdir(parents=True)
    external.mkdir()
    suffix = ".exe" if os.name == "nt" else ""
    managed_command = managed / f"idp-tool-source-test{suffix}"
    external_command = external / f"idp-tool-source-test{suffix}"
    executable_content = b"" if os.name == "nt" else b"#!/bin/sh\nexit 0\n"
    managed_command.write_bytes(executable_content)
    external_command.write_bytes(executable_content)
    managed_command.chmod(0o755)
    external_command.chmod(0o755)
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$source = Resolve-CommandSourceOutsidePath "
        "-Name 'idp-tool-source-test' -ExcludedDirectory $env:TEST_EXCLUDED; "
        "[Console]::Out.Write($source)"
    )
    environment = os.environ.copy()
    environment["TEST_EXCLUDED"] = str(managed)
    environment["PATH"] = os.pathsep.join([str(managed), str(external), environment["PATH"]])

    result = run_powershell(script, environment=environment)

    assert Path(result).resolve() == external_command.resolve()


def test_prepend_path_helper_rejects_path_override_and_restores_path(tmp_path: Path) -> None:
    module_path = str(COMMON_MODULE).replace("'", "''")
    prepend_path = str(tmp_path).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$before = $env:PATH; $message = ''; "
        "try { Invoke-WithPrependedPath "
        f"-Path '{prepend_path}' -Environment @{{ PATH = 'unsafe' }} "
        "-ScriptBlock { throw 'must not run' } } catch { $message = $_.Exception.Message }; "
        "[pscustomobject]@{ Before = $before; After = $env:PATH; Message = $message } "
        "| ConvertTo-Json -Compress"
    )

    result = json.loads(run_powershell(script, environment=os.environ.copy()))

    assert result["Before"] == result["After"]
    assert "Invalid scoped process environment variable 'PATH'" in result["Message"]


def test_windows_idp_cli_bridge_resolves_reviewed_wrapper_layout(tmp_path: Path) -> None:
    sam_bin = tmp_path / "AWS SAM CLI" / "bin"
    sam_runtime = tmp_path / "AWS SAM CLI" / "runtime"
    sam_module = sam_runtime / "Lib" / "site-packages" / "samcli"
    node_root = tmp_path / "Node JS"
    npm_cli = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    sam_module.mkdir(parents=True)
    npm_cli.parent.mkdir(parents=True)
    sam_bin.mkdir(parents=True)
    (sam_bin / "sam.cmd").write_text(
        '"%~dp0/../runtime/python.exe" -m samcli %*\n', encoding="utf-8"
    )
    (sam_runtime / "python.exe").write_bytes(b"")
    (node_root / "node.exe").write_bytes(b"")
    (node_root / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    npm_cli.write_text("// npm\n", encoding="utf-8")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$result = Resolve-WindowsIdpCliBridge "
        "-SamCommandSource $env:TEST_SAM_SOURCE "
        "-NodeCommandSource $env:TEST_NODE_SOURCE "
        "-NpmCommandSource $env:TEST_NPM_SOURCE; "
        "$result | ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_SAM_SOURCE": str(sam_bin / "sam.cmd"),
            "TEST_NODE_SOURCE": str(node_root / "node.exe"),
            "TEST_NPM_SOURCE": str(node_root / "npm.cmd"),
        }
    )

    result = json.loads(run_powershell(script, environment=environment))

    assert result["BridgeRequired"] is True
    assert Path(result["SamPythonPath"]).resolve() == (sam_runtime / "python.exe").resolve()
    assert Path(result["NodeExecutablePath"]) == node_root / "node.exe"
    assert Path(result["NpmCliPath"]) == npm_cli
    assert result["SamNativeExecutablePath"] is None
    assert result["NpmNativeExecutablePath"] is None


def test_windows_idp_cli_bridge_accepts_native_executables(tmp_path: Path) -> None:
    paths = {name: tmp_path / name for name in ("sam.exe", "node.exe", "npm.exe")}
    for path in paths.values():
        path.write_bytes(b"")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$result = Resolve-WindowsIdpCliBridge "
        "-SamCommandSource $env:TEST_SAM_SOURCE "
        "-NodeCommandSource $env:TEST_NODE_SOURCE "
        "-NpmCommandSource $env:TEST_NPM_SOURCE; "
        "$result | ConvertTo-Json -Compress"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_SAM_SOURCE": str(paths["sam.exe"]),
            "TEST_NODE_SOURCE": str(paths["node.exe"]),
            "TEST_NPM_SOURCE": str(paths["npm.exe"]),
        }
    )

    result = json.loads(run_powershell(script, environment=environment))

    assert result["BridgeRequired"] is False
    assert Path(result["SamNativeExecutablePath"]) == paths["sam.exe"]
    assert Path(result["NpmNativeExecutablePath"]) == paths["npm.exe"]
    assert result["SamPythonPath"] is None
    assert result["NodeExecutablePath"] is None
    assert result["NpmCliPath"] is None


@pytest.mark.parametrize(
    ("sam_source", "message"),
    [("relative-sam.cmd", "must be an absolute file"), ("bad.exe", "must be an absolute file")],
)
def test_windows_idp_cli_bridge_rejects_untrusted_sources(
    tmp_path: Path, sam_source: str, message: str
) -> None:
    node = tmp_path / "node.exe"
    npm = tmp_path / "npm.exe"
    node.write_bytes(b"")
    npm.write_bytes(b"")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$message = ''; try { Resolve-WindowsIdpCliBridge "
        "-SamCommandSource $env:TEST_SAM_SOURCE "
        "-NodeCommandSource $env:TEST_NODE_SOURCE "
        "-NpmCommandSource $env:TEST_NPM_SOURCE | Out-Null "
        "} catch { $message = $_.Exception.Message }; [Console]::Out.Write($message)"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_SAM_SOURCE": sam_source,
            "TEST_NODE_SOURCE": str(node),
            "TEST_NPM_SOURCE": str(npm),
        }
    )

    result = run_powershell(script, environment=environment)

    assert message in result


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("wrapper", "does not match the reviewed official launcher layout"),
        ("runtime", "has no bundled Python runtime"),
        ("module", "has no bundled samcli module"),
    ],
)
def test_windows_idp_cli_bridge_rejects_invalid_sam_layout(
    tmp_path: Path, failure: str, message: str
) -> None:
    sam_bin = tmp_path / "SAM" / "bin"
    sam_runtime = tmp_path / "SAM" / "runtime"
    sam_bin.mkdir(parents=True)
    wrapper = sam_bin / "sam.cmd"
    wrapper.write_text(
        "@echo off\n" if failure == "wrapper" else '"%~dp0/../runtime/python.exe" -m samcli %*\n',
        encoding="utf-8",
    )
    if failure != "runtime":
        sam_runtime.mkdir()
        (sam_runtime / "python.exe").write_bytes(b"")
    if failure not in {"runtime", "module"}:
        (sam_runtime / "Lib" / "site-packages" / "samcli").mkdir(parents=True)
    node = tmp_path / "node.exe"
    npm = tmp_path / "npm.exe"
    node.write_bytes(b"")
    npm.write_bytes(b"")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$message = ''; try { Resolve-WindowsIdpCliBridge "
        "-SamCommandSource $env:TEST_SAM_SOURCE -NodeCommandSource $env:TEST_NODE_SOURCE "
        "-NpmCommandSource $env:TEST_NPM_SOURCE | Out-Null } "
        "catch { $message = $_.Exception.Message }; [Console]::Out.Write($message)"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_SAM_SOURCE": str(wrapper),
            "TEST_NODE_SOURCE": str(node),
            "TEST_NPM_SOURCE": str(npm),
        }
    )

    assert message in run_powershell(script, environment=environment)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("extension", "not a native executable or supported official wrapper"),
        ("directory", "must share an installation directory"),
        ("module", "has no npm CLI module"),
    ],
)
def test_windows_idp_cli_bridge_rejects_invalid_npm_layout(
    tmp_path: Path, failure: str, message: str
) -> None:
    sam = tmp_path / "sam.exe"
    sam.write_bytes(b"")
    node_root = tmp_path / "node"
    npm_root = tmp_path / "other-node" if failure == "directory" else node_root
    node_root.mkdir()
    npm_root.mkdir(exist_ok=True)
    node = node_root / "node.exe"
    npm = npm_root / ("npm.bat" if failure == "extension" else "npm.cmd")
    node.write_bytes(b"")
    npm.write_text("@echo off\n", encoding="utf-8")
    if failure != "module":
        npm_cli = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
        npm_cli.parent.mkdir(parents=True)
        npm_cli.write_text("// npm\n", encoding="utf-8")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$message = ''; try { Resolve-WindowsIdpCliBridge "
        "-SamCommandSource $env:TEST_SAM_SOURCE -NodeCommandSource $env:TEST_NODE_SOURCE "
        "-NpmCommandSource $env:TEST_NPM_SOURCE | Out-Null } "
        "catch { $message = $_.Exception.Message }; [Console]::Out.Write($message)"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "TEST_SAM_SOURCE": str(sam),
            "TEST_NODE_SOURCE": str(node),
            "TEST_NPM_SOURCE": str(npm),
        }
    )

    assert message in run_powershell(script, environment=environment)


def test_command_source_resolver_fails_when_only_managed_shim_exists(tmp_path: Path) -> None:
    managed = tmp_path / "managed" / "Scripts"
    managed.mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    command = managed / f"idp-only-managed-test{suffix}"
    command.write_bytes(b"" if os.name == "nt" else b"#!/bin/sh\nexit 0\n")
    command.chmod(0o755)
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$message = ''; try { Resolve-CommandSourceOutsidePath "
        "-Name 'idp-only-managed-test' -ExcludedDirectory $env:TEST_EXCLUDED | Out-Null } "
        "catch { $message = $_.Exception.Message }; [Console]::Out.Write($message)"
    )
    environment = os.environ.copy()
    environment["TEST_EXCLUDED"] = str(managed)
    environment["PATH"] = os.pathsep.join([str(managed), environment["PATH"]])

    result = run_powershell(script, environment=environment)

    assert "was not found outside the managed IDP CLI environment" in result


def test_idp_cli_toolchain_keeps_python_runtimes_split() -> None:
    lock = json.loads((ROOT / "vendor" / "idp.lock.json").read_text(encoding="utf-8"))
    deploy = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
    production = (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(
        encoding="utf-8"
    )
    validation = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )

    assert lock["cliPythonVersion"] == "3.12"
    assert lock["cliBuildTools"] == {
        "cfnLint": "1.53.1",
        "ruff": "0.15.22",
        "uv": "0.9.6",
    }
    assert "foreach ($command in 'aws', 'git', 'sam', 'node', 'npm')" in deploy
    assert "foreach ($command in 'aws', 'git', 'sam', 'docker'" not in deploy
    assert "foreach ($command in 'aws', 'git', 'python'," not in deploy
    assert 'importlib.metadata.version("jsonschema") == "4.26.0"' in deploy
    assert "Platform Python 3.13 requires jsonschema 4.26.0" in deploy
    assert '.local/tools/idp-cli-$($lock.version)-py$pythonRuntimeTag' in deploy
    assert "lib/idp_common_pkg')[all]" in deploy
    assert "Invoke-WithPrependedPath" in deploy
    assert ".BridgeRequired" not in deploy
    assert "$bridgeIdentity = ($bridgeSources" in deploy
    assert "$cliEnvironment = @{ PYTHONUTF8 = '1' }" in deploy
    assert "$cliEnvironment[$entry.Name] = [string]$entry.Value" in deploy
    assert "IsNullOrWhiteSpace([string]$entry.Value)" not in deploy
    for fragment in (
        '"cfn-lint==$($lock.cliBuildTools.cfnLint)"',
        '"ruff==$($lock.cliBuildTools.ruff)"',
        '"uv==$($lock.cliBuildTools.uv)"',
        "'--force-reinstall', '--no-deps'",
        "|tools=$buildToolIdentity",
        "$buildToolExecutables = @(",
        "($bridgeExecutables + $buildToolExecutables)",
    ):
        assert fragment in deploy
    for executable in ("ruff", "cfn-lint", "uv"):
        assert deploy.index(
            f"Invoke-Checked -Command {executable} -Arguments @('--version')"
        ) < deploy.index("[IO.File]::WriteAllText($installMarker")
    assert deploy.index("Invoke-Checked -Command sam -Arguments @('--version')") < deploy.index(
        "[IO.File]::WriteAllText($installMarker"
    )
    assert "Python.Python.3.12" in bootstrap
    assert "Resolve-PythonLaunch -Version '3.13'" in bootstrap
    assert "& python --version" not in bootstrap
    assert production.index("python-version: '3.12'") < production.index(
        "python-version: '3.13'"
    )
    assert "python-version: '3.12'" not in validation


def classify_stack_lookup_error(error_text: str) -> bool:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$errorText = [Environment]::GetEnvironmentVariable('TEST_STACK_ERROR'); "
        "$result = Test-AwsCloudFormationStackNotFound -ErrorText $errorText; "
        "[Console]::Out.Write($result.ToString().ToLowerInvariant())"
    )
    environment = os.environ.copy()
    environment["TEST_STACK_ERROR"] = error_text
    return run_powershell(script, environment=environment) == "true"


def stack_policy_is_valid(policy: dict[str, object]) -> bool:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$policy = [Environment]::GetEnvironmentVariable('TEST_STACK_POLICY') | "
        "ConvertFrom-Json -Depth 30; "
        "try { Assert-AwsStatefulStackPolicy -Policy $policy; "
        "[Console]::Out.Write('true') } catch { [Console]::Out.Write('false') }"
    )
    environment = os.environ.copy()
    environment["TEST_STACK_POLICY"] = json.dumps(policy)
    return run_powershell(script, environment=environment) == "true"


def resolve_azure_cli_launch(command_source: Path) -> dict[str, object]:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"$module = Import-Module -Force -PassThru '{module_path}'; "
        "$source = [Environment]::GetEnvironmentVariable('TEST_AZ_SOURCE'); "
        "$launch = & $module { param($value) "
        "Resolve-AzureCliLaunch -CommandSource $value } $source; "
        "$launch | ConvertTo-Json -Depth 5 -Compress"
    )
    environment = os.environ.copy()
    environment["TEST_AZ_SOURCE"] = str(command_source)
    return json.loads(run_powershell(script, environment=environment))


def test_windows_az_cmd_resolution_uses_bundled_python(tmp_path: Path) -> None:
    install_directory = tmp_path / "CLI2"
    wrapper_directory = install_directory / "wbin"
    wrapper_directory.mkdir(parents=True)
    wrapper = wrapper_directory / "az.CMD"
    wrapper.write_text("@echo off\n", encoding="utf-8")
    python_executable = install_directory / "python.exe"
    python_executable.write_bytes(b"")

    launch = resolve_azure_cli_launch(wrapper)

    assert Path(str(launch["FilePath"])) == python_executable
    assert launch["PrefixArguments"] == ["-IBm", "azure.cli"]
    assert launch["Installer"] == "MSI"


def test_msi_launch_preserves_literal_arguments_and_restores_installer(tmp_path: Path) -> None:
    recorder = tmp_path / "record-arguments.ps1"
    recorder.write_text(
        "param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Remaining)\n"
        "$payload = [pscustomobject]@{ "
        "ArgumentsBase64 = @($Remaining | ForEach-Object { "
        "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($_)) }); "
        "Installer = $env:AZ_INSTALLER }\n"
        "[Console]::Out.Write(($payload | ConvertTo-Json -Depth 5 -Compress))\n",
        encoding="utf-8",
    )
    uri = (
        "https://graph.microsoft.com/v1.0/applications?"
        "$filter=startswith(displayName,%27A%26B%27)&$select=id,appId&$skiptoken=a%2Bb"
    )
    body = (
        '{"displayName":"A&B (déjà vu)",'
        '"description":"quoted \\"value\\"",'
        '"tags":["$literal"]}'
    )
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"$module = Import-Module -Force -PassThru '{module_path}'; "
        "$pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source; "
        "$launch = [pscustomobject]@{ FilePath = $pwsh; "
        "PrefixArguments = @('-NoLogo', '-NoProfile', '-File', "
        "[Environment]::GetEnvironmentVariable('TEST_AZ_RECORDER')); Installer = 'MSI' }; "
        "$uri = [Environment]::GetEnvironmentVariable('TEST_AZ_URI'); "
        "$body = [Environment]::GetEnvironmentVariable('TEST_AZ_BODY'); "
        "$env:AZ_INSTALLER = 'original'; "
        "$raw = & $module { param($spec, $graphUri, $jsonBody) "
        "Invoke-AzureCliLaunch -Launch $spec -Arguments "
        "@('rest', '--uri', $graphUri, '--body', $jsonBody) "
        "} $launch $uri $body; "
        "$child = ($raw | Out-String).Trim() | ConvertFrom-Json; "
        "$result = [pscustomobject]@{ Child = $child; After = $env:AZ_INSTALLER }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Depth 10 -Compress))"
    )
    environment = os.environ.copy()
    environment["TEST_AZ_RECORDER"] = str(recorder)
    environment["TEST_AZ_URI"] = uri
    environment["TEST_AZ_BODY"] = body

    result = json.loads(run_powershell(script, environment=environment))

    expected_arguments = ["rest", "--uri", uri, "--body", body]
    expected_base64 = [
        base64.b64encode(argument.encode("utf-8")).decode("ascii")
        for argument in expected_arguments
    ]
    assert result["Child"]["ArgumentsBase64"] == expected_base64
    assert result["Child"]["Installer"] == "MSI"
    assert result["After"] == "original"


def test_failed_msi_launch_restores_installer(tmp_path: Path) -> None:
    failure = tmp_path / "fail.ps1"
    failure.write_text(
        "[Console]::Error.WriteLine('do-not-emit-azure-stderr'); exit 9\n",
        encoding="utf-8",
    )
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"$module = Import-Module -Force -PassThru '{module_path}'; "
        "$pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source; "
        "$launch = [pscustomobject]@{ FilePath = $pwsh; "
        "PrefixArguments = @('-NoLogo', '-NoProfile', '-File', "
        "[Environment]::GetEnvironmentVariable('TEST_AZ_FAILURE')); Installer = 'MSI' }; "
        "$env:AZ_INSTALLER = 'original'; "
        "$PSNativeCommandUseErrorActionPreference = $true; "
        "$failedClosed = $false; "
        "$message = ''; "
        "$uri = 'https://graph.microsoft.com/v1.0/applications/"
        "11111111-1111-1111-1111-111111111111?`$filter=private&`$select=id'; "
        "try { & $module { param($spec, $target) "
        "Invoke-AzureCliLaunch -Launch $spec -Arguments "
        "@('rest', '--method', 'GET', '--uri', $target, '--body', '{\"private\":true}') "
        "} $launch $uri | Out-Null } catch { $failedClosed = $true; $message = $_.Exception.Message }; "
        "$result = [pscustomobject]@{ FailedClosed = $failedClosed; "
        "After = $env:AZ_INSTALLER; NativePreferenceAfter = "
        "$PSNativeCommandUseErrorActionPreference; Message = $message }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )
    environment = os.environ.copy()
    environment["TEST_AZ_FAILURE"] = str(failure)

    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    result = json.loads(completed.stdout)

    assert result["FailedClosed"] is True
    assert result["After"] == "original"
    assert result["NativePreferenceAfter"] is True
    assert "az rest GET graph.microsoft.com/v1.0/applications/{id}" in result["Message"]
    assert "11111111-1111-1111-1111-111111111111" not in result["Message"]
    assert "$filter" not in result["Message"]
    assert "private" not in result["Message"]
    assert "do-not-emit-azure-stderr" not in completed.stdout + completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake executable exercises the native az path")
def test_native_azure_cli_preserves_literal_uri_and_json_arguments(tmp_path: Path) -> None:
    fake_az = tmp_path / "az"
    fake_az.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "print(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_az.chmod(0o755)
    uri = (
        "https://graph.microsoft.com/v1.0/applications?"
        "$filter=startswith(displayName,%27A%26B%27)&$select=id,appId&$skiptoken=a%2Bb"
    )
    body = (
        '{"displayName":"A&B (déjà vu)",'
        '"description":"quoted \\"value\\"",'
        '"tags":["$literal"]}'
    )
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$uri = [Environment]::GetEnvironmentVariable('TEST_AZ_URI'); "
        "$body = [Environment]::GetEnvironmentVariable('TEST_AZ_BODY'); "
        "$result = Invoke-AzureCli -Arguments "
        "@('rest', '--uri', $uri, '--body', $body); "
        "[Console]::Out.Write(($result | Out-String).Trim())"
    )
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]
    environment["TEST_AZ_URI"] = uri
    environment["TEST_AZ_BODY"] = body

    arguments = json.loads(run_powershell(script, environment=environment))

    assert arguments == ["rest", "--uri", uri, "--body", body]


@pytest.mark.parametrize(
    "relative_path",
    [
        "scripts/provision-entra.ps1",
        "scripts/provision-entra-federation.ps1",
        "scripts/provision-github-azure.ps1",
    ],
)
def test_graph_provisioning_uses_safe_azure_cli_launcher(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")

    assert "$raw = Invoke-AzureCli -Arguments $arguments" in source
    assert 'throw "Microsoft Graph $Method request failed.' in source
    assert "$raw = & az @arguments" not in source


def test_custom_role_rest_call_uses_safe_azure_cli_launcher() -> None:
    source = (ROOT / "scripts" / "provision-github-azure.ps1").read_text(
        encoding="utf-8"
    )

    assert "Invoke-AzureCli -Arguments @(" in source
    assert "Failed to create or update the custom Azure deployment role." in source
    assert "& az rest" not in source


def test_permission_id_helper_accepts_empty_collection_and_reuses_existing_id() -> None:
    entra_script = ROOT / "scripts" / "provision-entra.ps1"
    module_path = str(entra_script).replace("'", "''")
    existing_id = "22222222-2222-2222-2222-222222222222"
    script = (
        "$tokens = $null; $errors = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{module_path}', "
        "[ref]$tokens, [ref]$errors); "
        "$definition = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Get-OrCreatePermissionId' }, $true); "
        "Invoke-Expression $definition.Extent.Text; "
        "$generated = Get-OrCreatePermissionId -Existing @() -Value 'Loan.Read'; "
        f"$existing = @([pscustomobject]@{{ value = 'Loan.Read'; id = '{existing_id}' }}); "
        "$reused = Get-OrCreatePermissionId -Existing $existing -Value 'Loan.Read'; "
        "$result = [pscustomobject]@{ Generated = $generated; Reused = $reused }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )

    result = json.loads(run_powershell(script, environment=os.environ.copy()))

    assert str(uuid.UUID(result["Generated"])) == result["Generated"]
    assert result["Reused"] == existing_id


def test_entra_app_role_values_are_namespaced_from_delegated_scopes() -> None:
    entra_script = ROOT / "scripts" / "provision-entra.ps1"
    module_path = str(entra_script).replace("'", "''")
    script = (
        "$tokens = $null; $errors = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{module_path}', "
        "[ref]$tokens, [ref]$errors); "
        "$definition = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Get-ApplicationRoleValue' }, $true); "
        "Invoke-Expression $definition.Extent.Text; "
        "$role = Get-ApplicationRoleValue -PermissionValue 'Loan.Read'; "
        "[Console]::Out.Write($role)"
    )

    role_value = run_powershell(script, environment=os.environ.copy())
    source = entra_script.read_text(encoding="utf-8")

    assert role_value == "Loan.Read.Role"
    assert role_value != "Loan.Read"
    assert "$roleValue = Get-ApplicationRoleValue -PermissionValue $permission.Value" in source
    assert "value = $roleValue" in source


def test_entra_permission_namespace_preflight_rejects_reserved_or_duplicate_values() -> None:
    entra_script = ROOT / "scripts" / "provision-entra.ps1"
    module_path = str(entra_script).replace("'", "''")
    script = (
        "$tokens = $null; $errors = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{module_path}', "
        "[ref]$tokens, [ref]$errors); "
        "$definition = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Assert-PermissionValueNamespaces' }, $true); "
        "Invoke-Expression $definition.Extent.Text; "
        "$valid = $true; "
        "try { Assert-PermissionValueNamespaces -PermissionValues @('Loan.Read', 'Document.Read') } "
        "catch { $valid = $false }; "
        "$reservedRejected = $false; "
        "try { Assert-PermissionValueNamespaces -PermissionValues @('Loan.Read', 'Loan.Read.Role') } "
        "catch { $reservedRejected = $true }; "
        "$duplicateRejected = $false; "
        "try { Assert-PermissionValueNamespaces -PermissionValues @('Loan.Read', 'loan.read') } "
        "catch { $duplicateRejected = $true }; "
        "$result = [pscustomobject]@{ Valid = $valid; ReservedRejected = $reservedRejected; "
        "DuplicateRejected = $duplicateRejected }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )

    result = json.loads(run_powershell(script, environment=os.environ.copy()))
    source = entra_script.read_text(encoding="utf-8")
    preflight_call = "Assert-PermissionValueNamespaces -PermissionValues $permissionValues"
    first_graph_mutation = "$apiApp = Ensure-Application"

    assert result == {"Valid": True, "ReservedRejected": True, "DuplicateRejected": True}
    assert "[StringComparer]::OrdinalIgnoreCase" in source
    assert preflight_call in source
    assert source.index(preflight_call) < source.index(first_graph_mutation)


@pytest.mark.parametrize(
    "stack_id",
    [
        "loan-idp-prod",
        "arn:aws:cloudformation:us-west-2:123456789012:stack/loan-idp-prod/abc123",
    ],
)
@pytest.mark.parametrize("prefix", ["", "aws: [ERROR]: "])
def test_cloudformation_classifier_accepts_only_stack_not_found(stack_id: str, prefix: str) -> None:
    message = (
        f"{prefix}An error occurred (ValidationError) when calling the DescribeStacks operation: "
        f"Stack with id {stack_id} does not exist"
    )

    assert classify_stack_lookup_error(message)


@pytest.mark.parametrize(
    "message",
    [
        (
            "An error occurred (AccessDenied) when calling the DescribeStacks operation: "
            "User is not authorized"
        ),
        (
            "An error occurred (ThrottlingException) when calling the DescribeStacks operation: "
            "Rate exceeded"
        ),
        (
            "An error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack name is invalid"
        ),
        (
            "WARNING: credentials are near expiry\n"
            "An error occurred (ValidationError) when calling the DescribeStacks operation: "
            "Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [WARNING]: An error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [ERROR]: An error occurred (AccessDenied) when calling the "
            "DescribeStacks operation: User is not authorized"
        ),
        (
            "aws: [ERROR]:An error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [ERROR]:\tAn error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [ERROR]:\nAn error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [ERROR]: aws: [ERROR]: An error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist"
        ),
        (
            "aws: [ERROR]: An error occurred (ValidationError) when calling the "
            "DescribeStacks operation: Stack with id loan-idp-prod does not exist\nextra diagnostic"
        ),
        "",
    ],
)
def test_cloudformation_classifier_fails_closed_for_other_errors(message: str) -> None:
    assert not classify_stack_lookup_error(message)


def test_aws_cli_failure_context_omits_deployment_arguments() -> None:
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        "$tokens = $null; $errors = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{module_path}', "
        "[ref]$tokens, [ref]$errors); "
        "$definition = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -eq 'Get-AwsCliFailureContext' }, $true); "
        "Invoke-Expression $definition.Extent.Text; "
        "$context = Get-AwsCliFailureContext -Arguments "
        "@('cloudformation', 'deploy', '--parameter-overrides', "
        "'Tenant=secret-tenant', 'Email=secret@example.invalid'); "
        "$invalid = Get-AwsCliFailureContext -Arguments @('--profile', 'secretname'); "
        "$result = [pscustomobject]@{ Context = $context; Invalid = $invalid }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )

    result = json.loads(run_powershell(script, environment=os.environ.copy()))
    source = COMMON_MODULE.read_text(encoding="utf-8")

    assert result == {"Context": "cloudformation deploy", "Invalid": "unknown operation"}
    assert "secret" not in json.dumps(result)
    assert '$($Arguments -join' not in source


def test_aws_cli_failures_do_not_emit_arguments_or_native_stderr(tmp_path: Path) -> None:
    if os.name == "nt":
        fake_aws = tmp_path / "aws.cmd"
        fake_aws.write_text("@echo off\necho %* 1>&2\nexit /b 9\n", encoding="utf-8")
    else:
        fake_aws = tmp_path / "aws"
        fake_aws.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >&2\nexit 9\n", encoding="utf-8")
        fake_aws.chmod(0o755)

    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        "$secret = 'Tenant=do-not-emit-this-value'; "
        "$messages = @(); "
        "try { Invoke-Aws -Profile 'test' -Region 'us-west-2' -Arguments "
        "@('cloudformation', 'deploy', '--parameter-overrides', $secret) | Out-Null } "
        "catch { $messages += $_.Exception.Message }; "
        "try { Get-AwsCloudFormationStackDescription -Profile 'test' -Region 'us-west-2' "
        "-StackName 'do-not-emit-this-stack' | Out-Null } "
        "catch { $messages += $_.Exception.Message }; "
        "[Console]::Out.Write(($messages | ConvertTo-Json -Compress))"
    )
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]

    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    all_streams = completed.stdout + completed.stderr

    assert json.loads(completed.stdout) == [
        "AWS CLI failed while running 'aws cloudformation deploy'.",
        "AWS CLI failed while running 'aws cloudformation describe-stacks'.",
    ]
    assert "do-not-emit" not in all_streams
    assert "--parameter-overrides" not in all_streams


def test_stateful_stack_policy_requires_default_allow_and_all_protected_types() -> None:
    policy = json.loads(
        (ROOT / "infra" / "stack-policies" / "protect-stateful-resources.json").read_text(
            encoding="utf-8"
        )
    )

    assert stack_policy_is_valid(policy)

    policy["Statement"][1]["Condition"]["StringEquals"]["ResourceType"].remove("AWS::KMS::Key")
    assert not stack_policy_is_valid(policy)

    policy["Statement"][1]["Condition"]["StringEquals"]["ResourceType"].append("AWS::KMS::Key")
    policy["Statement"][1]["Action"].remove("Update:Replace")
    assert not stack_policy_is_valid(policy)
