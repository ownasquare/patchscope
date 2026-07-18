"""Deterministic Markdown and SARIF 2.1.0 review exports."""

from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, cast

from patchscope.intake import validate_relative_path

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_SARIF_LEVEL = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}


def export_markdown(review: Mapping[str, object] | object) -> str:
    """Render a portable review report without emitting raw HTML."""

    payload = _mapping(review)
    findings = _findings(payload)
    runs = _runs(payload)
    previews = _previews(payload)
    request = _mapping(payload.get("request"))
    title = _safe_text(
        payload.get("title") or payload.get("name") or request.get("title"),
        "PatchScope code review",
        200,
    )
    raw_summary = _mapping(payload.get("summary"))
    if raw_summary:
        summary = (
            f"Risk score **{_positive_int(raw_summary.get('risk_score'), 0)}/100**. "
            f"Recommendation: **{_enum_text(raw_summary.get('recommendation'), 'comment')}**."
        )
    else:
        summary = "Static and deterministic review results."
    severity_counts = Counter(_enum_text(item.get("severity"), "info") for item in findings)
    category_counts = Counter(_enum_text(item.get("category"), "readability") for item in findings)

    lines = [
        f"# {title}",
        "",
        summary,
        "",
        "## Summary",
        "",
        f"- Findings: **{len(findings)}**",
        "- Severity: "
        + ", ".join(
            f"{name} {severity_counts.get(name, 0)}"
            for name in ("critical", "high", "medium", "low", "info")
        ),
        "- Categories: "
        + ", ".join(
            f"{name} {category_counts.get(name, 0)}"
            for name in ("bug", "security", "performance", "readability")
        ),
    ]
    if runs:
        lines.extend(
            [
                "",
                "## Analyzer status",
                "",
                "| Analyzer | Status | Findings | Duration |",
                "| --- | --- | ---: | ---: |",
            ]
        )
        for run in runs:
            run_findings = run.get("findings")
            finding_count = (
                _positive_int(run.get("findings_count"), 0)
                if not isinstance(run_findings, Sequence)
                else len(run_findings)
            )
            duration = _positive_int(run.get("duration_ms"), 0)
            lines.append(
                "| "
                + " | ".join(
                    (
                        _table_text(run.get("analyzer"), "unknown"),
                        _table_text(_enum_text(run.get("status"), "unknown"), "unknown"),
                        str(finding_count),
                        f"{duration} ms",
                    )
                )
                + " |"
            )
            message = _safe_text(run.get("message"), "", 500)
            if message and _enum_text(run.get("status"), "completed") not in {
                "completed",
                "succeeded",
            }:
                lines.append(f"  - {_inline(message)}")

    lines.extend(["", "## Findings", ""])
    if not findings:
        lines.append("No findings were reported by the analyzers that completed successfully.")
    for index, finding in enumerate(findings, start=1):
        severity = _enum_text(finding.get("severity"), "info").upper()
        message = _safe_text(finding.get("message"), "Review finding", 1_000)
        finding_title = _safe_text(finding.get("title"), message, 300)
        path = _safe_path(finding.get("path"))
        start_line = _positive_int(finding.get("start_line"), 1)
        end_line = max(_positive_int(finding.get("end_line"), start_line), start_line)
        location = (
            f"{path}:{start_line}" if start_line == end_line else f"{path}:{start_line}-{end_line}"
        )
        triage = _enum_text(
            finding.get("triage") or finding.get("triage_status") or finding.get("status"),
            "open",
        )
        triage_note = _safe_text(
            finding.get("triage_note") or finding.get("note"),
            "",
            2_000,
        )
        lines.extend(
            [
                f"### {index}. [{severity}] {finding_title}",
                "",
                f"- Location: `{_code_span(location)}`",
                f"- Category: `{_code_span(_enum_text(finding.get('category'), 'readability'))}`",
                f"- Rule: `{_code_span(_safe_text(finding.get('rule_id'), 'unknown', 200))}`",
                f"- Analyzer: `{_code_span(_safe_text(finding.get('analyzer'), 'unknown', 100))}`",
                f"- Confidence: `{_code_span(_enum_text(finding.get('confidence'), 'medium'))}`",
                f"- Triage: `{_code_span(triage)}`",
            ]
        )
        if triage_note:
            lines.append(f"- Triage note: {_inline(triage_note)}")
        if message != finding_title:
            lines.extend(["", message])
        suggestion = _safe_text(finding.get("suggestion"), "", 2_000)
        if suggestion:
            lines.append(f"- Suggested action: {_inline(suggestion)}")
        snippet = finding.get("snippet") or finding.get("evidence")
        if isinstance(snippet, str) and snippet:
            lines.extend(["", _fenced(snippet[:4_000], "text")])
        lines.append("")

    applicable = [preview for preview in previews if preview.get("applicable") is True]
    if applicable:
        lines.extend(["## Refactor previews", ""])
        for preview in applicable:
            path = _safe_path(preview.get("path"))
            rationale = _safe_text(preview.get("rationale"), "Proposed source edit.", 1_000)
            lines.extend([f"### `{_code_span(path)}`", "", rationale, ""])
            patch = preview.get("unified_diff")
            if isinstance(patch, str) and patch:
                lines.extend([_fenced(patch[:200_000], "diff"), ""])
            notes = preview.get("safety_notes")
            if isinstance(notes, Sequence) and not isinstance(notes, (str, bytes, bytearray)):
                for note in notes:
                    lines.append(f"- {_inline(_safe_text(note, 'Review before applying.', 1_000))}")
                lines.append("")
    lines.extend(
        [
            "## Proof boundary",
            "",
            (
                "Imported source was treated as data. PatchScope did not import it, run it, "
                "install its dependencies, or execute its repository configuration. Analyzer "
                "availability and failures are reported separately above."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def export_sarif(review: Mapping[str, object] | object) -> dict[str, object]:
    """Build a SARIF 2.1.0 object with relative paths and stable fingerprints."""

    payload = _mapping(review)
    findings = _findings(payload)
    analyzer_runs = {str(run.get("analyzer", "unknown")): run for run in _runs(payload)}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        grouped[_safe_text(finding.get("analyzer"), "patchscope", 100)].append(finding)
    analyzer_names = sorted(set(grouped) | set(analyzer_runs)) or ["patchscope"]
    sarif_runs: list[dict[str, object]] = []
    for analyzer in analyzer_names:
        analyzer_findings = grouped.get(analyzer, [])
        rules: dict[str, dict[str, object]] = {}
        results: list[dict[str, object]] = []
        for finding in analyzer_findings:
            rule_id = _safe_text(finding.get("rule_id"), "unknown", 200)
            category = _enum_text(finding.get("category"), "readability")
            severity = _enum_text(finding.get("severity"), "info")
            message = _plain_text(finding.get("message"), "Review finding", 1_000)
            suggestion = _plain_text(finding.get("suggestion"), "", 2_000)
            triage = _enum_text(
                finding.get("triage") or finding.get("triage_status") or finding.get("status"),
                "open",
            )
            triage_note = _plain_text(
                finding.get("triage_note") or finding.get("note"),
                "",
                2_000,
            )
            rules.setdefault(
                rule_id,
                {
                    "id": rule_id,
                    "name": _sarif_name(rule_id),
                    "shortDescription": {"text": message},
                    "fullDescription": {"text": suggestion or message},
                    "properties": {"category": category, "defaultSeverity": severity},
                },
            )
            path = _safe_path(finding.get("path"))
            start_line = _positive_int(finding.get("start_line"), 1)
            end_line = max(_positive_int(finding.get("end_line"), start_line), start_line)
            start_column = _positive_int(finding.get("start_column"), 1)
            end_column = max(
                _positive_int(finding.get("end_column"), start_column + 1), start_column + 1
            )
            fingerprint = _safe_fingerprint(
                finding.get("fingerprint") or finding.get("id") or f"{rule_id}:{path}:{start_line}"
            )
            result: dict[str, object] = {
                "ruleId": rule_id,
                "level": _SARIF_LEVEL.get(severity, "note"),
                "message": {"text": message},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": path, "uriBaseId": "%SRCROOT%"},
                            "region": {
                                "startLine": start_line,
                                "endLine": end_line,
                                "startColumn": start_column,
                                "endColumn": end_column,
                            },
                        }
                    }
                ],
                "partialFingerprints": {"primaryLocationLineHash": fingerprint},
                "properties": {
                    "category": category,
                    "severity": severity,
                    "confidence": _enum_text(finding.get("confidence"), "medium"),
                    "triage": triage,
                },
            }
            if suggestion:
                result["properties"]["suggestedAction"] = suggestion  # type: ignore[index]
            if triage_note:
                result["properties"]["triageNote"] = triage_note  # type: ignore[index]
            results.append(result)
        run_payload = analyzer_runs.get(analyzer, {})
        status = _enum_text(run_payload.get("status"), "completed")
        invocation: dict[str, object] = {
            "executionSuccessful": status in {"completed", "succeeded"},
            "properties": {
                "status": status,
                "durationMs": _positive_int(run_payload.get("duration_ms"), 0),
            },
        }
        if status not in {"completed", "succeeded"}:
            invocation["toolExecutionNotifications"] = [
                {
                    "level": "error" if status == "error" else "warning",
                    "message": {
                        "text": _plain_text(
                            run_payload.get("message"),
                            f"{analyzer} ended with status {status}.",
                            1_000,
                        )
                    },
                }
            ]
        driver: dict[str, object] = {
            "name": analyzer,
            "rules": [rules[key] for key in sorted(rules)],
        }
        if analyzer == "semgrep":
            driver["informationUri"] = "https://github.com/returntocorp/semgrep"
        sarif_runs.append(
            {
                "tool": {"driver": driver},
                "invocations": [invocation],
                "originalUriBaseIds": {"%SRCROOT%": {"uri": "file:///src/"}},
                "results": results,
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": sarif_runs,
    }


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    if is_dataclass(value) and not isinstance(value, type):
        dumped_dataclass: dict[str, Any] = asdict(cast(Any, value))
        return dumped_dataclass
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"findings": list(value)}
    return {}


