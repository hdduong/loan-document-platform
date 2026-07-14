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
