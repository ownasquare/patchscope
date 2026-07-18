from __future__ import annotations

from dataclasses import dataclass

import pytest

from patchscope.agent.model import EvidenceSynthesizer, ModelFinding, ModelReview


@dataclass
class Source:
    path: str
    content: str


class StructuredModel:
    def __init__(self, response: ModelReview) -> None:
        self.response = response

    def with_structured_output(self, _schema: object) -> StructuredModel:
        return self

    def invoke(self, _messages: object) -> ModelReview:
        return self.response


def test_offline_synthesis_deduplicates_and_preserves_provenance() -> None:
    finding = {
        "rule_id": "PS001",
        "path": "app.py",
        "start_line": 1,
        "end_line": 1,
        "category": "security",
        "severity": "high",
        "message": "Dynamic evaluation executes untrusted input.",
        "evidence": "eval(value)",
        "suggestion": "Use a strict parser.",
        "analyzer": "heuristics",
    }
    findings, metadata, warnings = EvidenceSynthesizer(mode="offline").synthesize(
        files=[Source("app.py", "eval(value)")],
        parse_summaries=[],
        analyzer_findings=[finding, finding],
        metadata={},
    )
    assert len(findings) == 1
    assert findings[0]["sources"] == ["heuristics"]
    assert metadata["mode"] == "offline"
    assert warnings == []


def test_model_finding_requires_exact_evidence() -> None:
    response = ModelReview(
        summary="One verified issue.",
        findings=[
            ModelFinding(
                title="Unsafe dynamic evaluation",
                description="Input reaches eval.",
                category="security",
                severity="high",
                path="app.py",
                start_line=1,
                end_line=1,
                evidence="eval(value)",
                suggestion="Use a strict parser.",
                confidence=0.9,
            ),
            ModelFinding(
                title="Invented database query",
                description="A query was imagined.",
                category="performance",
                severity="medium",
                path="app.py",
                start_line=1,
                end_line=1,
                evidence="SELECT * FROM imaginary",
                suggestion="Batch it.",
                confidence=0.4,
            ),
        ],
    )
    findings, metadata, _ = EvidenceSynthesizer(
        mode="openai", model_factory=lambda: StructuredModel(response)
    ).synthesize(
        files=[Source("app.py", "eval(value)")],
        parse_summaries=[],
        analyzer_findings=[],
        metadata={},
    )
    assert [finding["title"] for finding in findings] == ["Unsafe dynamic evaluation"]
    assert metadata["accepted_model_findings"] == 1


def test_model_finding_requires_evidence_inside_its_declared_line_range() -> None:
    response = ModelReview(
        summary="Only the correctly located issue is accepted.",
        findings=[
            ModelFinding(
                title="Wrongly located dynamic evaluation",
                description="The evidence exists, but not at the claimed location.",
                category="security",
                severity="high",
                path="app.py",
                start_line=1,
                end_line=1,
                evidence="eval(value)",
                suggestion="Use a strict parser.",
                confidence=0.9,
            ),
            ModelFinding(
                title="Correctly located dynamic evaluation",
                description="The declared range contains the exact evidence.",
                category="security",
                severity="high",
                path="app.py",
                start_line=2,
                end_line=2,
                evidence="eval(value)",
                suggestion="Use a strict parser.",
                confidence=0.9,
            ),
        ],
    )

    findings, metadata, _ = EvidenceSynthesizer(
        mode="openai", model_factory=lambda: StructuredModel(response)
    ).synthesize(
        files=[Source("app.py", "safe()\neval(value)\n")],
        parse_summaries=[],
        analyzer_findings=[],
        metadata={},
    )

    assert [finding["title"] for finding in findings] == ["Correctly located dynamic evaluation"]
    assert metadata["accepted_model_findings"] == 1


def test_auto_mode_falls_back_without_dropping_static_findings() -> None:
    def broken_model() -> object:
        raise RuntimeError("provider unavailable")

    findings, metadata, warnings = EvidenceSynthesizer(
        mode="auto", model_factory=broken_model
    ).synthesize(
        files=[Source("app.py", "eval(value)")],
        parse_summaries=[],
        analyzer_findings=[{"path": "app.py", "message": "Static issue"}],
        metadata={},
    )
    assert len(findings) == 1
    assert metadata["mode"] == "offline_fallback"
    assert warnings and "deterministic findings were preserved" in warnings[0]


def test_forced_openai_requires_credentials() -> None:
    with pytest.raises(ValueError, match="server-side API key"):
        EvidenceSynthesizer(mode="openai").synthesize(
            files=[], parse_summaries=[], analyzer_findings=[], metadata={}
        )
