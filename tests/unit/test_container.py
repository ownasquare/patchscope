from pathlib import Path

import pytest
from pydantic import SecretStr

from patchscope.agent.model import EvidenceSynthesizer
from patchscope.analyzers import AnalyzerRunner
from patchscope.config import Settings
from patchscope.container import build_service
from patchscope.github import GitHubClient
from patchscope.service import ReviewService


@pytest.mark.parametrize(
    ("openai_key", "github_token", "expected_secret", "authorization_configured"),
    [
        (SecretStr("   "), SecretStr(" "), None, False),
        (SecretStr("provider-key"), SecretStr("github-token"), "provider-key", True),
    ],
)
def test_build_service_composes_dependencies_and_normalizes_blank_secrets(
    tmp_path: Path,
    openai_key: SecretStr,
    github_token: SecretStr,
    expected_secret: str | None,
    authorization_configured: bool,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ai_mode="auto",
        openai_api_key=openai_key,
        github_token=github_token,
        analyzer_timeout_seconds=1,
        github_timeout_seconds=1,
        _env_file=None,
    )

    service = build_service(settings)
    try:
        assert isinstance(service, ReviewService)
        assert isinstance(service.workflow.dependencies.analyzer_runner, AnalyzerRunner)
        synthesizer = service.workflow.dependencies.synthesizer
        assert isinstance(synthesizer, EvidenceSynthesizer)
        assert synthesizer.api_key == expected_secret
        assert isinstance(service.github_client, GitHubClient)
        assert ("Authorization" in service.github_client.headers) is authorization_configured
        assert service.ready() is True
        assert service.intake.limits.max_files == settings.max_files
        assert service.intake.limits.max_total_bytes == settings.max_review_bytes
    finally:
        service.close()

    assert not (tmp_path / "patchscope.db-wal").exists()
