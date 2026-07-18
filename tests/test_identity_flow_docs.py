from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
FLOW_DIR = ROOT / "docs" / "flows"
FLOW_FILES = (
    FLOW_DIR / "entra-ui-flow.html",
    FLOW_DIR / "entra-certificate-api-testing-flow.html",
)


class FlowHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.ids: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if element_id := attributes.get("id"):
            self.ids.append(element_id)
        if tag in {"a", "link"} and (href := attributes.get("href")):
            self.links.append(href)


@pytest.mark.parametrize("path", FLOW_FILES, ids=lambda path: path.stem)
def test_flow_html_has_complete_accessible_offline_structure(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    parser = FlowHtmlParser()
    parser.feed(source)
    parser.close()

    assert source.splitlines()[:2] == ["<!doctype html>", '<html lang="en">']
    assert source.rstrip().endswith("</html>")
    assert "<script" not in source.lower()
    assert "style=" not in source.lower()
    assert len(parser.ids) == len(set(parser.ids))

    tag_names = [tag for tag, _ in parser.tags]
    for required_tag in ("head", "title", "body", "main", "header", "nav", "section", "h1"):
        assert required_tag in tag_names

    assert any(
        tag == "meta" and attrs.get("name") == "viewport"
        for tag, attrs in parser.tags
    )
    assert any(
        tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy"
        for tag, attrs in parser.tags
    )
    assert any(
        attrs.get("role") == "region" and attrs.get("tabindex") == "0"
        for _, attrs in parser.tags
    )
    assert '<span class="sr-only">' in source

    for href in parser.links:
        parsed = urlsplit(href)
        if href.startswith("#"):
            continue
        assert not parsed.scheme and not parsed.netloc, f"Remote link in {path}: {href}"
        link_path = Path(parsed.path)
        assert not link_path.is_absolute(), f"Absolute link in {path}: {href}"
        target = (path.parent / link_path).resolve()
        assert target.is_relative_to(ROOT.resolve()), f"Link escapes repository in {path}: {href}"
        assert target.is_file(), f"Broken local link in {path}: {href}"


def test_certificate_client_flow_matches_enforced_auth_and_lifecycle() -> None:
    source = FLOW_FILES[1].read_text(encoding="utf-8")

    for required in (
        "private_key_jwt",
        "api://&lt;product-api-client-guid&gt;/.default",
        "idtyp=app",
        "azpacr=2",
        "https://sts.windows.net/&lt;tenant-guid&gt;/",
        "sub = &lt;managed-identity-principal-object-id&gt;",
        "POST /v1/loans",
        "POST /v1/loans/23051/documents",
        "Idempotency-Key",
        "NO_THREATS_FOUND",
        "does not move, rename, or copy",
    ):
        assert required in source

    assert "client ID + secret" not in source
    assert "CloudTrail log of every action" not in source
    assert "the API app itself" not in source


def test_shared_stylesheet_is_balanced_and_has_no_remote_dependency() -> None:
    source = (FLOW_DIR / "identity-flow.css").read_text(encoding="utf-8")

    assert source.count("{") == source.count("}")
    assert "@import" not in source
    assert "url(" not in source
    assert "http://" not in source
    assert "https://" not in source


def test_ui_flow_matches_delegated_auth_and_browser_storage_policy() -> None:
    source = FLOW_FILES[0].read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    for required in (
        "authorization code",
        "PKCE",
        "sessionStorage",
        "acquireTokenSilent",
        "route scope <span class=\"mono\">P</span>",
        "matching assigned role <span class=\"mono\">P.Role</span>",
        "no Entra bearer token",
        "The create-document key is different",
        "does not move S3 objects",
    ):
        assert required in normalized

    assert "client secret" not in source
    assert "exact browser tab" not in source
    assert "Nothing sensitive persists" not in source


def test_markdown_entry_points_link_both_flows() -> None:
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    flow_readme = (FLOW_DIR / "README.md").read_text(encoding="utf-8")
    ui_handoff = (ROOT / "docs" / "ui-handoff.md").read_text(encoding="utf-8")
    claude_instructions = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    ui_claude_instructions = (ROOT / "apps" / "web" / "CLAUDE.md").read_text(encoding="utf-8")

    assert "[Identity and end-to-end flows](docs/flows/README.md)" in root_readme
    assert "[Target React UI flow](entra-ui-flow.html)" in flow_readme
    assert "[Certificate-client API testing flow](entra-certificate-api-testing-flow.html)" in flow_readme
    assert "SCREENING -> EXTRACTING -> SUCCEEDED" in ui_handoff
    assert "current processor does not persist `SELECTED`" in ui_handoff
    assert "`docs/flows/README.md`" in claude_instructions
    assert "`../../docs/flows/README.md`" in ui_claude_instructions
