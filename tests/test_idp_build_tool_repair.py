from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path


def create_console_probe_wheel(directory: Path) -> Path:
    wheel = directory / "repair_probe-1.0.0-py3-none-any.whl"
    dist_info = "repair_probe-1.0.0.dist-info"
    files = {
        "repair_probe.py": "def main():\n    print('restored')\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\nName: repair-probe\nVersion: 1.0.0\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: repository-test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\nrepair-probe = repair_probe:main\n"
        ),
    }
    record_path = f"{dist_info}/RECORD"
    files[record_path] = "".join(f"{name},,\n" for name in (*files, record_path))
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


def test_force_reinstall_recreates_a_missing_console_launcher(tmp_path: Path) -> None:
    wheel = create_console_probe_wheel(tmp_path)
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    scripts = python.parent
    launcher = scripts / ("repair-probe.exe" if os.name == "nt" else "repair-probe")
    install = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        str(wheel),
    ]

    subprocess.run(install, check=True, capture_output=True, text=True)
    assert launcher.is_file()
    launcher.unlink()
    assert not launcher.exists()
    installed_version = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; print(m.version('repair-probe'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert installed_version.stdout.strip() == "1.0.0"

    repair = [*install[:-1], "--force-reinstall", str(wheel)]
    subprocess.run(repair, check=True, capture_output=True, text=True)

    assert launcher.is_file()
    completed = subprocess.run(
        [str(launcher)], check=True, capture_output=True, text=True
    )
    assert completed.stdout.strip() == "restored"
