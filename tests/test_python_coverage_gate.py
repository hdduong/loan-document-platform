from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def load_gate():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check-python-coverage.py"
    spec = importlib.util.spec_from_file_location("python_coverage_gate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summary(covered: int, statements: int) -> dict[str, int]:
    return {"covered_lines": covered, "num_statements": statements}


def test_coverage_gate_requires_eighty_percent_for_every_service_file() -> None:
    gate = load_gate()
    report = {
        "files": {
            "services\\loan_api\\app.py": {"summary": summary(8, 10)},
            "services/upload_processor/app.py": {"summary": summary(9, 10)},
            "tests/test_loan_api.py": {"summary": summary(0, 100)},
        },
        "totals": summary(17, 20),
    }

    assert gate.validate_report(report) == [
        ("services/loan_api/app.py", 80.0),
        ("services/upload_processor/app.py", 90.0),
    ]

    report["files"]["services\\loan_api\\app.py"]["summary"] = summary(79, 100)
    report["totals"] = summary(88, 100)
    with pytest.raises(ValueError, match=r"services/loan_api/app\.py=79\.00%"):
        gate.validate_report(report)


def test_coverage_gate_rejects_missing_production_measurements() -> None:
    gate = load_gate()

    with pytest.raises(ValueError, match="no production Python files"):
        gate.validate_report(
            {
                "files": {"tests/test_example.py": {"summary": summary(10, 10)}},
                "totals": summary(10, 10),
            }
        )
