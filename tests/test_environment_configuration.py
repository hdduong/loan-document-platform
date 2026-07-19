from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "configure-environment.ps1"
COMMON_MODULE = ROOT / "scripts" / "common.psm1"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def valid_environment() -> dict[str, object]:
    values = json.loads(
        (ROOT / "config" / "environments" / "prod.example.json").read_text(
            encoding="utf-8"
        )
    )
    values.update(
        {
            "environment": "dev",
            "azureSubscriptionId": "11111111-1111-1111-1111-111111111111",
            "azureContainerRegistryName": "loanidpdev12345678",
            "awsProfile": "synthetic-profile",
            "awsAccountId": "123456789012",
            "domainName": "example.test",
            "route53HostedZoneId": "ZSYNTHETIC123",
            "uiHostName": "idp-dev.example.test",
            "apiHostName": "api-dev.example.test",
            "entraTenantId": "22222222-2222-2222-2222-222222222222",
            "alertEmail": "alerts@example.test",
            "budgetEmail": "budget@example.test",
        }
    )
    return values


def run_pwsh(command: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def normalized_powershell_error(value: str) -> str:
    return " ".join(ANSI_ESCAPE.sub("", value).split())


def test_normalized_powershell_error_removes_hosted_ansi_and_line_wrapping() -> None:
    value = "\x1b[31;1mmust keep the AWS data plane in\x1b[0m\n\x1b[31;1mus-west-2.\x1b[0m"

    assert normalized_powershell_error(value) == "must keep the AWS data plane in us-west-2."


def write_test_certificate_bundle(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic Test CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def load_script_functions(names: tuple[str, ...]) -> str:
    escaped_script = str(SCRIPT).replace("'", "''")
    quoted_names = ", ".join(f"'{name}'" for name in names)
    return (
        "$tokens = $null; $parseErrors = $null; "
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{escaped_script}', "
        "[ref]$tokens, [ref]$parseErrors); "
        "if ($parseErrors.Count -ne 0) { throw 'configure-environment.ps1 does not parse' }; "
        f"foreach ($name in @({quoted_names})) {{ "
        "$definition = $ast.Find({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and "
        "$node.Name -ceq $name }, $true); "
        "if ($null -eq $definition) { throw \"Missing function $name\" }; "
        "Invoke-Expression $definition.Extent.Text }; "
    )


def test_configure_environment_has_fail_closed_repository_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")

    for fragment in (
        "#requires -Version 7.2",
        "Invoke-AzureCli -Arguments",
        "Invoke-Aws -Profile",
        ") -CaptureJson -ForceProfile",
        "Where-Object { -not [bool]$_.Config.PrivateZone }",
        "'check-ignore', '--quiet'",
        "[System.Text.UTF8Encoding]::new($false)",
        "Read-EnvironmentConfig -Path $temporaryPath",
        "[System.IO.File]::Replace($validatedConfigPath, $resolvedEnvironmentFile, $backupConfigPath, $true)",
        "$env:REQUESTS_CA_BUNDLE = $resolvedBundle",
        "$env:SSL_CERT_FILE = $resolvedBundle",
        "$env:AWS_CA_BUNDLE = $resolvedBundle",
        "Cloud identifiers, contacts, profile names, and the complete configuration were not displayed.",
        "Read-Host 'Email for operational alerts' -MaskInput",
        "Remove-Item -LiteralPath $validatedConfigPath -Force -WhatIf:$false",
    ):
        assert fragment in source

    assert "Assert-AwsIdentity" not in source
    assert "C:\\Users\\" not in source
    assert "Set-Content -LiteralPath $resolvedEnvironmentFile" not in source
    assert "Write-Host \"AWS identity:" not in source
    assert "$publicZones[$index].Name" not in source
    assert 'Read-Host "UI hostname [$defaultUiHost]"' not in source
    for parameter in (
        "AwsProfile",
        "HostedZoneId",
        "UiHostName",
        "ApiHostName",
        "AzureContainerRegistryName",
    ):
        assert f"([string]${parameter}).Trim()" in source
        assert f"${parameter}.Trim()" not in source
    for variable in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "AWS_CA_BUNDLE"):
        assert f"$env:{variable} = $caPath" in bootstrap
    assert source.count("Set-Variable -Name PSNativeCommandUseErrorActionPreference") == 4
    assert source.count("-Scope Local -WhatIf:$false") == 4


def test_configuration_helper_functions_canonicalize_and_reject_unsafe_values() -> None:
    command = load_script_functions(
        (
            "Test-ConfiguredValue",
            "ConvertTo-CanonicalDnsName",
            "Test-HostNameForDomain",
            "Assert-ValidEmailAddress",
            "Assert-ExistingValueMatches",
        )
    )
    command += (
        "$badEmailRejected = $false; "
        "$whitespaceEmailRejected = $false; "
        "try { Assert-ValidEmailAddress -Value \"alerts@example.test`r`nBcc:x@example.test\" "
        "-Name 'AlertEmail' } catch { $badEmailRejected = $true }; "
        "try { Assert-ValidEmailAddress -Value ' alerts@example.test ' -Name 'AlertEmail' "
        "} catch { $whitespaceEmailRejected = $true }; "
        "$guidEquivalentAccepted = $true; "
        "$differentGuidRejected = $false; "
        "$guidConfig = [pscustomobject]@{ azureSubscriptionId = "
        "'{ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB}' }; "
        "try { Assert-ExistingValueMatches -Config $guidConfig -Name 'azureSubscriptionId' "
        "-Expected 'abcdefab-cdef-abcd-efab-cdefabcdefab' -Comparison Guid } "
        "catch { $guidEquivalentAccepted = $false }; "
        "try { Assert-ExistingValueMatches -Config $guidConfig -Name 'azureSubscriptionId' "
        "-Expected '33333333-3333-3333-3333-333333333333' -Comparison Guid } "
        "catch { $differentGuidRejected = $true }; "
        "$result = [pscustomobject]@{ "
        "Placeholder = Test-ConfiguredValue -Value 'example.com'; "
        "Configured = Test-ConfiguredValue -Value 'customer.example'; "
        "Canonical = ConvertTo-CanonicalDnsName -Value ' API.DEV.EXAMPLE.TEST. '; "
        "InDomain = Test-HostNameForDomain -HostName 'api-dev.example.test' "
        "-DomainName 'example.test'; "
        "OutsideDomain = Test-HostNameForDomain -HostName 'api-dev.example.net' "
        "-DomainName 'example.test'; "
        "BadEmailRejected = $badEmailRejected; "
        "WhitespaceEmailRejected = $whitespaceEmailRejected; "
        "GuidEquivalentAccepted = $guidEquivalentAccepted; "
        "DifferentGuidRejected = $differentGuidRejected }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )

    completed = run_pwsh(command, os.environ.copy())

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "Placeholder": False,
        "Configured": True,
        "Canonical": "api.dev.example.test",
        "InDomain": True,
        "OutsideDomain": False,
        "BadEmailRejected": True,
        "WhitespaceEmailRejected": True,
        "GuidEquivalentAccepted": True,
        "DifferentGuidRejected": True,
    }


def test_ca_bundle_requires_parseable_certificates_and_rejects_any_private_key_label(
    tmp_path: Path,
) -> None:
    valid_bundle = tmp_path / "valid.pem"
    write_test_certificate_bundle(valid_bundle)
    valid_bundle.write_bytes(valid_bundle.read_bytes().replace(b"\n", b"\r\n"))
    malformed_bundle = tmp_path / "malformed.pem"
    malformed_bundle.write_text(
        "-----BEGIN CERTIFICATE-----\nU1lOVEhFVElD\n-----END CERTIFICATE-----\n",
        encoding="ascii",
    )
    unsafe_bundle = tmp_path / "unsafe.pem"
    unsafe_bundle.write_text(
        valid_bundle.read_text(encoding="ascii")
        + "  -----BEGIN ENCRYPTED "
        + "PRIVATE KEY-----\nU1lOVEhFVElD\n-----END ENCRYPTED "
        + "PRIVATE KEY-----\n",
        encoding="ascii",
    )
    orphan_boundary_bundle = tmp_path / "orphan-boundary.pem"
    orphan_boundary_bundle.write_text(
        valid_bundle.read_text(encoding="ascii")
        + "-----END "
        + "PRIVATE KEY-----\n",
        encoding="ascii",
    )
    escaped_module = str(COMMON_MODULE).replace("'", "''")
    command = (
        f"Import-Module -Force '{escaped_module}'; "
        "$valid = [Environment]::GetEnvironmentVariable('TEST_VALID_BUNDLE'); "
        "$malformed = [Environment]::GetEnvironmentVariable('TEST_MALFORMED_BUNDLE'); "
        "$unsafe = [Environment]::GetEnvironmentVariable('TEST_UNSAFE_BUNDLE'); "
        "$orphan = [Environment]::GetEnvironmentVariable('TEST_ORPHAN_BUNDLE'); "
        "$validAccepted = $true; $malformedRejected = $false; $unsafeRejected = $false; "
        "$orphanRejected = $false; "
        "try { Assert-CertificateOnlyBundle -Path $valid | Out-Null } "
        "catch { $validAccepted = $false }; "
        "try { Assert-CertificateOnlyBundle -Path $malformed | Out-Null } "
        "catch { $malformedRejected = $true }; "
        "try { Assert-CertificateOnlyBundle -Path $unsafe | Out-Null } "
        "catch { $unsafeRejected = $true }; "
        "try { Assert-CertificateOnlyBundle -Path $orphan | Out-Null } "
        "catch { $orphanRejected = $true }; "
        "$result = [pscustomobject]@{ ValidAccepted = $validAccepted; "
        "MalformedRejected = $malformedRejected; UnsafeRejected = $unsafeRejected; "
        "OrphanRejected = $orphanRejected }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )
    environment = os.environ.copy()
    environment["TEST_VALID_BUNDLE"] = str(valid_bundle)
    environment["TEST_MALFORMED_BUNDLE"] = str(malformed_bundle)
    environment["TEST_UNSAFE_BUNDLE"] = str(unsafe_bundle)
    environment["TEST_ORPHAN_BUNDLE"] = str(orphan_boundary_bundle)

    completed = run_pwsh(command, environment)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ValidAccepted": True,
        "MalformedRejected": True,
        "UnsafeRejected": True,
        "OrphanRejected": True,
    }


def test_azure_identity_validation_canonicalizes_and_redacts_malformed_values() -> None:
    escaped_module = str(COMMON_MODULE).replace("'", "''")
    command = (
        f"Import-Module -Force '{escaped_module}'; "
        "$valid = [pscustomobject]@{ id = '{ABCDEFAB-CDEF-ABCD-EFAB-CDEFABCDEFAB}'; "
        "tenantId = '12345678-90AB-CDEF-1234-567890ABCDEF' }; "
        "$accepted = Assert-AzureIdentity -Account $valid "
        "-ExpectedSubscriptionId 'abcdefab-cdef-abcd-efab-cdefabcdefab' "
        "-ExpectedTenantId '12345678-90ab-cdef-1234-567890abcdef'; "
        "$malformedMessage = ''; "
        "$malformed = [pscustomobject]@{ id = 'raw-subscription-sentinel'; "
        "tenantId = 'raw-tenant-sentinel' }; "
        "try { Assert-AzureIdentity -Account $malformed | Out-Null } "
        "catch { $malformedMessage = $_.Exception.Message }; "
        "$result = [pscustomobject]@{ "
        "SubscriptionId = $accepted.SubscriptionId.ToString(); "
        "TenantId = $accepted.TenantId.ToString(); Message = $malformedMessage }; "
        "[Console]::Out.Write(($result | ConvertTo-Json -Compress))"
    )

    completed = run_pwsh(command, os.environ.copy())

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["SubscriptionId"] == "abcdefab-cdef-abcd-efab-cdefabcdefab"
    assert result["TenantId"] == "12345678-90ab-cdef-1234-567890abcdef"
    assert result["Message"] == "Azure account lookup returned invalid identity identifiers."
    assert "raw-subscription-sentinel" not in completed.stdout + completed.stderr
    assert "raw-tenant-sentinel" not in completed.stdout + completed.stderr


def test_atomic_writer_validates_and_replaces_without_bom(tmp_path: Path) -> None:
    destination = tmp_path / "environment.json"
    destination.write_text("original\n", encoding="utf-8")
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(valid_environment()), encoding="utf-8")
    escaped_module = str(COMMON_MODULE).replace("'", "''")
    command = f"Import-Module -Force '{escaped_module}'; "
    command += load_script_functions(("New-ValidatedEnvironmentConfigFile",))
    command += (
        "$destination = [Environment]::GetEnvironmentVariable('TEST_DESTINATION'); "
        "$candidate = [Environment]::GetEnvironmentVariable('TEST_CANDIDATE'); "
        "$config = Get-Content -Raw -LiteralPath $candidate | ConvertFrom-Json -Depth 20; "
        "$validated = New-ValidatedEnvironmentConfigFile -Config $config -Destination $destination; "
        "$backup = \"$destination.backup.json\"; "
        "[System.IO.File]::Replace($validated, $destination, $backup, $true); "
        "Remove-Item -LiteralPath $backup -Force; "
        "[Console]::Out.Write('ok')"
    )
    environment = os.environ.copy()
    environment["TEST_DESTINATION"] = str(destination)
    environment["TEST_CANDIDATE"] = str(candidate)

    completed = run_pwsh(command, environment)

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "ok"
    raw = destination.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\n")
    assert json.loads(raw) == valid_environment()
    assert list(tmp_path.glob(".environment.json.*.json")) == []


