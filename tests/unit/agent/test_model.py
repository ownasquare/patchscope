from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from patchscope.agent.model import EvidenceSynthesizer, ModelFinding, ModelReview


@dataclass
class Source:
    path: str
    content: str


class StructuredModel:
    def __init__(self, response: ModelReview) -> None:
        self.response = response
        self.calls: list[list[tuple[str, str]]] = []

    def with_structured_output(self, _schema: object) -> StructuredModel:
        return self

    def invoke(self, messages: list[tuple[str, str]]) -> ModelReview:
        self.calls.append(messages)
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
    assert metadata["provider"] == "openai"
    assert metadata["model"] == "gpt-5-mini"
    assert metadata["provider_error_type"] == "RuntimeError"
    assert metadata["fallback_reason"] == warnings[-1]
    assert warnings and "deterministic findings were preserved" in warnings[0]


def test_forced_openai_requires_credentials() -> None:
    with pytest.raises(ValueError, match="server-side API key"):
        EvidenceSynthesizer(mode="openai").synthesize(
            files=[], parse_summaries=[], analyzer_findings=[], metadata={}
        )


def test_total_prompt_ceiling_is_exact_deterministic_and_truncates_every_section() -> None:
    response = ModelReview(summary="No additional issues.")
    first_model = StructuredModel(response)
    second_model = StructuredModel(response)
    inputs: dict[str, Any] = {
        "files": [Source("app.py", "s" * 20_000)],
        "parse_summaries": [{"structure": "p" * 20_000}],
        "analyzer_findings": [
            {
                "rule_id": "STATIC-1",
                "path": "app.py",
                "start_line": 1,
                "message": "a" * 20_000,
            }
        ],
        "metadata": {"context": "m" * 20_000},
    }

    first = EvidenceSynthesizer(
        mode="openai",
        model_factory=lambda: first_model,
        max_prompt_chars=4_000,
    ).synthesize(**inputs)
    second = EvidenceSynthesizer(
        mode="openai",
        model_factory=lambda: second_model,
        max_prompt_chars=4_000,
    ).synthesize(**inputs)

    first_findings, first_metadata, first_warnings = first
    assert first_model.calls == second_model.calls
    assert first_metadata["prompt_sections"] == second[1]["prompt_sections"]
    assert first_metadata["prompt_chars"] == 4_000
    assert first_metadata["completion_token_limit"] == 4_096
    assert sum(len(message) for _role, message in first_model.calls[0]) == 4_000
    assert first_metadata["prompt_truncated"] is True
    assert all(section["truncated"] for section in first_metadata["prompt_sections"].values())
    assert [
        first_metadata["prompt_sections"][name]["prompt_chars"]
        for name in ("metadata", "parse_summaries", "analyzer_findings", "sources")
    ] == sorted(
        first_metadata["prompt_sections"][name]["prompt_chars"]
        for name in ("metadata", "parse_summaries", "analyzer_findings", "sources")
    )
    assert "complete local static findings were preserved" in first_warnings[0]
    assert len(first_findings) == 1
    assert len(first_findings[0]["description"]) == 20_000


def test_prompt_truncation_never_drops_or_mutates_unique_static_finding_content() -> None:
    model = StructuredModel(ModelReview(summary="No additional issues."))
    static_findings = [
        {
            "rule_id": f"STATIC-{index}",
            "path": f"src/file_{index}.py",
            "start_line": index + 1,
            "end_line": index + 1,
            "message": f"Static issue {index}: " + (str(index) * 500),
            "category": "maintainability",
            "severity": "low",
            "analyzer": "static-test",
        }
        for index in range(40)
    ]

    findings, metadata, warnings = EvidenceSynthesizer(
        mode="openai",
        model_factory=lambda: model,
        max_prompt_chars=4_000,
    ).synthesize(
        files=[Source(item["path"], "pass\n") for item in static_findings],
        parse_summaries=[{"summary": "p" * 10_000}],
        analyzer_findings=static_findings,
        metadata={"context": "m" * 10_000},
    )

    assert metadata["prompt_truncated"] is True
    assert warnings and "static findings were preserved" in warnings[0]
    assert len(findings) == len(static_findings)
    assert {finding["description"] for finding in findings} == {
        item["message"] for item in static_findings
    }


def test_prompt_truncation_and_provider_fallback_have_separate_metadata() -> None:
    class BrokenStructuredModel(StructuredModel):
        def invoke(self, messages: list[tuple[str, str]]) -> ModelReview:
            self.calls.append(messages)
            raise TimeoutError("provider detail is not public")

    model = BrokenStructuredModel(ModelReview(summary="Unused."))
    findings, metadata, warnings = EvidenceSynthesizer(
        mode="auto",
        model_factory=lambda: model,
        max_prompt_chars=4_000,
    ).synthesize(
        files=[Source("app.py", "s" * 20_000)],
        parse_summaries=[{"summary": "p" * 20_000}],
        analyzer_findings=[{"path": "app.py", "message": "Static issue"}],
        metadata={"context": "m" * 20_000},
    )

    assert len(findings) == 1
    assert metadata["mode"] == "offline_fallback"
    assert metadata["prompt_truncated"] is True
    assert metadata["provider_error_type"] == "TimeoutError"
    assert metadata["completion_token_limit"] == 4_096
    assert "TimeoutError" in metadata["fallback_reason"]
    assert len(warnings) == 2
    assert "truncated" in warnings[0]
    assert "unavailable" in warnings[1]


def test_default_model_passes_completion_budget_without_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("langchain_openai.ChatOpenAI", FakeChatOpenAI)

    model = EvidenceSynthesizer(
        mode="openai",
        api_key="configured",
        model_name="configured-model",
        max_completion_tokens=777,
    )._default_model()

    assert isinstance(model, FakeChatOpenAI)
    assert captured == {
        "model": "configured-model",
        "api_key": "configured",
        "timeout": 30,
        "max_retries": 1,
        "max_completion_tokens": 777,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_prompt_chars": 3_999},
        {"max_prompt_chars": 1_000_001},
        {"max_completion_tokens": 255},
        {"max_completion_tokens": 16_385},
    ],
)
def test_synthesizer_rejects_provider_budgets_outside_safe_ranges(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="must be between"):
        EvidenceSynthesizer(**kwargs)
