"""Application service joining intake, review orchestration, and persistence."""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, cast

import anyio

from patchscope import __version__
from patchscope.config import Settings
from patchscope.domain import (
    AIMetadata,
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingSeverity,
    FindingTriage,
    PromptSectionUsage,
    ReviewDetail,
    ReviewPage,
    ReviewRequest,
    ReviewResult,
    ReviewSourceKind,
    ReviewStatus,
)
from patchscope.domain import (
    SourceFile as DomainSourceFile,
)
from patchscope.errors import IntakeError as PublicIntakeError
from patchscope.errors import ReviewNotFoundError
from patchscope.github import GitHubClient, changed_line_ranges
from patchscope.intake import IntakeError, IntakeLimits, SourceFile, SourceIntake
from patchscope.languages import LANGUAGE_REGISTRY


class ReviewService:
    """Own one complete, durable review lifecycle."""

    def __init__(
        self,
        *,
        settings: Settings,
        repository: Any,
        workflow: Any,
        github_client: GitHubClient,
        intake: SourceIntake,
        markdown_exporter: Any,
        sarif_exporter: Any,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.workflow = workflow
        self.github_client = github_client
        self.intake = intake
        self.markdown_exporter = markdown_exporter
        self.sarif_exporter = sarif_exporter
        self._review_locks_guard = Lock()
        self._review_locks: dict[str, Lock] = {}

    def review_text(self, *, filename: str, content: str, title: str | None = None) -> ReviewDetail:
        try:
            bundle = self.intake.from_mapping({filename: content})
        except IntakeError as exc:
            raise PublicIntakeError(str(exc), detail={"intake_code": exc.code}) from exc
        return self._run_review(
            files=bundle.files,
            source_kind=ReviewSourceKind.TEXT,
            source_reference=filename,
            title=title or f"Review {PurePosixPath(filename).name}",
            metadata={"skipped_files": str(len(bundle.skipped_paths))},
        )

    def review_upload(
        self,
        *,
        filename: str,
        content: bytes,
        title: str | None = None,
    ) -> ReviewDetail:
        try:
            if PurePosixPath(filename).suffix.casefold() == ".zip":
                bundle = self.intake.from_zip(content)
            else:
                bundle = self.intake.from_mapping({filename: content})
        except IntakeError as exc:
            raise PublicIntakeError(str(exc), detail={"intake_code": exc.code}) from exc
        return self._run_review(
            files=bundle.files,
            source_kind=ReviewSourceKind.FILE,
            source_reference=filename,
            title=title or f"Review {PurePosixPath(filename).name}",
            metadata={"skipped_files": str(len(bundle.skipped_paths))},
        )

    async def review_github(self, *, url: str, title: str | None = None) -> ReviewDetail:
        pull_request = await self.github_client.fetch_pull_request(url)
        sources: list[SourceFile] = []
        changed_ranges: dict[str, list[list[int]]] = {}
        for item in pull_request.files:
            language, _ = _infer_language_for_service(item.path)
            ranges = changed_line_ranges(item.patch)
            if item.status == "added" and not ranges:
                line_count = max(1, len(item.content.splitlines()))
                ranges = ((1, line_count),)
            changed_ranges[item.path] = [[start, end] for start, end in ranges]
            sources.append(
                SourceFile.create(
                    item.path,
                    item.content,
                    language_hint=language,
                    is_patch=item.is_patch,
                )
            )
        return await anyio.to_thread.run_sync(
            partial(
                self._run_review,
                files=sources,
                source_kind=ReviewSourceKind.GITHUB,
                source_reference=pull_request.ref.canonical_url,
                title=title or pull_request.title,
                metadata={
                    "author": pull_request.author,
                    "base_branch": pull_request.base_branch,
                    "head_branch": pull_request.head_branch,
                    "head_sha": pull_request.head_sha,
                    "skipped_files": str(len(pull_request.skipped_files)),
                    "patch_only_files": str(sum(source.is_patch for source in pull_request.files)),
                    "change_scope": "added_lines",
                },
                analysis_metadata={"changed_line_ranges": changed_ranges},
            )
        )

    def _run_review(
        self,
        *,
        files: Sequence[SourceFile],
        source_kind: ReviewSourceKind,
        source_reference: str,
        title: str,
        metadata: dict[str, str],
        analysis_metadata: Mapping[str, Any] | None = None,
    ) -> ReviewDetail:
        domain_files = [
            DomainSourceFile(
                path=source.path,
                content=source.content,
                language=source.language_hint,
                sha256=source.sha256,
            )
            for source in files
        ]
        request = ReviewRequest(
            source_kind=source_kind,
            source_reference=source_reference,
            title=title,
            files=domain_files,
            ai_mode=self.settings.ai_mode,
            metadata=metadata,
        )
        review_id = request.review_id
        with self._review_lock(review_id):
            review_id = self.repository.create_review(request)
            current = self.repository.require_review(review_id)
            if current.status is ReviewStatus.COMPLETED:
                return cast(ReviewDetail, current)
            self.repository.mark_running(review_id)
            try:
                state = self.workflow.invoke(
                    files=files,
                    metadata={
                        "review_id": review_id,
                        "title": title,
                        "source_kind": source_kind.value,
                        **metadata,
                        **dict(analysis_metadata or {}),
                    },
                )
                result = _result_from_state(state)
                return cast(ReviewDetail, self.repository.save_result(review_id, result))
            except Exception as exc:
                safe_message = f"Review failed during {type(exc).__name__}"
                self.repository.mark_failed(review_id, safe_message)
                raise

    def _review_lock(self, review_id: str) -> Lock:
        with self._review_locks_guard:
            return self._review_locks.setdefault(review_id, Lock())

    def list_reviews(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: ReviewStatus | None = None,
    ) -> ReviewPage:
        return cast(
            ReviewPage,
            self.repository.list_reviews(limit=limit, offset=offset, status=status),
        )

    def get_review(self, review_id: str) -> ReviewDetail:
        try:
            return cast(ReviewDetail, self.repository.require_review(review_id))
        except Exception as exc:
            if type(exc).__name__ == "ReviewNotFoundError":
                raise ReviewNotFoundError("The requested review was not found") from exc
            raise

    def update_finding(
        self,
        *,
        review_id: str,
        fingerprint: str,
        status: str,
        note: str | None,
    ) -> Finding:
        triage = {
            "open": FindingTriage.OPEN,
            "acknowledged": FindingTriage.ACKNOWLEDGED,
            "accepted": FindingTriage.ACKNOWLEDGED,
            "fixed": FindingTriage.FIXED,
            "resolved": FindingTriage.FIXED,
            "ignored": FindingTriage.IGNORED,
            "dismissed": FindingTriage.IGNORED,
        }[status]
        try:
            return cast(
                Finding,
                self.repository.update_finding_triage(review_id, fingerprint, triage, note=note),
            )
        except Exception as exc:
            if type(exc).__name__ in {"ReviewNotFoundError", "FindingNotFoundError"}:
                raise ReviewNotFoundError("The requested review finding was not found") from exc
            raise

    def export(self, review_id: str, export_format: str) -> tuple[bytes, str, str]:
        review = self.get_review(review_id)
        if export_format == "markdown":
            content = self.markdown_exporter(review)
            if not isinstance(content, str):
                content = str(content)
            return content.encode("utf-8"), "text/markdown", f"patchscope-{review_id}.md"
        if export_format == "sarif":
            import json

            payload = self.sarif_exporter(review)
            content = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            return content, "application/sarif+json", f"patchscope-{review_id}.sarif.json"
        raise ValueError("Unsupported export format")

    def capabilities(self) -> dict[str, object]:
        binary_capabilities = []
        for name in ("ruff", "mypy", "semgrep"):
            available = shutil.which(name) is not None
            binary_capabilities.append(
                {
                    "name": name,
                    "status": "available" if available else "unavailable",
                    "detail": (
                        "Available for bounded static analysis."
                        if available
                        else "Not installed; the review continues with other analyzers."
                    ),
                }
            )
        provider_configured = bool(
            self.settings.openai_api_key and self.settings.openai_api_key.get_secret_value().strip()
        )
        binary_capabilities.extend(
            [
                {
                    "name": "tree-sitter",
                    "status": "available",
                    "detail": "Structural parsing is enabled for supported languages.",
                },
                {
                    "name": "ai-synthesis",
                    "status": "available" if provider_configured else "degraded",
                    "detail": (
                        "OpenAI synthesis is configured."
                        if provider_configured
                        else (
                            "Deterministic offline synthesis is active; no provider key is "
                            "configured."
                        )
                    ),
                },
            ]
        )
        return {
            "version": __version__,
            "ai_mode": self.settings.ai_mode,
            "source_execution": False,
            "inputs": ["text", "file", "zip", "public_github_pull_request"],
            "languages": list(LANGUAGE_REGISTRY.language_names),
            "analyzers": binary_capabilities,
            "exports": ["markdown", "sarif"],
        }

    def ready(self) -> bool:
        ping = getattr(self.repository, "ping", None)
        if callable(ping):
            return bool(ping())
        return True

    def close(self) -> None:
        close = getattr(self.repository, "close", None)
        if callable(close):
            close()


def _result_from_state(state: Mapping[str, Any]) -> ReviewResult:
    refactors = {
        str(item.get("finding_fingerprint")): str(item.get("diff"))
        for item in state.get("refactors", [])
        if isinstance(item, Mapping) and item.get("finding_fingerprint") and item.get("diff")
    }
    findings: list[Finding] = []
    for raw_value in state.get("findings", []):
        if not isinstance(raw_value, Mapping):
            continue
        raw = dict(raw_value)
        message = str(raw.get("message") or raw.get("description") or raw.get("title") or "Finding")
        workflow_fingerprint = str(raw.get("fingerprint") or "")
        path = str(raw.get("path") or "unknown.txt")
        try:
            category = FindingCategory(str(raw.get("category", "maintainability")))
        except ValueError:
            category = FindingCategory.MAINTAINABILITY
        try:
            severity = FindingSeverity(str(raw.get("severity", "info")))
        except ValueError:
            severity = FindingSeverity.INFO
        start_line = max(1, _positive_int(raw.get("start_line"), 1))
        end_line = max(start_line, _positive_int(raw.get("end_line"), start_line))
        finding = Finding.build(
            path=path,
            rule_id=str(raw.get("rule_id") or "PATCHSCOPE"),
            start_line=start_line,
            end_line=end_line,
            start_column=_optional_positive_int(raw.get("start_column")),
            end_column=_optional_positive_int(raw.get("end_column")),
            message=message,
            title=str(raw.get("title") or message),
            category=category,
            severity=severity,
            analyzer=str(raw.get("analyzer") or "patchscope"),
            evidence=str(raw.get("evidence") or ""),
            suggestion=str(raw.get("suggestion") or "Review this code path."),
            refactor_diff=refactors.get(workflow_fingerprint),
        )
        findings.append(finding)

    analyzer_runs = [
        run
        for raw in state.get("analyzer_runs", [])
        if isinstance(raw, Mapping) and (run := _analyzer_run(raw)) is not None
    ]
    raw_ai_value = state.get("ai_metadata")
    raw_ai: Mapping[str, Any] = raw_ai_value if isinstance(raw_ai_value, Mapping) else {}
    raw_mode = str(raw_ai.get("mode", "offline"))
    mode = raw_mode if raw_mode in {"openai", "offline_fallback"} else "offline"
    prompt_sections: dict[str, PromptSectionUsage] = {}
    raw_sections = raw_ai.get("prompt_sections")
    if isinstance(raw_sections, Mapping):
        for raw_name, raw_usage in raw_sections.items():
            if not isinstance(raw_usage, Mapping):
                continue
            original_chars = _optional_nonnegative_int(raw_usage.get("original_chars"))
            included_chars = _optional_nonnegative_int(raw_usage.get("included_chars"))
            prompt_section_chars = _optional_nonnegative_int(raw_usage.get("prompt_chars"))
            if original_chars is None or included_chars is None or prompt_section_chars is None:
                continue
            try:
                prompt_sections[str(raw_name)] = PromptSectionUsage(
                    original_chars=original_chars,
                    included_chars=included_chars,
                    prompt_chars=prompt_section_chars,
                    truncated=included_chars < original_chars,
                )
            except ValueError:
                continue
    prompt_char_limit = _optional_bounded_int(raw_ai.get("prompt_char_limit"), 4_000, 1_000_000)
    prompt_chars = _optional_bounded_int(raw_ai.get("prompt_chars"), 0, 1_000_000)
    if (
        prompt_char_limit is not None
        and prompt_chars is not None
        and prompt_chars > prompt_char_limit
    ):
        prompt_chars = None
    raw_warnings = state.get("warnings", [])
    warning_values = (
        raw_warnings
        if isinstance(raw_warnings, Sequence) and not isinstance(raw_warnings, (str, bytes))
        else []
    )
    warnings = tuple(str(item)[:2_000] for item in warning_values[:20] if str(item))
    ai_metadata = AIMetadata(
        mode=mode,
        provider=str(raw_ai.get("provider"))[:160] if raw_ai.get("provider") else None,
        model=str(raw_ai.get("model"))[:160] if raw_ai.get("model") else None,
        summary=str(raw_ai.get("summary"))[:2_000] if raw_ai.get("summary") else None,
        finding_count=_optional_nonnegative_int(raw_ai.get("finding_count")) or 0,
        accepted_model_findings=(
            _optional_nonnegative_int(raw_ai.get("accepted_model_findings")) or 0
        ),
        fallback_reason=(
            str(raw_ai.get("fallback_reason"))[:2_000] if raw_ai.get("fallback_reason") else None
        ),
        provider_error_type=(
            str(raw_ai.get("provider_error_type"))[:160]
            if raw_ai.get("provider_error_type")
            else None
        ),
        completion_token_limit=_optional_bounded_int(
            raw_ai.get("completion_token_limit"), 256, 16_384
        ),
        prompt_char_limit=prompt_char_limit,
        prompt_chars=prompt_chars,
        prompt_truncated=any(section.truncated for section in prompt_sections.values()),
        prompt_sections=prompt_sections,
        warnings=warnings,
    )
    return ReviewResult(
        findings=findings,
        analyzer_runs=analyzer_runs,
        stage_trace=[str(stage) for stage in state.get("stage_trace", [])],
        ai_metadata=ai_metadata,
    )


def _analyzer_run(raw: Mapping[str, Any]) -> AnalyzerRun | None:
    name = str(raw.get("analyzer") or raw.get("name") or "analyzer")
    raw_status = str(raw.get("status") or "failed")
    status = {
        "succeeded": AnalyzerStatus.COMPLETED,
        "completed": AnalyzerStatus.COMPLETED,
        "unavailable": AnalyzerStatus.UNAVAILABLE,
        "timeout": AnalyzerStatus.TIMED_OUT,
        "timed_out": AnalyzerStatus.TIMED_OUT,
        "degraded": AnalyzerStatus.DEGRADED,
        "not_applicable": AnalyzerStatus.NOT_APPLICABLE,
        "error": AnalyzerStatus.FAILED,
        "failed": AnalyzerStatus.FAILED,
    }.get(raw_status, AnalyzerStatus.FAILED)
    now = datetime.now(UTC)
    started = _datetime_value(raw.get("started_at")) or now
    finished = _datetime_value(raw.get("finished_at"))
    findings_value = raw.get("findings")
    count = raw.get("findings_count")
    if not isinstance(count, int):
        count = len(findings_value) if isinstance(findings_value, Sequence) else 0
    return AnalyzerRun(
        analyzer=name,
        status=status,
        started_at=started,
        finished_at=finished,
        duration_ms=max(0, _positive_int(raw.get("duration_ms"), 0)),
        findings_count=max(0, count),
        detail=(
            str(raw.get("detail") or raw.get("message"))[:4_000]
            if raw.get("detail") or raw.get("message")
            else None
        ),
    )


def _datetime_value(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _positive_int(value: object, default: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default
    )


def _optional_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _optional_bounded_int(value: object, minimum: int, maximum: int) -> int | None:
    candidate = _optional_nonnegative_int(value)
    if candidate is not None and minimum <= candidate <= maximum:
        return candidate
    return None


def _optional_positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return None


def _infer_language_for_service(path: str) -> tuple[str | None, bool]:
    from patchscope.intake import infer_language

    return infer_language(path)


def dump_public(value: Any) -> Any:
    """Convert domain/dataclass values into JSON-compatible public data."""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def build_intake(settings: Settings) -> SourceIntake:
    return SourceIntake(
        IntakeLimits(
            max_files=settings.max_files,
            max_file_bytes=settings.max_file_bytes,
            max_total_bytes=settings.max_review_bytes,
            max_archive_bytes=settings.max_review_bytes,
        )
    )
