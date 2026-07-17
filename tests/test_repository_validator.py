from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_validator():
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate-repository.py"
    spec = importlib.util.spec_from_file_location("repository_validator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workflow_trigger_names_supports_all_github_yaml_forms() -> None:
    validator = load_validator()
    path = Path("workflow.yml")

    assert validator.workflow_trigger_names("pull_request", path) == {"pull_request"}
    assert validator.workflow_trigger_names(["push", "workflow_dispatch"], path) == {
        "push",
        "workflow_dispatch",
    }
    assert validator.workflow_trigger_names(
        {"pull_request_target": {"types": ["opened"]}}, path
    ) == {"pull_request_target"}


@pytest.mark.parametrize("value", [None, 17, ["push", 17], {17: {}}])
def test_workflow_trigger_names_rejects_invalid_values(value: object) -> None:
    validator = load_validator()

    with pytest.raises(ValueError):
        validator.workflow_trigger_names(value, Path("workflow.yml"))


def test_markdown_links_must_resolve_inside_repository(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    docs = repository / "docs"
    docs.mkdir(parents=True)
    (repository / "README.md").write_text("# Repository\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    markdown = docs / "guide.md"
    monkeypatch.setattr(validator, "ROOT", repository)

    markdown.write_text("[valid](../README.md)\n", encoding="utf-8")
    validator.validate_markdown_links(markdown)

    markdown.write_text("[escape](../../outside.md)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the repository"):
        validator.validate_markdown_links(markdown)

    markdown.write_text(f"[absolute]({outside.as_posix()})\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Absolute Markdown link"):
        validator.validate_markdown_links(markdown)

    encoded_path = docs / "%2e%2e" / "%2e%2e"
    encoded_path.mkdir(parents=True)
    (encoded_path / "outside.md").write_text("# Encoded path\n", encoding="utf-8")
    markdown.write_text(
        "[encoded escape](%2e%2e/%2e%2e/outside.md)\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="escapes the repository"):
        validator.validate_markdown_links(markdown)


def test_active_feature_path_can_change_within_specs(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    feature = repository / "specs" / "002-next-feature"
    feature.mkdir(parents=True)
    for name in ("spec.md", "plan.md", "tasks.md"):
        (feature / name).write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.setattr(validator, "ROOT", repository)

    resolved = validator.resolve_repository_path(
        repository, "specs/002-next-feature", "active feature path"
    )

    assert resolved == feature.resolve()
    assert resolved.is_relative_to((repository / "specs").resolve())


def test_azure_control_plane_rejects_an_aws_public_api(tmp_path: Path, monkeypatch) -> None:
    validator = load_validator()
    repository = tmp_path / "repository"
    files = {
        ".dockerignore": "**/.env\n**/*.pem\n**/*.key\n**/*.pfx\n**/*.pdf\n",
        ".specify/feature.json": '{"feature_directory":"specs/002-azure-api-control-plane"}',
        "vendor/idp.lock.json": '{"deploymentMode":"headless"}',
        "scripts/deploy-idp.ps1": (
            "idp-cli deploy --headless IdpCloudFormationExecutionRoleArn "
            "IdpRolePermissionsBoundaryArn PermissionsBoundaryArn= Set-AwsStatefulStackPolicy"
        ),
        "scripts/deploy-platform.ps1": (
            "PlatformCloudFormationExecutionRoleArn PlatformRolePermissionsBoundaryArn "
            "Set-AwsStatefulStackPolicy"
        ),
        "infra/api/template.yaml": (
            "EntraTenantOidcProvider:\n"
            "AzureApiRuntimeRole:\n"
            "RolePermissionsBoundaryArn:\n"
            + ("PermissionsBoundary: !Ref RolePermissionsBoundaryArn\n" * 5)
            + "sts:AssumeRoleWithWebIdentity\n"
            "sts.windows.net/x/:aud\n"
            "sts.windows.net/x/:sub\n"
            "${SourceBucket.Arn}/quarantine/tenants/*\n"
            "prefix: quarantine/tenants/\n"
            "UploadCompletionStreamMapping:\n"
            "Type: AWS::Lambda::EventSourceMapping\n"
            "EventSourceArn: !GetAtt RegistryTable.StreamArn\n"
            "dynamodb:GetRecords\n"
            "BisectBatchOnFunctionError: true\n"
            "StartingPosition: TRIM_HORIZON\n"
            "Destination: !GetAtt UploadProcessorDlq.Arn\n"
            "ReportBatchItemFailures\n"
            "AWS::ApiGatewayV2::Api"
        ),
        "infra/bootstrap/template.yaml": (
            "PlatformCloudFormationExecutionRole:\nIdpCloudFormationExecutionRole:\n"
            "PlatformRolePermissionsBoundary:\nIdpRolePermissionsBoundary:\n"
            "iam:PermissionsBoundary:\niam:PolicyARN:\niam:PassedToService:\n"
            "DenyPlatformBoundaryRemoval\nDenyIdpBoundaryRemoval\n"
            "stack/${PlatformStackName}/*\nstack/${IdpStackName}/*\n"
        ),
        "infra/stack-policies/protect-stateful-resources.json": (
            '{"Statement":[{"Effect":"Deny","Action":["Update:Delete","Update:Replace"],'
            '"Condition":{"StringEquals":{"ResourceType":["AWS::DynamoDB::Table",'
            '"AWS::KMS::Key","AWS::S3::Bucket"]}}}]}'
        ),
        "infra/azure/main.bicep": (
            "Microsoft.App/containerApps Microsoft.ManagedIdentity/userAssignedIdentities "
            "Microsoft.ContainerRegistry/registries Microsoft.Web/staticSites "
            "apiCustomDomainCertificateId customDomains: "
            "param maximumQueryItems int = 5000 "
            "param maximumLoanArchiveDocuments int = 500 "
            "param maximumLoanArchiveManifestBytes int = 4194304 "
            "name: 'MAXIMUM_QUERY_ITEMS' name: 'MAXIMUM_LOAN_ARCHIVE_DOCUMENTS' "
            "name: 'MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES'"
        ),
        "infra/azure/acr-build-api.yml": (
            "version: v1.1.0\n"
            "env:\n"
            "  - DOCKER_BUILDKIT=1\n"
            "steps:\n"
            "  - build: --tag $Registry/{{.Values.image}} "
            "--file services/azure_api/Dockerfile .\n"
            "  - push:\n"
            "      - $Registry/{{.Values.image}}\n"
        ),
        "services/azure_api/main.py": "# runtime HOST_NOT_ALLOWED",
        "services/azure_api/auth.py": "# auth",
        "services/azure_api/aws_credentials.py": "# federation",
        "services/azure_api/settings.py": "# settings",
        "services/azure_api/Dockerfile": (
            "FROM python:3.13.14-slim-bookworm@sha256:"
            "9d7f287598e1a5a978c015ee176d8216435aaf335ed69ac3c38dd1bbb10e8d64\n"
            "RUN --mount=type=secret,id=enterprise_ca,required=false true\n"
            "USER 10001:10001\n"
            "CMD [\"uvicorn\", \"--no-access-log\"]\n"
        ),
        "services/loan_api/app.py": (
            'key = f"quarantine/tenants/{tenant}/source.pdf"\n'
            "connect_timeout=3 read_timeout=10 tcp_keepalive=True\n"
            'retries={"mode": "standard", "total_max_attempts": 3}\n'
            "MAXIMUM_QUERY_ITEMS MAXIMUM_LOAN_ARCHIVE_DOCUMENTS "
            "MAXIMUM_LOAN_ARCHIVE_MANIFEST_BYTES"
        ),
        "services/azure_api/requirements.txt": (
            "azure-identity==1.25.3\n"
            "boto3==1.43.49\n"
            "fastapi==0.139.1\n"
            "PyJWT[crypto]==2.13.0\n"
            "starlette==1.3.1\n"
            "uvicorn==0.51.0\n"
        ),
        "requirements-dev.txt": (
            "azure-identity==1.25.3\n"
            "boto3==1.43.49\n"
            "fastapi==0.139.1\n"
            "httpx2==2.7.0\n"
            "PyJWT[crypto]==2.13.0\n"
            "starlette==1.3.1\n"
        ),
        "scripts/deploy-azure.ps1": (
            "az acr run --file infra/azure/acr-build-api.yml "
            '--set "image=${ImageRepository}:$ImageTag" '
            "trivy image --severity HIGH,CRITICAL --ignore-unfixed "
            "--format cyclonedx # Production deployment cannot skip "
            "Get-LiveApiCustomDomainBinding dnsCutoverPerformed "
            "maximumQueryItems maximumLoanArchiveDocuments maximumLoanArchiveManifestBytes"
        ),
        "scripts/deploy-all.ps1": "deploy-azure.ps1 deploy-platform.ps1",
        "scripts/deploy-web.ps1": "az staticwebapp deploy",
        "scripts/cutover-api-domain.ps1": "azure.api.imageScan",
        "scripts/install-trivy.ps1": (
            "$version = '0.72.0'\n"
            "$expectedSha256 = 'bbb64b9695866ce4a7a8f5c9592002c5961cab378577fa3f8a040df362b9b2ea'\n"
            "github.com/aquasecurity/trivy/releases/download/v$version/$assetName\n"
            "Get-FileHash -LiteralPath $archivePath -Algorithm SHA256\n"
        ),
        "scripts/provision-entra-federation.ps1": "# provision",
        ".github/workflows/deploy-prod.yml": (
            "run: ./scripts/install-trivy.ps1\n"
        ),
        ".github/workflows/validate.yml": (
            "jobs:\n"
            "  validate:\n"
            "    steps:\n"
            "      - name: Build image\n"
            "        env:\n"
            "          DOCKER_BUILDKIT: '1'\n"
            "        run: docker build --file services/azure_api/Dockerfile .\n"
            "      - run: ./scripts/install-trivy.ps1\n"
            "      - run: >-\n"
            "          trivy image --scanners vuln --severity HIGH,CRITICAL "
            "--ignore-unfixed --exit-code 1 image\n"
            "      - run: trivy image --format cyclonedx image\n"
        ),
    }
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(validator, "ROOT", repository)

    with pytest.raises(ValueError, match="Obsolete AWS public API surface"):
        validator.validate_azure_control_plane()

    template = repository / "infra" / "api" / "template.yaml"
    template.write_text(template.read_text(encoding="utf-8").replace("AWS::ApiGatewayV2::Api", ""), encoding="utf-8")
    validator.validate_azure_control_plane()

    acr_task_path = repository / "infra" / "azure" / "acr-build-api.yml"
    acr_task = acr_task_path.read_text(encoding="utf-8")
    acr_task_path.write_text(acr_task.replace("DOCKER_BUILDKIT=1", "DOCKER_BUILDKIT=0"), encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly enable BuildKit"):
        validator.validate_azure_control_plane()
    acr_task_path.write_text(acr_task, encoding="utf-8")

    push_step = "  - push:\n      - $Registry/{{.Values.image}}\n"
    acr_task_path.write_text(acr_task.replace(push_step, ""), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one push step"):
        validator.validate_azure_control_plane()
    acr_task_path.write_text(acr_task, encoding="utf-8")

    workflow_path = repository / ".github" / "workflows" / "validate.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    workflow_path.write_text(workflow.replace("DOCKER_BUILDKIT: '1'", "DOCKER_BUILDKIT: '0'"), encoding="utf-8")
    with pytest.raises(ValueError, match="Docker builds must explicitly enable BuildKit"):
        validator.validate_azure_control_plane()
    workflow_path.write_text(workflow, encoding="utf-8")

    dockerfile_path = repository / "services" / "azure_api" / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    dockerfile_path.write_text("# syntax=docker/dockerfile:1\n" + dockerfile, encoding="utf-8")
    with pytest.raises(ValueError, match="frontend must be pinned by immutable digest"):
        validator.validate_azure_control_plane()
    dockerfile_path.write_text(dockerfile, encoding="utf-8")

    deploy_path = repository / "scripts" / "deploy-azure.ps1"
    deploy_script = deploy_path.read_text(encoding="utf-8")
    deploy_path.write_text(deploy_script.replace("az acr run", "az acr build"), encoding="utf-8")
    with pytest.raises(ValueError, match="Exact-image production gate lacks"):
        validator.validate_azure_control_plane()
    deploy_path.write_text(deploy_script, encoding="utf-8")

    retired_edge = repository / "scripts" / "deploy-edge.ps1"
    retired_edge.write_text("# legacy", encoding="utf-8")
    with pytest.raises(ValueError, match="Retired AWS edge deployment source"):
        validator.validate_azure_control_plane()


def test_upload_completion_stream_mapping_is_durable_and_bounded() -> None:
    repository = Path(__file__).resolve().parents[1]
    template = (repository / "infra" / "api" / "template.yaml").read_text(encoding="utf-8")

    for required_fragment in (
        "StreamViewType: NEW_AND_OLD_IMAGES",
        "UploadCompletionStreamMapping:",
        "EventSourceArn: !GetAtt RegistryTable.StreamArn",
        "FunctionResponseTypes:\n        - ReportBatchItemFailures",
        "BisectBatchOnFunctionError: true",
        "MaximumRecordAgeInSeconds: 3600",
        "MaximumRetryAttempts: 5",
        "StartingPosition: TRIM_HORIZON",
        "Destination: !GetAtt UploadProcessorDlq.Arn",
        "dynamodb:DescribeStream",
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:ListStreams",
    ):
        assert required_fragment in template
