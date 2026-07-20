from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
from typing import Any

MINIMUM_LINE_COVERAGE = 80.0
PRODUCTION_ROOTS = (PurePosixPath("services"), PurePosixPath("tooling"))


def normalized_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    indexes = [
        path.parts.index(root.name)
        for root in PRODUCTION_ROOTS
        if root.name in path.parts
    ]
    if not indexes:
        return path
    return PurePosixPath(*path.parts[min(indexes) :])


def line_percentage(summary: dict[str, Any]) -> float:
    statements = int(summary.get("num_statements", 0))
    covered = int(summary.get("covered_lines", 0))
    if statements <= 0:
        return 100.0
    return covered * 100.0 / statements


def validate_report(report: dict[str, Any]) -> list[tuple[str, float]]:
    files = report.get("files")
    if not isinstance(files, dict):
        raise ValueError("Coverage report is missing its files map.")

    measured: list[tuple[str, float]] = []
    failures: list[tuple[str, float]] = []
    normalized_files = sorted(
        ((normalized_path(str(raw_path)), details) for raw_path, details in files.items()),
        key=lambda item: item[0].as_posix(),
    )
    for path, details in normalized_files:
        if not any(path.is_relative_to(root) for root in PRODUCTION_ROOTS) or path.suffix != ".py":
            continue
        if not isinstance(details, dict) or not isinstance(details.get("summary"), dict):
            raise ValueError(f"Coverage report has no summary for {path}.")
        percentage = line_percentage(details["summary"])
        measured.append((path.as_posix(), percentage))
        if percentage + 1e-9 < MINIMUM_LINE_COVERAGE:
            failures.append((path.as_posix(), percentage))

    if not measured:
        raise ValueError(
            "Coverage report contains no production Python files under services/ or tooling/."
        )

    totals = report.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("Coverage report is missing its totals summary.")
    total_percentage = line_percentage(totals)
    if total_percentage + 1e-9 < MINIMUM_LINE_COVERAGE:
        failures.append(("TOTAL", total_percentage))

    if failures:
        detail = ", ".join(f"{path}={percentage:.2f}%" for path, percentage in failures)
        raise ValueError(
            f"Python line coverage must be at least {MINIMUM_LINE_COVERAGE:.0f}% per production file "
            f"and overall: {detail}"
        )
    return measured


def main() -> None:
    parser = argparse.ArgumentParser(description="Enforce per-file production Python coverage.")
    parser.add_argument("report", nargs="?", type=Path, default=Path("coverage.json"))
    args = parser.parse_args()
    with args.report.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    measured = validate_report(report)
    for path, percentage in measured:
        print(f"{path}: {percentage:.2f}%")
    print(f"Python coverage gate passed at {MINIMUM_LINE_COVERAGE:.0f}% per file and overall.")


if __name__ == "__main__":
    main()
