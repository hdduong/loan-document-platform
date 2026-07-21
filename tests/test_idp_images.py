from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tooling import idp_images

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config" / "idp" / "images.json"
LOCK_PATH = ROOT / "vendor" / "idp.lock.json"
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "idp-image-release.schema.json"
REPOSITORY_URI = (
    "123456789012.dkr.ecr.us-west-2.amazonaws.com/loan-document-dev-idp-images"
)
PLATFORM_COMMIT = "a" * 40


def load_inputs() -> tuple[dict, dict, dict]:
    return (
        idp_images.load_json(CONTRACT_PATH),
        idp_images.load_json(LOCK_PATH),
        idp_images.load_json(SCHEMA_PATH),
    )


def evidence(entry: dict, index: int) -> dict:
    digest = "sha256:" + hashlib.sha256(entry["logicalName"].encode()).hexdigest()
    return idp_images.build_fragment(
        logical_name=entry["logicalName"],
        source_path=entry["sourcePath"],
        digest_parameter=entry["digestParameter"],
        repository_uri=REPOSITORY_URI,
        tag=f"release-dev-123-{index}",
        digest=digest,
        media_type="application/vnd.docker.distribution.manifest.v2+json",
        scanner_version="0.72.0",
        scan_report_sha256=f"{index + 1:064x}",
        scan_completed_at="2026-07-19T00:00:00Z",
        provenance_url=(
            f"https://github.com/hdduong/aws-idp-custom-platform/attestations/{index + 1}"
        ),
        provenance_predicate_type="https://slsa.dev/provenance/v1",
        sbom_url=(
            f"https://github.com/hdduong/aws-idp-custom-platform/attestations/{index + 101}"
        ),
        sbom_predicate_type="https://cyclonedx.org/bom",
        sbom_sha256=f"{index + 101:064x}",
    )


def valid_manifest() -> tuple[dict, dict, dict, dict]:
    contract, lock, schema = load_inputs()
    fragments = [evidence(entry, index) for index, entry in enumerate(contract["images"])]
    manifest = idp_images.assemble_release(
        fragments,
        contract,
        lock,
        release_id="dev-12345678",
        environment="dev",
        account_id="123456789012",
        region="us-west-2",
        repository_uri=REPOSITORY_URI,
        platform_commit=PLATFORM_COMMIT,
        workflow_repository="hdduong/aws-idp-custom-platform",
        workflow_ref="refs/heads/main",
        workflow_run_id="12345678",
        workflow_run_attempt=1,
        workflow_run_url=(
            "https://github.com/hdduong/aws-idp-custom-platform/actions/runs/12345678"
        ),
        actor="hdduong",
        lock_sha256=idp_images.normalized_text_sha256(LOCK_PATH),
        image_contract_sha256=idp_images.normalized_text_sha256(CONTRACT_PATH),
        built_at="2026-07-19T00:00:00Z",
    )
    return manifest, contract, lock, schema


def validate(manifest: dict, contract: dict, lock: dict, schema: dict, **expected):
    defaults = {
        "expected_environment": "dev",
        "expected_account": "123456789012",
        "expected_region": "us-west-2",
        "expected_repository_uri": REPOSITORY_URI,
        "expected_platform_commit": PLATFORM_COMMIT,
        "expected_workflow_repository": "hdduong/aws-idp-custom-platform",
        "expected_ref": "refs/heads/main",
    }
    defaults.update(expected)
    return idp_images.validate_release(
        manifest,
        contract,
        lock,
        schema,
        lock_path=LOCK_PATH,
        contract_path=CONTRACT_PATH,
        **defaults,
    )


def test_contract_matches_pinned_upstream_and_exact_inventory() -> None:
    contract, lock, _ = load_inputs()
    indexed = idp_images.validate_contract(contract, lock)
    idp_images.validate_lock(lock, LOCK_PATH, CONTRACT_PATH)

    assert set(indexed) == idp_images.EXPECTED_IMAGE_NAMES
    assert len({entry["digestParameter"] for entry in indexed.values()}) == 15
    assert len({entry["lambdaLogicalId"] for entry in indexed.values()}) == 15


def test_overlay_uses_cross_platform_vite_entrypoint() -> None:
    overlay = (ROOT / "vendor" / "patches" / "idp-v0.5.16-external-images.patch").read_text(
        encoding="utf-8"
    )

    assert "src/ui/package.json" in overlay
    assert "./node_modules/vite/bin/vite.js build" in overlay
    assert "+    \"build\": " in overlay


def test_lock_checksum_is_validated_only_when_recorded() -> None:
    _, lock, _ = load_inputs()

    idp_images.validate_lock(lock, LOCK_PATH, CONTRACT_PATH)
    lock["externalImageOverlay"]["lockSha256"] = "0" * 64

    with pytest.raises(idp_images.ImageContractError, match="lock checksum"):
        idp_images.validate_lock(lock, LOCK_PATH, CONTRACT_PATH)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("path", "../../etc/passwd", "overlay path"),
        ("path", "/etc/passwd", "overlay path"),
        ("imageContractPath", "../images.json", "contract path"),
    ],
)
def test_lock_rejects_unreviewed_repository_paths(field: str, value: str, message: str) -> None:
    _, lock, _ = load_inputs()
    lock["externalImageOverlay"][field] = value

    with pytest.raises(idp_images.ImageContractError, match=message):
        idp_images.validate_lock(lock, LOCK_PATH, CONTRACT_PATH)


