"""Dependency composition for the default local PatchScope service."""

from __future__ import annotations

from patchscope.agent.model import EvidenceSynthesizer
from patchscope.agent.workflow import ReviewWorkflow, WorkflowDependencies
from patchscope.analyzers import (
    AnalyzerRunner,
    HeuristicAnalyzer,
    MypyAnalyzer,
    RuffAnalyzer,
    SemgrepAnalyzer,
)
from patchscope.config import Settings
from patchscope.database import create_database
from patchscope.github import GitHubClient
from patchscope.parsing import TreeSitterParser
from patchscope.repository import ReviewRepository
from patchscope.service import ReviewService, build_intake


def build_service(settings: Settings) -> ReviewService:
    """Build one local service with explicit dependencies and no hidden network calls."""

    from patchscope.exports import export_markdown, export_sarif
    from patchscope.refactor import RefactorEngine

    database = create_database(settings.database_url)
    repository = ReviewRepository(database)
    analyzer_runner = AnalyzerRunner(
        [
            HeuristicAnalyzer(),
            RuffAnalyzer(timeout_seconds=settings.analyzer_timeout_seconds),
            MypyAnalyzer(timeout_seconds=settings.analyzer_timeout_seconds),
            SemgrepAnalyzer(timeout_seconds=settings.analyzer_timeout_seconds),
        ]
    )
    raw_secret = (
        settings.openai_api_key.get_secret_value().strip()
        if settings.openai_api_key is not None
        else ""
    )
    secret = raw_secret or None
    workflow = ReviewWorkflow(
        WorkflowDependencies(
            parser=TreeSitterParser(),
            analyzer_runner=analyzer_runner,
            refactor_engine=RefactorEngine(),
            synthesizer=EvidenceSynthesizer(
                mode=settings.ai_mode,
                model_name=settings.openai_model,
                api_key=secret,
            ),
        )
    )
    raw_github_token = (
        settings.github_token.get_secret_value().strip()
        if settings.github_token is not None
        else ""
    )
    github_token = raw_github_token or None
    github_client = GitHubClient(
        token=github_token,
        timeout_seconds=settings.github_timeout_seconds,
        max_files=settings.max_files,
        max_total_bytes=settings.max_review_bytes,
        max_file_bytes=settings.max_file_bytes,
    )
    return ReviewService(
        settings=settings,
        repository=repository,
        workflow=workflow,
        github_client=github_client,
        intake=build_intake(settings),
        markdown_exporter=export_markdown,
        sarif_exporter=export_sarif,
    )


__all__ = ["build_service"]
