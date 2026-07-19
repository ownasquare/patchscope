from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from patchscope.config import Settings
from patchscope.database import create_database
from patchscope.domain import AnalyzerStatus, ReviewStatus
from patchscope.errors import IntakeError as PublicIntakeError
from patchscope.errors import ReviewNotFoundError as PublicReviewNotFoundError
from patchscope.github import GitHubPullRequest, GitHubPullRequestRef, GitHubSource
from patchscope.languages import LANGUAGE_REGISTRY
from patchscope.repository import ReviewRepository
from patchscope.service import (
    ReviewService,
    _analyzer_run,
    _result_from_state,
    build_intake,
    dump_public,
)


class CaptureWorkflow:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def invoke(self, *, files: list[object], metadata: dict[str, object]) -> dict[str, object]:
        self.calls.append({"files": files, "metadata": metadata})
        if self.error is not None:
            raise self.error
        return {
            "findings": [],
            "analyzer_runs": [],
            "ai_metadata": {"mode": "offline"},
            "stage_trace": ["parse", "analyze", "synthesize", "refactor", "score"],
            "warnings": [],
        }


class FakeGitHubClient:
    async def fetch_pull_request(self, _value: str) -> GitHubPullRequest:
        return GitHubPullRequest(
            ref=GitHubPullRequestRef(owner="acme", repository="shop", number=9),
            title="Patch-only fix",
            author="casey",
            head_sha="b" * 40,
            base_branch="main",
            head_branch="fix",
            files=(
                GitHubSource(
                    path="src/fix.py",
                    content="@@ -1 +1 @@\n-old\n+new\n",
                    status="modified",
                    additions=1,
                    deletions=1,
                    patch="@@ -1 +1 @@\n-old\n+new\n",
                    is_patch=True,
                ),
            ),
            skipped_files=("assets/logo.png",),
        )


def _make_service(
    tmp_path: Path,
    *,
    workflow: CaptureWorkflow | None = None,
    openai_api_key: SecretStr | None = None,
) -> ReviewService:
    settings = Settings(
        data_dir=tmp_path,
        ai_mode="offline",
        openai_api_key=openai_api_key,
        _env_file=None,
    )
    repository = ReviewRepository(create_database(settings.database_url))
    return ReviewService(
        settings=settings,
        repository=repository,
        workflow=workflow or CaptureWorkflow(),
        github_client=FakeGitHubClient(),  # type: ignore[arg-type]
        intake=build_intake(settings),
        markdown_exporter=lambda _review: "# Review",
        sarif_exporter=lambda review: {"review_id": review.id},
    )


