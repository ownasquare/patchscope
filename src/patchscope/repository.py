"""Transactional persistence and triage operations for PatchScope reviews."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from patchscope.database import AnalyzerRunRow, Database, FindingRow, ReviewFileRow, ReviewRow
from patchscope.domain import (
    AIMetadata,
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingPage,
    FindingSeverity,
    FindingTriage,
    ReviewDetail,
    ReviewListItem,
    ReviewPage,
    ReviewRequest,
    ReviewResult,
    ReviewSourceKind,
    ReviewStatus,
    ReviewSummary,
    SourceFile,
    utc_now,
)


class RepositoryError(RuntimeError):
    """Base class for expected persistence failures."""


class ReviewNotFoundError(RepositoryError):
    def __init__(self, review_id: str) -> None:
        super().__init__(f"Review '{review_id}' was not found")
        self.review_id = review_id


class FindingNotFoundError(RepositoryError):
    def __init__(self, review_id: str, fingerprint: str) -> None:
        super().__init__(f"Finding '{fingerprint}' was not found in review '{review_id}'")
        self.review_id = review_id
        self.fingerprint = fingerprint


class RepositoryConflictError(RepositoryError):
    """Raised when a deterministic identity collides with different content."""


_SEVERITY_ORDER = {
    FindingSeverity.CRITICAL: 0,
    FindingSeverity.HIGH: 1,
    FindingSeverity.MEDIUM: 2,
    FindingSeverity.LOW: 3,
    FindingSeverity.INFO: 4,
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _json_dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _record_id(prefix: str, *parts: str) -> str:
    payload = "\x00".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _finding_sort_key(finding: Finding) -> tuple[int, str, int, str, str]:
    return (
        _SEVERITY_ORDER[finding.severity],
        finding.path.casefold(),
        finding.start_line,
        finding.rule_id.casefold(),
        finding.fingerprint,
    )


class ReviewRepository:
    """Provide deterministic, short-transaction review persistence."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def ping(self) -> bool:
        """Return whether a trivial database read succeeds."""

        try:
            with self._database.session_factory() as session:
                session.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def close(self) -> None:
        """Release pooled database resources."""

        self._database.dispose()

    def create_review(self, request: ReviewRequest) -> str:
        """Create a pending review, returning the existing ID for identical input."""

        summary = ReviewSummary()
        now = utc_now()
        review_id = request.review_id
        title = (request.title or request.source_reference or request.files[0].path).strip()[:300]

        with self._database.session_factory() as session:
            existing = session.scalar(
                select(ReviewRow).where(
                    ReviewRow.request_fingerprint == request.request_fingerprint
                )
            )
            if existing is not None:
                return existing.id

            session.add(
                ReviewRow(
                    id=review_id,
                    request_fingerprint=request.request_fingerprint,
                    source_kind=request.source_kind.value,
                    source_reference=request.source_reference,
                    title=title,
                    ai_mode=request.ai_mode,
                    request_metadata_json=_json_dump(request.metadata),
                    status=ReviewStatus.PENDING.value,
                    summary_json=_json_dump(summary),
                    stage_trace_json="[]",
                    ai_metadata_json=_json_dump(AIMetadata()),
                    total_findings=0,
                    risk_score=0,
                    recommendation=summary.recommendation.value,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
            )
            # These mappings intentionally avoid ORM relationships. Flush the parent so
            # SQLite's foreign-key enforcement cannot observe child rows first.
            session.flush()
            for position, source in enumerate(request.files):
                session.add(
                    ReviewFileRow(
                        id=_record_id("file", review_id, source.path),
                        review_id=review_id,
                        position=position,
                        path=source.path,
                        language=source.language,
                        sha256=source.sha256,
                        byte_size=source.byte_size,
                        content=source.content,
                    )
                )

            try:
                session.commit()
            except IntegrityError as error:
                session.rollback()
                existing = session.scalar(
                    select(ReviewRow).where(
                        ReviewRow.request_fingerprint == request.request_fingerprint
                    )
                )
                if existing is not None:
                    return existing.id
                raise RepositoryConflictError(
                    "A deterministic review identity collided with different stored content"
                ) from error
        return review_id

    def mark_running(self, review_id: str) -> None:
        with self._database.session_factory.begin() as session:
            row = self._require_row(session, review_id)
            row.status = ReviewStatus.RUNNING.value
            row.error_message = None
            row.completed_at = None
            row.updated_at = utc_now()

    def mark_failed(self, review_id: str, error_message: str) -> None:
        normalized_error = error_message.strip()[:4_000] or "Review failed"
        with self._database.session_factory.begin() as session:
            row = self._require_row(session, review_id)
            now = utc_now()
            row.status = ReviewStatus.FAILED.value
            row.error_message = normalized_error
            row.completed_at = now
            row.updated_at = now

    def save_result(self, review_id: str, result: ReviewResult) -> ReviewDetail:
        """Atomically replace generated results while preserving matching user triage."""

        with self._database.session_factory.begin() as session:
            review = self._require_row(session, review_id)
            existing_triage = {
                row.fingerprint: (row.triage, row.triage_note, row.triaged_at)
                for row in session.scalars(
                    select(FindingRow).where(FindingRow.review_id == review_id)
                )
            }
            session.execute(delete(FindingRow).where(FindingRow.review_id == review_id))
            session.execute(delete(AnalyzerRunRow).where(AnalyzerRunRow.review_id == review_id))

            deduplicated: dict[str, Finding] = {}
            for finding in sorted(result.findings, key=_finding_sort_key):
                deduplicated.setdefault(finding.fingerprint, finding)

            persisted_findings: list[Finding] = []
            for position, finding in enumerate(deduplicated.values()):
                triage = finding.triage
                triage_note = finding.triage_note
                triaged_at = finding.triaged_at
                if finding.fingerprint in existing_triage:
                    stored_triage, stored_note, stored_at = existing_triage[finding.fingerprint]
                    triage = FindingTriage(stored_triage)
                    triage_note = stored_note
                    triaged_at = _utc(stored_at)
                    finding = finding.model_copy(
                        update={
                            "triage": triage,
                            "triage_note": triage_note,
                            "triaged_at": triaged_at,
                        }
                    )
                persisted_findings.append(finding)
                session.add(self._finding_row(review_id, position, finding))

            analyzer_runs = {run.analyzer: run for run in result.analyzer_runs}
            for position, analyzer in enumerate(sorted(analyzer_runs)):
                run = analyzer_runs[analyzer]
                session.add(
                    AnalyzerRunRow(
                        id=_record_id("run", review_id, analyzer),
                        review_id=review_id,
                        position=position,
                        analyzer=run.analyzer,
                        status=run.status.value,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        duration_ms=run.duration_ms,
                        findings_count=run.findings_count,
                        detail=run.detail,
                    )
                )

            summary = ReviewSummary.from_findings(persisted_findings)
            now = utc_now()
            review.status = ReviewStatus.COMPLETED.value
            review.summary_json = _json_dump(summary)
            review.stage_trace_json = _json_dump(result.stage_trace)
            review.ai_metadata_json = _json_dump(result.ai_metadata)
            review.total_findings = summary.total_findings
            review.risk_score = summary.risk_score
            review.recommendation = summary.recommendation.value
            review.error_message = None
            review.updated_at = now
            review.completed_at = now

        return self.require_review(review_id)

    def get_review(self, review_id: str) -> ReviewDetail | None:
        with self._database.session_factory() as session:
            row = session.get(ReviewRow, review_id)
            if row is None:
                return None
            return self._detail_from_row(session, row)

    def require_review(self, review_id: str) -> ReviewDetail:
        review = self.get_review(review_id)
        if review is None:
            raise ReviewNotFoundError(review_id)
        return review

    def list_reviews(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        status: ReviewStatus | None = None,
    ) -> ReviewPage:
        self._validate_page(limit, offset)
        conditions = []
        if status is not None:
            conditions.append(ReviewRow.status == status.value)

        with self._database.session_factory() as session:
            total_statement = select(func.count()).select_from(ReviewRow).where(*conditions)
            total = int(session.scalar(total_statement) or 0)
            statement = (
                select(ReviewRow)
                .where(*conditions)
                .order_by(ReviewRow.created_at.desc(), ReviewRow.id.desc())
                .offset(offset)
                .limit(limit)
            )
            rows = session.scalars(statement).all()
            items = [self._list_item_from_row(row) for row in rows]
        return ReviewPage(items=items, total=total, limit=limit, offset=offset)

    def list_findings(
        self,
        review_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        category: FindingCategory | None = None,
        severity: FindingSeverity | None = None,
        triage: FindingTriage | None = None,
    ) -> FindingPage:
        self._validate_page(limit, offset)
        with self._database.session_factory() as session:
            self._require_row(session, review_id)
            conditions = [FindingRow.review_id == review_id]
            if category is not None:
                conditions.append(FindingRow.category == category.value)
            if severity is not None:
                conditions.append(FindingRow.severity == severity.value)
            if triage is not None:
                conditions.append(FindingRow.triage == triage.value)

            total = int(
                session.scalar(select(func.count()).select_from(FindingRow).where(*conditions)) or 0
            )
            rows = session.scalars(
                select(FindingRow)
                .where(*conditions)
                .order_by(FindingRow.position, FindingRow.id)
                .offset(offset)
                .limit(limit)
            ).all()
            items = [self._finding_from_row(row) for row in rows]
        return FindingPage(items=items, total=total, limit=limit, offset=offset)

    def update_finding_triage(
        self,
        review_id: str,
        fingerprint: str,
        triage: FindingTriage,
        note: str | None = None,
    ) -> Finding:
        normalized_note = note.strip() if note and note.strip() else None
        if normalized_note and len(normalized_note) > 2_000:
            raise ValueError("triage note cannot exceed 2000 characters")

        with self._database.session_factory.begin() as session:
            review = self._require_row(session, review_id)
            finding = session.scalar(
                select(FindingRow).where(
                    FindingRow.review_id == review_id,
                    FindingRow.fingerprint == fingerprint,
                )
            )
            if finding is None:
                raise FindingNotFoundError(review_id, fingerprint)

            finding.triage = triage.value
            finding.triage_note = normalized_note
            finding.triaged_at = None if triage is FindingTriage.OPEN else utc_now()

            all_findings = [
                self._finding_from_row(row)
                for row in session.scalars(
                    select(FindingRow)
                    .where(FindingRow.review_id == review_id)
                    .order_by(FindingRow.position)
                )
            ]
            summary = ReviewSummary.from_findings(all_findings)
            self._apply_summary(review, summary)
            review.updated_at = utc_now()
            updated = self._finding_from_row(finding)
        return updated

    @staticmethod
    def _validate_page(limit: int, offset: int) -> None:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        if offset < 0:
            raise ValueError("offset cannot be negative")

    @staticmethod
    def _require_row(session: Session, review_id: str) -> ReviewRow:
        row = session.get(ReviewRow, review_id)
        if row is None:
            raise ReviewNotFoundError(review_id)
        return row

    @staticmethod
    def _apply_summary(review: ReviewRow, summary: ReviewSummary) -> None:
        review.summary_json = _json_dump(summary)
        review.total_findings = summary.total_findings
        review.risk_score = summary.risk_score
        review.recommendation = summary.recommendation.value

    @staticmethod
    def _finding_row(review_id: str, position: int, finding: Finding) -> FindingRow:
        return FindingRow(
            id=_record_id("finding", review_id, finding.fingerprint),
            review_id=review_id,
            position=position,
            fingerprint=finding.fingerprint,
            path=finding.path,
            rule_id=finding.rule_id,
            start_line=finding.start_line,
            end_line=finding.end_line or finding.start_line,
            start_column=finding.start_column,
            end_column=finding.end_column,
            message=finding.message,
            title=finding.title or finding.message,
            category=finding.category.value,
            severity=finding.severity.value,
            analyzer=finding.analyzer,
            evidence=finding.evidence,
            suggestion=finding.suggestion,
            refactor_diff=finding.refactor_diff,
            triage=finding.triage.value,
            triage_note=finding.triage_note,
            triaged_at=finding.triaged_at,
        )

    @staticmethod
    def _finding_from_row(row: FindingRow) -> Finding:
        return Finding(
            fingerprint=row.fingerprint,
            path=row.path,
            rule_id=row.rule_id,
            start_line=row.start_line,
            end_line=row.end_line,
            start_column=row.start_column,
            end_column=row.end_column,
            message=row.message,
            title=row.title,
            category=FindingCategory(row.category),
            severity=FindingSeverity(row.severity),
            analyzer=row.analyzer,
            evidence=row.evidence,
            suggestion=row.suggestion,
            refactor_diff=row.refactor_diff,
            triage=FindingTriage(row.triage),
            triage_note=row.triage_note,
            triaged_at=_utc(row.triaged_at),
        )

    @staticmethod
    def _analyzer_from_row(row: AnalyzerRunRow) -> AnalyzerRun:
        return AnalyzerRun(
            analyzer=row.analyzer,
            status=AnalyzerStatus(row.status),
            started_at=_utc(row.started_at) or utc_now(),
            finished_at=_utc(row.finished_at),
            duration_ms=row.duration_ms,
            findings_count=row.findings_count,
            detail=row.detail,
        )

    def _detail_from_row(self, session: Session, row: ReviewRow) -> ReviewDetail:
        file_rows = session.scalars(
            select(ReviewFileRow)
            .where(ReviewFileRow.review_id == row.id)
            .order_by(ReviewFileRow.position, ReviewFileRow.id)
        ).all()
        files = [
            SourceFile(
                path=file_row.path,
                content=file_row.content,
                language=file_row.language,
                sha256=file_row.sha256,
            )
            for file_row in file_rows
        ]
        finding_rows = session.scalars(
            select(FindingRow)
            .where(FindingRow.review_id == row.id)
            .order_by(FindingRow.position, FindingRow.id)
        ).all()
        findings = [self._finding_from_row(finding_row) for finding_row in finding_rows]
        analyzer_rows = session.scalars(
            select(AnalyzerRunRow)
            .where(AnalyzerRunRow.review_id == row.id)
            .order_by(AnalyzerRunRow.position, AnalyzerRunRow.id)
        ).all()
        analyzer_runs = [self._analyzer_from_row(analyzer_row) for analyzer_row in analyzer_rows]
        metadata = _json_load(row.request_metadata_json, {})
        if not isinstance(metadata, dict):
            metadata = {}
        request = ReviewRequest(
            source_kind=ReviewSourceKind(row.source_kind),
            source_reference=row.source_reference,
            title=row.title,
            files=files,
            ai_mode=row.ai_mode,
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        summary = ReviewSummary.from_findings(findings)
        stage_trace = _json_load(row.stage_trace_json, [])
        if not isinstance(stage_trace, list):
            stage_trace = []
        ai_metadata_value = _json_load(row.ai_metadata_json, {})
        if not isinstance(ai_metadata_value, dict):
            ai_metadata_value = {}
        return ReviewDetail(
            id=row.id,
            status=ReviewStatus(row.status),
            request=request,
            summary=summary,
            findings=findings,
            analyzer_runs=analyzer_runs,
            stage_trace=[str(stage) for stage in stage_trace],
            ai_metadata=AIMetadata.model_validate(ai_metadata_value),
            created_at=_utc(row.created_at) or utc_now(),
            updated_at=_utc(row.updated_at) or utc_now(),
            completed_at=_utc(row.completed_at),
            error_message=row.error_message,
        )

    @staticmethod
    def _list_item_from_row(row: ReviewRow) -> ReviewListItem:
        summary_value = _json_load(row.summary_json, {})
        if not isinstance(summary_value, dict):
            summary_value = {}
        return ReviewListItem(
            id=row.id,
            title=row.title,
            source_kind=ReviewSourceKind(row.source_kind),
            status=ReviewStatus(row.status),
            summary=ReviewSummary.model_validate(summary_value),
            created_at=_utc(row.created_at) or utc_now(),
            updated_at=_utc(row.updated_at) or utc_now(),
        )


Repository = ReviewRepository

__all__ = [
    "FindingNotFoundError",
    "Repository",
    "RepositoryConflictError",
    "RepositoryError",
    "ReviewNotFoundError",
    "ReviewRepository",
]
