from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
IDP_DIR = ROOT / "config" / "idp"
SPEC_KIT_VERSION = "0.12.15"
SPEC_KIT_COMMIT = "7b91c1eda46e1107a53831cd3f14f608b4b7bad0"
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


def validate_markdown_links(path: Path) -> None:
    content = path.read_text(encoding="utf-8")
    for raw_target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content):
        target = raw_target.strip().strip("<>")
        if not target or target.startswith(("#", "http://", "https://", "mailto:")):
            continue
        relative_target = target.split("#", 1)[0].replace("%20", " ")
        require(
            (path.parent / relative_target).resolve().exists(),
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
    require(
        active_feature["feature_directory"] == "specs/001-loan-document-platform",
        "The checked-in active feature must point to the brownfield baseline.",
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
        ".claude/README.md",
        ".specify/README.md",
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
    require("**Version**: 1.0.0" in constitution, "Project constitution must declare version 1.0.0.")


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
        require("pull_request_target" not in workflow, f"Unsafe pull_request_target trigger in {path}")
        validate_workflow_actions(workflow, path)

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