def _records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [_mapping(item) for item in value]


def _runs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _records(payload.get("analyzer_runs") or payload.get("runs"))


def _findings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records = _records(payload.get("findings"))
    if not records:
        for run in _runs(payload):
            records.extend(_records(run.get("findings")))
    unique: dict[str, dict[str, Any]] = {}
    for finding in records:
        key = _safe_text(
            finding.get("fingerprint") or finding.get("id"),
            f"{finding.get('analyzer')}:{finding.get('rule_id')}:{finding.get('path')}:{finding.get('start_line')}",
            500,
        )
        unique[key] = finding
    return sorted(
        unique.values(),
        key=lambda item: (
            _SEVERITY_ORDER.get(_enum_text(item.get("severity"), "info"), 9),
            _safe_path(item.get("path")),
            _positive_int(item.get("start_line"), 1),
            _safe_text(item.get("rule_id"), "", 200),
        ),
    )


def _previews(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    previews = _records(payload.get("refactor_previews") or payload.get("previews"))
    if previews:
        return previews
    for finding in _records(payload.get("findings")):
        patch = finding.get("refactor_diff")
        if isinstance(patch, str) and patch:
            previews.append(
                {
                    "path": finding.get("path"),
                    "applicable": True,
                    "rationale": finding.get("suggestion"),
                    "unified_diff": patch,
                    "safety_notes": ["Preview only; run focused tests before applying."],
                }
            )
    return previews


def _safe_text(value: object, fallback: str, limit: int) -> str:
    if hasattr(value, "value"):
        value = value.value
    if not isinstance(value, str):
        return fallback
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    compact = " ".join(value.split())
    return html.escape(compact[:limit], quote=False) if compact else fallback


def _plain_text(value: object, fallback: str, limit: int) -> str:
    return html.unescape(_safe_text(value, fallback, limit))


def _enum_text(value: object, fallback: str) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value).casefold() if isinstance(value, str) and value else fallback


def _safe_path(value: object) -> str:
    try:
        return validate_relative_path(str(value))
    except (TypeError, ValueError):
        return "invalid-path"


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and value >= 0 else default


def _table_text(value: object, fallback: str) -> str:
    return _safe_text(value, fallback, 500).replace("|", "\\|")


def _inline(value: str) -> str:
    return value.replace("\n", " ")


def _code_span(value: str) -> str:
    return value.replace("`", "\\`")


def _fenced(value: str, language: str) -> str:
    cleaned = re.sub(r"[\x00\x0b\x0c]", "", value)
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", cleaned)), default=0)
    fence = "`" * max(longest + 1, 4)
    return f"{fence}{language}\n{cleaned}\n{fence}"


def _sarif_name(rule_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", rule_id).strip("_")[:128] or "unknown"


def _safe_fingerprint(value: object) -> str:
    text = _plain_text(value, "unknown", 500)
    return re.sub(r"[^A-Za-z0-9._:-]", "_", text)


__all__ = ["export_markdown", "export_sarif"]