def test_atomic_writer_leaves_original_unchanged_when_validation_fails(tmp_path: Path) -> None:
    destination = tmp_path / "environment.json"
    original = b"original bytes\n"
    destination.write_bytes(original)
    invalid = valid_environment()
    invalid["awsRegion"] = "us-east-1"
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(invalid), encoding="utf-8")
    escaped_module = str(COMMON_MODULE).replace("'", "''")
    command = f"Import-Module -Force '{escaped_module}'; "
    command += load_script_functions(("New-ValidatedEnvironmentConfigFile",))
    command += (
        "$destination = [Environment]::GetEnvironmentVariable('TEST_DESTINATION'); "
        "$candidate = [Environment]::GetEnvironmentVariable('TEST_CANDIDATE'); "
        "$config = Get-Content -Raw -LiteralPath $candidate | ConvertFrom-Json -Depth 20; "
        "New-ValidatedEnvironmentConfigFile -Config $config -Destination $destination | Out-Null"
    )
    environment = os.environ.copy()
    environment["TEST_DESTINATION"] = str(destination)
    environment["TEST_CANDIDATE"] = str(candidate)

    completed = run_pwsh(command, environment)

    assert completed.returncode != 0
    assert "must keep the AWS data plane in us-west-2" in normalized_powershell_error(
        completed.stderr
    )
    assert destination.read_bytes() == original
    assert list(tmp_path.glob(".environment.json.*.json")) == []


