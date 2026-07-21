"""Contract, release-manifest, and GitHub matrix tooling for AWS IDP images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IMAGE_NAMES = {
    "assessment-function",
    "bda-completion-function",
    "bda-invoke-function",
    "bda-processresults-function",
    "classification-function",
    "evaluation-function",
    "extraction-function",
    "mlflow-logger-function",
    "ocr-function",
    "processresults-function",
    "rule-validation-function",
    "rule-validation-orchestration-function",
    "rule-validation-policy-classification-function",
    "summarization-function",
    "test-execution-aggregation-function",
}
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_URI_PATTERN = re.compile(r"^[^:@\s]+(?:[/:][^:@\s]+)*@sha256:[0-9a-f]{64}$")


class ImageContractError(ValueError):
    """Raised for contract, manifest, or release-context drift."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ImageContractError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ImageContractError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ImageContractError(f"Cannot read JSON object {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ImageContractError(f"JSON object required: {path}")
    return value


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def write_canonical_json(path: Path, value: Any) -> str:
    payload = canonical_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def normalized_text_sha256(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except OSError as exc:
        raise ImageContractError(f"Cannot read checksum input {path}: {exc}") from exc
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_lock(lock: dict[str, Any], lock_path: Path, contract_path: Path) -> None:
    if lock.get("version") != "0.5.16" or lock.get("deploymentMode") != "headless":
        raise ImageContractError("IDP lock must pin headless version 0.5.16")
    if not re.fullmatch(r"[0-9a-f]{40}", str(lock.get("commit", ""))):
        raise ImageContractError("IDP lock has an invalid source commit")
    overlay = lock.get("externalImageOverlay")
    if not isinstance(overlay, dict):
        raise ImageContractError("IDP lock must pin externalImageOverlay")
    if overlay.get("path") != "vendor/patches/idp-v0.5.16-external-images.patch":
        raise ImageContractError("external image overlay path is not the reviewed repository path")
    if overlay.get("imageContractPath") != "config/idp/images.json":
        raise ImageContractError("image contract path is not the reviewed repository path")
    overlay_path = ROOT / overlay["path"]
    if normalized_text_sha256(overlay_path) != overlay.get("sha256"):
        raise ImageContractError("external image overlay checksum does not match lock")
    if normalized_text_sha256(contract_path) != overlay.get("imageContractSha256"):
        raise ImageContractError("image contract checksum does not match lock")
    recorded_lock_sha256 = overlay.get("lockSha256")
    if recorded_lock_sha256 is not None and normalized_text_sha256(lock_path) != recorded_lock_sha256:
        raise ImageContractError("lock checksum does not match its recorded release evidence")


def validate_contract(
    contract: dict[str, Any], lock: dict[str, Any], source_root: Path | None = None
) -> dict[str, dict[str, Any]]:
    if contract.get("schemaVersion") != "1.0":
        raise ImageContractError("schemaVersion does not match the IDP lock")
    if contract.get("upstreamVersion") != lock.get("version"):
        raise ImageContractError("image contract version does not match the IDP lock")
    if contract.get("upstreamCommit") != lock.get("commit"):
        raise ImageContractError("image contract commit does not match the IDP lock")
    if contract.get("platform") != "linux/arm64" or contract.get("architecture") != "arm64":
        raise ImageContractError("image contract platform/architecture must be linux/arm64")
    if contract.get("context") != "." or contract.get("dockerfile") != "Dockerfile.optimized":
        raise ImageContractError("image contract must use the pinned root context and Dockerfile")
    base_images = contract.get("baseImages")
    if (
        not isinstance(base_images, dict)
        or len(base_images) < 2
        or any("@sha256:" not in str(v) for v in base_images.values())
    ):
        raise ImageContractError("image contract must pin base-image digests")
    images = contract.get("images")
    if not isinstance(images, list) or len(images) != 15:
        raise ImageContractError("image contract must contain exactly 15 images")
    indexed: dict[str, dict[str, Any]] = {}
    logical_ids: set[str] = set()
    parameters: set[str] = set()
    for image in images:
        if not isinstance(image, dict):
            raise ImageContractError("image entry must be an object")
        name = image.get("logicalName")
        source = str(image.get("sourcePath", ""))
        parameter = image.get("digestParameter")
        logical_id = image.get("lambdaLogicalId")
        if name not in EXPECTED_IMAGE_NAMES:
            raise ImageContractError(f"Unknown logical image: {name}")
        if name in indexed:
            raise ImageContractError(f"Duplicate logical image: {name}")
        if not source.startswith("patterns/unified/src/") or ".." in Path(source).parts:
            raise ImageContractError(f"Invalid source path: {source}")
        if not isinstance(parameter, str) or not re.fullmatch(r"[A-Z][A-Za-z0-9]+Digest", parameter):
            raise ImageContractError(f"Invalid digest parameter: {parameter}")
        if parameter in parameters:
            raise ImageContractError("Duplicate digest parameter")
        if not isinstance(logical_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,254}", logical_id):
            raise ImageContractError(f"Invalid Lambda logical ID: {logical_id}")
        if logical_id in logical_ids:
            raise ImageContractError("Duplicate Lambda logical ID")
        args = image.get("buildArgs", {})
        if (
            not isinstance(args, dict)
            or set(args) - {"INSTALL_GIT"}
            or (args and args.get("INSTALL_GIT") != "true")
        ):
            raise ImageContractError("Unexpected extra build argument")
        if source_root is not None and not (source_root / source).is_dir():
            raise ImageContractError(f"Source path does not exist: {source}")
        indexed[name] = image
        logical_ids.add(logical_id)
        parameters.add(parameter)
    if set(indexed) != EXPECTED_IMAGE_NAMES:
        raise ImageContractError("image contract is not the exact 15-image inventory")
    return indexed


def build_github_matrix(contract: dict[str, Any], lock: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    indexed = validate_contract(contract, lock)
    return {
        "include": [
            {
                "logicalName": name,
                "sourcePath": indexed[name]["sourcePath"],
                "digestParameter": indexed[name]["digestParameter"],
                "installGit": "true"
                if indexed[name].get("buildArgs", {}).get("INSTALL_GIT") == "true"
                else "false",
            }
            for name in sorted(indexed)
        ]
    }


def build_fragment(
    *,
    logical_name: str,
    source_path: str,
    digest_parameter: str,
    repository_uri: str,
    tag: str,
    digest: str,
    media_type: str,
    scanner_version: str,
    scan_report_sha256: str,
    scan_completed_at: str,
    provenance_url: str,
    provenance_predicate_type: str,
    sbom_url: str,
    sbom_predicate_type: str,
    sbom_sha256: str,
) -> dict[str, Any]:
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ImageContractError("image digest is not immutable")
    return {
        "logicalName": logical_name,
        "sourcePath": source_path,
        "digestParameter": digest_parameter,
        "repositoryUri": repository_uri,
        "tag": tag,
        "digest": digest,
        "imageUri": f"{repository_uri}@{digest}",
        "platform": "linux/arm64",
        "mediaType": media_type,
        "scan": {
            "scanner": "trivy",
            "scannerVersion": scanner_version,
            "status": "PASS",
            "severityThreshold": "HIGH,CRITICAL",
            "completedAt": scan_completed_at,
            "reportSha256": scan_report_sha256,
        },
        "provenance": {"attestationUrl": provenance_url, "predicateType": provenance_predicate_type},
        "sbom": {
            "attestationUrl": sbom_url,
            "predicateType": sbom_predicate_type,
            "format": "cyclonedx-json",
            "documentSha256": sbom_sha256,
        },
    }


def assemble_release(
    fragments: Iterable[dict[str, Any]],
    contract: dict[str, Any],
    lock: dict[str, Any],
    *,
    release_id: str,
    environment: str,
    account_id: str,
    region: str,
    repository_uri: str,
    platform_commit: str,
    workflow_repository: str,
    workflow_ref: str,
    workflow_run_id: str,
    workflow_run_attempt: int,
    workflow_run_url: str,
    actor: str,
    lock_sha256: str | None = None,
    image_contract_sha256: str | None = None,
    built_at: str | None = None,
) -> dict[str, Any]:
    validate_contract(contract, lock)
    images = sorted(list(fragments), key=lambda item: str(item.get("logicalName", "")))
    if len(images) != 15 or {image.get("logicalName") for image in images} != EXPECTED_IMAGE_NAMES:
        raise ImageContractError("release must contain exactly one fragment for every image")
    return {
        "schemaVersion": "1.0",
        "releaseId": release_id,
        "status": "COMPLETE",
        "environment": environment,
        "aws": {"accountId": account_id, "region": region, "repositoryUri": repository_uri},
        "source": {
            "upstreamRepository": lock["repository"],
            "upstreamVersion": lock["version"],
            "upstreamCommit": lock["commit"],
            "lockSha256": lock_sha256 or normalized_text_sha256(ROOT / "vendor/idp.lock.json"),
            "imageContractSha256": image_contract_sha256
            or normalized_text_sha256(ROOT / "config/idp/images.json"),
            "overlaySha256": lock["externalImageOverlay"]["sha256"],
            "platformCommit": platform_commit,
        },
        "workflow": {
            "repository": workflow_repository,
            "ref": workflow_ref,
            "runId": str(workflow_run_id),
            "runAttempt": int(workflow_run_attempt),
            "runUrl": workflow_run_url,
            "actor": actor,
        },
        "builtAt": built_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "platform": "linux/arm64",
        "images": images,
    }


def _schema_validate(manifest: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ImageContractError("schema validation failed: " + "; ".join(error.message for error in errors))


def build_parameter_map(manifest: dict[str, Any], contract: dict[str, Any]) -> dict[str, str]:
    indexed = validate_contract(
        contract,
        {
            "version": manifest["source"]["upstreamVersion"],
            "commit": manifest["source"]["upstreamCommit"],
            "platform": "linux/arm64",
        },
    )
    values: dict[str, str] = {"IdpImageRepositoryUri": manifest["aws"]["repositoryUri"]}
    for image in manifest["images"]:
        entry = indexed[image["logicalName"]]
        values[entry["digestParameter"]] = image["digest"]
    return values


def validate_release(
    manifest: dict[str, Any],
    contract: dict[str, Any],
    lock: dict[str, Any],
    schema: dict[str, Any],
    *,
    lock_path: Path,
    contract_path: Path,
    expected_environment: str,
    expected_account: str,
    expected_region: str,
    expected_repository_uri: str,
    expected_platform_commit: str,
    expected_workflow_repository: str,
    expected_ref: str,
) -> dict[str, Any]:
    validate_lock(lock, lock_path, contract_path)
    validate_contract(contract, lock)
    _schema_validate(manifest, schema)
    if manifest["environment"] != expected_environment:
        raise ImageContractError("environment does not match")
    if manifest["aws"]["accountId"] != expected_account:
        raise ImageContractError("AWS account does not match")
    if manifest["aws"]["region"] != expected_region:
        raise ImageContractError("AWS region does not match")
    if manifest["aws"]["repositoryUri"] != expected_repository_uri:
        raise ImageContractError("ECR repository does not match")
    if manifest["source"]["platformCommit"] != expected_platform_commit:
        raise ImageContractError("platform commit does not match")
    if manifest["source"]["upstreamCommit"] != lock["commit"]:
        raise ImageContractError("upstream commit does not match")
    if manifest["workflow"]["repository"] != expected_workflow_repository:
        raise ImageContractError("workflow repository does not match")
    if manifest["workflow"]["ref"] != expected_ref:
        raise ImageContractError("workflow ref does not match")
    if manifest["source"]["lockSha256"] != normalized_text_sha256(lock_path):
        raise ImageContractError("lock checksum does not match")
    if manifest["source"]["imageContractSha256"] != normalized_text_sha256(contract_path):
        raise ImageContractError("image-contract checksum does not match")
    if manifest["source"]["overlaySha256"] != lock["externalImageOverlay"]["sha256"]:
        raise ImageContractError("overlay checksum does not match")
    indexed = validate_contract(contract, lock)
    names = [image["logicalName"] for image in manifest["images"]]
    if len(names) != len(set(names)) or set(names) != EXPECTED_IMAGE_NAMES:
        raise ImageContractError("duplicate image or release image set is not exactly 15 unique images")
    for image in manifest["images"]:
        if (
            image["repositoryUri"] != expected_repository_uri
            or image["imageUri"] != f"{expected_repository_uri}@{image['digest']}"
        ):
            raise ImageContractError("ECR image URI does not match the release repository")
        if image["digestParameter"] != indexed[image["logicalName"]]["digestParameter"]:
            raise ImageContractError("digest parameter does not match image contract")
        if not DIGEST_PATTERN.fullmatch(image["digest"]) or not IMAGE_URI_PATTERN.fullmatch(
            image["imageUri"]
        ):
            raise ImageContractError("image does not match an immutable digest reference")
    return manifest


def _load_inputs(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return load_json(Path(args.contract)), load_json(Path(args.lock)), load_json(Path(args.schema))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate-contract", "emit-github-matrix"):
        command = sub.add_parser(name)
        command.add_argument("--contract", required=True)
        command.add_argument("--lock", required=True)
        if name == "validate-contract":
            command.add_argument("--source-root")
        else:
            command.add_argument("--github-output", required=True)
    fragment = sub.add_parser("write-fragment")
    for option in (
        "contract",
        "lock",
        "output",
        "logical-name",
        "repository-uri",
        "tag",
        "digest",
        "media-type",
        "scanner-version",
        "scan-report",
        "scan-completed-at",
        "provenance-url",
        "provenance-predicate-type",
        "sbom",
        "sbom-url",
        "sbom-predicate-type",
    ):
        fragment.add_argument(f"--{option}", required=True)
    assemble = sub.add_parser("assemble-release")
    for option in (
        "contract",
        "lock",
        "schema",
        "fragments",
        "output",
        "checksum-output",
        "release-id",
        "environment",
        "account",
        "region",
        "repository-uri",
        "platform-commit",
        "workflow-repository",
        "workflow-ref",
        "workflow-run-id",
        "workflow-run-attempt",
        "workflow-run-url",
        "actor",
    ):
        assemble.add_argument(f"--{option}", required=True)
    assemble.add_argument("--built-at")
    validate = sub.add_parser("validate-release")
    for option in (
        "contract",
        "lock",
        "schema",
        "manifest",
        "environment",
        "account",
        "region",
        "repository-uri",
        "platform-commit",
        "workflow-repository",
        "workflow-ref",
        "parameters-output",
    ):
        validate.add_argument(f"--{option}", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"validate-contract", "emit-github-matrix"}:
            contract, lock = load_json(Path(args.contract)), load_json(Path(args.lock))
            source = Path(args.source_root) if getattr(args, "source_root", None) else None
            validate_lock(lock, Path(args.lock), Path(args.contract))
            indexed = validate_contract(contract, lock, source)
            if args.command == "emit-github-matrix":
                Path(args.github_output).write_text(
                    "matrix=" + json.dumps(build_github_matrix(contract, lock), separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
            return 0
        if args.command == "write-fragment":
            contract, lock = load_json(Path(args.contract)), load_json(Path(args.lock))
            indexed = validate_contract(contract, lock)
            entry = indexed[args.logical_name]
            fragment = build_fragment(
                logical_name=args.logical_name,
                source_path=entry["sourcePath"],
                digest_parameter=entry["digestParameter"],
                repository_uri=args.repository_uri,
                tag=args.tag,
                digest=args.digest,
                media_type=args.media_type,
                scanner_version=args.scanner_version,
                scan_report_sha256=normalized_text_sha256(Path(args.scan_report)),
                scan_completed_at=args.scan_completed_at,
                provenance_url=args.provenance_url,
                provenance_predicate_type=args.provenance_predicate_type,
                sbom_url=args.sbom_url,
                sbom_predicate_type=args.sbom_predicate_type,
                sbom_sha256=normalized_text_sha256(Path(args.sbom)),
            )
            write_canonical_json(Path(args.output), fragment)
            return 0
        if args.command == "assemble-release":
            contract, lock, schema = _load_inputs(args)
            fragments = [load_json(path) for path in sorted(Path(args.fragments).glob("*.json"))]
            manifest = assemble_release(
                fragments,
                contract,
                lock,
                release_id=args.release_id,
                environment=args.environment,
                account_id=args.account,
                region=args.region,
                repository_uri=args.repository_uri,
                platform_commit=args.platform_commit,
                workflow_repository=args.workflow_repository,
                workflow_ref=args.workflow_ref,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=int(args.workflow_run_attempt),
                workflow_run_url=args.workflow_run_url,
                actor=args.actor,
                built_at=args.built_at,
            )
            validate_release(
                manifest,
                contract,
                lock,
                schema,
                lock_path=Path(args.lock),
                contract_path=Path(args.contract),
                expected_environment=args.environment,
                expected_account=args.account,
                expected_region=args.region,
                expected_repository_uri=args.repository_uri,
                expected_platform_commit=args.platform_commit,
                expected_workflow_repository=args.workflow_repository,
                expected_ref=args.workflow_ref,
            )
            digest = write_canonical_json(Path(args.output), manifest)
            Path(args.checksum_output).write_text(f"{digest}  {Path(args.output).name}\n", encoding="utf-8")
            print("Assembled and validated complete IDP image release")
            return 0
        if args.command == "validate-release":
            contract, lock, schema = _load_inputs(args)
            manifest = load_json(Path(args.manifest))
            validate_release(
                manifest,
                contract,
                lock,
                schema,
                lock_path=Path(args.lock),
                contract_path=Path(args.contract),
                expected_environment=args.environment,
                expected_account=args.account,
                expected_region=args.region,
                expected_repository_uri=args.repository_uri,
                expected_platform_commit=args.platform_commit,
                expected_workflow_repository=args.workflow_repository,
                expected_ref=args.workflow_ref,
            )
            write_canonical_json(Path(args.parameters_output), build_parameter_map(manifest, contract))
            print("validated IDP image release")
            return 0
    except ImageContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
