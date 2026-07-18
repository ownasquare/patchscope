import pytest
from pydantic import ValidationError

from patchscope.domain import (
    Finding,
    FindingCategory,
    FindingSeverity,
    FindingTriage,
    MergeRecommendation,
    ReviewRequest,
    ReviewSummary,
    SourceFile,
)


def test_source_file_computes_stable_content_identity() -> None:
    first = SourceFile(path="src/app.py", content="print('hello')\n")
    second = SourceFile(path="src/app.py", content="print('hello')\n")

    assert first.sha256 == second.sha256
    assert first.byte_size == len(first.content.encode("utf-8"))


@pytest.mark.parametrize("path", ["", "/tmp/app.py", "../app.py", "src/../../app.py"])
def test_source_file_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        SourceFile(path=path, content="pass\n")


def test_finding_identity_is_stable() -> None:
    first = Finding.build(path="app.py", rule_id="PS001", start_line=3, message="unsafe eval")
    second = Finding.build(path="app.py", rule_id="PS001", start_line=3, message="unsafe eval")

    assert first.fingerprint == second.fingerprint


def test_finding_identity_changes_with_evidence_location() -> None:
    first = Finding.build(path="app.py", rule_id="PS001", start_line=3, message="unsafe eval")
    second = Finding.build(path="app.py", rule_id="PS001", start_line=4, message="unsafe eval")

    assert first.fingerprint != second.fingerprint


def test_review_request_identity_is_independent_of_file_order() -> None:
    python_file = SourceFile(path="app.py", content="print('hello')\n")
    typescript_file = SourceFile(path="web.ts", content="export const value = 1;\n")

    first = ReviewRequest(files=[python_file, typescript_file], title="First display title")
    second = ReviewRequest(files=[typescript_file, python_file], title="Different display title")

    assert first.request_fingerprint == second.request_fingerprint
    assert first.review_id == second.review_id


def test_review_request_rejects_duplicate_paths() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ReviewRequest(
            files=[
                SourceFile(path="app.py", content="one\n"),
                SourceFile(path="app.py", content="two\n"),
            ]
        )


def test_summary_counts_active_findings_and_produces_recommendation() -> None:
    findings = [
        Finding.build(
            path="app.py",
            rule_id="PS001",
            start_line=1,
            message="unsafe eval",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
        ),
        Finding.build(
            path="app.py",
            rule_id="PS002",
            start_line=2,
            message="long function",
            category=FindingCategory.READABILITY,
            severity=FindingSeverity.LOW,
            triage=FindingTriage.IGNORED,
        ),
    ]

    summary = ReviewSummary.from_findings(findings)

    assert summary.total_findings == 2
    assert summary.open_findings == 1
    assert summary.by_category[FindingCategory.SECURITY] == 1
    assert summary.by_severity[FindingSeverity.HIGH] == 1
    assert summary.risk_score == 20
    assert summary.recommendation is MergeRecommendation.REQUEST_CHANGES


def test_domain_models_are_immutable() -> None:
    finding = Finding.build(path="app.py", rule_id="PS001", start_line=1, message="unsafe eval")

    with pytest.raises(ValidationError):
        finding.message = "changed"  # type: ignore[misc]
