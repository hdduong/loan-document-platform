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
