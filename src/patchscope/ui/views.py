"""Task-oriented Streamlit views for intake, review triage, and evidence export."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from importlib.resources import files as resource_files
from typing import Any, Protocol, cast

import streamlit as st

from patchscope.languages import LANGUAGE_REGISTRY
from patchscope.ui.client import ApiError, ExportArtifact, JsonObject, client_from_environment
from patchscope.ui.components import (
    as_mapping,
    filter_findings,
    finding_fingerprint,
    finding_location,
    finding_option_label,
    first_value,
    github_preflight,
    humanize,
    normalized_status,
    render_empty_state,
    render_finding_detail,
    render_review_metrics,
    review_findings,
    review_id,
    review_name,
    review_status,
    review_summary,
    safe_markdown,
    status_badge,
    text_preflight,
    unique_values,
    upload_preflight,
)
from patchscope.ui.theme import apply_theme

MAX_UI_SOURCE_BYTES = 2_000_000
WORKSPACE_NEW = "new"
WORKSPACE_REVIEWS = "reviews"
WORKSPACE_DETAIL = "detail"


class UiClient(Protocol):
    def health(self) -> JsonObject: ...

    def capabilities(self) -> JsonObject: ...

    def list_reviews(self, *, limit: int = 100, status: str | None = None) -> list[JsonObject]: ...

    def get_review(self, review_id: str) -> JsonObject: ...

    def create_text_review(
        self, *, filename: str, content: str, name: str | None = None
    ) -> JsonObject: ...

    def create_file_review(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        name: str | None = None,
    ) -> JsonObject: ...

    def create_github_review(self, *, url: str, name: str | None = None) -> JsonObject: ...

    def update_finding(
        self,
        *,
        review_id: str,
        fingerprint: str,
        status: str,
        note: str | None = None,
    ) -> JsonObject: ...

    def download_export(self, review_id: str, export_format: str) -> ExportArtifact: ...


@st.cache_resource(show_spinner=False)
def _cached_client() -> UiClient:
    return client_from_environment()


def _resolve_client() -> UiClient:
    injected = st.session_state.get("_patchscope_client")
    if injected is not None:
        return cast(UiClient, injected)
    return _cached_client()


def _initialize_state() -> None:
    st.session_state.setdefault("patchscope_selected_review_id", None)
    st.session_state.setdefault("patchscope_selected_review", None)
    st.session_state.setdefault("patchscope_notice", None)
    st.session_state.setdefault("patchscope_exports", {})
    st.session_state.setdefault("patchscope_workspace_page", WORKSPACE_NEW)
    st.session_state.setdefault("patchscope_workspace_navigation", WORKSPACE_NEW)


def _set_notice(tone: str, message: str) -> None:
    st.session_state["patchscope_notice"] = {"tone": tone, "message": message}


def _render_notice() -> None:
    notice = st.session_state.pop("patchscope_notice", None)
    if not isinstance(notice, Mapping) or not isinstance(notice.get("message"), str):
        return
    message = str(notice["message"])
    if notice.get("tone") == "success":
        st.success(message)
    elif notice.get("tone") == "warning":
        st.warning(message)
    else:
        st.error(message)


def _friendly_error(error: Exception) -> str:
    if isinstance(error, ApiError):
        return error.message
    return "Something unexpected interrupted the request. Try again."


def _unwrap_review(payload: Mapping[str, Any]) -> JsonObject:
    nested = payload.get("review")
    if isinstance(nested, Mapping):
        return dict(nested)
    return dict(payload)


def _remember_review(review: Mapping[str, Any]) -> None:
    item = _unwrap_review(review)
    item_id = review_id(item)
    st.session_state["patchscope_selected_review"] = item
    st.session_state["patchscope_selected_review_id"] = item_id or None
    st.session_state["patchscope_workspace_page"] = WORKSPACE_DETAIL


def _safe_service_snapshot(client: UiClient) -> tuple[JsonObject | None, JsonObject | None]:
    health: JsonObject | None = None
    capabilities: JsonObject | None = None
    with suppress(Exception):
        health = client.health()
    with suppress(Exception):
        capabilities = client.capabilities()
    return health, capabilities


def _render_sidebar(client: UiClient) -> bool:
    health, capabilities = _safe_service_snapshot(client)
    with st.sidebar:
        if health is None:
            st.error("Review service unavailable")
        st.caption(
            "Review code without executing it. Findings remain linked to source "
            "evidence and preview-only refactors."
        )
        with st.expander(
            "System details",
            expanded=False,
            icon=":material/info:",
        ):
            if health is None:
                st.caption(
                    "Runtime details are unavailable. Intake remains visible so "
                    "you can prepare a review."
                )
            else:
                st.caption("API health")
                st.json(health, expanded=False)
            if capabilities is not None:
                analyzers = first_value(capabilities, "analyzers", "tools")
                if analyzers:
                    st.caption("Analyzer availability")
                    st.json(analyzers, expanded=False)
                ai_mode = first_value(capabilities, "ai_mode", "model_mode")
                if ai_mode:
                    st.caption(f"Synthesis: {humanize(ai_mode)}")
    return health is not None


def _navigate_workspace() -> None:
    destination = st.session_state.get("patchscope_workspace_navigation")
    if destination in {WORKSPACE_NEW, WORKSPACE_REVIEWS}:
        st.session_state["patchscope_workspace_page"] = destination


def _show_workspace(destination: str) -> None:
    if destination not in {WORKSPACE_NEW, WORKSPACE_REVIEWS}:
        return
    st.session_state["patchscope_workspace_page"] = destination
    st.session_state["patchscope_workspace_navigation"] = destination
    st.rerun()


def _validate_text_source(filename: str, content: str) -> str | None:
    if not filename.strip():
        return "Enter a supported source filename."
    if LANGUAGE_REGISTRY.language_for_path(
        filename.strip()
    ) is None and not LANGUAGE_REGISTRY.is_patch_path(filename.strip()):
        return "Use a supported source filename such as service.py, Dockerfile, or change.patch."
    if not content.strip():
        return "Paste source code before checking the review boundary."
    if len(content.encode("utf-8")) > MAX_UI_SOURCE_BYTES:
        return "The pasted source is larger than the 2 MB workbench limit."
    return None


def _load_example_source() -> None:
    example = resource_files("patchscope.data").joinpath("insecure_checkout.py.txt")
    st.session_state["paste_review_name"] = "Insecure checkout example"
    st.session_state["paste_filename"] = "insecure_checkout.py"
    st.session_state["paste_source"] = example.read_text(encoding="utf-8")


def _render_paste_intake(client: UiClient, *, service_available: bool) -> None:
    st.button(
        "Load example review",
        key="load_example_review",
        on_click=_load_example_source,
        help="Populate a local, credential-free example with several review categories.",
    )
    with st.form("paste_review_form", border=True):
        st.subheader("Paste code", anchor=False)
        st.caption("Best for one focused file or a small reproducible example.")
        filename = st.text_input(
            "Filename",
            key="paste_filename",
            placeholder="checkout.py",
            help="The extension selects parsing and syntax highlighting.",
        )
        content = st.text_area(
            "Source code",
            key="paste_source",
            height=260,
            max_chars=2_000_000,
            placeholder="Paste the file exactly as it should be reviewed…",
        )
        with st.expander(
            "More options",
            expanded=False,
            icon=":material/tune:",
            type="compact",
        ):
            name = st.text_input(
                "Review name (optional)",
                key="paste_review_name",
                placeholder="Checkout input validation",
                help="Shown in Reviews. PatchScope uses the filename when left blank.",
            )
        submitted = st.form_submit_button(
            "Run review",
            type="primary",
            width="stretch",
            disabled=not service_available,
        )
    if submitted:
        error = _validate_text_source(filename, content)
        if error:
            _set_notice("warning", error)
            st.rerun()
        else:
            _start_review(
                client,
                text_preflight(name=name, filename=filename, content=content),
            )


def _render_upload_intake(client: UiClient, *, service_available: bool) -> None:
    with st.form("upload_review_form", border=True):
        st.subheader("Upload files", anchor=False)
        st.caption("Review one source file or a ZIP archive up to 2 MB.")
        uploaded = st.file_uploader(
            "Source file or ZIP archive",
            accept_multiple_files=False,
            help="Maximum size: 2 MB. Unsupported files are rejected before review.",
        )
        with st.expander(
            "More options",
            expanded=False,
            icon=":material/tune:",
            type="compact",
        ):
            name = st.text_input(
                "Review name (optional)",
                key="upload_review_name",
                placeholder="Payment service review",
                help="Shown in Reviews. PatchScope uses the filename when left blank.",
            )
        submitted = st.form_submit_button(
            "Run review",
            type="primary",
            width="stretch",
            disabled=not service_available,
        )
    if submitted:
        if uploaded is None:
            _set_notice("warning", "Choose a source file to review.")
            st.rerun()
        content = uploaded.getvalue()
        if not content:
            _set_notice("warning", "The selected source file is empty.")
            st.rerun()
        if len(content) > MAX_UI_SOURCE_BYTES:
            _set_notice(
                "warning", "The selected source file is larger than the 2 MB workbench limit."
            )
            st.rerun()
        if not uploaded.name.casefold().endswith(".zip") and (
            LANGUAGE_REGISTRY.language_for_path(uploaded.name) is None
            and not LANGUAGE_REGISTRY.is_patch_path(uploaded.name)
        ):
            _set_notice(
                "warning",
                "Choose a supported source file, patch, or ZIP archive.",
            )
            st.rerun()
        _start_review(
            client,
            upload_preflight(
                name=name,
                filename=uploaded.name,
                content=content,
                content_type=uploaded.type or "text/plain",
            ),
        )


def _render_github_intake(client: UiClient, *, service_available: bool) -> None:
    with st.form("github_review_form", border=True):
        st.subheader("GitHub pull request", anchor=False)
        st.caption("Paste the URL of a public GitHub pull request.")
        url = st.text_input(
            "Pull request URL",
            key="github_pr_url",
            placeholder="https://github.com/owner/repository/pull/42",
        )
        with st.expander(
            "More options",
            expanded=False,
            icon=":material/tune:",
            type="compact",
        ):
            name = st.text_input(
                "Review name (optional)",
                key="github_review_name",
                placeholder="Payment validation PR",
                help="Shown in Reviews. PatchScope uses the repository and PR number when blank.",
            )
        submitted = st.form_submit_button(
            "Run review",
            type="primary",
            width="stretch",
            disabled=not service_available,
        )
    if submitted:
        try:
            preflight = github_preflight(name=name, url=url)
        except ValueError as error:
            _set_notice("warning", str(error))
            st.rerun()
        else:
            _start_review(client, preflight)


def _start_review(client: UiClient, preflight: Mapping[str, Any]) -> None:
    kind = str(preflight.get("kind", ""))
    try:
        with st.spinner("Running review checks…"):
            if kind == "text":
                created = client.create_text_review(
                    filename=str(preflight["filename"]),
                    content=str(preflight["content"]),
                    name=str(preflight.get("name") or "") or None,
                )
            elif kind == "file":
                created = client.create_file_review(
                    filename=str(preflight["filename"]),
                    content=bytes(preflight["content"]),
                    content_type=str(preflight.get("content_type") or "text/plain"),
                    name=str(preflight.get("name") or "") or None,
                )
            elif kind == "github":
                created = client.create_github_review(
                    url=str(preflight["url"]),
                    name=str(preflight.get("name") or "") or None,
                )
            else:
                raise ValueError("Unknown review intake mode")
        review = _unwrap_review(created)
        created_id = review_id(review)
        if created_id and not review_findings(review):
            with suppress(Exception):
                review = _unwrap_review(client.get_review(created_id))
        _remember_review(review)
        _set_notice("success", "Review completed. Findings are ready to triage.")
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _render_new_review(client: UiClient, *, service_available: bool) -> None:
    st.subheader("New review", anchor=False)
    intro, safety = st.columns([5, 1], vertical_alignment="center")
    intro.caption("Choose a source and run an evidence-backed code review.")
    with safety.popover(
        "How it works",
        icon=":material/info:",
        help="How PatchScope handles submitted code",
    ):
        st.markdown("**Your code is treated as untrusted text.**")
        st.caption(
            "PatchScope never executes submitted code and never applies suggested changes. "
            "Refactors are previews for you to inspect."
        )
    paste_tab, upload_tab, github_tab = st.tabs(["Paste code", "Upload file", "GitHub PR"])
    with paste_tab:
        _render_paste_intake(client, service_available=service_available)
    with upload_tab:
        _render_upload_intake(client, service_available=service_available)
    with github_tab:
        _render_github_intake(client, service_available=service_available)


def _review_matches(review: Mapping[str, Any], query: str, status: str) -> bool:
    if status != "all" and review_status(review) != status:
        return False
    if not query.strip():
        return True
    source = as_mapping(first_value(review, "source", "request", default={}))
    searchable = " ".join(
        (
            review_name(review),
            str(first_value(review, "filename", "source_type", default="")),
            str(first_value(source, "filename", "url", default="")),
        )
    ).casefold()
    return query.strip().casefold() in searchable


def _format_review_time(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    rendered = parsed.strftime("%b %d, %Y · %H:%M")
    timezone = parsed.tzname()
    return f"{rendered} {timezone}" if timezone else rendered


def _open_review(client: UiClient, item: Mapping[str, Any]) -> None:
    item_id = review_id(item)
    try:
        detail = client.get_review(item_id) if item_id else dict(item)
        _remember_review(_unwrap_review(detail))
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _render_review_card(client: UiClient, item: Mapping[str, Any], *, index: int) -> None:
    item_id = review_id(item)
    with st.container(border=True):
        identity, state, action = st.columns([4, 1.2, 1.4], vertical_alignment="center")
        identity.markdown(f"**{safe_markdown(review_name(item))}**")
        source = first_value(item, "filename", "source_type", "source_kind", default="Code review")
        created = first_value(item, "created_at", "updated_at")
        caption = humanize(source)
        if created:
            caption += f" · {_format_review_time(created)}"
        identity.caption(caption)
        with state:
            status_badge(review_status(item))
        label = "Open"
        if action.button(
            label,
            key=f"open_review_{_key_token(item_id or str(index))}",
            width="stretch",
        ):
            _open_review(client, item)


def _render_inbox(client: UiClient) -> None:
    heading, refresh = st.columns([4, 1], vertical_alignment="center")
    heading.subheader("Reviews", anchor=False)
    heading.caption("Return to saved findings, decisions, and refactor previews.")
    if refresh.button("Refresh", width="stretch", key="refresh_inbox"):
        st.rerun()

    try:
        reviews = client.list_reviews(limit=100)
    except Exception as error:
        st.error(_friendly_error(error))
        st.caption("No review data was changed. Start the API and refresh this inbox.")
        reviews = []

    filters = st.columns([3, 2])
    query = filters[0].text_input(
        "Search reviews",
        key="inbox_query",
        placeholder="Name, source, or filename",
    )
    status_options = ["all", *sorted({review_status(item) for item in reviews})]
    status = filters[1].selectbox(
        "Status",
        status_options,
        format_func=humanize,
        key="inbox_status",
    )
    visible = [item for item in reviews if _review_matches(item, query, status)]
    if not visible:
        message = (
            "No reviews match the current filters."
            if reviews
            else (
                "Paste code, upload a file, or add a public pull request to "
                "create the first review."
            )
        )
        render_empty_state("No reviews to show", message)
    else:
        for index, item in enumerate(visible, start=1):
            _render_review_card(client, item, index=index)


def _key_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def _review_recommendation(review: Mapping[str, Any]) -> str | None:
    summary = review_summary(review)
    value = first_value(
        review,
        "merge_recommendation",
        "recommendation",
        default=first_value(summary, "merge_recommendation", "recommendation"),
    )
    return str(value) if value else None


def _render_recommendation(review: Mapping[str, Any]) -> None:
    recommendation = _review_recommendation(review)
    if not recommendation:
        return
    normalized = normalized_status(recommendation, fallback="review")
    message = f"Decision: {humanize(recommendation)}"
    if normalized in {"approve", "approved", "ready", "merge"}:
        st.success(message)
    elif normalized in {"request_changes", "changes_requested", "hold", "block"}:
        st.warning(message)
    else:
        st.info(message)


def _perform_triage(
    client: UiClient,
    review: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    status: str,
    note: str | None,
    key_prefix: str,
) -> None:
    item_id = review_id(review)
    fingerprint = finding_fingerprint(finding)
    try:
        client.update_finding(
            review_id=item_id,
            fingerprint=fingerprint,
            status=status,
            note=note,
        )
        try:
            updated = _unwrap_review(client.get_review(item_id))
        except Exception:
            updated = _locally_update_finding(review, fingerprint, status, note)
        _remember_review(updated)
        st.session_state[f"patchscope_reset_status_filter_{key_prefix}"] = True
        message = {
            "acknowledged": "Finding acknowledged.",
            "ignored": "Finding ignored.",
            "open": "Finding reopened.",
        }.get(status, f"Finding marked {humanize(status).casefold()}.")
        _set_notice("success", message)
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _locally_update_finding(
    review: Mapping[str, Any], fingerprint: str, status: str, note: str | None
) -> JsonObject:
    updated = dict(review)
    findings: list[JsonObject] = []
    for finding in review_findings(review):
        if finding_fingerprint(finding) == fingerprint:
            finding = {**finding, "triage": status, "triage_status": status}
            if note:
                finding["triage_note"] = note
        findings.append(finding)
    updated["findings"] = findings
    return updated


def _render_triage(
    client: UiClient,
    review: Mapping[str, Any],
    finding: Mapping[str, Any],
    *,
    key_prefix: str,
) -> None:
    status = normalized_status(
        first_value(finding, "triage_status", "triage", "status", default="open"),
        fallback="open",
    )
    st.markdown("**Finding status**")
    token = _key_token(finding_fingerprint(finding))
    if status == "open":
        acknowledge, ignore = st.columns(2)
        if acknowledge.button(
            "Acknowledge",
            type="primary",
            width="stretch",
            key=f"{key_prefix}_acknowledge_{token}",
            help="Record that you reviewed this finding. No source code is changed.",
        ):
            _perform_triage(
                client,
                review,
                finding,
                status="acknowledged",
                note=None,
                key_prefix=key_prefix,
            )
        with ignore.popover(
            "Ignore",
            width="stretch",
            key=f"{key_prefix}_ignore_{token}",
            help="Record that this finding does not apply. No source code is changed.",
        ):
            note = st.text_input(
                "Note (optional)",
                key=f"{key_prefix}_ignore_note_{token}",
                placeholder="Why this does not apply",
            )
            if st.button(
                "Confirm ignore",
                width="stretch",
                key=f"{key_prefix}_confirm_ignore_{token}",
            ):
                _perform_triage(
                    client,
                    review,
                    finding,
                    status="ignored",
                    note=note or None,
                    key_prefix=key_prefix,
                )
    else:
        note = first_value(finding, "triage_note", "note")
        if note:
            st.caption(f"Decision note: {note}")
        if st.button(
            "Reopen",
            width="stretch",
            key=f"{key_prefix}_reopen_{token}",
            help="Return this finding to the open review queue.",
        ):
            _perform_triage(
                client,
                review,
                finding,
                status="open",
                note=None,
                key_prefix=key_prefix,
            )


def _render_findings(client: UiClient, review: Mapping[str, Any], *, key_prefix: str) -> None:
    findings = review_findings(review)
    if not findings:
        render_empty_state(
            "No findings",
            "PatchScope did not return an actionable finding. Check analyzer "
            "coverage in Summary before treating this as a clean review.",
        )
        return

    search = st.text_input(
        "Search findings",
        key=f"{key_prefix}_finding_query",
        placeholder="Issue, file, rule, or analyzer",
    )
    severity_options = unique_values(findings, "severity")
    category_options = unique_values(findings, "category")
    status_options = sorted(
        {
            normalized_status(
                first_value(item, "triage_status", "triage", "status", default="open")
            )
            for item in findings
        }
    )
    status_filter_key = f"{key_prefix}_status_filter"
    reset_status_filter = st.session_state.pop(
        f"patchscope_reset_status_filter_{key_prefix}", False
    )
    if reset_status_filter or status_filter_key not in st.session_state:
        st.session_state[status_filter_key] = status_options
    with st.popover(
        "Filters",
        icon=":material/filter_list:",
        help="Narrow findings by severity, category, or status.",
    ):
        severities = st.multiselect(
            "Severity",
            severity_options,
            default=severity_options,
            format_func=humanize,
            key=f"{key_prefix}_severity_filter",
        )
        categories = st.multiselect(
            "Category",
            category_options,
            default=category_options,
            format_func=humanize,
            key=f"{key_prefix}_category_filter",
        )
        statuses = st.multiselect(
            "Finding status",
            status_options,
            format_func=humanize,
            key=status_filter_key,
        )
    visible = filter_findings(
        findings,
        query=search,
        severities=severities,
        categories=categories,
        statuses=statuses,
    )
    st.caption(f"{len(visible)} of {len(findings)} findings")
    if not visible:
        render_empty_state("No matching findings", "Clear one or more filters to continue triage.")
        return

    selection, detail = st.columns([2, 3], vertical_alignment="top")
    with selection:
        selected_index = st.radio(
            "Finding",
            options=list(range(len(visible))),
            format_func=lambda index: finding_option_label(visible[index]),
            key=f"{key_prefix}_finding_selection",
        )
    selected = visible[int(selected_index)]
    with detail, st.container(border=True):
        render_finding_detail(selected)
        st.divider()
        _render_triage(client, review, selected, key_prefix=key_prefix)


def _review_files(review: Mapping[str, Any]) -> list[JsonObject]:
    request = as_mapping(review.get("request"))
    raw_files = first_value(
        review,
        "files",
        "source_files",
        default=first_value(request, "files", default=[]),
    )
    files: list[JsonObject] = []
    if isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, Mapping):
                files.append(dict(item))
            elif isinstance(item, str):
                files.append({"path": item})
    if files:
        return files
    paths = sorted({finding_location(item).split(":", 1)[0] for item in review_findings(review)})
    return [{"path": path} for path in paths]


def _render_changed_files(review: Mapping[str, Any], *, key_prefix: str) -> None:
    files = _review_files(review)
    if not files:
        render_empty_state(
            "No file detail",
            "This review does not include persisted file content or patch metadata.",
        )
        return
    selected_index = st.selectbox(
        "Changed file",
        options=list(range(len(files))),
        format_func=lambda index: str(
            first_value(files[index], "path", "filename", default=f"File {index + 1}")
        ),
        key=f"{key_prefix}_changed_file",
    )
    selected = files[int(selected_index)]
    path = str(first_value(selected, "path", "filename", default="Source file"))
    st.subheader(path, anchor=False)
    language = str(first_value(selected, "language", default="")) or None
    diff = first_value(selected, "diff", "patch", "unified_diff")
    content = first_value(selected, "content", "source", "text")
    if diff:
        st.caption("Unified diff")
        st.code(str(diff), language="diff", wrap_lines=False)
    elif content:
        st.caption("Submitted source")
        st.code(str(content), language=language, line_numbers=True, wrap_lines=False)
    else:
        related = [
            item
            for item in review_findings(review)
            if finding_location(item).split(":", 1)[0] == path
        ]
        st.caption(
            f"{len(related)} findings reference this file. Full source was not "
            "retained in the review response."
        )


def _summary_rows(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    return [{label: humanize(key), "Count": count} for key, count in value.items()]


def _prepare_export(client: UiClient, review: Mapping[str, Any], export_format: str) -> None:
    item_id = review_id(review)
    try:
        with st.spinner(f"Preparing {humanize(export_format)} evidence…"):
            artifact = client.download_export(item_id, export_format)
        exports = dict(st.session_state.get("patchscope_exports", {}))
        exports[f"{item_id}:{export_format}"] = artifact
        st.session_state["patchscope_exports"] = exports
        _set_notice("success", f"{humanize(export_format)} export is ready.")
    except Exception as error:
        _set_notice("error", _friendly_error(error))
    st.rerun()


def _render_exports(client: UiClient, review: Mapping[str, Any], *, key_prefix: str) -> None:
    item_id = review_id(review)
    with st.expander("Export review evidence", expanded=False):
        st.caption(
            "Exports preserve persisted findings and triage state. Preparing an "
            "export does not rerun the review."
        )
        controls = st.columns(2)
        for index, export_format in enumerate(("markdown", "sarif")):
            if controls[index].button(
                f"Prepare {humanize(export_format)}",
                key=f"{key_prefix}_prepare_{export_format}",
                width="stretch",
                help=(
                    "SARIF works with GitHub code scanning and compatible developer tools."
                    if export_format == "sarif"
                    else "Markdown is useful for pull-request descriptions and review notes."
                ),
            ):
                _prepare_export(client, review, export_format)
        exports = st.session_state.get("patchscope_exports", {})
        if isinstance(exports, Mapping):
            for export_format in ("markdown", "sarif"):
                artifact = exports.get(f"{item_id}:{export_format}")
                if isinstance(artifact, ExportArtifact):
                    st.download_button(
                        f"Download {humanize(export_format)}",
                        data=artifact.content,
                        file_name=artifact.filename,
                        mime=artifact.media_type,
                        key=f"{key_prefix}_download_{export_format}",
                        width="stretch",
                    )


def _render_summary(client: UiClient, review: Mapping[str, Any], *, key_prefix: str) -> None:
    summary = review_summary(review)
    severity_rows = _summary_rows(
        first_value(summary, "by_severity", "severity_counts"), "Severity"
    )
    category_rows = _summary_rows(
        first_value(summary, "by_category", "category_counts"), "Category"
    )
    count_columns = st.columns(2)
    with count_columns[0]:
        st.markdown("**Severity**")
        if severity_rows:
            st.dataframe(severity_rows, hide_index=True, width="stretch")
        else:
            st.caption("Severity totals are not available.")
    with count_columns[1]:
        st.markdown("**Category**")
        if category_rows:
            st.dataframe(category_rows, hide_index=True, width="stretch")
        else:
            st.caption("Category totals are not available.")

    analyzers = first_value(
        review, "analyzer_runs", "analyzers", default=first_value(summary, "analyzers")
    )
    st.markdown("**Analyzer coverage**")
    if isinstance(analyzers, list) and analyzers:
        st.dataframe(analyzers, hide_index=True, width="stretch")
    elif isinstance(analyzers, Mapping) and analyzers:
        rows = [
            {"Analyzer": humanize(name), "Status": humanize(status)}
            for name, status in analyzers.items()
        ]
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption(
            "Analyzer coverage was not reported. Do not interpret this review "
            "as proof that every checker ran."
        )

    with st.expander("Review execution details", expanded=False):
        trace = first_value(review, "stage_trace", "workflow_trace", default=[])
        if isinstance(trace, Sequence) and not isinstance(trace, (str, bytes)) and trace:
            st.caption("Workflow stages")
            st.write(" → ".join(humanize(stage) for stage in trace))
        metadata = first_value(review, "ai_metadata", "model_metadata", "technical_details")
        if metadata:
            st.caption("Synthesis provenance")
            st.json(metadata, expanded=False)
        if not trace and not metadata:
            st.caption("No additional execution metadata was reported.")
    _render_exports(client, review, key_prefix=key_prefix)


def _render_review_detail(client: UiClient, review: JsonObject, *, key_prefix: str) -> None:
    header, state = st.columns([4, 1], vertical_alignment="center")
    header.subheader(review_name(review), anchor=False)
    request = as_mapping(review.get("request"))
    source = first_value(
        review,
        "filename",
        "source_type",
        "source_kind",
        default=first_value(request, "source_kind", default="Code review"),
    )
    header.caption(humanize(source))
    with state:
        status_badge(review_status(review))
    findings = review_findings(review)
    _render_recommendation(review)
    render_review_metrics(review, findings)
    findings_tab, files_tab, summary_tab = st.tabs(["Findings", "Code", "Report"])
    with findings_tab:
        _render_findings(client, review, key_prefix=key_prefix)
    with files_tab:
        _render_changed_files(review, key_prefix=key_prefix)
    with summary_tab:
        _render_summary(client, review, key_prefix=key_prefix)


def _render_detail_workspace(client: UiClient, review: JsonObject) -> None:
    actions = st.columns([1.2, 1.2, 5], vertical_alignment="center")
    if actions[0].button(
        "Back to reviews",
        key="back_to_reviews",
        width="stretch",
        icon=":material/arrow_back:",
    ):
        _show_workspace(WORKSPACE_REVIEWS)
    if actions[1].button(
        "Review another",
        key="review_another",
        width="stretch",
        icon=":material/add:",
    ):
        _show_workspace(WORKSPACE_NEW)
    _render_review_detail(client, review, key_prefix="detail")


def render_app() -> None:
    st.set_page_config(
        page_title="PatchScope · Code review workbench",
        page_icon="⌘",
        layout="wide",
        initial_sidebar_state="collapsed",
        menu_items={
            "Get Help": None,
            "Report a bug": None,
            "About": (
                "PatchScope reviews code as untrusted text and returns "
                "evidence-backed findings and preview-only refactors."
            ),
        },
    )
    apply_theme()
    _initialize_state()
    client = _resolve_client()
    service_available = _render_sidebar(client)

    st.title("PatchScope", anchor=False)
    st.caption("Review code, inspect the evidence, and decide what changes to keep.")
    _render_notice()
    if not service_available:
        st.error(
            "The review service isn't running. Start it in another terminal with "
            "`patchscope serve`, then refresh this page. From a source checkout, prefix the "
            "command with `uv run`."
        )

    page = st.session_state.get("patchscope_workspace_page", WORKSPACE_NEW)
    selected = st.session_state.get("patchscope_selected_review")
    if page == WORKSPACE_DETAIL and isinstance(selected, Mapping):
        _render_detail_workspace(client, dict(selected))
        return
    if page == WORKSPACE_DETAIL:
        st.session_state["patchscope_workspace_page"] = WORKSPACE_NEW
        st.session_state["patchscope_workspace_navigation"] = WORKSPACE_NEW
        page = WORKSPACE_NEW

    st.segmented_control(
        "Workspace",
        options=[WORKSPACE_NEW, WORKSPACE_REVIEWS],
        format_func=lambda value: {
            WORKSPACE_NEW: "New review",
            WORKSPACE_REVIEWS: "Reviews",
        }[value],
        selection_mode="single",
        required=True,
        key="patchscope_workspace_navigation",
        on_change=_navigate_workspace,
        label_visibility="collapsed",
    )
    page = st.session_state.get("patchscope_workspace_page", WORKSPACE_NEW)
    if page == WORKSPACE_REVIEWS:
        _render_inbox(client)
    else:
        _render_new_review(client, service_available=service_available)


__all__ = ["UiClient", "render_app"]