def test_pr_workflow_applies_reviewed_overlay_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "build-idp-images.yml").read_text(
        encoding="utf-8"
    )
    pr_job = workflow.split("  build-pr:", maxsplit=1)[1].split(
        "  publish-manifest:", maxsplit=1
    )[0]

    check = "apply --check --whitespace=error-all"
    apply = "apply --whitespace=error-all"
    build = "uses: docker/build-push-action@"
    assert check in pr_job
    assert apply in pr_job
    assert pr_job.index(check) < pr_job.index(apply) < pr_job.index(build)
    assert "External-image overlay touched unexpected files" in pr_job


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(schemaVersion="2.0"), "schemaVersion"),
        (lambda value: value.update(upstreamVersion="0.5.15"), "version"),
        (lambda value: value.update(upstreamCommit="b" * 40), "commit"),
        (lambda value: value.update(platform="linux/amd64"), "platform"),
        (lambda value: value.update(architecture="x86_64"), "architecture"),
        (lambda value: value.update(context="patterns/unified"), "root context"),
        (lambda value: value.update(baseImages={}), "base-image digests"),
        (lambda value: value["images"].pop(), "exactly 15"),
        (
            lambda value: value["images"][1].update(
                logicalName=value["images"][0]["logicalName"]
            ),
            "Duplicate logical image",
        ),
        (
            lambda value: value["images"][1].update(
                digestParameter=value["images"][0]["digestParameter"]
            ),
            "Duplicate digest parameter",
        ),
        (
            lambda value: value["images"][1].update(
                lambdaLogicalId=value["images"][0]["lambdaLogicalId"]
            ),
            "Duplicate Lambda logical",
        ),
        (
            lambda value: value["images"][0].pop("lambdaLogicalId"),
            "Invalid Lambda logical ID",
        ),
        (
            lambda value: value["images"][0].update(lambdaLogicalId="1Invalid"),
            "Invalid Lambda logical ID",
        ),
        (
            lambda value: value["images"][0].update(sourcePath="../escape"),
            "source path",
        ),
        (
            lambda value: value["images"][0].update(buildArgs={"EXTRA": "true"}),
            "Unexpected extra build argument",
        ),
    ],
)
def test_contract_rejects_drift(mutation, message: str) -> None:
    contract, lock, _ = load_inputs()
    mutation(contract)

    with pytest.raises(idp_images.ImageContractError, match=message):
        idp_images.validate_contract(contract, lock)


def test_release_validates_and_maps_all_digest_parameters(tmp_path: Path) -> None:
    manifest, contract, lock, schema = valid_manifest()

    assert validate(manifest, contract, lock, schema) is manifest
    parameters = idp_images.build_parameter_map(manifest, contract)
    assert parameters["IdpImageRepositoryUri"] == REPOSITORY_URI
    assert len(parameters) == 16
    assert all(
        idp_images.DIGEST_PATTERN.fullmatch(value)
        for name, value in parameters.items()
        if name != "IdpImageRepositoryUri"
    )

    output = tmp_path / "manifest.json"
    first_hash = idp_images.write_canonical_json(output, manifest)
    second_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    assert first_hash == second_hash
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_github_matrix_contains_exact_reviewed_build_inputs(tmp_path: Path) -> None:
    _, contract, lock, _ = valid_manifest()
    matrix = idp_images.build_github_matrix(contract, lock)

    assert len(matrix["include"]) == 15
    assert {entry["logicalName"] for entry in matrix["include"]} == idp_images.EXPECTED_IMAGE_NAMES
    assert sum(entry["installGit"] == "true" for entry in matrix["include"]) == 1

    output = tmp_path / "github-output.txt"
    result = idp_images.main(
        [
            "emit-github-matrix",
            "--contract",
            str(CONTRACT_PATH),
            "--lock",
            str(LOCK_PATH),
            "--github-output",
            str(output),
        ]
    )
    assert result == 0
    name, value = output.read_text(encoding="utf-8").strip().split("=", 1)
    assert name == "matrix"
    assert len(json.loads(value)["include"]) == 15


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["images"].pop(), "too short"),
        (
            lambda value: value["images"].append(copy.deepcopy(value["images"][0])),
            "too long",
        ),
        (
            lambda value: value["images"][1].update(
                logicalName=value["images"][0]["logicalName"]
            ),
            "duplicate image",
        ),
        (
            lambda value: value["images"][0].update(platform="linux/amd64"),
            "linux/arm64",
        ),
        (
            lambda value: value["images"][0].update(imageUri="repo:latest"),
            "does not match",
        ),
        (
            lambda value: value["images"][0]["scan"].update(status="FAIL"),
            "PASS",
        ),
        (
            lambda value: value["images"][0].pop("provenance"),
            "required property",
        ),
        (
            lambda value: value["source"].update(upstreamCommit="b" * 40),
            "upstream commit",
        ),
        (
            lambda value: value["source"].update(lockSha256="b" * 64),
            "lock checksum",
        ),
        (
            lambda value: value["source"].update(imageContractSha256="b" * 64),
            "image-contract checksum",
        ),
        (
            lambda value: value["source"].update(overlaySha256="b" * 64),
            "overlay checksum",
        ),
    ],
)
def test_release_rejects_partial_mutable_or_untrusted_evidence(mutation, message: str) -> None:
    manifest, contract, lock, schema = valid_manifest()
    mutation(manifest)

    with pytest.raises(idp_images.ImageContractError, match=message):
        validate(manifest, contract, lock, schema)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("expected_environment", "prod", "environment"),
        ("expected_account", "999999999999", "AWS account"),
        ("expected_region", "us-east-1", "AWS region"),
        (
            "expected_repository_uri",
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/other",
            "ECR repository",
        ),
        ("expected_platform_commit", "b" * 40, "platform commit"),
        ("expected_workflow_repository", "other/repo", "workflow repository"),
        ("expected_ref", "refs/heads/other", "workflow ref"),
    ],
)
def test_release_rejects_wrong_deployment_context(field: str, value: str, message: str) -> None:
    manifest, contract, lock, schema = valid_manifest()

    with pytest.raises(idp_images.ImageContractError, match=message):
        validate(manifest, contract, lock, schema, **{field: value})


