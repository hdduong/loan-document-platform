from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = ROOT / "scripts" / "idp_windows_cli_bridge" / "idp_windows_cli_bridge.py"


def load_bridge() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "idp_windows_cli_bridge_under_test", BRIDGE_PATH
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_sam_bridge_preserves_argument_boundaries(tmp_path: Path, monkeypatch) -> None:
    bridge = load_bridge()
    python = tmp_path / "AWS SAM CLI" / "runtime" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    arguments = ["build", "--parameter-overrides", 'Name=a&b $value "quoted"']
    monkeypatch.delenv("IDP_SAM_NATIVE_EXECUTABLE", raising=False)
    monkeypatch.setenv("IDP_SAM_CLI_PYTHON", str(python))

    assert bridge._child_command("sam", arguments) == [
        str(python),
        "-m",
        "samcli",
        *arguments,
    ]


def test_empty_stale_native_target_uses_validated_sam_runtime(tmp_path: Path, monkeypatch) -> None:
    bridge = load_bridge()
    python = tmp_path / "SAM" / "python.exe"
    python.parent.mkdir()
    python.write_bytes(b"")
    monkeypatch.setenv("IDP_SAM_NATIVE_EXECUTABLE", "")
    monkeypatch.setenv("IDP_SAM_CLI_PYTHON", str(python))

    assert bridge._child_command("sam", ["--version"]) == [
        str(python),
        "-m",
        "samcli",
        "--version",
    ]


def test_npm_bridge_and_native_relays_use_exact_targets(tmp_path: Path, monkeypatch) -> None:
    bridge = load_bridge()
    node = tmp_path / "Node JS" / "node.exe"
    npm_cli = tmp_path / "Node JS" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    sam = tmp_path / "SAM CLI" / "sam.exe"
    for path in (node, npm_cli, sam):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    arguments = ["run", "build", "--", "--label=a&b"]
    monkeypatch.delenv("IDP_NPM_NATIVE_EXECUTABLE", raising=False)
    monkeypatch.setenv("IDP_NODE_EXECUTABLE", str(node))
    monkeypatch.setenv("IDP_NPM_CLI_JS", str(npm_cli))
    monkeypatch.setenv("IDP_SAM_NATIVE_EXECUTABLE", str(sam))

    assert bridge._child_command("npm", arguments) == [str(node), str(npm_cli), *arguments]
    assert bridge._child_command("sam", ["--version"]) == [str(sam), "--version"]


def test_main_uses_no_shell_and_propagates_child_exit(tmp_path: Path, monkeypatch) -> None:
    bridge = load_bridge()
    sam = tmp_path / "SAM CLI" / "sam.exe"
    sam.parent.mkdir(parents=True)
    sam.write_bytes(b"")
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=23)

    monkeypatch.setenv("IDP_SAM_NATIVE_EXECUTABLE", str(sam))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "sam.exe"), "build", "a&b"])
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert bridge.main() == 23
    assert observed == {
        "command": [str(sam), "build", "a&b"],
        "kwargs": {"check": False, "shell": False},
    }


@pytest.mark.parametrize("missing_kind", ["unset", "relative", "missing"])
def test_bridge_rejects_untrusted_targets(
    tmp_path: Path, monkeypatch, missing_kind: str
) -> None:
    bridge = load_bridge()
    monkeypatch.delenv("IDP_SAM_NATIVE_EXECUTABLE", raising=False)
    if missing_kind == "unset":
        monkeypatch.delenv("IDP_SAM_CLI_PYTHON", raising=False)
    elif missing_kind == "relative":
        relative = Path("relative-python.exe")
        (tmp_path / relative).write_bytes(b"")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("IDP_SAM_CLI_PYTHON", str(relative))
    else:
        monkeypatch.setenv("IDP_SAM_CLI_PYTHON", str(tmp_path / "missing.exe"))

    with pytest.raises(RuntimeError, match="SAM CLI bridge target is unavailable"):
        bridge._child_command("sam", [])


def test_main_fails_closed_for_unsupported_launcher_and_os_error(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    bridge = load_bridge()
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "unknown.exe")])
    assert bridge.main() == 127
    assert "unsupported IDP child-tool bridge" in capsys.readouterr().err

    sam = tmp_path / "native" / "sam.exe"
    sam.parent.mkdir()
    sam.write_bytes(b"")
    monkeypatch.setenv("IDP_SAM_NATIVE_EXECUTABLE", str(sam))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "shim" / "sam.exe")])

    def fail_run(*_args: object, **_kwargs: object) -> None:
        raise OSError("child launch failed")

    monkeypatch.setattr(subprocess, "run", fail_run)
    assert bridge.main() == 127
    assert "child launch failed" in capsys.readouterr().err


def test_main_rejects_a_self_referencing_native_target(tmp_path: Path, monkeypatch, capsys) -> None:
    bridge = load_bridge()
    launcher = tmp_path / "sam.exe"
    launcher.write_bytes(b"")
    monkeypatch.setenv("IDP_SAM_NATIVE_EXECUTABLE", str(launcher))
    monkeypatch.setattr(sys, "argv", [str(launcher.with_suffix("")), "--version"])

    assert bridge.main() == 127
    assert "bridge cannot target itself" in capsys.readouterr().err


def test_self_target_detection_handles_missing_files_and_exe_suffix(tmp_path: Path) -> None:
    bridge = load_bridge()

    assert bridge._targets_launcher(
        str(tmp_path / "Scripts" / "sam.exe"), str(tmp_path / "Scripts" / "sam")
    )
    assert not bridge._targets_launcher(
        str(tmp_path / "external" / "sam.exe"), str(tmp_path / "Scripts" / "sam")
    )
