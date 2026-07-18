from __future__ import annotations

import base64
import json
import os
import subprocess
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
        "$message = ''; "
        "$uri = 'https://graph.microsoft.com/v1.0/applications/"
        "11111111-1111-1111-1111-111111111111?`$filter=private&`$select=id'; "
        "try { & $module { param($spec, $target) "
        "Invoke-AzureCliLaunch -Launch $spec -Arguments "
        "@('rest', '--method', 'GET', '--uri', $target, '--body', '{\"private\":true}') "
        "} $launch $uri | Out-Null } catch { $failedClosed = $true; $message = $_.Exception.Message }; "
        "$result = [pscustomobject]@{ FailedClosed = $failedClosed; "
        "After = $env:AZ_INSTALLER; Message = $message }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )
    environment = os.environ.copy()
    environment["TEST_AZ_FAILURE"] = str(failure)

    result = json.loads(run_powershell(script, environment=environment))

    assert result["FailedClosed"] is True
    assert result["After"] == "original"
    assert "az rest GET graph.microsoft.com/v1.0/applications/{id}" in result["Message"]
    assert "11111111-1111-1111-1111-111111111111" not in result["Message"]
    assert "$filter" not in result["Message"]
    assert "private" not in result["Message"]


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
