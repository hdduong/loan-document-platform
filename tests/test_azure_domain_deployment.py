from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMON_MODULE = ROOT / "scripts" / "common.psm1"
ENVIRONMENT_EXAMPLE = ROOT / "config" / "environments" / "prod.example.json"


def environment_config() -> dict[str, object]:
    values = json.loads(ENVIRONMENT_EXAMPLE.read_text(encoding="utf-8"))
    values.update(
        {
            "azureSubscriptionId": "11111111-1111-4111-8111-111111111111",
            "azureContainerRegistryName": "loanplatformprod123",
            "awsProfile": "loan-platform-prod",
            "awsAccountId": "123456789012",
            "route53HostedZoneId": "Z1234567890",
            "entraTenantId": "22222222-2222-4222-8222-222222222222",
            "alertEmail": "operations@example.com",
            "budgetEmail": "budget@example.com",
        }
    )
    return values


def read_environment_config(tmp_path: Path, values: dict[str, object]) -> subprocess.CompletedProcess[str]:
    config_path = tmp_path / "environment.json"
    config_path.write_text(json.dumps(values), encoding="utf-8")
    module_path = str(COMMON_MODULE).replace("'", "''")
    environment_path = str(config_path).replace("'", "''")
    script = (
        f"Import-Module -Force '{module_path}'; "
        f"Read-EnvironmentConfig -Path '{environment_path}' | Out-Null; "
        "[Console]::Out.Write('ok')"
    )
    return subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("environment", ["dev", "test", "stage", "prod"])
def test_powershell_environment_config_accepts_only_supported_names(
    tmp_path: Path, environment: str
) -> None:
    values = environment_config()
    values["environment"] = environment

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ok"


@pytest.mark.parametrize("environment", ["production", "prodd", "PROD", "dev-1"])
def test_powershell_environment_config_rejects_unknown_names(
    tmp_path: Path, environment: str
) -> None:
    values = environment_config()
    values["environment"] = environment

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "environment to be one of" in completed.stderr


@pytest.mark.parametrize("hostname", ["API.LOANS.EXAMPLE.COM", "api.loans.example.com."])
def test_powershell_environment_config_requires_canonical_api_hostname(
    tmp_path: Path, hostname: str
) -> None:
    values = environment_config()
    values["apiHostName"] = hostname

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "lowercase without a trailing dot" in completed.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("azureApiMaxReplicas", 301),
        ("azureApiConcurrentRequestsPerReplica", 1001),
    ],
)
def test_powershell_scaling_error_names_every_enforced_bound(
    tmp_path: Path, field: str, value: int
) -> None:
    values = environment_config()
    values[field] = value

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "azureApiMaxReplicas <= 300" in completed.stderr
    assert "azureApiConcurrentRequestsPerReplica <= 1000" in completed.stderr


def test_bicep_and_runtime_share_the_strict_environment_enum() -> None:
    bicep = (ROOT / "infra" / "azure" / "main.bicep").read_text(encoding="utf-8")
    settings = (ROOT / "services" / "azure_api" / "settings.py").read_text(encoding="utf-8")

    for environment in ("dev", "test", "stage", "prod"):
        assert f"'{environment}'" in bicep
        assert f'"{environment}"' in settings
    assert "@allowed([" in bicep
    assert "SUPPORTED_ENVIRONMENTS" in settings


def test_azure_deployment_preserves_domains_and_waits_for_a_ready_certificate() -> None:
    script = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")

    for fragment in (
        "unexpected custom hostname bindings",
        "-AllowIncompleteResume ([bool]$BindCustomDomain)",
        "resume explicitly with -BindCustomDomain",
        "ConvertFrom-CertificateSubject",
        "Multiple managed certificates exist",
        "Wait-ManagedApiCertificate",
        "provisioningState",
        "-ceq 'Succeeded'",
        "replaced the pre-existing API custom-domain certificate unexpectedly",
    ):
        assert fragment in script


def test_production_web_publish_revalidates_the_live_api_domain() -> None:
    script = (ROOT / "scripts" / "deploy-web.ps1").read_text(encoding="utf-8")
    validation_index = script.index("Assert-LiveProductionApiDomain\n}")
    publication_index = script.index("Publish reviewed UI build")

    assert validation_index < publication_index
    for fragment in (
        "az containerapp hostname list",
        "az containerapp env certificate list",
        "provisioningState",
        "route53', 'list-resource-record-sets'",
        "Production API CNAME does not target the live Container App FQDN",
        "@('/health', '/ready')",
    ):
        assert fragment in script
