"""Relay Windows console entry points without invoking the command processor."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _required_file(variable: str, tool: str) -> str:
    value = os.environ.get(variable, "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{tool} bridge target is unavailable")
    return value


def _optional_file(variable: str, tool: str) -> str | None:
    value = os.environ.get(variable, "")
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"{tool} bridge target is unavailable")
    return value


def _child_command(launcher: str, arguments: list[str]) -> list[str]:
    if launcher == "sam":
        native = _optional_file("IDP_SAM_NATIVE_EXECUTABLE", "SAM CLI")
        if native:
            return [native, *arguments]
        python = _required_file("IDP_SAM_CLI_PYTHON", "SAM CLI")
        return [python, "-m", "samcli", *arguments]
    if launcher == "npm":
        native = _optional_file("IDP_NPM_NATIVE_EXECUTABLE", "npm CLI")
        if native:
            return [native, *arguments]
        node = _required_file("IDP_NODE_EXECUTABLE", "Node.js")
        npm_cli = _required_file("IDP_NPM_CLI_JS", "npm CLI")
        return [node, npm_cli, *arguments]
    raise RuntimeError("unsupported IDP child-tool bridge")


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve() == right.resolve()


def _targets_launcher(target: str, launcher: str) -> bool:
    launcher_path = Path(launcher)
    candidates = [launcher_path]
    if launcher_path.suffix.casefold() != ".exe":
        candidates.append(Path(f"{launcher}.exe"))
    return any(_same_file(Path(target), candidate) for candidate in candidates)


def main() -> int:
    launcher = Path(sys.argv[0]).stem.casefold()
    try:
        command = _child_command(launcher, sys.argv[1:])
        if _targets_launcher(command[0], sys.argv[0]):
            raise RuntimeError("IDP child-tool bridge cannot target itself")
        return subprocess.run(command, check=False, shell=False).returncode
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
