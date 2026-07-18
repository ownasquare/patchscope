"""Mypy adapter that ignores repository configuration and third-party plugins."""

from __future__ import annotations

import re
import tempfile
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

_DIAGNOSTIC_RE = re.compile(
    r"^(?P<path>.+):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<kind>error|warning|note): (?P<message>.*?)(?:\s+\[(?P<code>[^\]]+)\])?$"
)
_HIGH_SIGNAL_CODES = frozenset(
    {
        "arg-type",
        "assignment",
        "attr-defined",
        "call-arg",
        "index",
        "name-defined",
        "operator",
        "return-value",
        "union-attr",
    }
)


class _Runner(Protocol):
    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult: ...


class MypyAnalyzer:
    name = "mypy"

    def __init__(
        self,
        *,
        executable: str = "mypy",
        timeout_seconds: float = 45.0,
        runner: _Runner | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner or FixedCommandRunner()

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun:
        python_sources = [source for source in files if not source.is_patch and _is_python(source)]
        if not python_sources:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.NOT_APPLICABLE,
                message="No Python source files were available for mypy.",
            )
        with tempfile.TemporaryDirectory(prefix="patchscope-mypy-") as temporary:
            config_path = Path(temporary) / "mypy.ini"
            config_path.write_text("[mypy]\nplugins =\n", encoding="utf-8")
            arguments = (
                "--config-file",
                str(config_path),
                "--no-incremental",
                "--no-site-packages",
                "--follow-imports",
                "skip",
                "--ignore-missing-imports",
                "--show-column-numbers",
                "--show-error-codes",
                "--no-error-summary",
                "--hide-error-context",
                "--no-pretty",
                *(str(root.joinpath(*Path(source.path).parts)) for source in python_sources),
            )
            result = self.runner.run(
                self.executable,
                arguments,
                cwd=root,
                timeout_seconds=self.timeout_seconds,
            )
            command = display_command(result.argv, root, (config_path,))
        if result.status is not AnalyzerStatus.SUCCEEDED:
            return AnalyzerRun(
                analyzer=self.name,
                status=result.status,
                duration_ms=result.duration_ms,
                command=command,
                message=result.message,
                exit_code=result.exit_code,
            )
        if result.exit_code not in {0, 1, None}:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="mypy exited before producing a complete type review.",
                exit_code=result.exit_code,
            )
        findings = tuple(
            sorted(
                filter(
                    None,
                    (self._finding(line, files, root) for line in result.stdout.splitlines()),
                ),
                key=lambda item: (item.path, item.start_line, item.start_column, item.rule_id),
            )
        )
        return AnalyzerRun(
            analyzer=self.name,
            status=AnalyzerStatus.SUCCEEDED,
            findings=findings,
            duration_ms=result.duration_ms,
            command=command,
            message="mypy completed with repository configuration and plugins disabled.",
            exit_code=result.exit_code,
        )

    def _finding(self, line: str, files: list[SourceFile], root: Path) -> Finding | None:
        match = _DIAGNOSTIC_RE.match(line)
        if match is None or match.group("kind") == "note":
            return None
        path = normalize_reported_path(match.group("path"), root)
        if path is None:
            return None
        line_number = int(match.group("line"))
        column = int(match.group("column"))
        code = match.group("code") or "type-check"
        message = bounded_message(match.group("message"), fallback="mypy found a type error.")
        severity = FindingSeverity.HIGH if code in _HIGH_SIGNAL_CODES else FindingSeverity.MEDIUM
        return Finding(
            id=finding_id(self.name, code, path, line_number, message),
            analyzer=self.name,
            rule_id=code,
            category=FindingCategory.BUG,
            severity=severity,
            message=message,
            path=path,
            start_line=line_number,
            end_line=line_number,
            start_column=max(column, 1),
            end_column=max(column + 1, 2),
            confidence=FindingConfidence.HIGH,
            snippet=source_snippet(files, path, line_number),
            suggestion="Align the value and declared type, then exercise the affected behavior.",
        )


def _is_python(source: SourceFile) -> bool:
    return (source.language_hint or "") == "python" or source.path.endswith((".py", ".pyi"))


__all__ = ["MypyAnalyzer"]