def test_json_loader_rejects_duplicate_keys_and_non_objects(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")

    with pytest.raises(idp_images.ImageContractError, match="Duplicate JSON key"):
        idp_images.load_json(duplicate)
    with pytest.raises(idp_images.ImageContractError, match="JSON object"):
        idp_images.load_json(array)


def test_cli_assembles_validates_and_writes_parameters(tmp_path: Path, capsys) -> None:
    manifest, contract, _, _ = valid_manifest()
    fragments = tmp_path / "fragments"
    fragments.mkdir()
    for index, fragment in enumerate(manifest["images"]):
        idp_images.write_canonical_json(fragments / f"{index:02d}.json", fragment)
    output = tmp_path / "release.json"
    checksum = tmp_path / "release.sha256"

    result = idp_images.main(
        [
            "assemble-release",
            "--contract",
            str(CONTRACT_PATH),
            "--lock",
            str(LOCK_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--fragments",
            str(fragments),
            "--output",
            str(output),
            "--checksum-output",
            str(checksum),
            "--release-id",
            "dev-12345678",
            "--environment",
            "dev",
            "--account",
            "123456789012",
            "--region",
            "us-west-2",
            "--repository-uri",
            REPOSITORY_URI,
            "--platform-commit",
            PLATFORM_COMMIT,
            "--workflow-repository",
            "hdduong/aws-idp-custom-platform",
            "--workflow-ref",
            "refs/heads/main",
            "--workflow-run-id",
            "12345678",
            "--workflow-run-attempt",
            "1",
            "--workflow-run-url",
            "https://github.com/hdduong/aws-idp-custom-platform/actions/runs/12345678",
            "--actor",
            "hdduong",
            "--built-at",
            "2026-07-19T00:00:00Z",
        ]
    )
    assert result == 0
    assert "validated" in capsys.readouterr().out
    assert checksum.read_text(encoding="utf-8").endswith("release.json\n")

    parameters = tmp_path / "parameters.json"
    result = idp_images.main(
        [
            "validate-release",
            "--contract",
            str(CONTRACT_PATH),
            "--lock",
            str(LOCK_PATH),
            "--schema",
            str(SCHEMA_PATH),
            "--manifest",
            str(output),
            "--environment",
            "dev",
            "--account",
            "123456789012",
            "--region",
            "us-west-2",
            "--repository-uri",
            REPOSITORY_URI,
            "--platform-commit",
            PLATFORM_COMMIT,
            "--workflow-repository",
            "hdduong/aws-idp-custom-platform",
            "--workflow-ref",
            "refs/heads/main",
            "--parameters-output",
            str(parameters),
        ]
    )
    assert result == 0
    assert len(json.loads(parameters.read_text(encoding="utf-8"))) == 16
    assert contract["images"][0]["digestParameter"] in parameters.read_text()


def test_cli_reports_contract_error_without_traceback(tmp_path: Path, capsys) -> None:
    bad_contract = tmp_path / "contract.json"
    bad_contract.write_text("{}", encoding="utf-8")

    result = idp_images.main(
        [
            "validate-contract",
            "--contract",
            str(bad_contract),
            "--lock",
            str(LOCK_PATH),
        ]
    )

    assert result == 1
    assert "ERROR:" in capsys.readouterr().err
