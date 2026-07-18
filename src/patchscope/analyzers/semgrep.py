"""Semgrep adapter using only PatchScope-owned local rules and fixed arguments."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path, PurePosixPath
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

_RULES = """\
rules:
  - id: patchscope.semgrep.python-dynamic-execution
    languages: [python]
    severity: ERROR
    message: Dynamic Python execution can run attacker-controlled code.
    metadata:
      category: security
      confidence: high
    pattern-either:
      - pattern: eval(...)
      - pattern: exec(...)
  - id: patchscope.semgrep.python-shell-true
    languages: [python]
    severity: ERROR
    message: A subprocess enables shell parsing.
    metadata:
      category: security
      confidence: high
    pattern: subprocess.$FUNC(..., shell=True, ...)
  - id: patchscope.semgrep.javascript-dynamic-execution
    languages: [javascript, typescript]
    severity: ERROR
    message: Dynamic JavaScript execution can run attacker-controlled code.
    metadata:
      category: security
      confidence: high
    pattern: eval(...)
  - id: patchscope.semgrep.javascript-inner-html
    languages: [javascript, typescript]
    severity: ERROR
    message: Direct HTML assignment can create a cross-site scripting path.
    metadata:
      category: security
      confidence: high
    pattern: $TARGET.innerHTML = $VALUE
  - id: patchscope.semgrep.java-runtime-exec
    languages: [java]
    severity: ERROR
    message: Runtime.exec creates a command-injection boundary.
    metadata:
      category: security
      confidence: high
    pattern: Runtime.getRuntime().exec(...)
  - id: patchscope.semgrep.go-shell-command
    languages: [go]
    severity: ERROR
    message: A Go subprocess explicitly invokes a command shell.
    metadata:
      category: security
      confidence: high
    pattern-either:
      - pattern: exec.Command("sh", "-c", ...)
      - pattern: exec.Command("bash", "-c", ...)
  - id: patchscope.semgrep.c-unsafe-string-copy
    languages: [c]
    severity: ERROR
    message: An unbounded C string operation can overwrite memory.
    metadata:
      category: security
      confidence: high
    pattern-either:
      - pattern: strcpy(...)
      - pattern: strcat(...)
      - pattern: sprintf(...)
"""


class _Runner(Protocol):
    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult: ...


class SemgrepAnalyzer:
    name = "semgrep"

    def __init__(
        self,
        *,
        executable: str = "semgrep",
        timeout_seconds: float = 60.0,
        runner: _Runner | None = None,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner or FixedCommandRunner()

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun:
        targets = [source for source in files if not source.is_patch and _supported(source)]
        if not targets:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.NOT_APPLICABLE,
                message="No Semgrep-supported source files were available.",
            )
        with tempfile.TemporaryDirectory(prefix="patchscope-semgrep-") as temporary:
            rule_path = Path(temporary) / "rules.yml"
            rule_path.write_text(_RULES, encoding="utf-8")
            arguments = (
                "scan",
                "--config",
                str(rule_path),
                "--json",
                "--quiet",
                "--metrics=off",
                "--disable-version-check",
                "--no-git-ignore",
                "--jobs",
                "1",
                "--timeout",
                "5",
                "--timeout-threshold",
                "1",
                "--max-target-bytes",
                "1000000",
                *(str(root.joinpath(*PurePosixPath(source.path).parts)) for source in targets),
            )
            result = self.runner.run(
                self.executable,
                arguments,
                cwd=root,
                timeout_seconds=self.timeout_seconds,
            )
            command = display_command(result.argv, root, (rule_path,))
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
                message="Semgrep exited before producing a complete security review.",
                exit_code=result.exit_code,
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="Semgrep returned malformed JSON output.",
                exit_code=result.exit_code,
            )
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message="Semgrep returned an unexpected result envelope.",
                exit_code=result.exit_code,
            )
        errors = payload.get("errors", [])
        if isinstance(errors, list) and errors:
            return AnalyzerRun(
                analyzer=self.name,
                status=AnalyzerStatus.ERROR,
                duration_ms=result.duration_ms,
                command=command,
                message=(
                    f"Semgrep reported {len(errors)} scan error(s); partial results were discarded."
                ),
                exit_code=result.exit_code,
            )
        findings = tuple(
            sorted(
                filter(
                    None,
                    (self._finding(item, files, root) for item in payload.get("results", [])),
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
            message="Semgrep completed with PatchScope-owned offline rules.",
            exit_code=result.exit_code,
        )

    def _finding(self, item: object, files: list[SourceFile], root: Path) -> Finding | None:
        if not isinstance(item, dict):
            return None
        rule_id = item.get("check_id")
        path = normalize_reported_path(item.get("path"), root)
        start = item.get("start")
        end = item.get("end")
        extra = item.get("extra")
        if (
            not isinstance(rule_id, str)
            or path is None
            or not isinstance(start, dict)
            or not isinstance(extra, dict)
        ):
            return None
        line = _positive_int(start.get("line"), 1)
        column = _positive_int(start.get("col"), 1)
        end_line = line
        end_column = column + 1
        if isinstance(end, dict):
            end_line = max(_positive_int(end.get("line"), line), line)
            end_column = _positive_int(end.get("col"), column + 1)
        raw_metadata = extra.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
        category = _category(metadata.get("category"))
        severity = _severity(extra.get("severity"), category)
        confidence = _confidence(metadata.get("confidence"))
        message = bounded_message(
            extra.get("message"),
            fallback="Semgrep found a source pattern that requires review.",
        )
        return Finding(
            id=finding_id(self.name, rule_id, path, line, message),
            analyzer=self.name,
            rule_id=rule_id,
            category=category,
            severity=severity,
            message=message,
            path=path,
            start_line=line,
            end_line=end_line,
            start_column=column,
            end_column=end_column,
            confidence=confidence,
            snippet=source_snippet(files, path, line),
            suggestion=(
                "Review the data flow and replace the risky primitive with an allowlisted "
                "alternative."
            ),
        )


def _supported(source: SourceFile) -> bool:
    language = source.language_hint or ""
    if language in {"c", "go", "java", "javascript", "python", "tsx", "typescript"}:
        return True
    return PurePosixPath(source.path).suffix.casefold() in {
        ".c",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".py",
        ".ts",
        ".tsx",
    }


def _category(value: object) -> FindingCategory:
    try:
        return FindingCategory(str(value).casefold())
    except ValueError:
        return FindingCategory.SECURITY


def _severity(value: object, category: FindingCategory) -> FindingSeverity:
    normalized = str(value).casefold()
    if normalized == "error":
        return (
            FindingSeverity.HIGH if category is FindingCategory.SECURITY else FindingSeverity.MEDIUM
        )
    if normalized == "warning":
        return FindingSeverity.MEDIUM
    return FindingSeverity.LOW


def _confidence(value: object) -> FindingConfidence:
    try:
        return FindingConfidence(str(value).casefold())
    except ValueError:
        return FindingConfidence.MEDIUM


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


__all__ = ["SemgrepAnalyzer"]
