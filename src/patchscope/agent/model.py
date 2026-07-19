"""Provider-optional, evidence-validated review synthesis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from patchscope.agent.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from patchscope.domain import (
    FindingCategory,
    FindingSeverity,
    FindingTriage,
    summarize_finding_values,
)

_DEFAULT_MAX_PROMPT_CHARS = 120_000
_TRUNCATION_MARKER = "\n...[truncated]"
# Preserve the most evidence-bearing input first while still reserving bounded
# context for every section. Short sections return their unused share to the pool.
_PROMPT_SECTION_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("metadata", 1),
    ("parse_summaries", 2),
    ("analyzer_findings", 3),
    ("sources", 6),
)


@dataclass(frozen=True, slots=True)
class _PreparedPrompt:
    messages: tuple[tuple[str, str], tuple[str, str]]
    metadata: dict[str, Any]
    warnings: tuple[str, ...]


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
        max_prompt_chars: int = _DEFAULT_MAX_PROMPT_CHARS,
        max_completion_tokens: int = 4_096,
        max_source_chars: int | None = None,
    ) -> None:
        if max_source_chars is not None:
            if max_prompt_chars != _DEFAULT_MAX_PROMPT_CHARS:
                raise ValueError("Use max_prompt_chars instead of combining both prompt limits")
            max_prompt_chars = max_source_chars
        prompt_overhead = len(SYSTEM_PROMPT) + len(
            USER_TEMPLATE.format(
                metadata="",
                parse_summaries="",
                analyzer_findings="",
                sources="",
            )
        )
        if not 4_000 <= max_prompt_chars <= 1_000_000 or max_prompt_chars <= prompt_overhead:
            raise ValueError("max_prompt_chars must be between 4000 and 1000000")
        if not 256 <= max_completion_tokens <= 16_384:
            raise ValueError("max_completion_tokens must be between 256 and 16384")
        self.mode = mode
        self.model_name = model_name
        self.api_key = api_key
        self.model_factory = model_factory
        self.max_prompt_chars = max_prompt_chars
        self.max_completion_tokens = max_completion_tokens
        # Compatibility for callers of the pre-v0.1.1 constructor. The value now
        # bounds the complete provider prompt, not only source text.
        self.max_source_chars = max_prompt_chars

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

        prepared = self._prepare_prompt(
            files=files,
            parse_summaries=parse_summaries,
            analyzer_findings=normalized,
            metadata=metadata,
        )
        warnings.extend(prepared.warnings)
        try:
            review = self._invoke_model(prepared.messages)
            additions = self._validate_model_findings(review.findings, files)
            merged = self._deduplicate([*normalized, *additions])
            ai_metadata = self._metadata("openai", len(merged))
            ai_metadata.update(prepared.metadata)
            ai_metadata["summary"] = review.summary
            ai_metadata["accepted_model_findings"] = len(additions)
            return merged, ai_metadata, warnings
        except Exception as exc:
            if self.mode == "openai":
                raise
            fallback_reason = (
                "AI synthesis was unavailable "
                f"({type(exc).__name__}); deterministic findings were preserved."
            )
            warnings.append(fallback_reason)
            metadata_result = self._metadata("offline_fallback", len(normalized))
            metadata_result.update(prepared.metadata)
            metadata_result["fallback_reason"] = fallback_reason
            metadata_result["provider_error_type"] = type(exc).__name__
            return normalized, metadata_result, warnings

    def _invoke_model(
        self,
        messages: Sequence[tuple[str, str]],
    ) -> ModelReview:
        model = self.model_factory() if self.model_factory else self._default_model()
        structured = model.with_structured_output(ModelReview)
        response = structured.invoke(list(messages))
        return (
            response if isinstance(response, ModelReview) else ModelReview.model_validate(response)
        )

    def _default_model(self) -> Any:
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model_name,
            api_key=self.api_key,
            timeout=30,
            max_retries=1,
            max_completion_tokens=self.max_completion_tokens,
        )

    def _prepare_prompt(
        self,
        *,
        files: Sequence[Any],
        parse_summaries: Sequence[Mapping[str, Any]],
        analyzer_findings: Sequence[Mapping[str, Any]],
        metadata: Mapping[str, Any],
    ) -> _PreparedPrompt:
        section_values = {
            "metadata": json.dumps(dict(metadata), sort_keys=True, default=str),
            "parse_summaries": json.dumps(list(parse_summaries), sort_keys=True, default=str),
            "analyzer_findings": json.dumps(list(analyzer_findings), sort_keys=True, default=str),
            "sources": self._source_text(files),
        }
        empty_user_prompt = USER_TEMPLATE.format(
            metadata="",
            parse_summaries="",
            analyzer_findings="",
            sources="",
        )
        dynamic_limit = self.max_prompt_chars - len(SYSTEM_PROMPT) - len(empty_user_prompt)
        allocations = _weighted_character_allocations(
            {name: len(section_values[name]) for name, _weight in _PROMPT_SECTION_WEIGHTS},
            dynamic_limit,
        )
        rendered: dict[str, str] = {}
        usage: dict[str, dict[str, int | bool]] = {}
        for name, _weight in _PROMPT_SECTION_WEIGHTS:
            value = section_values[name]
            rendered_value, included_chars = _render_section(value, allocations[name])
            rendered[name] = rendered_value
            usage[name] = {
                "original_chars": len(value),
                "included_chars": included_chars,
                "prompt_chars": len(rendered_value),
                "truncated": included_chars < len(value),
            }
        user_prompt = USER_TEMPLATE.format(**rendered)
        prompt_chars = len(SYSTEM_PROMPT) + len(user_prompt)
        if prompt_chars > self.max_prompt_chars:  # pragma: no cover - invariant guard
            raise RuntimeError("Provider prompt exceeded its configured character ceiling")
        prompt_truncated = any(bool(section["truncated"]) for section in usage.values())
        warnings: tuple[str, ...] = ()
        if prompt_truncated:
            counts = ", ".join(
                f"{name} {section['included_chars']}/{section['original_chars']} chars"
                for name, section in usage.items()
            )
            warnings = (
                f"Provider prompt was truncated within {self.max_prompt_chars} characters "
                f"({counts}); complete local static findings were preserved.",
            )
        return _PreparedPrompt(
            messages=(("system", SYSTEM_PROMPT), ("human", user_prompt)),
            metadata={
                "prompt_char_limit": self.max_prompt_chars,
                "prompt_chars": prompt_chars,
                "prompt_truncated": prompt_truncated,
                "prompt_sections": usage,
                "completion_token_limit": self.max_completion_tokens,
            },
            warnings=warnings,
        )

    @staticmethod
    def _source_text(files: Sequence[Any]) -> str:
        blocks: list[str] = []
        for source in files:
            path = str(_value(source, "path", "unknown"))
            content = str(_value(source, "content", ""))
            blocks.append(f"--- {path}\n{content}")
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
            "provider": "openai" if mode in {"openai", "offline_fallback"} else None,
            "model": self.model_name if mode in {"openai", "offline_fallback"} else None,
            "finding_count": finding_count,
            "source_execution": False,
        }


def _weighted_character_allocations(
    lengths: Mapping[str, int],
    capacity: int,
) -> dict[str, int]:
    """Allocate a hard character budget proportionally, redistributing unused shares."""

    allocations = {name: 0 for name, _weight in _PROMPT_SECTION_WEIGHTS}
    remaining = max(0, capacity)
    active = [name for name, _weight in _PROMPT_SECTION_WEIGHTS if lengths.get(name, 0) > 0]
    weights = dict(_PROMPT_SECTION_WEIGHTS)
    section_order = {name: index for index, (name, _weight) in enumerate(_PROMPT_SECTION_WEIGHTS)}
    while remaining and active:
        total_weight = sum(weights[name] for name in active)
        shares = {name: remaining * weights[name] // total_weight for name in active}
        undistributed = remaining - sum(shares.values())
        ranked = sorted(
            active,
            key=lambda name: (
                -(remaining * weights[name] % total_weight),
                section_order[name],
            ),
        )
        for name in ranked[:undistributed]:
            shares[name] += 1

        granted = 0
        for name in active:
            need = lengths[name] - allocations[name]
            amount = min(need, shares[name])
            allocations[name] += amount
            granted += amount
        remaining -= granted
        active = [name for name in active if allocations[name] < lengths[name]]
        if granted == 0:  # pragma: no cover - defensive against malformed weights
            break
    return allocations


def _render_section(value: str, budget: int) -> tuple[str, int]:
    if len(value) <= budget:
        return value, len(value)
    if budget <= 0:
        return "", 0
    if budget <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:budget], 0
    included_chars = budget - len(_TRUNCATION_MARKER)
    return f"{value[:included_chars]}{_TRUNCATION_MARKER}", included_chars


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
