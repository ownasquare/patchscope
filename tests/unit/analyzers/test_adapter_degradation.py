from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchscope.analyzers.base import (
    AnalyzerStatus,
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
)
from patchscope.analyzers.process import ProcessResult
from patchscope.analyzers.ruff import RuffAnalyzer, _safe_fix
from patchscope.analyzers.semgrep import SemgrepAnalyzer, _supported
from patchscope.intake import SourceFile, materialize_sources


class FakeRunner:
    def __init__(
        self,
        *,
        status: AnalyzerStatus = AnalyzerStatus.SUCCEEDED,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = 0,
        message: str = "",
    ) -> None:
        self.status = status
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.message = message
        self.calls: list[tuple[str, tuple[str, ...], Path, float]] = []

    def run(
        self,
        executable: str,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        self.calls.append((executable, arguments, cwd, timeout_seconds))
        return ProcessResult(
            status=self.status,
            argv=(executable, *arguments),
            duration_ms=9,
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
            message=self.message,
        )


class ExplodingRunner:
    def run(self, *_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("runner must not execute")


def _python_source(tmp_path: Path, content: str = "pass\n") -> SourceFile:
    source = SourceFile.create("app.py", content, language_hint="python")
    materialize_sources([source], tmp_path)
    return source


def test_ruff_returns_success_without_launching_when_no_python_source(tmp_path: Path) -> None:
    markdown = SourceFile.create("README.md", "text\n", language_hint="markdown")
    patch = SourceFile.create("change.patch", "diff\n", is_patch=True)

    run = RuffAnalyzer(runner=ExplodingRunner()).analyze([markdown, patch], tmp_path)

    assert run.status is AnalyzerStatus.NOT_APPLICABLE
    assert run.findings == ()
    assert "No Python" in run.message


@pytest.mark.parametrize("status", [AnalyzerStatus.TIMEOUT, AnalyzerStatus.ERROR])
def test_ruff_preserves_terminal_process_failures(
    status: AnalyzerStatus,
    tmp_path: Path,
) -> None:
    source = _python_source(tmp_path)
    runner = FakeRunner(status=status, exit_code=None, message="bounded failure")

    run = RuffAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is status
    assert run.message == "bounded failure"
    assert run.findings == ()
    assert "<workspace>" in run.command


def test_ruff_rejects_nonstandard_exit_code(tmp_path: Path) -> None:
    source = _python_source(tmp_path)

    run = RuffAnalyzer(runner=FakeRunner(stdout="[]", exit_code=2)).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.ERROR
    assert run.exit_code == 2
    assert "complete review" in run.message


@pytest.mark.parametrize(
    ("stdout", "expected_message"),
    [
        ("{", "malformed JSON"),
        ("{}", "unexpected result envelope"),
    ],
)
def test_ruff_rejects_malformed_or_unexpected_json(
    stdout: str,
    expected_message: str,
    tmp_path: Path,
) -> None:
    source = _python_source(tmp_path)

    run = RuffAnalyzer(runner=FakeRunner(stdout=stdout)).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.ERROR
    assert expected_message in run.message


def test_ruff_filters_invalid_records_and_normalizes_categories_and_safe_fix(
    tmp_path: Path,
) -> None:
    source = _python_source(tmp_path, "dangerous()\nfor item in values:\n    pass\n")
    payload = [
        None,
        {"code": "F821"},
        {
            "code": "F821",
            "filename": str(tmp_path.parent / "outside.py"),
            "location": {"row": 1, "column": 1},
            "message": "outside",
        },
        {
            "code": "S307",
            "filename": str(tmp_path / "app.py"),
            "location": {"row": 0, "column": 0},
            "end_location": {"row": 0, "column": 0},
            "message": None,
            "fix": {
                "applicability": "safe",
                "edits": [
                    {
                        "location": {"row": 1, "column": 1},
                        "end_location": {"row": 1, "column": 12},
                        "content": "safe_call()",
                    }
                ],
            },
        },
        {
            "code": "PERF203",
            "filename": "app.py",
            "location": {"row": 2, "column": 1},
            "message": "performance",
            "fix": {"applicability": "unsafe", "edits": []},
        },
        {
            "code": "F401",
            "filename": "app.py",
            "location": {"row": 3, "column": 1},
            "message": "bug",
        },
        {
            "code": "W291",
            "filename": "app.py",
            "location": {"row": 3, "column": 2},
            "message": "readability",
        },
    ]

    run = RuffAnalyzer(runner=FakeRunner(stdout=json.dumps(payload))).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert len(run.findings) == 4
    by_rule = {finding.rule_id: finding for finding in run.findings}
    assert by_rule["S307"].category is FindingCategory.SECURITY
    assert by_rule["S307"].severity is FindingSeverity.HIGH
    assert by_rule["S307"].message == "Ruff reported S307."
    assert by_rule["S307"].start_line == 1
    assert by_rule["S307"].end_column == 2
    assert by_rule["S307"].suggested_replacement == "safe_call()"
    assert by_rule["S307"].autofix_safe is True
    assert by_rule["S307"].properties["replacement_end_column"] == 12
    assert by_rule["PERF203"].category is FindingCategory.PERFORMANCE
    assert by_rule["PERF203"].severity is FindingSeverity.MEDIUM
    assert by_rule["PERF203"].suggestion is not None
    assert by_rule["F401"].category is FindingCategory.BUG
    assert by_rule["W291"].category is FindingCategory.READABILITY


@pytest.mark.parametrize(
    "fix",
    [
        None,
        {"applicability": "unsafe"},
        {"applicability": "safe", "edits": []},
        {"applicability": "safe", "edits": [None]},
        {
            "applicability": "safe",
            "edits": [{"location": None, "end_location": {}, "content": "replacement"}],
        },
        {
            "applicability": "safe",
            "edits": [
                {
                    "location": {"row": 1, "column": 1},
                    "end_location": {"row": 1, "column": 2},
                    "content": "x" * 20_001,
                }
            ],
        },
        {
            "applicability": "safe",
            "edits": [
                {
                    "location": {"row": 0, "column": 1},
                    "end_location": {"row": 1, "column": 2},
                    "content": "replacement",
                }
            ],
        },
    ],
)
def test_ruff_safe_fix_fails_closed_for_untrusted_edit_shapes(fix: object) -> None:
    assert _safe_fix(fix) == (None, False, {})


def test_semgrep_returns_success_without_launching_for_unsupported_sources(tmp_path: Path) -> None:
    markdown = SourceFile.create("README.md", "text\n", language_hint="markdown")
    patch = SourceFile.create("change.patch", "diff\n", is_patch=True)

    run = SemgrepAnalyzer(runner=ExplodingRunner()).analyze([markdown, patch], tmp_path)

    assert run.status is AnalyzerStatus.NOT_APPLICABLE
    assert run.findings == ()
    assert "No Semgrep-supported" in run.message


def test_semgrep_preserves_terminal_process_failure(tmp_path: Path) -> None:
    source = _python_source(tmp_path)
    runner = FakeRunner(
        status=AnalyzerStatus.TIMEOUT,
        exit_code=None,
        message="bounded timeout",
    )

    run = SemgrepAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.TIMEOUT
    assert run.message == "bounded timeout"
    assert run.findings == ()


def test_semgrep_rejects_nonstandard_exit_code(tmp_path: Path) -> None:
    source = _python_source(tmp_path)

    run = SemgrepAnalyzer(runner=FakeRunner(stdout="{}", exit_code=2)).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.ERROR
    assert run.exit_code == 2
    assert "complete security review" in run.message


@pytest.mark.parametrize(
    ("stdout", "expected_message"),
    [
        ("{", "malformed JSON"),
        ("[]", "unexpected result envelope"),
        ('{"results": {}}', "unexpected result envelope"),
        ('{"results": [], "errors": [{"message": "private detail"}]}', "scan error"),
    ],
)
def test_semgrep_rejects_malformed_partial_or_unexpected_json(
    stdout: str,
    expected_message: str,
    tmp_path: Path,
) -> None:
    source = _python_source(tmp_path)

    run = SemgrepAnalyzer(runner=FakeRunner(stdout=stdout)).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.ERROR
    assert expected_message in run.message
    assert "private detail" not in run.message


def test_semgrep_filters_invalid_records_and_applies_conservative_defaults(
    tmp_path: Path,
) -> None:
    source = _python_source(tmp_path, "eval(value)\nwarn(value)\nother(value)\n")
    payload = {
        "results": [
            None,
            {"check_id": "missing-shape"},
            {
                "check_id": "outside",
                "path": str(tmp_path.parent / "outside.py"),
                "start": {"line": 1, "col": 1},
                "extra": {"message": "outside"},
            },
            {
                "check_id": "unknown-metadata",
                "path": "app.py",
                "start": {"line": 0, "col": 0},
                "end": {"line": 0, "col": 0},
                "extra": {
                    "message": None,
                    "severity": "INFO",
                    "metadata": {"category": "unknown", "confidence": "unknown"},
                },
            },
            {
                "check_id": "readability-error",
                "path": "app.py",
                "start": {"line": 2, "col": 1},
                "extra": {
                    "message": "readability",
                    "severity": "ERROR",
                    "metadata": {"category": "readability", "confidence": "low"},
                },
            },
            {
                "check_id": "warning",
                "path": "app.py",
                "start": {"line": 3, "col": 1},
                "extra": {
                    "message": "warning",
                    "severity": "WARNING",
                    "metadata": None,
                },
            },
            {
                "check_id": "security-error",
                "path": "app.py",
                "start": {"line": 3, "col": 2},
                "extra": {
                    "message": "security",
                    "severity": "ERROR",
                    "metadata": None,
                },
            },
        ],
        "errors": "not-a-list",
    }

    run = SemgrepAnalyzer(runner=FakeRunner(stdout=json.dumps(payload))).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert len(run.findings) == 4
    by_rule = {finding.rule_id: finding for finding in run.findings}
    unknown = by_rule["unknown-metadata"]
    assert unknown.category is FindingCategory.SECURITY
    assert unknown.severity is FindingSeverity.LOW
    assert unknown.confidence is FindingConfidence.MEDIUM
    assert unknown.message == "Semgrep found a source pattern that requires review."
    assert unknown.start_line == 1
    assert unknown.end_column == 2
    assert by_rule["readability-error"].severity is FindingSeverity.MEDIUM
    assert by_rule["readability-error"].confidence is FindingConfidence.LOW
    assert by_rule["warning"].severity is FindingSeverity.MEDIUM
    assert by_rule["security-error"].severity is FindingSeverity.HIGH


def test_semgrep_command_redacts_workspace_and_ephemeral_rules_path(tmp_path: Path) -> None:
    source = _python_source(tmp_path)
    runner = FakeRunner(stdout='{"results": [], "errors": []}')

    run = SemgrepAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert "<workspace>/app.py" in run.command
    assert "<temporary-1>" in run.command
    assert not any("patchscope-semgrep-" in argument for argument in run.command)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (SourceFile.create("file.unknown", "x\n", language_hint="go"), True),
        (SourceFile.create("script.TS", "x\n", language_hint="unknown"), True),
        (SourceFile.create("README.md", "x\n", language_hint="markdown"), False),
    ],
)
def test_semgrep_supported_languages_use_hint_then_casefolded_suffix(
    source: SourceFile,
    expected: bool,
) -> None:
    assert _supported(source) is expected