def write_fake_cloud_clis(directory: Path) -> None:
    az = directory / "az"
    az.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "if sys.argv[1:] != ['account', 'show', '--output', 'json']:\n"
        "    raise SystemExit(9)\n"
        "print(json.dumps({'id': os.environ.get('TEST_AZURE_SUBSCRIPTION_ID', "
        "'11111111-1111-1111-1111-111111111111'), "
        "'tenantId': os.environ.get('TEST_AZURE_TENANT_ID', "
        "'22222222-2222-2222-2222-222222222222')}))\n",
        encoding="utf-8",
    )
    az.chmod(0o755)
    aws = directory / "aws"
    aws.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['TEST_AWS_ARGS_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(args) + '\\n')\n"
        "if args == ['configure', 'list-profiles']:\n"
        "    print('synthetic-profile')\n"
        "elif 'sts' in args and 'get-caller-identity' in args:\n"
        "    print(json.dumps({'Account': os.environ.get('TEST_AWS_ACCOUNT_ID', "
        "'123456789012'), "
        "'Arn': 'arn:aws:iam::123456789012:role/synthetic'}))\n"
        "elif 'route53' in args and 'list-hosted-zones' in args:\n"
        "    print(json.dumps({'HostedZones': ["
        "{'Id': '/hostedzone/ZSYNTHETIC123', 'Name': 'example.test.', "
        "'Config': {'PrivateZone': False}}, "
        "{'Id': '/hostedzone/ZPRIVATE123', 'Name': 'private.example.test.', "
        "'Config': {'PrivateZone': True}}]}))\n"
        "else:\n"
        "    raise SystemExit(9)\n",
        encoding="utf-8",
    )
    aws.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake executables exercise the full CLI flow")
