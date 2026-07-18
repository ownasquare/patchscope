from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from patchscope.database import Database
from patchscope.domain import (
    AIMetadata,
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingSeverity,
    FindingTriage,
    ReviewRequest,
    ReviewResult,
    ReviewSourceKind,
    ReviewStatus,
    SourceFile,
)
from patchscope.repository import FindingNotFoundError, ReviewNotFoundError, ReviewRepository


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[ReviewRepository]:
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path / 'patchscope.db'}")
    database.create_schema()
    yield ReviewRepository(database)
    database.dispose()


def make_request(
    content: str = "eval(user_input)\n", *, title: str = "Demo review"
) -> ReviewRequest:
    return ReviewRequest(
        source_kind=ReviewSourceKind.TEXT,
        title=title,
        files=[SourceFile(path="demo.py", content=content, language="python")],
    )


def make_result() -> ReviewResult:
    started_at = datetime.now(UTC) - timedelta(milliseconds=25)
    findings = [
        Finding.build(
            path="demo.py",
            rule_id="PS001",
            start_line=1,
            message="Dynamic evaluation can execute untrusted input",
            category=FindingCategory.SECURITY,
            severity=FindingSeverity.HIGH,
            analyzer="patchscope-heuristics",
            evidence="eval(user_input)",
            suggestion="Use an explicit parser for the accepted input grammar.",
        ),
        Finding.build(
            path="demo.py",
            rule_id="E501",
            start_line=1,
            message="Line is too long",
            category=FindingCategory.READABILITY,
            severity=FindingSeverity.LOW,
            analyzer="ruff",
        ),
    ]
    run = AnalyzerRun(
        analyzer="patchscope-heuristics",
        status=AnalyzerStatus.COMPLETED,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        duration_ms=25,
        findings_count=1,
    )
    return ReviewResult(
        findings=findings,
        analyzer_runs=[run],
        stage_trace=["parse", "analyze", "synthesize", "refactor", "score"],
        ai_metadata=AIMetadata(mode="offline"),
    )


def test_review_round_trip_and_result_replacement(repository: ReviewRepository) -> None:
    request = make_request()

    review_id = repository.create_review(request)
    assert repository.create_review(request) == review_id
    repository.mark_running(review_id)
    stored = repository.save_result(review_id, make_result())

    assert stored.id == review_id
    assert stored.status is ReviewStatus.COMPLETED
    assert stored.request.files == request.files
    assert stored.summary.total_findings == len(stored.findings) == 2
    assert [finding.severity for finding in stored.findings] == [
        FindingSeverity.HIGH,
        FindingSeverity.LOW,
    ]
    assert stored.stage_trace == ["parse", "analyze", "synthesize", "refactor", "score"]

    replacement = ReviewResult(findings=[make_result().findings[0]])
    replaced = repository.save_result(review_id, replacement)
    assert replaced.summary.total_findings == 1
    assert len(replaced.findings) == 1


def test_list_reviews_is_stably_paginated(repository: ReviewRepository) -> None:
    created_ids = []
    for index in range(5):
        review_id = repository.create_review(
            make_request(f"print({index})\n", title=f"Review {index}")
        )
        repository.save_result(review_id, ReviewResult())
        created_ids.append(review_id)

    first_page = repository.list_reviews(limit=2, offset=0)
    second_page = repository.list_reviews(limit=2, offset=2)

    assert first_page.total == 5
    assert first_page.has_more is True
    assert second_page.total == 5
    assert set(item.id for item in first_page.items).isdisjoint(
        item.id for item in second_page.items
    )
    assert set(created_ids) == {item.id for item in repository.list_reviews(limit=10).items}


def test_triage_is_persisted_and_preserved_when_result_is_refreshed(
    repository: ReviewRepository,
) -> None:
    review_id = repository.create_review(make_request())
    result = make_result()
    repository.save_result(review_id, result)
    fingerprint = result.findings[0].fingerprint

    updated = repository.update_finding_triage(
        review_id,
        fingerprint,
        FindingTriage.ACKNOWLEDGED,
        note="Owner confirmed; remediation is queued.",
    )
    repository.save_result(review_id, result)
    stored = repository.require_review(review_id)

    assert updated.triage is FindingTriage.ACKNOWLEDGED
    assert updated.triage_note == "Owner confirmed; remediation is queued."
    assert stored.findings[0].triage is FindingTriage.ACKNOWLEDGED
    assert stored.findings[0].triaged_at is not None


def test_finding_pagination_and_filters(repository: ReviewRepository) -> None:
    review_id = repository.create_review(make_request())
    repository.save_result(review_id, make_result())

    page = repository.list_findings(
        review_id,
        category=FindingCategory.SECURITY,
        limit=1,
    )

    assert page.total == 1
    assert page.items[0].category is FindingCategory.SECURITY
    assert page.has_more is False


def test_missing_review_and_finding_raise_typed_errors(repository: ReviewRepository) -> None:
    with pytest.raises(ReviewNotFoundError):
        repository.require_review("rev_missing")

    review_id = repository.create_review(make_request())
    with pytest.raises(FindingNotFoundError):
        repository.update_finding_triage(review_id, "missing", FindingTriage.FIXED)


def test_failed_review_records_bounded_error(repository: ReviewRepository) -> None:
    review_id = repository.create_review(make_request())

    repository.mark_failed(review_id, "provider timeout")
    stored = repository.require_review(review_id)

    assert stored.status is ReviewStatus.FAILED
    assert stored.error_message == "provider timeout"
    assert stored.completed_at is not None
