"""Ruff adapter with isolated configuration and normalized JSON findings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from patchscope.analyzers.base import (
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    finding_id,
)
from patchscope.analyzers.process import FixedCommandRunner, ProcessResult
from patchscope.analyzers.utils import (
    bounded_message,
    display_command,
    normalize_reported_path,
    source_snippet,
)
from patchscope.intake import SourceFile


class _Runner(Protocol):
    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult: ...


class RuffAnalyzer:
    name = "ruff"

    def __init__(
        self,
        *,
        executable: str = "ruff",
        timeout_seconds: float = 30.0,
        runner: _Runner | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner or FixedCommandRunner()

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun:
        if not any(_is_python(source) for source in files if not source.is_patch):
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.NOT_APPLICABLE,
                message="No Python source files were available for Ruff.",
            )
        arguments = (
            "check",
            "--isolated",
            "--no-cache",
            "--output-format",
            "json",
            "--exit-zero",
            "--select",
            "E,F,W,B,SIM,UP,S,PERF,ASYNC",
            str(root),
        )
        result = self.runner.run(
            self.executable,
            arguments,
            cwd=root,
            timeout_seconds=self.timeout_seconds,
        )
        command = display_command(result.argv, root)
        if result.status is not AnalyzerStatus.SUCCEEDED:
            return AnalyzerRun(
                analyzer=self.name,
                status=result.status,
                duration_ms=result.duration_ms,
                command=command,
                message=result.message,
                exit_code=result.exit_code,
            )
        if result.exit_code not in {0, None}:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="Ruff exited before producing a complete review.",
                exit_code=result.exit_code,
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="Ruff returned malformed JSON output.",
                exit_code=result.exit_code,
            )
        if not isinstance(payload, list):
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="Ruff returned an unexpected result envelope.",
                exit_code=result.exit_code,
            )
        findings = tuple(
            sorted(
                filter(None, (self._finding(item, files, root) for item in payload)),
                key=lambda item: (item.path, item.start_line, item.start_column, item.rule_id),
            )
        )
        return AnalyzerRun(
            analyzer=self.name,
            status=AnalyzerStatus.SUCCEEDED,
            findings=findings,
            duration_ms=result.duration_ms,
            command=command,
            message="Ruff completed with isolated configuration.",
            exit_code=result.exit_code,
        )

    def _finding(self, item: object, files: list[SourceFile], root: Path) -> Finding | None:
        if not isinstance(item, dict):
            return None
        code = item.get("code")
        path = normalize_reported_path(item.get("filename"), root)
        location = item.get("location")
        end_location = item.get("end_location")
        if not isinstance(code, str) or path is None or not isinstance(location, dict):
            return None
        row = _positive_int(location.get("row"), 1)
        column = _positive_int(location.get("column"), 1)
        end_row = row
        end_column = column + 1
        if isinstance(end_location, dict):
            end_row = max(_positive_int(end_location.get("row"), row), row)
            end_column = _positive_int(end_location.get("column"), column + 1)
        message = bounded_message(
            item.get("message"),
            fallback=f"Ruff reported {code}.",
        )
        category, severity = _classification(code)
        replacement, safe, properties = _safe_fix(item.get("fix"))
        suggestion = None
        if isinstance(item.get("fix"), dict):
            suggestion = "Ruff provides a candidate edit; review the preview before applying it."
        return Finding(
            id=finding_id(self.name, code, path, row, message),
            analyzer=self.name,
            rule_id=code,
            category=category,
            severity=severity,
            message=message,
            path=path,
            start_line=row,
            end_line=end_row,
            start_column=column,
            end_column=end_column,
            confidence=FindingConfidence.HIGH,
            snippet=source_snippet(files, path, row),
            suggestion=suggestion,
            suggested_replacement=replacement,
            autofix_safe=safe,
            properties=properties,
        )


def _classification(code: str) -> tuple[FindingCategory, FindingSeverity]:
    if code.startswith("S"):
        return FindingCategory.SECURITY, FindingSeverity.HIGH
    if code.startswith(("PERF", "ASYNC")):
        return FindingCategory.PERFORMANCE, FindingSeverity.MEDIUM
    if code.startswith(("F", "B", "E9")):
        return FindingCategory.BUG, FindingSeverity.MEDIUM
    return FindingCategory.READABILITY, FindingSeverity.LOW


def _safe_fix(
    value: object,
) -> tuple[str | None, bool, dict[str, str | int | float | bool | None]]:
    if not isinstance(value, dict) or value.get("applicability") != "safe":
        return None, False, {}
    edits = value.get("edits")
    if not isinstance(edits, list) or len(edits) != 1 or not isinstance(edits[0], dict):
        return None, False, {}
    edit = edits[0]
    start = edit.get("location")
    end = edit.get("end_location")
    content = edit.get("content")
    if (
        not isinstance(start, dict)
        or not isinstance(end, dict)
        or not isinstance(content, str)
        or len(content) > 20_000
    ):
        return None, False, {}
    start_line = _positive_int(start.get("row"), 0)
    end_line = _positive_int(end.get("row"), 0)
    start_column = _positive_int(start.get("column"), 0)
    end_column = _positive_int(end.get("column"), 0)
    if min(start_line, end_line, start_column, end_column) <= 0:
        return None, False, {}
    return (
        content,
        True,
        {
            "replacement_start_line": start_line,
            "replacement_end_line": end_line,
            "replacement_start_column": start_column,
            "replacement_end_column": end_column,
        },
    )


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


def _is_python(source: SourceFile) -> bool:
    return (source.language_hint or "") == "python" or source.path.endswith((".py", ".pyi"))


__all__ = ["RuffAnalyzer"]
