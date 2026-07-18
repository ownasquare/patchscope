"""Provider-optional, evidence-validated review synthesis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from patchscope.agent.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from patchscope.domain import (
    FindingCategory,
    FindingSeverity,
    FindingTriage,
    summarize_finding_values,
)


class ModelFinding(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(min_length=3, max_length=1_500)
    category: Literal["bug", "security", "performance", "readability", "maintainability", "testing"]
    severity: Literal["critical", "high", "medium", "low", "info"]
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    evidence: str = Field(min_length=1, max_length=800)
    suggestion: str = Field(min_length=3, max_length=1_500)
    confidence: float = Field(ge=0.0, le=1.0)


class ModelReview(BaseModel):
    summary: str = Field(min_length=3, max_length=2_000)
    findings: list[ModelFinding] = Field(default_factory=list, max_length=30)


class Synthesizer(Protocol):
    def synthesize(
        self,
        *,
        files: Sequence[Any],
        parse_summaries: Sequence[Mapping[str, Any]],
        analyzer_findings: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]: ...


class EvidenceSynthesizer:
    """Deduplicate static evidence and optionally add validated model findings."""

    def __init__(
        self,
        *,
        mode: Literal["auto", "offline", "openai"] = "auto",
        model_name: str = "gpt-5-mini",
        api_key: str | None = None,
        model_factory: Callable[[], Any] | None = None,
        max_source_chars: int = 120_000,
    ) -> None:
        self.mode = mode
        self.model_name = model_name
        self.api_key = api_key
        self.model_factory = model_factory
        self.max_source_chars = max_source_chars

    def synthesize(
        self,
        *,
        files: Sequence[Any],
        parse_summaries: Sequence[Mapping[str, Any]],
        analyzer_findings: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        normalized = self._deduplicate(analyzer_findings)
        warnings: list[str] = []
        if self.mode == "openai" and not (self.api_key or self.model_factory):
            raise ValueError("OpenAI mode requires a server-side API key")
        use_model = self.mode == "openai" or (
            self.mode == "auto" and bool(self.api_key or self.model_factory)
        )
        if not use_model:
            return normalized, self._metadata("offline", len(normalized)), warnings

        try:
            review = self._invoke_model(
                files=files,
                parse_summaries=parse_summaries,
                analyzer_findings=normalized,
                metadata=metadata,
            )
            additions = self._validate_model_findings(review.findings, files)
            merged = self._deduplicate([*normalized, *additions])
            ai_metadata = self._metadata("openai", len(merged))
            ai_metadata["summary"] = review.summary
            ai_metadata["accepted_model_findings"] = len(additions)
            return merged, ai_metadata, warnings
        except Exception as exc:
            if self.mode == "openai":
                raise
            warnings.append(
                "AI synthesis was unavailable "
                f"({type(exc).__name__}); deterministic findings were preserved."
            )
            metadata_result = self._metadata("offline_fallback", len(normalized))
            metadata_result["provider_error_type"] = type(exc).__name__
            return normalized, metadata_result, warnings

    def _invoke_model(
        self,
        *,
        files: Sequence[Any],
        parse_summaries: Sequence[Mapping[str, Any]],
        analyzer_findings: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> ModelReview:
        model = self.model_factory() if self.model_factory else self._default_model()
        structured = model.with_structured_output(ModelReview)
        sources = self._bounded_sources(files)
        user_prompt = USER_TEMPLATE.format(
            metadata=json.dumps(dict(metadata), sort_keys=True, default=str),
            parse_summaries=json.dumps(list(parse_summaries), sort_keys=True, default=str),
            analyzer_findings=json.dumps(list(analyzer_findings), sort_keys=True, default=str),
            sources=sources,
        )
        response = structured.invoke([("system", SYSTEM_PROMPT), ("human", user_prompt)])
        return (
            response if isinstance(response, ModelReview) else ModelReview.model_validate(response)
        )

    def _default_model(self) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model_name,
            api_key=self.api_key,
            temperature=0,
            timeout=30,
            max_retries=1,
        )

    def _bounded_sources(self, files: Sequence[Any]) -> str:
        blocks: list[str] = []
        used = 0
        for source in files:
            path = str(_value(source, "path", "unknown"))
            content = str(_value(source, "content", ""))
            remaining = self.max_source_chars - used
            if remaining <= 0:
                break
            bounded = content[:remaining]
            blocks.append(f"--- {path}\n{bounded}")
            used += len(bounded)
        return "\n".join(blocks)

    def _validate_model_findings(
        self,
        findings: Sequence[ModelFinding],
        files: Sequence[Any],
    ) -> list[dict[str, Any]]:
        sources = {
            str(_value(source, "path", "")): str(_value(source, "content", "")) for source in files
        }
        accepted: list[dict[str, Any]] = []
        for finding in findings:
            content = sources.get(finding.path)
            if content is None or finding.end_line < finding.start_line:
                continue
            lines = content.splitlines()
            if finding.start_line > len(lines) or finding.end_line > len(lines):
                continue
            local = "\n".join(lines[finding.start_line - 1 : finding.end_line])
            evidence = finding.evidence.strip()
            if not evidence or evidence not in local:
                continue
            payload = finding.model_dump()
            payload.update(
                {
                    "rule_id": "AI-EVIDENCE",
                    "fingerprint": _fingerprint(payload),
                    "sources": ["ai"],
                    "analyzer": "langchain-openai",
                    "status": "open",
                }
            )
            accepted.append(payload)
        return accepted

    def _deduplicate(
        self,
        findings: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        deduped: dict[str, dict[str, Any]] = {}
        severity_rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
        for raw in findings:
            finding = _normalize_finding(raw)
            key = str(finding.get("fingerprint") or _fingerprint(finding))
            finding["fingerprint"] = key
            current = deduped.get(key)
            if current is None:
                deduped[key] = finding
                continue
            sources = sorted(
                set(_as_list(current.get("sources"))) | set(_as_list(finding.get("sources")))
            )
            current["sources"] = sources
            if severity_rank.get(str(finding.get("severity")), 0) > severity_rank.get(
                str(current.get("severity")), 0
            ):
                finding["sources"] = sources
                deduped[key] = finding
        return sorted(
            deduped.values(),
            key=lambda value: (
                -severity_rank.get(str(value.get("severity")), 0),
                str(value.get("path", "")),
                int(value.get("start_line", 1)),
            ),
        )

    def _metadata(self, mode: str, finding_count: int) -> dict[str, Any]:
        return {
            "mode": mode,
            "provider": "openai" if mode == "openai" else None,
            "model": self.model_name if mode == "openai" else None,
            "finding_count": finding_count,
            "source_execution": False,
        }


def summarize_findings(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[tuple[FindingCategory, FindingSeverity, FindingTriage]] = []
    for item in findings:
        try:
            category = FindingCategory(str(item.get("category", "maintainability")))
        except ValueError:
            category = FindingCategory.MAINTAINABILITY
        try:
            severity = FindingSeverity(str(item.get("severity", "info")))
        except ValueError:
            severity = FindingSeverity.INFO
        try:
            triage = FindingTriage(str(item.get("status", "open")))
        except ValueError:
            triage = FindingTriage.OPEN
        values.append((category, severity, triage))
    return summarize_finding_values(values).model_dump(mode="json")


def _normalize_finding(raw: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    message = str(result.get("description") or result.get("message") or "Review finding")
    result.setdefault("title", message.split(".", 1)[0][:160])
    result.setdefault("description", message)
    result.setdefault("message", message)
    result.setdefault("category", "maintainability")
    result.setdefault("severity", "info")
    result.setdefault("path", "unknown")
    result.setdefault("start_line", 1)
    result.setdefault("end_line", result["start_line"])
    if not result.get("evidence"):
        result["evidence"] = str(result.get("snippet") or "")
    if not result.get("suggestion"):
        result["suggestion"] = "Review this code path."
    result.setdefault("confidence", 0.8)
    result.setdefault("rule_id", "PATCHSCOPE")
    analyzer = str(result.get("analyzer") or result.get("source") or "patchscope")
    result.setdefault("analyzer", analyzer)
    result.setdefault("sources", [analyzer])
    result.setdefault("status", "open")
    return result


def _fingerprint(finding: Mapping[str, Any]) -> str:
    material = "|".join(
        str(finding.get(key, "")) for key in ("path", "rule_id", "start_line", "category", "title")
    )
    return hashlib.sha256(material.encode()).hexdigest()[:20]


def _value(item: Any, name: str, default: Any) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return []
