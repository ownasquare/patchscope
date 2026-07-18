"""Shared, serialization-friendly analyzer contracts."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from patchscope.intake import SourceFile, validate_relative_path


class AnalyzerStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"


class FindingCategory(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    READABILITY = "readability"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Finding:
    """One normalized, source-located review finding."""

    id: str
    analyzer: str
    rule_id: str
    category: FindingCategory
    severity: FindingSeverity
    message: str
    path: str
    start_line: int
    end_line: int
    start_column: int = 1
    end_column: int = 1
    confidence: FindingConfidence = FindingConfidence.MEDIUM
    snippet: str | None = None
    suggestion: str | None = None
    suggested_replacement: str | None = None
    autofix_safe: bool = False
    fingerprint: str = ""
    properties: dict[str, str | int | float | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_relative_path(self.path))
        object.__setattr__(self, "category", FindingCategory(self.category))
        object.__setattr__(self, "severity", FindingSeverity(self.severity))
        object.__setattr__(self, "confidence", FindingConfidence(self.confidence))
        for name in ("id", "analyzer", "rule_id", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be blank")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("finding line range is invalid")
        if self.start_column < 1 or self.end_column < 1:
            raise ValueError("finding column range is invalid")
        if self.snippet is not None and len(self.snippet) > 4_000:
            raise ValueError("finding snippet is too large")
        if self.suggested_replacement is not None and len(self.suggested_replacement) > 20_000:
            raise ValueError("suggested replacement is too large")
        if not self.fingerprint:
            object.__setattr__(
                self,
                "fingerprint",
                finding_fingerprint(
                    self.analyzer,
                    self.rule_id,
                    self.path,
                    self.start_line,
                    self.message,
                ),
            )

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        result = asdict(self)
        result["category"] = self.category.value
        result["severity"] = self.severity.value
        result["confidence"] = self.confidence.value
        return result


@dataclass(frozen=True, slots=True)
class AnalyzerRun:
    """One analyzer's explicit outcome, including non-success terminal states."""

    analyzer: str
    status: AnalyzerStatus
    findings: tuple[Finding, ...] = ()
    duration_ms: int = 0
    command: tuple[str, ...] = ()
    message: str = ""
    exit_code: int | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if not self.analyzer.strip():
            raise ValueError("analyzer cannot be blank")
        object.__setattr__(self, "status", AnalyzerStatus(self.status))
        if self.duration_ms < 0:
            raise ValueError("duration_ms cannot be negative")
        if self.status is not AnalyzerStatus.SUCCEEDED and self.findings:
            raise ValueError("non-success analyzer runs cannot claim findings")

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "analyzer": self.analyzer,
            "status": self.status.value,
            "findings": [finding.model_dump() for finding in self.findings],
            "duration_ms": self.duration_ms,
            "command": list(self.command),
            "message": self.message,
            "exit_code": self.exit_code,
            "version": self.version,
        }


class AnalyzerAdapter(Protocol):
    name: str

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun: ...


def finding_fingerprint(
    analyzer: str,
    rule_id: str,
    path: str,
    start_line: int,
    message: str,
) -> str:
    normalized_message = " ".join(message.split()).casefold()
    value = "\x00".join((analyzer, rule_id, path, str(start_line), normalized_message))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finding_id(analyzer: str, rule_id: str, path: str, line: int, message: str) -> str:
    return f"{analyzer}-{finding_fingerprint(analyzer, rule_id, path, line, message)[:16]}"


__all__ = [
    "AnalyzerAdapter",
    "AnalyzerRun",
    "AnalyzerStatus",
    "Finding",
    "FindingCategory",
    "FindingConfidence",
    "FindingSeverity",
    "finding_fingerprint",
    "finding_id",
]
