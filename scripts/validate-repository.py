from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
IDP_DIR = ROOT / "config" / "idp"
SPEC_KIT_VERSION = "0.12.15"
SPEC_KIT_COMMIT = "7b91c1eda46e1107a53831cd3f14f608b4b7bad0"
GITHUB_REPOSITORY = "hdduong/aws-idp-custom-platform"
SPEC_KIT_SKILLS = {
    "speckit-analyze",
    "speckit-checklist",
    "speckit-clarify",
    "speckit-constitution",
    "speckit-converge",
    "speckit-implement",
    "speckit-plan",
    "speckit-specify",
    "speckit-tasks",
    "speckit-taskstoissues",
}


def repository_files(ignored_roots: set[str]) -> list[Path]:
    files: list[Path] = []
    for directory, subdirectories, filenames in os.walk(ROOT):
        subdirectories[:] = [name for name in subdirectories if name not in ignored_roots]
        base = Path(directory)
        files.extend(base / name for name in filenames)
    return files


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalized_text_sha256(path: Path) -> str:
    """Hash reviewed text with stable newlines across Git checkout platforms."""

    content = path.read_text(encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_workflow_actions(value: Any, path: Path) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses" and isinstance(child, str) and not child.startswith("./"):
                require(
                    re.search(r"@[0-9a-f]{40}$", child) is not None,
                    f"Workflow action must be pinned to an immutable commit SHA in {path}: {child}",
                )
            validate_workflow_actions(child, path)
    elif isinstance(value, list):
        for child in value:
            validate_workflow_actions(child, path)


def workflow_trigger_names(value: Any, path: Path) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        require(all(isinstance(item, str) for item in value), f"Invalid workflow trigger list: {path}")
        return set(value)
    if isinstance(value, dict):
        require(all(isinstance(item, str) for item in value), f"Invalid workflow trigger map: {path}")
        return set(value)
    raise ValueError(f"Workflow must declare an on trigger: {path}")


def resolve_repository_path(base: Path, target: str, label: str) -> Path:
    require(not PurePosixPath(target).is_absolute(), f"Absolute {label}: {target}")
    require(not PureWindowsPath(target).is_absolute(), f"Absolute {label}: {target}")
    resolved = (base / target).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as error:
        raise ValueError(f"{label.capitalize()} escapes the repository: {target}") from error
    return resolved


def validate_copilot_review_gate() -> None:
    path = ROOT / ".github" / "workflows" / "copilot-review.yml"
    with path.open("r", encoding="utf-8") as handle:
        workflow = yaml.load(handle, Loader=yaml.BaseLoader)
    require(isinstance(workflow, dict), f"Invalid Copilot review workflow: {path}")
    triggers = workflow.get("on")
    require(isinstance(triggers, dict), "Copilot review gate must declare explicit triggers.")
    trigger_names = workflow_trigger_names(triggers, path)
    require(trigger_names == {"pull_request"}, "Copilot review gate must run only for pull requests.")
    pull_request = triggers["pull_request"]
    require(isinstance(pull_request, dict), "Copilot pull_request trigger must specify event types.")
    require(
        set(pull_request.get("types", [])) == {"opened", "reopened", "synchronize", "ready_for_review"},
        "Copilot review gate trigger types changed.",
    )

    permissions = workflow.get("permissions")
    require(
        permissions == {"contents": "read", "pull-requests": "read"},
        "Copilot review gate must remain metadata-only and read-only.",
    )
    jobs = workflow.get("jobs")
    require(isinstance(jobs, dict) and set(jobs) == {"copilot-review"}, "Unexpected Copilot jobs.")
    job = jobs["copilot-review"]
    require(job.get("timeout-minutes") == "20", "Copilot review gate timeout changed.")
    steps = job.get("steps")
    require(isinstance(steps, list) and len(steps) == 1, "Copilot review gate must have one metadata step.")
    step = steps[0]
    require("uses" not in step, "Copilot review gate must not execute a third-party action or checkout code.")
    script = step.get("run", "")
    for required_fragment in (
        "copilot-pull-request-reviewer[bot]",
        ".state == \\\"COMMENTED\\\"",
        ".commit_id == \\\"${HEAD_SHA}\\\"",
        "pulls/${PR_NUMBER}/reviews",
        "exit 1",
    ):
        require(required_fragment in script, f"Copilot exact-head gate is missing: {required_fragment}")


def validate_python_quality_gate() -> None:
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
    require("coverage==7.15.2" in requirements, "Coverage.py must remain pinned.")
    require("pytest-cov==7.1.0" in requirements, "pytest-cov must remain pinned.")

    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    coverage = project.get("tool", {}).get("coverage", {})
    run = coverage.get("run", {})
    require(run.get("branch") is True, "Python coverage must collect branch data.")
    require(run.get("source") == ["services"], "Python coverage must include every service module.")

    checker = (ROOT / "scripts" / "check-python-coverage.py").read_text(encoding="utf-8")
    require("MINIMUM_LINE_COVERAGE = 80.0" in checker, "Python per-file coverage floor changed.")
    require("PRODUCTION_ROOT = PurePosixPath(\"services\")" in checker, "Python coverage scope changed.")

    for workflow_name in ("validate.yml", "deploy-prod.yml"):
        workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
        for fragment in (
            "--cov=services",
            "--cov-branch",
            "--cov-report=json:coverage.json",
            "python scripts/check-python-coverage.py coverage.json",
        ):
            require(fragment in workflow, f"{workflow_name} does not enforce Python coverage: {fragment}")


def validate_web_quality_gate() -> None:
    web = ROOT / "apps" / "web"
    package_path = web / "package.json"
    authored_source = any(
        path.is_file()
        for directory in (web / "src", web / "e2e")
        if directory.exists()
        for path in directory.rglob("*")
    )
    require(not authored_source or package_path.is_file(), "React source exists without apps/web/package.json.")

    validate_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for fragment in (
        "npm audit --audit-level=high",
        "npm run typecheck",
        "npm run test:coverage",
        "npx playwright install --with-deps chromium",
        "npm run test:e2e:ci",
    ):
        require(fragment in validate_workflow, f"React validation workflow lacks: {fragment}")

    deploy_workflow = (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(encoding="utf-8")
    deploy_all = (ROOT / "scripts" / "deploy-all.ps1").read_text(encoding="utf-8")
    deploy_web = (ROOT / "scripts" / "deploy-web.ps1").read_text(encoding="utf-8")
    require("npm audit --audit-level=high" in deploy_web, "Production web deployment lacks the dependency vulnerability gate.")
    for prohibited in ("skip_ui_tests", "SKIP_UI_TESTS", "SkipUiTests"):
        require(prohibited not in deploy_workflow + deploy_all, f"Production UI test bypass remains: {prohibited}")
    require("SkipTests" not in deploy_web, "Production web deployment must not permit skipping tests.")
    require("--if-present" not in deploy_web, "Production web tests must fail closed when scripts are missing.")
    for fragment in ("npm run typecheck", "npm run test:coverage", "npm run test:e2e:ci"):
        require(fragment in deploy_web, f"Production web deployment lacks: {fragment}")

    if not package_path.is_file():
        return

    package = load_json(package_path)
    require((web / "package-lock.json").is_file(), "React package-lock.json is required.")
    scripts = package.get("scripts", {})
    for name in ("lint", "typecheck", "test:coverage", "build", "test:e2e", "test:e2e:ci"):
        require(isinstance(scripts.get(name), str) and scripts[name], f"React package script is required: {name}")
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    for name in ("@axe-core/playwright", "@playwright/test", "@vitest/coverage-v8", "msw"):
        require(name in dependencies, f"React test dependency is required: {name}")

    vitest_path = web / "vitest.config.ts"
    playwright_path = web / "playwright.config.ts"
    require(vitest_path.is_file(), "React Vitest coverage configuration is required.")
    require(playwright_path.is_file(), "React Playwright configuration is required.")
    vitest = vitest_path.read_text(encoding="utf-8")
    require(re.search(r"perFile\s*:\s*true", vitest) is not None, "Vitest coverage must be per-file.")
    for metric in ("lines", "statements", "functions", "branches"):
        require(
            re.search(rf"{metric}\s*:\s*(?:8[0-9]|9[0-9]|100)\b", vitest) is not None,
            f"Vitest per-file {metric} coverage must be at least 80%.",
        )
    require(any((web / "e2e").rglob("*.spec.ts")), "At least one Playwright integration test is required.")


def validate_azure_control_plane() -> None:
    """Keep Azure as the sole public API and AWS as a private headless data plane."""

    feature = load_json(ROOT / ".specify" / "feature.json")
    require(
        feature.get("feature_directory") == "specs/002-azure-api-control-plane",
        "The Azure API control-plane packet must remain the active feature.",
    )

    for relative_path in (
        "infra/azure/main.bicep",
        "infra/azure/acr-build-api.yml",
        "services/azure_api/main.py",
        "services/azure_api/auth.py",
        "services/azure_api/aws_credentials.py",
        "services/azure_api/settings.py",
        "services/azure_api/Dockerfile",
        "services/azure_api/requirements.txt",
        "scripts/deploy-azure.ps1",
        "scripts/deploy-all.ps1",
        "scripts/deploy-web.ps1",
        "scripts/cutover-api-domain.ps1",
        "scripts/provision-entra-federation.ps1",
    ):
        require((ROOT / relative_path).is_file(), f"Azure control-plane artifact is missing: {relative_path}")

    dockerfile = (ROOT / "services" / "azure_api" / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for sensitive_pattern in ("**/.env", "**/*.pem", "**/*.key", "**/*.pfx", "**/*.pdf"):
        require(
            sensitive_pattern in dockerignore,
            f"ACR build context does not exclude sensitive file pattern: {sensitive_pattern}",
        )
    require(
        re.search(r"^FROM\s+python:[^\s]+@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE) is not None,
        "Azure API base image must be pinned by immutable digest.",
    )
    require(
        "FROM python:3.13.14-slim-bookworm@sha256:9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64"
        in dockerfile,
        "Azure API must use the reviewed security-clean Python base digest.",
    )
    require("USER 10001:10001" in dockerfile, "Azure API container must run as the dedicated non-root user.")
    require("--no-access-log" in dockerfile, "Azure API must not log business identifiers from raw request paths.")
    require(
        "--mount=type=secret,id=enterprise_ca,required=false" in dockerfile,
        "Enterprise CA support must use an ephemeral BuildKit secret.",
    )
    for frontend in re.findall(r"^#\s*syntax\s*=\s*(\S+)\s*$", dockerfile, re.MULTILINE):
        require(
            re.search(r"@sha256:[0-9a-f]{64}$", frontend) is not None,
            "A Dockerfile syntax frontend must be pinned by immutable digest.",
        )
    for prohibited in ("--trusted-host", "PIP_TRUSTED_HOST", "PIP_NO_VERIFY"):
        require(prohibited not in dockerfile, f"Docker TLS verification bypass is prohibited: {prohibited}")

    acr_task_path = ROOT / "infra" / "azure" / "acr-build-api.yml"
    acr_task = yaml.safe_load(acr_task_path.read_text(encoding="utf-8"))
    require(isinstance(acr_task, dict), "The ACR API image task must be a YAML mapping.")
    require(acr_task.get("version") == "v1.1.0", "The ACR API image task must use schema version v1.1.0.")
    task_environment = acr_task.get("env")
    require(
        isinstance(task_environment, list) and "DOCKER_BUILDKIT=1" in task_environment,
        "The ACR API image task must explicitly enable BuildKit.",
    )
    task_steps = acr_task.get("steps")
    require(isinstance(task_steps, list), "The ACR API image task must define ordered steps.")
    expected_acr_image = "$Registry/{{.Values.image}}"
    build_positions = [
        index for index, step in enumerate(task_steps) if isinstance(step, dict) and "build" in step
    ]
    push_positions = [
        index for index, step in enumerate(task_steps) if isinstance(step, dict) and "push" in step
    ]
    require(len(build_positions) == 1, "The ACR API image task must define exactly one build step.")
    require(len(push_positions) == 1, "The ACR API image task must define exactly one push step.")
    require(build_positions[0] < push_positions[0], "The ACR API image task must push only after building.")
    build_command = " ".join(str(task_steps[build_positions[0]]["build"]).split())
    require(
        f"--tag {expected_acr_image}" in build_command
        and "--file services/azure_api/Dockerfile" in build_command
        and build_command.endswith(" ."),
        "The ACR API image task must build the reviewed Dockerfile from the repository root.",
    )
    pushed_images = task_steps[push_positions[0]]["push"]
    require(
        isinstance(pushed_images, list) and pushed_images == [expected_acr_image],
        "The ACR API image task must explicitly push the parameterized registry image.",
    )

    runtime_requirements = (ROOT / "services" / "azure_api" / "requirements.txt").read_text(encoding="utf-8")
    development_requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for required_pin in (
        "azure-identity==1.25.3",
        "boto3==1.43.49",
        "fastapi==0.139.1",
        "PyJWT[crypto]==2.13.0",
        "starlette==1.3.1",
        "uvicorn==0.51.0",
    ):
        require(required_pin in runtime_requirements, f"Reviewed Azure runtime pin is missing: {required_pin}")
    for required_pin in (
        "azure-identity==1.25.3",
        "boto3==1.43.49",
        "fastapi==0.139.1",
        "httpx2==2.7.0",
        "PyJWT[crypto]==2.13.0",
        "starlette==1.3.1",
    ):
        require(required_pin in development_requirements, f"Development/runtime pin is inconsistent: {required_pin}")
    require(
        "httpx==0.28.1" not in development_requirements.splitlines(),
        "Starlette 1.3.1 TestClient must use its preferred httpx2 compatibility package.",
    )

    for retired_path in ("scripts/deploy-edge.ps1", "infra/edge/template.yaml"):
        require(
            not (ROOT / retired_path).exists(),
            f"Retired AWS edge deployment source must not remain runnable: {retired_path}",
        )

    lock = load_json(ROOT / "vendor" / "idp.lock.json")
    require(lock.get("deploymentMode") == "headless", "The pinned IDP deployment must remain headless.")
    idp_deploy = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    require("--headless" in idp_deploy, "The IDP deployment script must enforce --headless.")

    aws_template = (ROOT / "infra" / "api" / "template.yaml").read_text(encoding="utf-8")
    for prohibited in (
        "AWS::ApiGateway",
        "AWS::AppSync",
        "AWS::CloudFront",
        "LoanApiFunction:",
        "appsync:",
    ):
        require(prohibited not in aws_template, f"Obsolete AWS public API surface remains: {prohibited}")
    for required_fragment in (
        "EntraTenantOidcProvider:",
        "AzureApiRuntimeRole:",
        "RolePermissionsBoundaryArn:",
        "PermissionsBoundary: !Ref RolePermissionsBoundaryArn",
        "sts:AssumeRoleWithWebIdentity",
        "/:aud",
        "/:sub",
        "${SourceBucket.Arn}/quarantine/tenants/*",
        "prefix: quarantine/tenants/",
        "UploadCompletionStreamMapping:",
        "Type: AWS::Lambda::EventSourceMapping",
        "EventSourceArn: !GetAtt RegistryTable.StreamArn",
        "dynamodb:GetRecords",
        "BisectBatchOnFunctionError: true",
        "StartingPosition: TRIM_HORIZON",
        "Destination: !GetAtt UploadProcessorDlq.Arn",
        "ReportBatchItemFailures",
    ):
        require(required_fragment in aws_template, f"AWS federation template lacks: {required_fragment}")
    require(
        aws_template.count("PermissionsBoundary: !Ref RolePermissionsBoundaryArn") == 5,
        "Every platform-created IAM role must use the bootstrap permissions boundary.",
    )

    bootstrap_template = (ROOT / "infra" / "bootstrap" / "template.yaml").read_text(encoding="utf-8")
    for required_fragment in (
        "PlatformCloudFormationExecutionRole:",
        "IdpCloudFormationExecutionRole:",
        "PlatformRolePermissionsBoundary:",
        "IdpRolePermissionsBoundary:",
        "iam:PermissionsBoundary:",
        "iam:PolicyARN:",
        "iam:PassedToService:",
        "DenyPlatformBoundaryRemoval",
        "DenyIdpBoundaryRemoval",
        "stack/${PlatformStackName}/*",
        "stack/${IdpStackName}/*",
    ):
        require(required_fragment in bootstrap_template, f"AWS deployment least privilege lacks: {required_fragment}")
    require(
        "\n  CloudFormationExecutionRole:\n" not in bootstrap_template,
        "The obsolete shared CloudFormation execution role must not be restored.",
    )

    stack_policy = load_json(ROOT / "infra" / "stack-policies" / "protect-stateful-resources.json")
    protected_types: set[str] = set()
    protected_actions: set[str] = set()
    for statement in stack_policy.get("Statement", []):
        if statement.get("Effect") != "Deny":
            continue
        actions = statement.get("Action", [])
        protected_actions.update([actions] if isinstance(actions, str) else actions)
        protected_types.update(statement.get("Condition", {}).get("StringEquals", {}).get("ResourceType", []))
    require(
        {"Update:Delete", "Update:Replace"}.issubset(protected_actions),
        "Stateful stack policy must deny replacement and deletion.",
    )
    require(
        {"AWS::DynamoDB::Table", "AWS::KMS::Key", "AWS::S3::Bucket"}.issubset(protected_types),
        "Stateful stack policy must protect DynamoDB, KMS, and S3 resource types.",
    )

    deploy_platform = (ROOT / "scripts" / "deploy-platform.ps1").read_text(encoding="utf-8")
    deploy_idp = (ROOT / "scripts" / "deploy-idp.ps1").read_text(encoding="utf-8")
    for required_fragment in (
        "PlatformCloudFormationExecutionRoleArn",
        "PlatformRolePermissionsBoundaryArn",
        "Set-AwsStatefulStackPolicy",
    ):
        require(required_fragment in deploy_platform, f"Platform deployment gate lacks: {required_fragment}")
    for required_fragment in (
        "IdpCloudFormationExecutionRoleArn",
        "IdpRolePermissionsBoundaryArn",
        "PermissionsBoundaryArn=",
        "Set-AwsStatefulStackPolicy",
    ):
        require(required_fragment in deploy_idp, f"IDP deployment gate lacks: {required_fragment}")

    loan_runtime = (ROOT / "services" / "loan_api" / "app.py").read_text(encoding="utf-8")
    require(
        'key = f"quarantine/tenants/' in loan_runtime,
        "New source uploads must use the GuardDuty-protected top-level quarantine prefix.",
    )
    for prohibited in ("boto3.resource(", "boto3.client("):
        require(prohibited not in loan_runtime, f"Loan domain constructs an ambient AWS dependency: {prohibited}")
    for required_fragment in (
        "connect_timeout=3",
        "read_timeout=10",
        'retries={"mode": "standard", "total_max_attempts": 3}',
        "tcp_keepalive=True",
        "MAXIMUM_QUERY_ITEMS",
        "MAXIMUM_LOAN_ARCHIVE_DOCUMENTS",
        "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES",
    ):
        require(required_fragment in loan_runtime, f"Loan runtime hardening lacks: {required_fragment}")

    azure_bicep = (ROOT / "infra" / "azure" / "main.bicep").read_text(encoding="utf-8")
    for required_fragment in (
        "Microsoft.App/containerApps",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.ContainerRegistry/registries",
        "Microsoft.Web/staticSites",
        "apiCustomDomainCertificateId",
        "customDomains:",
        "param maximumQueryItems int = 5000",
        "param maximumLoanArchiveDocuments int = 500",
        "param maximumLoanArchiveManifestBytes int = 4194304",
        "name: 'MAXIMUM_QUERY_ITEMS'",
        "name: 'MAXIMUM_LOAN_ARCHIVE_DOCUMENTS'",
        "name: 'MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES'",
    ):
        require(required_fragment in azure_bicep, f"Azure Bicep lacks: {required_fragment}")

    azure_runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "services" / "azure_api").glob("*.py")
    )
    require("HOST_NOT_ALLOWED" in azure_runtime, "Production product routes must enforce the custom API hostname.")
    for prohibited in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "LOAN_API_ORIGIN_VERIFY_SECRET",
        "AppSync",
    ):
        require(prohibited not in azure_runtime, f"Azure runtime contains a prohibited integration: {prohibited}")

    active_deployment_files = [
        *sorted((ROOT / "scripts").glob("*.ps1")),
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
        ROOT / "infra" / "bootstrap" / "template.yaml",
    ]
    active_deployment = "\n".join(
        path.read_text(encoding="utf-8") for path in active_deployment_files if path.is_file()
    )
    for prohibited in (
        "LOAN_API_ORIGIN_VERIFY_SECRET",
        "OriginVerifySecret",
        "deploy-edge.ps1",
        "SkipEdge",
        "cloudfront:*",
        "apigateway:*",
        "wafv2:*",
        "wafv2.amazonaws.com",
    ):
        require(
            prohibited not in active_deployment,
            f"Retired AWS public-edge integration remains runnable: {prohibited}",
        )

    deploy_all = (ROOT / "scripts" / "deploy-all.ps1").read_text(encoding="utf-8")
    for required_fragment in ("deploy-azure.ps1", "deploy-platform.ps1"):
        require(required_fragment in deploy_all, f"Deployment orchestrator lacks: {required_fragment}")
    deploy_web = (ROOT / "scripts" / "deploy-web.ps1").read_text(encoding="utf-8")
    require("staticwebapp" in deploy_web.lower(), "Web deployment must target Azure Static Web Apps.")
    for prohibited in ("cloudfront", "s3 sync", "UiDistributionId"):
        require(prohibited.lower() not in deploy_web.lower(), f"Web deployment still targets AWS edge hosting: {prohibited}")

    deploy_azure = (ROOT / "scripts" / "deploy-azure.ps1").read_text(encoding="utf-8")
    for required_fragment in (
        "az acr run",
        "infra/azure/acr-build-api.yml",
        '--set "image=${ImageRepository}:$ImageTag"',
        "trivy image",
        "--severity HIGH,CRITICAL",
        "--ignore-unfixed",
        "--format cyclonedx",
        "Production deployment cannot skip",
        "Get-LiveApiCustomDomainBinding",
        "dnsCutoverPerformed",
        "maximumQueryItems",
        "maximumLoanArchiveDocuments",
        "maximumLoanArchiveManifestBytes",
    ):
        require(required_fragment in deploy_azure, f"Exact-image production gate lacks: {required_fragment}")
    require("az acr build" not in deploy_azure, "Production image builds must use the explicit BuildKit ACR task.")
    cutover = (ROOT / "scripts" / "cutover-api-domain.ps1").read_text(encoding="utf-8")
    require("azure.api.imageScan" in cutover, "API DNS cutover must verify exact-image scan evidence.")
    trivy_installer = (ROOT / "scripts" / "install-trivy.ps1").read_text(encoding="utf-8")
    for required_fragment in (
        "$version = '0.72.0'",
        "$expectedSha256 = 'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'",
        "github.com/aquasecurity/trivy/releases/download/v$version/$assetName",
        "Get-FileHash -LiteralPath $archivePath -Algorithm SHA256",
    ):
        require(required_fragment in trivy_installer, f"Trivy installer pin lacks: {required_fragment}")
    production_workflow = (ROOT / ".github" / "workflows" / "deploy-prod.yml").read_text(encoding="utf-8")
    for required_fragment in (
        "run: ./scripts/install-trivy.ps1",
    ):
        require(required_fragment in production_workflow, f"Production scanner pin lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in production_workflow,
        "Production workflow uses an action outside the repository's selected-action allowlist.",
    )
    validation_workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    for required_fragment in (
        "run: ./scripts/install-trivy.ps1",
        "trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1",
        "trivy image --format cyclonedx",
    ):
        require(required_fragment in validation_workflow, f"Pull-request image gate lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in validation_workflow,
        "Validation workflow uses an action outside the repository's selected-action allowlist.",
    )
    validation_document = yaml.load(validation_workflow, Loader=yaml.BaseLoader)
    validation_jobs = validation_document.get("jobs", {}) if isinstance(validation_document, dict) else {}
    require(isinstance(validation_jobs, dict), "Validation workflow jobs must be a mapping.")
    docker_build_steps = [
        step
        for job in validation_jobs.values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and "docker build" in str(step.get("run", ""))
    ]
    require(len(docker_build_steps) == 1, "Validation must contain exactly one Docker image build step.")
    build_environment = docker_build_steps[0].get("env", {})
    require(
        isinstance(build_environment, dict) and build_environment.get("DOCKER_BUILDKIT") == "1",
        "Validation Docker builds must explicitly enable BuildKit.",
    )


def validate_markdown_links(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    for raw_target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        relative_target = unquote(target.split("#", 1)[0])
        resolved_target = resolve_repository_path(path.parent, relative_target, "Markdown link")
        require(
            resolved_target.exists(),
            f"Broken local Markdown link in {path}: {target}",
        )


def validate_spec_kit() -> None:
    lock = load_json(ROOT / "vendor" / "spec-kit.lock.json")
    require(lock["repository"] == "https://github.com/github/spec-kit", "Unexpected Spec Kit source.")
    require(lock["version"] == SPEC_KIT_VERSION, "Spec Kit version changed without review.")
    require(lock["tag"] == f"v{SPEC_KIT_VERSION}", "Spec Kit tag does not match its version.")
    require(lock["commit"] == SPEC_KIT_COMMIT, "Spec Kit commit changed without review.")
    require(lock["integration"] == "claude", "Spec Kit must use the Claude integration.")
    require(lock["script"] == "ps", "Spec Kit must use PowerShell scripts on this repository.")
    require(lock["aiSkills"] is True, "Spec Kit Claude skills must remain enabled.")

    init_options = load_json(ROOT / ".specify" / "init-options.json")
    require(init_options["speckit_version"] == SPEC_KIT_VERSION, "Generated Spec Kit version drifted.")
    require(init_options["integration"] == "claude", "Generated integration must remain Claude.")
    require(init_options["ai_skills"] is True, "Claude skills must remain enabled.")
    require(init_options["script"] == "ps", "Generated scripts must remain PowerShell.")

    active_feature = load_json(ROOT / ".specify" / "feature.json")
    active_feature_path = active_feature.get("feature_directory")
    require(isinstance(active_feature_path, str) and active_feature_path, "Active feature path is missing.")
    active_feature_dir = resolve_repository_path(ROOT, active_feature_path, "active feature path")
    require(
        active_feature_dir.is_relative_to((ROOT / "specs").resolve()),
        "The active feature must remain under specs/.",
    )
    require(active_feature_dir.is_dir(), f"Active feature directory is missing: {active_feature_path}")
    for required_name in ("spec.md", "plan.md", "tasks.md"):
        require(
            (active_feature_dir / required_name).is_file(),
            f"Active feature is missing {required_name}: {active_feature_path}",
        )

    shared_assets = [
        ".specify/integration.json",
        ".specify/integrations/claude.manifest.json",
        ".specify/integrations/speckit.manifest.json",
        ".specify/memory/constitution.md",
        ".specify/scripts/powershell/check-prerequisites.ps1",
        ".specify/scripts/powershell/common.ps1",
        ".specify/scripts/powershell/create-new-feature.ps1",
        ".specify/scripts/powershell/setup-plan.ps1",
        ".specify/scripts/powershell/setup-tasks.ps1",
        ".specify/templates/checklist-template.md",
        ".specify/templates/constitution-template.md",
        ".specify/templates/plan-template.md",
        ".specify/templates/spec-template.md",
        ".specify/templates/tasks-template.md",
        ".specify/workflows/speckit/workflow.yml",
        ".specify/workflows/workflow-registry.json",
    ]
    for relative_path in shared_assets:
        require((ROOT / relative_path).is_file(), f"Missing Spec Kit shared asset: {relative_path}")

    for manifest_name, expected_integration in (
        ("claude.manifest.json", "claude"),
        ("speckit.manifest.json", "speckit"),
    ):
        manifest_path = ROOT / ".specify" / "integrations" / manifest_name
        manifest = load_json(manifest_path)
        require(manifest["integration"] == expected_integration, f"Integration mismatch in {manifest_path}")
        require(manifest["version"] == SPEC_KIT_VERSION, f"Version mismatch in {manifest_path}")
        require(isinstance(manifest.get("files"), dict), f"Missing generated file map in {manifest_path}")
        for relative_path, expected_sha256 in manifest["files"].items():
            generated_path = ROOT / relative_path
            require(generated_path.is_file(), f"Missing generated Spec Kit file: {relative_path}")
            require(
                normalized_text_sha256(generated_path) == expected_sha256,
                f"Generated Spec Kit file differs from its manifest: {relative_path}",
            )

    for skill_name in SPEC_KIT_SKILLS:
        path = ROOT / ".claude" / "skills" / skill_name / "SKILL.md"
        require(path.is_file(), f"Missing Claude Code skill: {path}")
        content = path.read_text(encoding="utf-8")
        require(content.startswith("---\n") or content.startswith("---\r\n"), f"Missing skill frontmatter: {path}")
        parts = re.split(r"^---\s*$", content, maxsplit=2, flags=re.MULTILINE)
        require(len(parts) >= 3, f"Invalid skill frontmatter: {path}")
        metadata = yaml.safe_load(parts[1])
        require(isinstance(metadata, dict), f"Invalid skill metadata: {path}")
        require(metadata.get("name") == skill_name, f"Claude skill name mismatch: {path}")
        require(metadata.get("user-invocable") is True, f"Claude skill must be user-invocable: {path}")
        require(
            metadata.get("disable-model-invocation") is False,
            f"Claude skill must allow model invocation: {path}",
        )
        for unresolved_token in ("{SCRIPT}", "{ARGS}", "__AGENT__", "__SPECKIT_COMMAND_"):
            require(unresolved_token not in content, f"Unrendered integration token in {path}")

    authored_artifacts = [
        "specs/README.md",
        "specs/001-loan-document-platform/spec.md",
        "specs/001-loan-document-platform/plan.md",
        "specs/001-loan-document-platform/research.md",
        "specs/001-loan-document-platform/data-model.md",
        "specs/001-loan-document-platform/quickstart.md",
        "specs/001-loan-document-platform/tasks.md",
        "specs/001-loan-document-platform/contracts/README.md",
        "specs/001-loan-document-platform/checklists/requirements.md",
        "specs/001-loan-document-platform/checklists/security.md",
        "specs/001-loan-document-platform/checklists/production-readiness.md",
        "specs/002-azure-api-control-plane/spec.md",
        "specs/002-azure-api-control-plane/plan.md",
        "specs/002-azure-api-control-plane/research.md",
        "specs/002-azure-api-control-plane/data-model.md",
        "specs/002-azure-api-control-plane/quickstart.md",
        "specs/002-azure-api-control-plane/tasks.md",
        "specs/002-azure-api-control-plane/contracts/README.md",
        "specs/002-azure-api-control-plane/checklists/requirements.md",
        ".claude/README.md",
        ".specify/README.md",
        ".github/copilot-instructions.md",
        "docs/spec-driven-development.md",
    ]
    unresolved_tokens = (
        "[FEATURE NAME]",
        "[###-feature-name]",
        "[YYYY-MM-DD]",
        "[NEEDS CLARIFICATION",
        "[Link to research.md]",
    )
    for relative_path in authored_artifacts:
        path = ROOT / relative_path
        require(path.is_file(), f"Missing project-owned specification artifact: {relative_path}")
        content = path.read_text(encoding="utf-8")
        for unresolved_token in unresolved_tokens:
            require(unresolved_token not in content, f"Unresolved template token in {path}: {unresolved_token}")
        validate_markdown_links(path)

    constitution = (ROOT / ".specify" / "memory" / "constitution.md").read_text(encoding="utf-8")
    version_match = re.search(r"\*\*Version\*\*: (\d+)\.(\d+)\.(\d+)", constitution)
    require(version_match is not None, "Project constitution must declare a semantic version.")
    constitution_version = tuple(int(part) for part in version_match.groups())
    require(constitution_version >= (1, 2, 0), "Project constitution predates mandatory coverage gates.")
    require("Mandatory Exact-Head Copilot Review" in constitution, "Constitution lacks Copilot governance.")
    require("Mandatory Coverage and Browser Integration" in constitution, "Constitution lacks coverage governance.")


def main() -> None:
    ignored_roots = {
        ".git",
        ".agents",
        ".codex",
        ".local",
        ".venv",
        "venv",
        "__pycache__",
        "work",
        "outputs",
        "node_modules",
    }
    files = repository_files(ignored_roots)
    for path in (item for item in files if item.suffix.lower() == ".json"):
        load_json(path)

    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        with path.open("r", encoding="utf-8") as handle:
            workflow = yaml.load(handle, Loader=yaml.BaseLoader)
        require(isinstance(workflow, dict), f"Invalid GitHub workflow: {path}")
        trigger_names = workflow_trigger_names(workflow.get("on"), path)
        require("pull_request_target" not in trigger_names, f"Unsafe pull_request_target trigger in {path}")
        validate_workflow_actions(workflow, path)

    validate_copilot_review_gate()
    validate_python_quality_gate()
    validate_web_quality_gate()
    validate_azure_control_plane()

    environment_example = load_json(ROOT / "config" / "environments" / "prod.example.json")
    require(
        environment_example["repositoryName"] == GITHUB_REPOSITORY.split("/", 1)[1],
        "Production example must use the canonical GitHub repository name.",
    )
    require(environment_example.get("azureMonthlyBudgetUsd", 0) >= 1, "Azure budget must be explicit.")
    require(environment_example.get("azureContainerAppsZoneRedundant") is True, "Production Container Apps must be zone redundant.")
    require(environment_example.get("azureApiMinReplicas", 0) >= 2, "Production API must keep at least two replicas.")
    require(100 <= environment_example.get("maximumQueryItems", 0) <= 100_000, "Production query limit is invalid.")
    require(
        1 <= environment_example.get("maximumLoanArchiveDocuments", 0) <= environment_example["maximumQueryItems"],
        "Production archive document limit is invalid.",
    )
    require(
        1024 <= environment_example.get("maximumLoanArchiveManifestBytes", 0) <= 20 * 1024 * 1024,
        "Production archive manifest limit is invalid.",
    )
    for relative_path in ("README.md", "docs/github-delivery.md"):
        content = (ROOT / relative_path).read_text(encoding="utf-8")
        require("hdduong/loan-document-platform" not in content, f"Old GitHub slug remains in {relative_path}")
        require(GITHUB_REPOSITORY in content, f"Canonical GitHub repository missing from {relative_path}")

    protection_script = (ROOT / "scripts" / "configure-github-protection.ps1").read_text(
        encoding="utf-8"
    )
    for required_fragment in (
        "Mandatory Copilot review",
        "review_draft_pull_requests = $true",
        "review_on_push = $true",
        "contexts = @('validate', 'copilot-review')",
        "azure/login@a457da9ea143d694b1b9c7c869ebb04ebe844ef5",
    ):
        require(required_fragment in protection_script, f"GitHub protection lacks: {required_fragment}")
    require(
        "aquasecurity/setup-trivy@" not in protection_script,
        "GitHub protection still allows the retired setup-trivy action.",
    )

    runtime_schema = load_json(ROOT / "contracts" / "runtime-config.schema.json")
    runtime_example = load_json(ROOT / "apps" / "web" / "public" / "runtime-config.example.json")
    Draft202012Validator(runtime_schema, format_checker=FormatChecker()).validate(runtime_example)

    manifest = load_json(IDP_DIR / "manifest.json")
    for name in ("screen", "full"):
        entry = manifest[name]
        path = IDP_DIR / entry["file"]
        require(path.is_file(), f"Missing {name} configuration: {path}")
        require(
            normalized_text_sha256(path) == entry["sourceSha256"],
            f"{path.name} differs from its reviewed manifest digest; regenerate/review the manifest intentionally.",
        )

    screen = load_json(IDP_DIR / manifest["screen"]["file"])
    require(screen["ocr"]["backend"] == "textract", "Screen OCR backend must be Textract.")
    require(screen["ocr"]["features"] == [], "Screen OCR features must remain empty (DetectDocumentText).")
    classification = screen["classification"]
    require(
        classification["maxPagesForClassification"] == "ALL",
        "Screen classification must inspect every package page.",
    )
    require(
        classification["classificationMethod"] == "multimodalPageLevelClassification",
        "Screen classification method changed.",
    )
    require(classification["sectionSplitting"] == "llm_determined", "Screen section splitting changed.")
    require(classification["contextPagesCount"] == "1", "Screen context page count changed.")
    require(
        classification["model"] == "us.amazon.nova-2-lite-v1:0",
        "Screen classification must use the reviewed Nova Lite profile.",
    )
    require(
        screen["extraction"]["model"] == "us.amazon.nova-2-lite-v1:0",
        "Screen evidence extraction must use the reviewed Nova Lite profile.",
    )
    require(screen["assessment"]["enabled"] is False, "Screen assessment must remain disabled.")
    require(screen["evaluation"]["enabled"] is False, "Screen evaluation must remain disabled.")

    closing_disclosure = next(
        item for item in screen["classes"] if item["$id"] == "L053_Closing_Disclosure"
    )
    require(
        len(closing_disclosure["properties"]) == 13,
        "Screen Closing Disclosure schema must remain the reviewed 13-field evidence schema.",
    )

    lock = load_json(ROOT / "vendor" / "idp.lock.json")
    require(lock["version"] == "0.5.16", "IDP version changed without an explicit upgrade review.")
    require(
        lock["commit"] == "1463fb6ff91c9e0169a148b33e6bc85d12bab995",
        "IDP commit changed without an explicit upgrade review.",
    )

    validate_spec_kit()

    prohibited_suffixes = {".pdf", ".tif", ".tiff", ".pfx", ".p12", ".pem", ".key"}
    secret_patterns = {
        "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    }
    for path in files:
        require(path.suffix.lower() not in prohibited_suffixes, f"Prohibited sensitive/binary file: {path}")
        if path.stat().st_size > 5 * 1024 * 1024:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in secret_patterns.items():
            require(pattern.search(content) is None, f"Possible {label} in public source file: {path}")

    print("Repository configuration invariants passed.")


if __name__ == "__main__":
    main()
