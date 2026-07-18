"""Fixed, inspectable LangGraph workflow for code review."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, cast

from langgraph.graph import END, START, StateGraph

from patchscope.agent.model import EvidenceSynthesizer, Synthesizer, summarize_findings
from patchscope.agent.state import ReviewState


@dataclass(frozen=True, slots=True)
class WorkflowDependencies:
    parser: Any
    analyzer_runner: Any
    refactor_engine: Any
    synthesizer: Synthesizer


class ReviewWorkflow:
    """Compile once, then invoke the same bounded graph for every review."""

    def __init__(self, dependencies: WorkflowDependencies) -> None:
        self.dependencies = dependencies
        builder = StateGraph(ReviewState)
        builder.add_node("parse", self._parse)
        builder.add_node("analyze", self._analyze)
        builder.add_node("synthesize", self._synthesize)
        builder.add_node("refactor", self._refactor)
        builder.add_node("score", self._score)
        builder.add_edge(START, "parse")
        builder.add_edge("parse", "analyze")
        builder.add_edge("analyze", "synthesize")
        builder.add_edge("synthesize", "refactor")
        builder.add_edge("refactor", "score")
        builder.add_edge("score", END)
        self.graph = builder.compile()

    def invoke(self, *, files: Sequence[Any], metadata: Mapping[str, Any]) -> ReviewState:
        return cast(
            ReviewState,
            self.graph.invoke(
                {
                    "files": list(files),
                    "metadata": dict(metadata),
                    "stage_trace": [],
                    "warnings": [],
                }
            ),
        )

    def _parse(self, state: ReviewState) -> dict[str, Any]:
        summaries: list[dict[str, Any]] = []
        warnings: list[str] = []
        for source in state["files"]:
            try:
                summaries.append(_dump(self.dependencies.parser.parse(source)))
            except Exception as exc:
                path = getattr(source, "path", "unknown")
                warnings.append(f"Parser unavailable for {path}: {type(exc).__name__}")
        return {"parse_summaries": summaries, "warnings": warnings, "stage_trace": ["parse"]}

    def _analyze(self, state: ReviewState) -> dict[str, Any]:
        runs = self.dependencies.analyzer_runner.analyze(state["files"])
        dumped_runs = [_dump(run) for run in runs]
        changed_ranges = state.get("metadata", {}).get("changed_line_ranges")
        if isinstance(changed_ranges, Mapping):
            for run in dumped_runs:
                raw_findings = run.get("findings")
                if not isinstance(raw_findings, list):
                    continue
                run["findings"] = [
                    finding
                    for finding in raw_findings
                    if isinstance(finding, Mapping)
                    and _finding_intersects_changes(finding, changed_ranges)
                ]
        return {
            "analyzer_runs": dumped_runs,
            "stage_trace": ["analyze"],
        }

    def _synthesize(self, state: ReviewState) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        for run in state.get("analyzer_runs", []):
            analyzer = str(run.get("analyzer") or run.get("name") or "analyzer")
            raw_findings = run.get("findings", [])
            if not isinstance(raw_findings, list):
                continue
            for item in raw_findings:
                if isinstance(item, Mapping):
                    payload = dict(item)
                    payload.setdefault("analyzer", analyzer)
                    if not payload.get("evidence") and payload.get("snippet"):
                        payload["evidence"] = payload["snippet"]
                    if not payload.get("suggestion"):
                        payload["suggestion"] = (
                            "Review this code path and add a focused regression test."
                        )
                    findings.append(payload)
        synthesized, ai_metadata, warnings = self.dependencies.synthesizer.synthesize(
            files=state["files"],
            parse_summaries=state.get("parse_summaries", []),
            analyzer_findings=findings,
            metadata=state.get("metadata", {}),
        )
        changed_ranges = state.get("metadata", {}).get("changed_line_ranges")
        if isinstance(changed_ranges, Mapping):
            scoped = [
                finding
                for finding in synthesized
                if _finding_intersects_changes(finding, changed_ranges)
            ]
            excluded_count = len(synthesized) - len(scoped)
            synthesized = scoped
            if excluded_count:
                warnings = [
                    *warnings,
                    f"Excluded {excluded_count} finding(s) outside changed pull-request lines.",
                ]
        return {
            "findings": synthesized,
            "ai_metadata": ai_metadata,
            "warnings": warnings,
            "stage_trace": ["synthesize"],
        }

    def _refactor(self, state: ReviewState) -> dict[str, Any]:
        sources = {getattr(item, "path", ""): item for item in state["files"]}
        previews: list[dict[str, Any]] = []
        warnings: list[str] = []
        for finding in state.get("findings", []):
            source = sources.get(str(finding.get("path", "")))
            if source is None:
                continue
            try:
                preview = self.dependencies.refactor_engine.preview(source, finding)
            except Exception as exc:
                warnings.append(
                    f"Refactor preview unavailable for {finding.get('path', 'unknown')}: "
                    f"{type(exc).__name__}"
                )
                continue
            payload = _dump(preview)
            unified_diff = payload.get("unified_diff") or payload.get("diff")
            if unified_diff:
                payload["diff"] = unified_diff
                payload["finding_fingerprint"] = finding.get("fingerprint")
                previews.append(payload)
        return {
            "refactors": previews,
            "warnings": warnings,
            "stage_trace": ["refactor"],
        }

    def _score(self, state: ReviewState) -> dict[str, Any]:
        summary = summarize_findings(state.get("findings", []))
        summary["files_reviewed"] = len(state.get("files", []))
        summary["analyzers"] = {
            str(run.get("analyzer") or run.get("name") or "analyzer"): str(
                run.get("status", "unknown")
            )
            for run in state.get("analyzer_runs", [])
        }
        return {"summary": summary, "stage_trace": ["score"]}


def default_synthesizer(**kwargs: Any) -> EvidenceSynthesizer:
    return EvidenceSynthesizer(**kwargs)


def _dump(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": str(value)}


def _finding_intersects_changes(
    finding: Mapping[str, Any],
    changed_ranges: Mapping[str, Any],
) -> bool:
    path = str(finding.get("path") or "")
    ranges = changed_ranges.get(path)
    if not isinstance(ranges, Sequence) or isinstance(ranges, (str, bytes)):
        return False
    start = finding.get("start_line")
    end = finding.get("end_line", start)
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return False
    if not isinstance(end, int) or isinstance(end, bool) or end < start:
        end = start
    for candidate in ranges:
        if (
            isinstance(candidate, Sequence)
            and not isinstance(candidate, (str, bytes))
            and len(candidate) == 2
            and isinstance(candidate[0], int)
            and isinstance(candidate[1], int)
            and start <= candidate[1]
            and end >= candidate[0]
        ):
            return True
    return False