def _zip_source(path: str, content: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(path, content)
    return output.getvalue()


def test_review_upload_handles_zip_and_translates_intake_failures(tmp_path: Path) -> None:
    workflow = CaptureWorkflow()
    service = _make_service(tmp_path, workflow=workflow)
    try:
        review = service.review_upload(
            filename="source.zip",
            content=_zip_source("src/checkout.py", "print('ok')\n"),
        )

        assert review.status is ReviewStatus.COMPLETED
        assert review.request.source_reference == "source.zip"
        assert review.request.files[0].path == "src/checkout.py"
        assert workflow.calls[0]["metadata"] == {
            "review_id": review.id,
            "title": "Review source.zip",
            "source_kind": "file",
            "skipped_files": "0",
        }

        with pytest.raises(PublicIntakeError) as captured:
            service.review_upload(filename="../escape.py", content=b"pass\n")
        assert captured.value.detail["intake_code"] == "unsafe_path"
    finally:
        service.close()


def test_failed_workflow_is_persisted_with_sanitized_error(tmp_path: Path) -> None:
    workflow = CaptureWorkflow(error=RuntimeError("provider secret must never be persisted"))
    service = _make_service(tmp_path, workflow=workflow)
    try:
        with pytest.raises(RuntimeError, match="provider secret"):
            service.review_text(filename="failure.py", content="pass\n")

        page = service.list_reviews()
        persisted = service.repository.require_review(page.items[0].id)
        assert persisted.status is ReviewStatus.FAILED
        assert persisted.error_message == "Review failed during RuntimeError"
        assert "provider secret" not in persisted.error_message
    finally:
        service.close()


@pytest.mark.asyncio
async def test_github_review_preserves_pull_request_provenance(tmp_path: Path) -> None:
    workflow = CaptureWorkflow()
    service = _make_service(tmp_path, workflow=workflow)
    try:
        review = await service.review_github(url="https://github.com/acme/shop/pull/9")

        assert review.request.source_reference == "https://github.com/acme/shop/pull/9"
        assert review.request.metadata == {
            "author": "casey",
            "base_branch": "main",
            "head_branch": "fix",
            "head_sha": "b" * 40,
            "skipped_files": "1",
            "patch_only_files": "1",
            "change_scope": "added_lines",
        }
        source = workflow.calls[0]["files"][0]  # type: ignore[index]
        assert source.path == "src/fix.py"
        assert source.is_patch is True
        assert workflow.calls[0]["metadata"]["changed_line_ranges"] == {  # type: ignore[index]
            "src/fix.py": [[1, 1]]
        }
    finally:
        service.close()


def test_repository_errors_are_sanitized_but_unexpected_errors_propagate(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    class ReviewNotFoundError(RuntimeError):
        pass

    class FindingNotFoundError(RuntimeError):
        pass

    try:
        service.repository.require_review = lambda _review_id: (_ for _ in ()).throw(
            ReviewNotFoundError("database detail")
        )
        with pytest.raises(PublicReviewNotFoundError, match="requested review was not found"):
            service.get_review("missing")

        service.repository.update_finding_triage = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FindingNotFoundError("database detail")
        )
        with pytest.raises(PublicReviewNotFoundError, match="review finding was not found"):
            service.update_finding(
                review_id="missing",
                fingerprint="finding",
                status="resolved",
                note=None,
            )

        service.repository.require_review = lambda _review_id: (_ for _ in ()).throw(
            ValueError("unexpected")
        )
        with pytest.raises(ValueError, match="unexpected"):
            service.get_review("broken")
    finally:
        service.close()


def test_exports_readiness_capabilities_and_close_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(tmp_path, openai_api_key=SecretStr("configured"))
    review = service.review_text(filename="safe.py", content="pass\n")
    service.markdown_exporter = lambda _review: 123
    monkeypatch.setattr("patchscope.service.shutil.which", lambda name: f"/tools/{name}")

    markdown, markdown_type, markdown_name = service.export(review.id, "markdown")
    sarif, sarif_type, sarif_name = service.export(review.id, "sarif")
    capabilities = service.capabilities()

    assert (markdown, markdown_type, markdown_name) == (
        b"123",
        "text/markdown",
        f"patchscope-{review.id}.md",
    )
    assert json.loads(sarif) == {"review_id": review.id}
    assert sarif_type == "application/sarif+json"
    assert sarif_name.endswith(".sarif.json")
    assert all(item["status"] == "available" for item in capabilities["analyzers"])
    assert capabilities["languages"] == list(LANGUAGE_REGISTRY.language_names)
    assert service.ready() is True
    with pytest.raises(ValueError, match="Unsupported export format"):
        service.export(review.id, "html")

    repository = service.repository
    close = Mock(wraps=repository.close)
    repository.close = close
    service.close()
    close.assert_called_once_with()


def test_optional_repository_lifecycle_methods_are_not_required(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, ai_mode="offline", _env_file=None)
    service = ReviewService(
        settings=settings,
        repository=SimpleNamespace(),
        workflow=CaptureWorkflow(),
        github_client=FakeGitHubClient(),  # type: ignore[arg-type]
        intake=build_intake(settings),
        markdown_exporter=str,
        sarif_exporter=lambda _value: {},
    )

    assert service.ready() is True
    service.close()


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("succeeded", AnalyzerStatus.COMPLETED),
        ("not_applicable", AnalyzerStatus.NOT_APPLICABLE),
        ("unavailable", AnalyzerStatus.UNAVAILABLE),
        ("timeout", AnalyzerStatus.TIMED_OUT),
        ("degraded", AnalyzerStatus.DEGRADED),
        ("unknown", AnalyzerStatus.FAILED),
    ],
)
def test_workflow_result_normalizes_untrusted_adapter_values(
    raw_status: str,
    expected: AnalyzerStatus,
) -> None:
    run = _analyzer_run(
        {
            "name": "adapter",
            "status": raw_status,
            "started_at": "2026-07-18T10:00:00Z",
            "finished_at": "not-a-date",
            "duration_ms": -1,
            "findings": [{}, {}],
            "detail": "bounded",
        }
    )

    assert run is not None
    assert run.status is expected
    assert run.started_at == datetime(2026, 7, 18, 10, tzinfo=UTC)
    assert run.finished_at is None
    assert run.duration_ms == 0
    assert run.findings_count == 2


