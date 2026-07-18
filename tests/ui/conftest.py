"""Deterministic API fake and Streamlit AppTest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from patchscope.ui.client import ApiError, ExportArtifact

APP_PATH = Path(__file__).resolve().parents[2] / "src" / "patchscope" / "streamlit_app.py"


def sample_review() -> dict[str, Any]:
    return {
        "id": "rev_demo",
        "status": "completed",
        "request": {
            "source_kind": "text",
            "title": "Checkout boundary review",
            "source_reference": "checkout.py",
            "files": [
                {
                    "path": "checkout.py",
                    "language": "python",
                    "content": "def checkout(user_input):\n    return eval(user_input)\n",
                }
            ],
        },
        "summary": {
            "total_findings": 2,
            "open_findings": 2,
            "risk_score": 48,
            "recommendation": "request_changes",
            "by_severity": {"critical": 1, "medium": 1},
            "by_category": {"security": 1, "readability": 1},
        },
        "findings": [
            {
                "fingerprint": "finding-security",
                "path": "checkout.py",
                "start_line": 2,
                "end_line": 2,
                "title": "Unsafe dynamic execution",
                "message": "Untrusted input reaches eval().",
                "category": "security",
                "severity": "critical",
                "analyzer": "patchscope-heuristics",
                "rule_id": "PS001",
                "evidence": "return eval(user_input)",
                "suggestion": "Replace dynamic evaluation with an explicit parser.",
                "refactor_diff": (
                    "--- a/checkout.py\n"
                    "+++ b/checkout.py\n"
                    "@@ -1,2 +1,2 @@\n"
                    "-    return eval(user_input)\n"
                    "+    return parse_checkout(user_input)\n"
                ),
                "triage": "open",
            },
            {
                "fingerprint": "finding-name",
                "path": "checkout.py",
                "start_line": 1,
                "end_line": 1,
                "title": "Add a return type",
                "message": "The public function has no return annotation.",
                "category": "readability",
                "severity": "medium",
                "analyzer": "mypy",
                "rule_id": "PS210",
                "evidence": "def checkout(user_input):",
                "suggestion": "Add the narrowest accurate return annotation.",
                "refactor_diff": None,
                "triage": "open",
            },
        ],
        "analyzer_runs": [
            {"analyzer": "ruff", "status": "completed", "findings_count": 0},
            {"analyzer": "mypy", "status": "completed", "findings_count": 1},
            {"analyzer": "semgrep", "status": "unavailable", "findings_count": 0},
        ],
        "stage_trace": ["parse", "analyze", "synthesize", "refactor", "score"],
        "ai_metadata": {"mode": "offline", "provider": None, "model": None},
        "created_at": "2026-07-18T12:00:00Z",
        "updated_at": "2026-07-18T12:00:01Z",
    }


class FakeUiClient:
    def __init__(self, review: dict[str, Any] | None = None) -> None:
        self.review = deepcopy(review or sample_review())
        self.text_calls: list[dict[str, Any]] = []
        self.file_calls: list[dict[str, Any]] = []
        self.github_calls: list[dict[str, Any]] = []
        self.triage_calls: list[dict[str, Any]] = []
        self.list_error: ApiError | None = None
        self.health_error: ApiError | None = None

    def health(self) -> dict[str, Any]:
        if self.health_error is not None:
            raise self.health_error
        return {"status": "ready"}

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": "0.1.0",
            "ai_mode": "offline",
            "source_execution": False,
            "analyzers": [
                {"name": "ruff", "status": "available", "detail": "Ready"},
                {"name": "semgrep", "status": "unavailable", "detail": "Optional"},
            ],
        }

    def list_reviews(self, *, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        del limit, status
        if self.list_error is not None:
            raise self.list_error
        item = {
            "id": self.review["id"],
            "title": self.review["request"]["title"],
            "source_kind": self.review["request"]["source_kind"],
            "status": self.review["status"],
            "summary": deepcopy(self.review["summary"]),
            "created_at": self.review["created_at"],
            "updated_at": self.review["updated_at"],
        }
        return [item]

    def get_review(self, review_id: str) -> dict[str, Any]:
        if review_id != self.review["id"]:
            raise ApiError("Review not found", status_code=404)
        return deepcopy(self.review)

    def create_text_review(
        self, *, filename: str, content: str, name: str | None = None
    ) -> dict[str, Any]:
        self.text_calls.append({"filename": filename, "content": content, "name": name})
        self.review["request"]["title"] = name or filename
        self.review["request"]["files"][0]["path"] = filename
        self.review["request"]["files"][0]["content"] = content
        return deepcopy(self.review)

    def create_file_review(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        self.file_calls.append(
            {
                "filename": filename,
                "content": content,
                "content_type": content_type,
                "name": name,
            }
        )
        return deepcopy(self.review)

    def create_github_review(self, *, url: str, name: str | None = None) -> dict[str, Any]:
        self.github_calls.append({"url": url, "name": name})
        return deepcopy(self.review)

    def update_finding(
        self,
        *,
        review_id: str,
        fingerprint: str,
        status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        self.triage_calls.append(
            {
                "review_id": review_id,
                "fingerprint": fingerprint,
                "status": status,
                "note": note,
            }
        )
        stored = {"accepted": "acknowledged", "dismissed": "ignored"}.get(status, status)
        for finding in self.review["findings"]:
            if finding["fingerprint"] == fingerprint:
                finding["triage"] = stored
                finding["triage_note"] = note
        return deepcopy(self.review)

    def download_export(self, review_id: str, export_format: str) -> ExportArtifact:
        suffix = "md" if export_format == "markdown" else "sarif.json"
        media_type = "text/markdown" if export_format == "markdown" else "application/sarif+json"
        return ExportArtifact(
            content=f"PatchScope export for {review_id}".encode(),
            filename=f"review.{suffix}",
            media_type=media_type,
        )


@pytest.fixture
def fake_client() -> FakeUiClient:
    return FakeUiClient()


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch) -> Callable[[FakeUiClient], AppTest]:
    def _run(fake: FakeUiClient) -> AppTest:
        import patchscope.ui.views as views

        views._cached_client.clear()
        monkeypatch.setattr(views, "client_from_environment", lambda: fake)
        return AppTest.from_file(str(APP_PATH), default_timeout=10).run()

    return _run


def visible_text(app: AppTest) -> str:
    collections = (
        app.title,
        app.header,
        app.subheader,
        app.markdown,
        app.caption,
        app.info,
        app.warning,
        app.error,
        app.success,
        app.code,
    )
    return "\n".join(str(element.value) for collection in collections for element in collection)


def find_button(app: AppTest, label: str) -> Any:
    return next(button for button in app.button if button.label == label)
