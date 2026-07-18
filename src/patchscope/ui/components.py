"""Reusable, defensive presentation helpers for PatchScope review JSON."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlparse

import streamlit as st

from patchscope.languages import LANGUAGE_REGISTRY

JsonMapping = Mapping[str, Any]
BadgeColor = Literal["red", "orange", "yellow", "blue", "green", "violet", "gray"]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEVERITY_COLORS: dict[str, BadgeColor] = {
    "critical": "red",
    "high": "orange",
    "medium": "yellow",
    "low": "blue",
    "info": "gray",
}
STATUS_COLORS: dict[str, BadgeColor] = {
    "completed": "green",
    "ready": "green",
    "acknowledged": "green",
    "accepted": "green",
    "fixed": "green",
    "open": "orange",
    "running": "blue",
    "queued": "violet",
    "dismissed": "gray",
    "ignored": "gray",
    "failed": "red",
    "error": "red",
    "unavailable": "red",
}
MARKDOWN_CONTROL = re.compile(r"([\\`*_{}\[\]()#+!|><$~])")
GITHUB_PR_PATTERN = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$",
    flags=re.IGNORECASE,
)


def as_mapping(value: object) -> JsonMapping:
    return value if isinstance(value, Mapping) else {}


def first_value(mapping: JsonMapping, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def humanize(value: object, *, fallback: str = "Unavailable") -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    raw = raw.rsplit(".", 1)[-1].replace("-", "_")
    words = [word for word in raw.split("_") if word]
    acronyms = {"ai": "AI", "api": "API", "id": "ID", "pr": "PR", "sql": "SQL"}
    rendered = [acronyms.get(word.casefold(), word.casefold()) for word in words]
    if rendered and rendered[0] not in acronyms.values():
        rendered[0] = rendered[0].capitalize()
    return " ".join(rendered) or fallback


def safe_markdown(value: object) -> str:
    return MARKDOWN_CONTROL.sub(r"\\\1", str(value or ""))


def normalized_status(value: object, *, fallback: str = "open") -> str:
    raw = str(value or "").strip().casefold().rsplit(".", 1)[-1]
    return raw.replace("-", "_").replace(" ", "_") or fallback


def review_id(review: JsonMapping) -> str:
    return str(first_value(review, "id", "review_id", "uuid", default=""))


def review_name(review: JsonMapping) -> str:
    direct = first_value(review, "name", "title", "display_name")
    if direct:
        return str(direct)
    source = as_mapping(first_value(review, "source", "request", default={}))
    source_title = first_value(source, "title", "name")
    if source_title:
        return str(source_title)
    filename = first_value(
        review, "filename", "path", default=first_value(source, "filename", "path")
    )
    if not filename:
        files = source.get("files")
        if isinstance(files, list) and files:
            first_file = as_mapping(files[0])
            filename = first_value(first_file, "path", "filename")
    return str(filename or "Untitled review")


def review_status(review: JsonMapping) -> str:
    return normalized_status(first_value(review, "status", "review_status", default="completed"))


def review_summary(review: JsonMapping) -> JsonMapping:
    return as_mapping(first_value(review, "summary", "metrics", default={}))


def review_findings(review: JsonMapping) -> list[dict[str, Any]]:
    raw = first_value(review, "findings", "issues", default=[])
    if not isinstance(raw, list):
        return []
    findings = [dict(item) for item in raw if isinstance(item, Mapping)]
    return sorted(
        findings,
        key=lambda item: (
            SEVERITY_ORDER.get(normalized_status(item.get("severity"), fallback="info"), 99),
            str(first_value(item, "path", "filename", default="")),
            int(first_value(item, "start_line", "line", default=0) or 0),
        ),
    )


def finding_fingerprint(finding: JsonMapping) -> str:
    value = first_value(finding, "fingerprint", "id", "finding_id", "rule_id", default="finding")
    return str(value)


def finding_title(finding: JsonMapping) -> str:
    return str(first_value(finding, "title", "message", "summary", default="Review finding"))


def finding_location(finding: JsonMapping) -> str:
    path = str(first_value(finding, "path", "filename", "file", default="Unknown file"))
    start = first_value(finding, "start_line", "line")
    end = first_value(finding, "end_line")
    if start and end and str(start) != str(end):
        return f"{path}:{start}-{end}"
    if start:
        return f"{path}:{start}"
    return path


def finding_option_label(finding: JsonMapping) -> str:
    severity = humanize(first_value(finding, "severity", default="info"))
    title = finding_title(finding)
    location = finding_location(finding)
    return f"{severity} · {title} · {location}"


def unique_values(findings: Sequence[JsonMapping], key: str) -> list[str]:
    values = {
        normalized_status(finding.get(key), fallback="") for finding in findings if finding.get(key)
    }
    return sorted(
        values, key=lambda value: SEVERITY_ORDER.get(value, 50) if key == "severity" else value
    )


def filter_findings(
    findings: Sequence[JsonMapping],
    *,
    query: str = "",
    severities: Iterable[str] = (),
    categories: Iterable[str] = (),
    statuses: Iterable[str] = (),
) -> list[dict[str, Any]]:
    query_folded = query.strip().casefold()
    severity_set = {normalized_status(value, fallback="") for value in severities}
    category_set = {normalized_status(value, fallback="") for value in categories}
    status_set = {normalized_status(value, fallback="") for value in statuses}
    matched: list[dict[str, Any]] = []
    for original in findings:
        finding = dict(original)
        severity = normalized_status(finding.get("severity"), fallback="info")
        category = normalized_status(finding.get("category"), fallback="other")
        status = normalized_status(
            first_value(finding, "triage_status", "triage", "status", default="open"),
            fallback="open",
        )
        searchable = " ".join(
            (
                finding_title(finding),
                finding_location(finding),
                str(first_value(finding, "rule_id", "analyzer", "source", default="")),
            )
        ).casefold()
        if query_folded and query_folded not in searchable:
            continue
        if severity_set and severity not in severity_set:
            continue
        if category_set and category not in category_set:
            continue
        if status_set and status not in status_set:
            continue
        matched.append(finding)
    return matched


def language_for_filename(filename: str) -> str:
    return LANGUAGE_REGISTRY.display_name_for_path(filename) or "Detected by service"


def github_pr_identity(url: str) -> tuple[str, str, int] | None:
    match = GITHUB_PR_PATTERN.fullmatch(url.strip())
    if match is None:
        return None
    parsed = urlparse(url.strip())
    if parsed.username or parsed.password or parsed.port:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def text_preflight(*, name: str, filename: str, content: str) -> dict[str, Any]:
    return {
        "kind": "text",
        "name": name.strip() or filename.strip(),
        "filename": filename.strip(),
        "content": content,
        "file_count": 1,
        "line_count": len(content.splitlines()),
        "byte_count": len(content.encode("utf-8")),
        "language": language_for_filename(filename),
    }


def upload_preflight(
    *, name: str, filename: str, content: bytes, content_type: str
) -> dict[str, Any]:
    is_archive = filename.casefold().endswith(".zip")
    return {
        "kind": "file",
        "name": name.strip() or filename,
        "filename": filename,
        "content": content,
        "content_type": content_type,
        "file_count": "From archive" if is_archive else 1,
        "line_count": (
            "From archive" if is_archive else content.count(b"\n") + (1 if content else 0)
        ),
        "byte_count": len(content),
        "language": "Detected per file" if is_archive else language_for_filename(filename),
    }


def github_preflight(*, name: str, url: str) -> dict[str, Any]:
    identity = github_pr_identity(url)
    if identity is None:
        raise ValueError(
            "Enter a public GitHub pull request URL such as https://github.com/org/repo/pull/42."
        )
    owner, repository, number = identity
    return {
        "kind": "github",
        "name": name.strip() or f"{owner}/{repository} PR #{number}",
        "url": url.strip(),
        "repository": f"{owner}/{repository}",
        "pull_request": number,
        "file_count": "From pull request",
        "line_count": "From pull request",
        "byte_count": "Bounded by service",
        "language": "Detected per file",
    }


def status_badge(status: object, *, prefix: str | None = None) -> None:
    normalized = normalized_status(status, fallback="unknown")
    label = humanize(normalized)
    if prefix:
        label = f"{prefix}: {label}"
    st.badge(label, color=STATUS_COLORS.get(normalized, "gray"))


def render_empty_state(title: str, message: str) -> None:
    with st.container(border=True):
        st.subheader(title, anchor=False)
        st.caption(message)


def _count_from_summary(summary: JsonMapping, key: str, fallback: int) -> int:
    value = summary.get(key)
    if isinstance(value, int):
        return value
    return fallback


def render_review_metrics(review: JsonMapping, findings: Sequence[JsonMapping]) -> None:
    summary = review_summary(review)
    high_risk = sum(
        1
        for finding in findings
        if normalized_status(finding.get("severity"), fallback="info") in {"critical", "high"}
    )
    request = as_mapping(review.get("request"))
    raw_files = first_value(
        review,
        "files",
        "source_files",
        default=first_value(request, "files", default=[]),
    )
    file_count = len(raw_files) if isinstance(raw_files, list) else 0
    file_count = _count_from_summary(summary, "files_reviewed", file_count)
    risk = first_value(
        summary, "risk_score", default=first_value(review, "risk_score", default="—")
    )
    columns = st.columns(4)
    columns[0].metric("Findings", _count_from_summary(summary, "total_findings", len(findings)))
    columns[1].metric(
        "Critical / high",
        high_risk,
        help="The findings to inspect first because they carry the greatest potential impact.",
    )
    columns[2].metric("Files reviewed", file_count or "—")
    columns[3].metric(
        "Risk score",
        risk,
        help="A 0-100 prioritization score based on finding severity and confidence.",
    )


def _render_evidence(evidence: object, *, language: str | None) -> None:
    if isinstance(evidence, str):
        st.code(evidence, language=language, wrap_lines=False)
        return
    if isinstance(evidence, list):
        for index, item in enumerate(evidence, start=1):
            if isinstance(item, Mapping):
                label = first_value(item, "label", "reason", "message", default=f"Evidence {index}")
                st.caption(str(label))
                excerpt = first_value(item, "excerpt", "snippet", "content", "text")
                if excerpt:
                    st.code(str(excerpt), language=language, wrap_lines=False)
            elif item:
                st.code(str(item), language=language, wrap_lines=False)
        return
    if evidence:
        st.text(str(evidence))


def refactor_diff(finding: JsonMapping) -> str | None:
    direct = first_value(
        finding,
        "refactor_diff",
        "diff",
        "patch",
        "suggested_patch",
        "unified_diff",
    )
    if isinstance(direct, str) and direct.strip():
        return direct
    preview = as_mapping(
        first_value(finding, "refactor_preview", "refactor", "suggestion", default={})
    )
    nested = first_value(preview, "diff", "patch", "unified_diff")
    return str(nested) if isinstance(nested, str) and nested.strip() else None


def render_finding_detail(finding: JsonMapping) -> None:
    severity = normalized_status(finding.get("severity"), fallback="info")
    category = normalized_status(finding.get("category"), fallback="other")
    status = normalized_status(
        first_value(finding, "triage_status", "triage", "status", default="open"),
        fallback="open",
    )
    st.subheader(finding_title(finding), anchor=False)
    badges = st.columns(3)
    with badges[0]:
        st.badge(humanize(severity), color=SEVERITY_COLORS.get(severity, "gray"))
    with badges[1]:
        st.badge(humanize(category), color="blue")
    with badges[2]:
        status_badge(status)
    st.caption(finding_location(finding))

    description = first_value(finding, "description", "explanation", "why_it_matters", "message")
    if description:
        st.write(str(description))
    recommendation = first_value(
        finding,
        "recommendation",
        "remediation",
        "suggestion",
        "suggested_fix",
        "fix_description",
    )
    if recommendation and str(recommendation) != str(description):
        st.markdown("**Recommended change**")
        st.write(str(recommendation))

    language = str(first_value(finding, "language", default="")) or None
    evidence = first_value(finding, "evidence", "source_context", "context", "excerpt")
    if evidence:
        st.markdown("**Evidence**")
        _render_evidence(evidence, language=language)

    diff = refactor_diff(finding)
    st.markdown("**Refactor preview**")
    if diff:
        st.caption("Preview only. PatchScope never applies a refactor to the submitted source.")
        st.code(diff, language="diff", wrap_lines=False)
    else:
        st.caption("No mechanically safe patch is available. Follow the recommendation above.")

    with st.expander("Technical details", expanded=False):
        details = {
            "Rule": first_value(finding, "rule_id", "rule", default="Not reported"),
            "Analyzer": first_value(finding, "analyzer", "source", "tool", default="PatchScope"),
            "Fingerprint": finding_fingerprint(finding),
        }
        for label, value in details.items():
            st.caption(label)
            st.code(str(value), language=None)
        raw = first_value(finding, "raw_output", "metadata")
        if raw:
            st.caption("Analyzer evidence")
            st.json(raw, expanded=False)


__all__ = [
    "as_mapping",
    "filter_findings",
    "finding_fingerprint",
    "finding_location",
    "finding_option_label",
    "finding_title",
    "first_value",
    "github_pr_identity",
    "github_preflight",
    "humanize",
    "language_for_filename",
    "normalized_status",
    "refactor_diff",
    "render_empty_state",
    "render_finding_detail",
    "render_review_metrics",
    "review_findings",
    "review_id",
    "review_name",
    "review_status",
    "review_summary",
    "safe_markdown",
    "status_badge",
    "text_preflight",
    "unique_values",
    "upload_preflight",
]