def test_result_conversion_defaults_invalid_findings_and_attaches_refactor() -> None:
    result = _result_from_state(
        {
            "findings": [
                "not-a-mapping",
                {
                    "fingerprint": "workflow-id",
                    "path": "demo.py",
                    "rule_id": "PS001",
                    "start_line": True,
                    "end_line": 0,
                    "start_column": 3,
                    "end_column": 0,
                    "description": "Evidence-backed finding",
                    "category": "unknown",
                    "severity": "unknown",
                },
            ],
            "refactors": [
                {"finding_fingerprint": "workflow-id", "diff": "--- a/demo.py\n+++ b/demo.py"},
                "not-a-mapping",
            ],
            "analyzer_runs": ["not-a-mapping"],
            "ai_metadata": "not-a-mapping",
            "warnings": ["offline"],
            "stage_trace": ["score"],
        }
    )

    finding = result.findings[0]
    assert finding.start_line == 1
    assert finding.end_line == 1
    assert finding.start_column == 3
    assert finding.end_column is None
    assert finding.category.value == "maintainability"
    assert finding.severity.value == "info"
    assert finding.refactor_diff == "--- a/demo.py\n+++ b/demo.py"
    assert result.ai_metadata.fallback_reason is None
    assert result.ai_metadata.warnings == ("offline",)


def test_result_conversion_keeps_truncation_separate_from_provider_fallback() -> None:
    result = _result_from_state(
        {
            "findings": [],
            "analyzer_runs": [],
            "stage_trace": ["synthesize"],
            "warnings": [
                "Provider prompt was truncated; complete local static findings were preserved.",
                "AI synthesis was unavailable (TimeoutError); deterministic findings were "
                "preserved.",
            ],
            "ai_metadata": {
                "mode": "offline_fallback",
                "provider": "openai",
                "model": "gpt-5-mini",
                "summary": "Static findings remain authoritative.",
                "finding_count": 4,
                "accepted_model_findings": 0,
                "source_execution": False,
                "fallback_reason": "AI synthesis was unavailable (TimeoutError).",
                "provider_error_type": "TimeoutError",
                "completion_token_limit": 4_096,
                "prompt_char_limit": 4_000,
                "prompt_chars": 4_000,
                "prompt_truncated": True,
                "prompt_sections": {
                    "metadata": {
                        "original_chars": 8_000,
                        "included_chars": 200,
                        "prompt_chars": 215,
                        "truncated": True,
                    },
                    "sources": {
                        "original_chars": 20_000,
                        "included_chars": 2_000,
                        "prompt_chars": 2_015,
                        "truncated": True,
                    },
                },
            },
        }
    )

    metadata = result.ai_metadata
    assert metadata.mode == "offline_fallback"
    assert metadata.provider == "openai"
    assert metadata.model == "gpt-5-mini"
    assert metadata.finding_count == 4
    assert metadata.provider_error_type == "TimeoutError"
    assert metadata.completion_token_limit == 4_096
    assert metadata.fallback_reason == "AI synthesis was unavailable (TimeoutError)."
    assert metadata.prompt_truncated is True
    assert metadata.prompt_sections["sources"].included_chars == 2_000
    assert metadata.warnings[0].startswith("Provider prompt was truncated")


def test_review_service_persists_successful_provider_metadata(tmp_path: Path) -> None:
    class ProviderMetadataWorkflow(CaptureWorkflow):
        def invoke(self, *, files: list[object], metadata: dict[str, object]) -> dict[str, object]:
            self.calls.append({"files": files, "metadata": metadata})
            return {
                "findings": [],
                "analyzer_runs": [],
                "stage_trace": ["parse", "analyze", "synthesize", "refactor", "score"],
                "warnings": ["Provider prompt was truncated."],
                "ai_metadata": {
                    "mode": "openai",
                    "provider": "openai",
                    "model": "gpt-5-mini",
                    "summary": "No additional issues.",
                    "finding_count": 0,
                    "accepted_model_findings": 0,
                    "completion_token_limit": 4_096,
                    "prompt_char_limit": 4_000,
                    "prompt_chars": 3_999,
                    "prompt_truncated": True,
                    "prompt_sections": {
                        "sources": {
                            "original_chars": 9_000,
                            "included_chars": 2_000,
                            "prompt_chars": 2_015,
                            "truncated": True,
                        }
                    },
                },
            }

    service = _make_service(tmp_path, workflow=ProviderMetadataWorkflow())
    try:
        review = service.review_text(filename="safe.py", content="pass\n")

        assert review.ai_metadata.mode == "openai"
        assert review.ai_metadata.summary == "No additional issues."
        assert review.ai_metadata.completion_token_limit == 4_096
        assert review.ai_metadata.prompt_chars == 3_999
        assert review.ai_metadata.prompt_truncated is True
        assert review.ai_metadata.fallback_reason is None
        assert review.ai_metadata.warnings == ("Provider prompt was truncated.",)
    finally:
        service.close()


@dataclass
class PublicValue:
    name: str


def test_dump_public_serializes_domain_models_and_dataclasses(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        review = service.review_text(filename="safe.py", content="pass\n")
        assert dump_public(review)["id"] == review.id
        assert dump_public(PublicValue(name="value")) == {"name": "value"}
        assert dump_public("plain") == "plain"
    finally:
        service.close()