def test_noninteractive_configuration_is_idempotent_and_redacts_identifiers(
    tmp_path: Path,
) -> None:
    write_fake_cloud_clis(tmp_path)
    bundle = tmp_path / "ca-bundle.pem"
    write_test_certificate_bundle(bundle)
    aws_log = tmp_path / "aws-arguments.jsonl"
    environment_path = ROOT / "config" / "environments" / f"pytest-{uuid.uuid4().hex}.json"
    initial = valid_environment()
    for name, value in {
        "azureSubscriptionId": "REPLACE_WITH_SUBSCRIPTION_GUID",
        "azureContainerRegistryName": "REPLACE_WITH_GLOBALLY_UNIQUE_ACR_NAME",
        "awsProfile": "REPLACE_WITH_IAM_IDENTITY_CENTER_PROFILE",
        "awsAccountId": "REPLACE_WITH_12_DIGIT_ACCOUNT_ID",
        "domainName": "example.com",
        "route53HostedZoneId": "REPLACE_WITH_HOSTED_ZONE_ID",
        "uiHostName": "loans.example.com",
        "apiHostName": "api.loans.example.com",
        "entraTenantId": "REPLACE_WITH_TENANT_GUID",
        "alertEmail": "REPLACE_WITH_OPERATIONS_EMAIL",
        "budgetEmail": "REPLACE_WITH_BUDGET_EMAIL",
    }.items():
        initial[name] = value
    environment_path.write_text(json.dumps(initial), encoding="utf-8")
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]
    environment["GITHUB_ACTIONS"] = "true"
    environment["TEST_AWS_ARGS_LOG"] = str(aws_log)
    arguments = [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-File",
        str(SCRIPT),
        "-EnvironmentFile",
        str(environment_path),
        "-CorporateCaBundlePath",
        str(bundle),
        "-HostedZoneId",
        "ZSYNTHETIC123",
        "-AlertEmail",
        "alerts@example.test",
        "-NonInteractive",
    ]
    try:
        first = subprocess.run(
            arguments, check=False, capture_output=True, text=True, env=environment
        )
        assert first.returncode == 0, first.stderr
        configured = json.loads(environment_path.read_text(encoding="utf-8"))
        first_registry = configured["azureContainerRegistryName"]
        assert configured["domainName"] == "example.test"
        assert configured["uiHostName"] == "idp-dev.example.test"
        assert configured["apiHostName"] == "api-dev.example.test"
        assert configured["budgetEmail"] == "alerts@example.test"
        assert configured["awsProfile"] == "synthetic-profile"
        assert first_registry.startswith("loanidpdev")

        second = subprocess.run(
            arguments, check=False, capture_output=True, text=True, env=environment
        )
        assert second.returncode == 0, second.stderr
        rerun = json.loads(environment_path.read_text(encoding="utf-8"))
        assert rerun["azureContainerRegistryName"] == first_registry

        null_parameter_command = (
            "& $env:TEST_CONFIGURATOR "
            "-EnvironmentFile $env:TEST_ENVIRONMENT_FILE "
            "-CorporateCaBundlePath $env:TEST_CA_BUNDLE "
            "-AwsProfile $null -HostedZoneId $null -UiHostName $null "
            "-ApiHostName $null -AzureContainerRegistryName $null "
            "-NonInteractive"
        )
        null_parameter_environment = {
            **environment,
            "TEST_CONFIGURATOR": str(SCRIPT),
            "TEST_ENVIRONMENT_FILE": str(environment_path),
            "TEST_CA_BUNDLE": str(bundle),
        }
        null_parameters = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-Command", null_parameter_command],
            check=False,
            capture_output=True,
            text=True,
            env=null_parameter_environment,
        )
        assert null_parameters.returncode == 0, null_parameters.stderr
        assert json.loads(environment_path.read_text(encoding="utf-8")) == rerun

        what_if = subprocess.run(
            [*arguments, "-WhatIf"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert what_if.returncode == 0, what_if.stderr
        assert not list(environment_path.parent.glob(f".{environment_path.name}.*.json"))

        stable_bytes = environment_path.read_bytes()
        azure_mismatch_environment = {**environment, "TEST_AZURE_SUBSCRIPTION_ID": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        azure_mismatch = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=azure_mismatch_environment,
        )
        assert azure_mismatch.returncode != 0
        assert "authenticated cloud identity does not match" in azure_mismatch.stderr
        assert environment_path.read_bytes() == stable_bytes

        tenant_mismatch_environment = {
            **environment,
            "TEST_AZURE_TENANT_ID": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        }
        tenant_mismatch = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=tenant_mismatch_environment,
        )
        assert tenant_mismatch.returncode != 0
        assert "authenticated cloud identity does not match" in tenant_mismatch.stderr
        assert environment_path.read_bytes() == stable_bytes

        aws_mismatch_environment = {**environment, "TEST_AWS_ACCOUNT_ID": "999999999999"}
        aws_mismatch = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            env=aws_mismatch_environment,
        )
        assert aws_mismatch.returncode != 0
        assert "authenticated cloud identity does not match" in aws_mismatch.stderr
        assert environment_path.read_bytes() == stable_bytes

        profile_mismatch = subprocess.run(
            [*arguments, "-AwsProfile", "other-profile"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert profile_mismatch.returncode != 0
        assert "authenticated cloud identity does not match" in profile_mismatch.stderr
        assert environment_path.read_bytes() == stable_bytes

        whitespace_email_arguments = list(arguments)
        email_index = whitespace_email_arguments.index("alerts@example.test")
        whitespace_email_arguments[email_index] = " alerts@example.test "
        whitespace_email = subprocess.run(
            whitespace_email_arguments,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert whitespace_email.returncode != 0
        assert "exactly one canonical email address" in whitespace_email.stderr
        assert environment_path.read_bytes() == stable_bytes

        private_zone_arguments = list(arguments)
        zone_index = private_zone_arguments.index("ZSYNTHETIC123")
        private_zone_arguments[zone_index] = "ZPRIVATE123"
        private_zone = subprocess.run(
            private_zone_arguments,
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert private_zone.returncode != 0
        assert "selected hosted-zone ID" in private_zone.stderr
        assert environment_path.read_bytes() == stable_bytes

        aws_calls = [json.loads(line) for line in aws_log.read_text(encoding="utf-8").splitlines()]
        service_calls = [call for call in aws_calls if call != ["configure", "list-profiles"]]
        assert service_calls
        assert all(
            call[:2] == ["--profile", "synthetic-profile"] for call in service_calls
        )

        all_output = (
            first.stdout
            + first.stderr
            + second.stdout
            + second.stderr
            + null_parameters.stdout
            + null_parameters.stderr
            + what_if.stdout
            + what_if.stderr
            + azure_mismatch.stdout
            + azure_mismatch.stderr
            + tenant_mismatch.stdout
            + tenant_mismatch.stderr
            + aws_mismatch.stdout
            + aws_mismatch.stderr
            + profile_mismatch.stdout
            + profile_mismatch.stderr
            + whitespace_email.stdout
            + whitespace_email.stderr
            + private_zone.stdout
            + private_zone.stderr
        )
        for private_value in (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "123456789012",
            "alerts@example.test",
            "example.test",
            "synthetic-profile",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "999999999999",
            "ZSYNTHETIC123",
            "ZPRIVATE123",
            "other-profile",
            first_registry,
        ):
            assert private_value not in all_output
    finally:
        environment_path.unlink(missing_ok=True)
