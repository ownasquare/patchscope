from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from patchscope.api.app import create_app
from patchscope.config import Settings
from patchscope.database import create_database
from patchscope.exports import export_markdown, export_sarif
from patchscope.github import GitHubPullRequest, GitHubPullRequestRef, GitHubSource
from patchscope.repository import ReviewRepository
from patchscope.service import ReviewService, build_intake


class FakeWorkflow:
    def invoke(self, *, files: list[object], metadata: dict[str, object]) -> dict[str, object]:
        source = files[0]
        path = str(source.path)
        content = str(source.content)
        evidence = "eval(value)" if "eval(value)" in content else content.splitlines()[0]
        return {
            "findings": [
                {
                    "fingerprint": "workflow-finding-1",
                    "path": path,
                    "rule_id": "PS001",
                    "start_line": 1,
                    "end_line": 1,
                    "title": "Unsafe dynamic evaluation",
                    "message": "Dynamic evaluation can execute untrusted input.",
                    "category": "security",
                    "severity": "high",
                    "analyzer": "patchscope-heuristics",
                    "evidence": evidence,
                    "suggestion": "Use a typed parser.",
                }
            ],
            "analyzer_runs": [
                {
                    "analyzer": "patchscope-heuristics",
                    "status": "succeeded",
                    "duration_ms": 2,
                    "message": "Completed without source execution.",
                    "findings": [{}],
                },
                {
                    "analyzer": "semgrep",
                    "status": "unavailable",
                    "duration_ms": 0,
                    "message": "Semgrep is not installed.",
                    "findings": [],
                },
            ],
            "refactors": [
                {
                    "finding_fingerprint": "workflow-finding-1",
                    "diff": f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-eval(value)\n+parse(value)",
                }
            ],
            "summary": {},
            "ai_metadata": {"mode": "offline"},
            "stage_trace": ["parse", "analyze", "synthesize", "refactor", "score"],
            "warnings": [],
            "metadata": metadata,
        }


class FakeGitHubClient:
    async def fetch_pull_request(self, value: str) -> GitHubPullRequest:
        ref = GitHubPullRequestRef(owner="acme", repository="shop", number=7)
        return GitHubPullRequest(
            ref=ref,
            title="Harden checkout",
            author="sam",
            head_sha="a" * 40,
            base_branch="main",
            head_branch="secure-checkout",
            files=(
                GitHubSource(
                    path="checkout.py",
                    content="eval(value)\n",
                    status="modified",
                    additions=1,
                    deletions=1,
                ),
            ),
        )


@pytest.fixture
def api_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        data_dir=tmp_path,
        ai_mode="offline",
        allowed_origins=("http://127.0.0.1:8501",),
        _env_file=None,
    )
    database = create_database(settings.database_url)
    service = ReviewService(
        settings=settings,
        repository=ReviewRepository(database),
        workflow=FakeWorkflow(),
        github_client=FakeGitHubClient(),  # type: ignore[arg-type]
        intake=build_intake(settings),
        markdown_exporter=export_markdown,
        sarif_exporter=export_sarif,
    )
    with TestClient(create_app(settings=settings, service=service)) as client:
        yield client
