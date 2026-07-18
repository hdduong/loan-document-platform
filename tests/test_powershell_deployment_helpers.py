from __future__ import annotations

import base64
import json
import os
import subprocess
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
    failure.write_text("exit 9\n", encoding="utf-8")
    module_path = str(COMMON_MODULE).replace("'", "''")
    script = (
        f"$module = Import-Module -Force -PassThru '{module_path}'; "
        "$pwsh = (Get-Command pwsh -CommandType Application | Select-Object -First 1).Source; "
        "$launch = [pscustomobject]@{ FilePath = $pwsh; "
        "PrefixArguments = @('-NoLogo', '-NoProfile', '-File', "
        "[Environment]::GetEnvironmentVariable('TEST_AZ_FAILURE')); Installer = 'MSI' }; "
        "$env:AZ_INSTALLER = 'original'; "
        "$failedClosed = $false; "
        "try { & $module { param($spec) "
        "Invoke-AzureCliLaunch -Launch $spec -Arguments @('rest', '--method', 'GET') "
        "} $launch | Out-Null } catch { $failedClosed = $true }; "
        "$result = [pscustomobject]@{ FailedClosed = $failedClosed; "
        "After = $env:AZ_INSTALLER }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )
    environment = os.environ.copy()
    environment["TEST_AZ_FAILURE"] = str(failure)

    result = json.loads(run_powershell(script, environment=environment))

    assert result == {"FailedClosed": True, "After": "original"}


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
    assert "$raw = & az @arguments" not in source


def test_custom_role_rest_call_uses_safe_azure_cli_launcher() -> None:
    source = (ROOT / "scripts" / "provision-github-azure.ps1").read_text(
        encoding="utf-8"
    )

    assert "Invoke-AzureCli -Arguments @(" in source
    assert "& az rest" not in source


@pytest.mark.parametrize(
    "stack_id",
    [
        "loan-idp-prod",
        "arn:aws:cloudformation:us-west-2:123456789012:stack/loan-idp-prod/abc123",
    ],
)
def test_cloudformation_classifier_accepts_only_stack_not_found(stack_id: str) -> None:
    message = (
        "An error occurred (ValidationError) when calling the DescribeStacks operation: "
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
        "",
    ],
)
def test_cloudformation_classifier_fails_closed_for_other_errors(message: str) -> None:
    assert not classify_stack_lookup_error(message)


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
