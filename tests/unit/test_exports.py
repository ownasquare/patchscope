from __future__ import annotations

from patchscope.analyzers.base import (
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
)
from patchscope.exports import export_markdown, export_sarif


def _finding() -> Finding:
    return Finding(
        id="ruff-1",
        analyzer="ruff",
        rule_id="F821",
        category=FindingCategory.BUG,
        severity=FindingSeverity.HIGH,
        message="Undefined name <user input>",
        path="src/app.py",
        start_line=3,
        end_line=3,
        confidence=FindingConfidence.HIGH,
        snippet="value = missing````name",
        suggestion="Define the value before use.",
    )


def test_markdown_export_reports_status_findings_and_safe_fences() -> None:
    review = {
        "title": "Review <unsafe>",
        "summary": "Deterministic result",
        "analyzer_runs": [
            AnalyzerRun("ruff", AnalyzerStatus.SUCCEEDED, (_finding(),), duration_ms=12),
            AnalyzerRun("semgrep", AnalyzerStatus.UNAVAILABLE, message="Not installed"),
        ],
    }

    output = export_markdown(review)

    assert "# Review &lt;unsafe&gt;" in output
    assert "| semgrep | unavailable | 0 | 0 ms |" in output
    assert "src/app.py:3" in output
    assert "`````text" in output
    assert "did not import it" in output


def test_sarif_export_uses_relative_paths_fingerprints_and_notifications() -> None:
    review = {
        "analyzer_runs": [
            AnalyzerRun("ruff", AnalyzerStatus.SUCCEEDED, (_finding(),)),
            {"analyzer": "mypy", "status": "completed", "findings": []},
            AnalyzerRun("semgrep", AnalyzerStatus.TIMEOUT, message="Time limit"),
        ]
    }

    sarif = export_sarif(review)
    runs = sarif["runs"]
    ruff_run = next(run for run in runs if run["tool"]["driver"]["name"] == "ruff")
    mypy_run = next(run for run in runs if run["tool"]["driver"]["name"] == "mypy")
    semgrep_run = next(run for run in runs if run["tool"]["driver"]["name"] == "semgrep")

    assert sarif["version"] == "2.1.0"
    assert (
        ruff_run["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        == "src/app.py"
    )
    assert "primaryLocationLineHash" in ruff_run["results"][0]["partialFingerprints"]
    assert "toolExecutionNotifications" not in ruff_run["invocations"][0]
    assert "toolExecutionNotifications" not in mypy_run["invocations"][0]
    assert semgrep_run["invocations"][0]["executionSuccessful"] is False
    assert semgrep_run["invocations"][0]["toolExecutionNotifications"][0]["level"] == "warning"


def test_exports_preserve_finding_triage_status_and_note() -> None:
    finding = {
        "fingerprint": "triaged-finding",
        "analyzer": "ruff",
        "rule_id": "F821",
        "category": "bug",
        "severity": "high",
        "message": "Undefined name",
        "title": "Undefined name",
        "path": "src/app.py",
        "start_line": 3,
        "end_line": 3,
        "triage": "fixed",
        "triage_note": "Covered by a focused regression test.",
    }
    review = {
        "findings": [finding],
        "analyzer_runs": [{"analyzer": "ruff", "status": "completed"}],
    }

    markdown = export_markdown(review)
    sarif = export_sarif(review)
    result = sarif["runs"][0]["results"][0]

    assert "- Triage: `fixed`" in markdown
    assert "- Triage note: Covered by a focused regression test." in markdown
    assert result["properties"]["triage"] == "fixed"
    assert result["properties"]["triageNote"] == "Covered by a focused regression test."
