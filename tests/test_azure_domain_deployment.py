from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

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


def test_powershell_environment_config_requires_lowercase_acr_name(tmp_path: Path) -> None:
    values = environment_config()
    values["azureContainerRegistryName"] = "LoanPlatformProd123"

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "lowercase alphanumeric Azure Container Registry" in completed.stderr


def test_powershell_scaling_rejects_replica_count_above_azure_limit(tmp_path: Path) -> None:
    values = environment_config()
    values["azureApiMaxReplicas"] = 301

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "azureApiMaxReplicas <= 300" in completed.stderr


@pytest.mark.parametrize("value", [0, 2, 1000])
def test_powershell_scaling_pins_http_target_to_one(tmp_path: Path, value: int) -> None:
    values = environment_config()
    values["azureApiConcurrentRequestsPerReplica"] = value

    completed = read_environment_config(tmp_path, values)

    assert completed.returncode != 0
    assert "azureApiConcurrentRequestsPerReplica = 1" in completed.stderr


def test_bicep_pins_http_scale_target_without_removing_horizontal_scale() -> None:
    bicep = (ROOT / "infra" / "azure" / "main.bicep").read_text(encoding="utf-8")

    assert "@allowed([\n  1\n])\nparam concurrentRequestsPerReplica int = 1" in bicep
    assert "concurrentRequests: string(concurrentRequestsPerReplica)" in bicep
    assert "maxReplicas: apiMaxReplicas" in bicep
    assert "not a hard request-admission cap" in bicep


def test_bicep_and_runtime_share_the_strict_environment_enum() -> None:
    bicep = (ROOT / "infra" / "azure" / "main.bicep").read_text(encoding="utf-8")
    settings = (ROOT / "services" / "azure_api" / "settings.py").read_text(encoding="utf-8")

    for environment in ("dev", "test", "stage", "prod"):
        assert f"'{environment}'" in bicep
        assert f'"{environment}"' in settings
    assert "@allowed([" in bicep
    assert "SUPPORTED_ENVIRONMENTS" in settings


def test_ci_and_production_image_builds_explicitly_use_buildkit() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
    workflow_steps = workflow["jobs"]["validate"]["steps"]
    docker_step = next(step for step in workflow_steps if "docker build" in step.get("run", ""))
    assert docker_step["env"]["DOCKER_BUILDKIT"] == "1"

    dockerfile = (ROOT / "services" / "azure_api" / "Dockerfile").read_text(encoding="utf-8")
    assert "RUN --mount=type=secret,id=enterprise_ca,required=false" in dockerfile
    for line in dockerfile.splitlines():
        if line.startswith("# syntax="):
            assert "@sha256:" in line

    task_path = ROOT / "infra" / "azure" / "acr-build-api.yml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    expected_image = "$Registry/{{.Values.image}}"
    assert task["env"] == ["DOCKER_BUILDKIT=1"]
    assert "--file services/azure_api/Dockerfile" in task["steps"][0]["build"]
    assert f"--tag {expected_image}" in task["steps"][0]["build"]
    assert task["steps"][1]["push"] == [expected_image]

    deploy_script = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")
    assert "az acr build" not in deploy_script
    for fragment in (
        "az acr run",
        "infra/azure/acr-build-api.yml",
        '--set "image=${ImageRepository}:$ImageTag"',
    ):
        assert fragment in deploy_script


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
