"""Immutable domain contracts shared by PatchScope's API, workflow, and storage."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return an aware UTC timestamp suitable for durable records."""

    return datetime.now(UTC)


def _stable_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_source_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or normalized == "." or path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be a safe relative source path")
    return path.as_posix()


class FindingCategory(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    READABILITY = "readability"
    MAINTAINABILITY = "maintainability"
    TESTING = "testing"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingTriage(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    FIXED = "fixed"
    IGNORED = "ignored"
    FALSE_POSITIVE = "false_positive"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewSourceKind(StrEnum):
    TEXT = "text"
    FILE = "file"
    GITHUB = "github"


class AnalyzerStatus(StrEnum):
    COMPLETED = "completed"
    NOT_APPLICABLE = "not_applicable"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


class MergeRecommendation(StrEnum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class DomainModel(BaseModel):
    """Strict immutable base class for values crossing process boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class SourceFile(DomainModel):
    path: str
    content: str
    language: str | None = None
    sha256: str = ""
    byte_size: int = Field(default=0, ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalize_source_path(value)

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str | None) -> str | None:
        return value.casefold() if value else None

    @model_validator(mode="after")
    def validate_content_digest(self) -> Self:
        expected = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        expected_size = len(self.content.encode("utf-8"))
        if self.sha256 and not hmac.compare_digest(self.sha256, expected):
            raise ValueError("sha256 does not match source content")
        if self.byte_size and self.byte_size != expected_size:
            raise ValueError("byte_size does not match source content")
        object.__setattr__(self, "sha256", expected)
        object.__setattr__(self, "byte_size", expected_size)
        return self


class ReviewRequest(DomainModel):
    source_kind: ReviewSourceKind = ReviewSourceKind.TEXT
    source_reference: str | None = None
    title: str | None = None
    files: list[SourceFile] = Field(min_length=1, max_length=1_000)
    ai_mode: Literal["auto", "offline", "openai"] = "auto"
    metadata: dict[str, str] = Field(default_factory=dict)
    request_fingerprint: str = ""
    review_id: str = ""

    @field_validator("files")
    @classmethod
    def validate_unique_paths(cls, value: list[SourceFile]) -> list[SourceFile]:
        paths = [source.path for source in value]
        if len(paths) != len(set(paths)):
            raise ValueError("source file paths must be unique")
        return value

    @model_validator(mode="after")
    def fill_identity(self) -> Self:
        payload = {
            "ai_mode": self.ai_mode,
            "files": sorted(
                ({"path": source.path, "sha256": source.sha256} for source in self.files),
                key=lambda item: item["path"],
            ),
            "source_kind": self.source_kind.value,
            "source_reference": self.source_reference or "",
        }
        fingerprint = _stable_digest(payload)
        review_id = f"rev_{fingerprint[:24]}"
        if self.request_fingerprint and not hmac.compare_digest(
            self.request_fingerprint, fingerprint
        ):
            raise ValueError("request_fingerprint does not match review input")
        if self.review_id and not hmac.compare_digest(self.review_id, review_id):
            raise ValueError("review_id does not match review input")
        object.__setattr__(self, "request_fingerprint", fingerprint)
        object.__setattr__(self, "review_id", review_id)
        return self


class Finding(DomainModel):
    fingerprint: str = ""
    path: str
    rule_id: str
    start_line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    start_column: int | None = Field(default=None, ge=1)
    end_column: int | None = Field(default=None, ge=1)
    message: str = Field(min_length=1)
    title: str | None = None
    category: FindingCategory = FindingCategory.BUG
    severity: FindingSeverity = FindingSeverity.MEDIUM
    analyzer: str = "patchscope"
    evidence: str = ""
    suggestion: str = ""
    refactor_diff: str | None = None
    triage: FindingTriage = FindingTriage.OPEN
    triage_note: str | None = Field(default=None, max_length=2_000)
    triaged_at: datetime | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _normalize_source_path(value)

    @model_validator(mode="after")
    def fill_derived_fields(self) -> Self:
        end_line = self.end_line or self.start_line
        if end_line < self.start_line:
            raise ValueError("end_line cannot precede start_line")
        object.__setattr__(self, "end_line", end_line)
        if not self.title:
            object.__setattr__(self, "title", self.message)

        expected = self.identity_for(
            path=self.path,
            rule_id=self.rule_id,
            start_line=self.start_line,
            message=self.message,
        )
        if self.fingerprint and not hmac.compare_digest(self.fingerprint, expected):
            raise ValueError("finding fingerprint does not match its identity fields")
        object.__setattr__(self, "fingerprint", expected)
        return self

    @classmethod
    def identity_for(cls, *, path: str, rule_id: str, start_line: int, message: str) -> str:
        return _stable_digest(
            {
                "message": " ".join(message.split()),
                "path": _normalize_source_path(path),
                "rule_id": rule_id.strip().casefold(),
                "start_line": start_line,
            }
        )

    @classmethod
    def build(
        cls,
        *,
        path: str,
        rule_id: str,
        start_line: int,
        message: str,
        **values: Any,
    ) -> Finding:
        return cls(
            path=path,
            rule_id=rule_id,
            start_line=start_line,
            message=message,
            **values,
        )


class AnalyzerRun(DomainModel):
    analyzer: str = Field(min_length=1)
    status: AnalyzerStatus
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    duration_ms: int = Field(default=0, ge=0)
    findings_count: int = Field(default=0, ge=0)
    detail: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self


class AIMetadata(DomainModel):
    mode: Literal["offline", "openai"] = "offline"
    provider: str | None = None
    model: str | None = None
    fallback_reason: str | None = Field(default=None, max_length=2_000)


_ACTIVE_TRIAGE = {FindingTriage.OPEN, FindingTriage.ACKNOWLEDGED}
_SEVERITY_WEIGHTS = {
    FindingSeverity.CRITICAL: 40,
    FindingSeverity.HIGH: 20,
    FindingSeverity.MEDIUM: 8,
    FindingSeverity.LOW: 3,
    FindingSeverity.INFO: 0,
}


class ReviewSummary(DomainModel):
    total_findings: int = Field(default=0, ge=0)
    open_findings: int = Field(default=0, ge=0)
    by_category: dict[FindingCategory, int] = Field(
        default_factory=lambda: {category: 0 for category in FindingCategory}
    )
    by_severity: dict[FindingSeverity, int] = Field(
        default_factory=lambda: {severity: 0 for severity in FindingSeverity}
    )
    risk_score: int = Field(default=0, ge=0, le=100)
    recommendation: MergeRecommendation = MergeRecommendation.APPROVE

    @classmethod
    def from_findings(cls, findings: list[Finding]) -> ReviewSummary:
        return summarize_finding_values(
            (finding.category, finding.severity, finding.triage) for finding in findings
        )


def summarize_finding_values(
    values: Iterable[tuple[FindingCategory, FindingSeverity, FindingTriage]],
) -> ReviewSummary:
    """Apply PatchScope's single authoritative risk and recommendation policy."""

    by_category = {category: 0 for category in FindingCategory}
    by_severity = {severity: 0 for severity in FindingSeverity}
    total = 0
    active_severities: list[FindingSeverity] = []
    for category, severity, triage in values:
        total += 1
        by_category[category] += 1
        by_severity[severity] += 1
        if triage in _ACTIVE_TRIAGE:
            active_severities.append(severity)

    risk_score = min(100, sum(_SEVERITY_WEIGHTS[severity] for severity in active_severities))
    active_severity_set = set(active_severities)
    if active_severity_set & {FindingSeverity.CRITICAL, FindingSeverity.HIGH} or risk_score >= 50:
        recommendation = MergeRecommendation.REQUEST_CHANGES
    elif active_severities:
        recommendation = MergeRecommendation.COMMENT
    else:
        recommendation = MergeRecommendation.APPROVE

    return ReviewSummary(
        total_findings=total,
        open_findings=len(active_severities),
        by_category=by_category,
        by_severity=by_severity,
        risk_score=risk_score,
        recommendation=recommendation,
    )


class ReviewResult(DomainModel):
    findings: list[Finding] = Field(default_factory=list)
    analyzer_runs: list[AnalyzerRun] = Field(default_factory=list)
    summary: ReviewSummary = Field(default_factory=ReviewSummary)
    stage_trace: list[str] = Field(default_factory=list)
    ai_metadata: AIMetadata = Field(default_factory=AIMetadata)

    @model_validator(mode="after")
    def derive_summary(self) -> Self:
        object.__setattr__(self, "summary", ReviewSummary.from_findings(self.findings))
        return self


class ReviewDetail(DomainModel):
    id: str
    status: ReviewStatus
    request: ReviewRequest
    summary: ReviewSummary = Field(default_factory=ReviewSummary)
    findings: list[Finding] = Field(default_factory=list)
    analyzer_runs: list[AnalyzerRun] = Field(default_factory=list)
    stage_trace: list[str] = Field(default_factory=list)
    ai_metadata: AIMetadata = Field(default_factory=AIMetadata)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    error_message: str | None = None


class ReviewListItem(DomainModel):
    id: str
    title: str
    source_kind: ReviewSourceKind
    status: ReviewStatus
    summary: ReviewSummary
    created_at: datetime
    updated_at: datetime


class ReviewPage(DomainModel):
    items: list[ReviewListItem]
    total: int = Field(ge=0)
    limit: int = Field(gt=0)
    offset: int = Field(ge=0)
    has_more: bool = False

    @model_validator(mode="after")
    def derive_has_more(self) -> Self:
        object.__setattr__(self, "has_more", self.offset + len(self.items) < self.total)
        return self


class FindingPage(DomainModel):
    items: list[Finding]
    total: int = Field(ge=0)
    limit: int = Field(gt=0)
    offset: int = Field(ge=0)
    has_more: bool = False

    @model_validator(mode="after")
    def derive_has_more(self) -> Self:
        object.__setattr__(self, "has_more", self.offset + len(self.items) < self.total)
        return self


__all__ = [
    "AIMetadata",
    "AnalyzerRun",
    "AnalyzerStatus",
    "Finding",
    "FindingCategory",
    "FindingPage",
    "FindingSeverity",
    "FindingTriage",
    "MergeRecommendation",
    "ReviewDetail",
    "ReviewListItem",
    "ReviewPage",
    "ReviewRequest",
    "ReviewResult",
    "ReviewSourceKind",
    "ReviewStatus",
    "ReviewSummary",
    "SourceFile",
    "summarize_finding_values",
    "utc_now",
]
