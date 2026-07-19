from __future__ import annotations

import re
from pathlib import PurePosixPath

import pytest
from pydantic import SecretStr

from patchscope.agent.model import EvidenceSynthesizer
from patchscope.config import Settings
from patchscope.github import GitHubClient, changed_line_ranges, parse_pull_request_url

pytestmark = pytest.mark.live

SAMPLE_PATH = "live_sample.py"
SAMPLE_SOURCE = "def run(user_code: str) -> object:\n    return eval(user_code)\n"
STATIC_SENTINEL = {
    "title": "Untrusted code reaches eval",
    "description": "The function evaluates caller-controlled source text.",
    "category": "security",
    "severity": "high",
    "path": SAMPLE_PATH,
    "start_line": 2,
    "end_line": 2,
    "evidence": "eval(user_code)",
    "suggestion": "Replace dynamic evaluation with an explicit allowlisted operation.",
    "confidence": 1.0,
    "rule_id": "LIVE-STATIC-SENTINEL",
    "fingerprint": "live-static-sentinel",
    "sources": ["live-sentinel"],
    "analyzer": "live-sentinel",
    "status": "open",
}


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None:
        pytest.fail(f"{name} is required for opted-in live acceptance")
    secret = value.get_secret_value().strip()
    if not secret:
        pytest.fail(f"{name} is required for opted-in live acceptance")
    return secret


def test_openai_synthesis_returns_evidence_bounded_structured_output() -> None:
    settings = Settings()
    api_key = _required_secret(settings.openai_api_key, "PATCHSCOPE_OPENAI_API_KEY")
    synthesizer = EvidenceSynthesizer(
        mode="openai",
        model_name=settings.openai_model,
        api_key=api_key,
        max_prompt_chars=4_096,
    )

    findings, metadata, warnings = synthesizer.synthesize(
        files=[{"path": SAMPLE_PATH, "content": SAMPLE_SOURCE}],
        parse_summaries=[{"path": SAMPLE_PATH, "language": "python", "parse_error": False}],
        analyzer_findings=[STATIC_SENTINEL],
        metadata={"source": "synthetic-live-acceptance", "source_execution": False},
    )

    assert warnings == []
    assert metadata["mode"] == "openai"
    assert metadata["provider"] == "openai"
    assert metadata["model"] == settings.openai_model
    assert metadata["source_execution"] is False
    assert isinstance(metadata.get("summary"), str) and metadata["summary"].strip()
    assert isinstance(metadata.get("accepted_model_findings"), int)
    assert metadata["accepted_model_findings"] >= 0
    assert metadata["finding_count"] == len(findings)
    assert any(item["rule_id"] == "LIVE-STATIC-SENTINEL" for item in findings)

    source_lines = SAMPLE_SOURCE.splitlines()
    for finding in findings:
        assert finding["path"] == SAMPLE_PATH
        start_line = int(finding["start_line"])
        end_line = int(finding["end_line"])
        assert 1 <= start_line <= end_line <= len(source_lines)
        local_source = "\n".join(source_lines[start_line - 1 : end_line])
        assert str(finding["evidence"]).strip() in local_source


@pytest.mark.asyncio
async def test_authenticated_github_intake_hydrates_bounded_public_source(
    live_github_pr_url: str,
) -> None:
    settings = Settings()
    token = _required_secret(settings.github_token, "PATCHSCOPE_GITHUB_TOKEN")
    expected_ref = parse_pull_request_url(live_github_pr_url)
    client = GitHubClient(
        token=token,
        timeout_seconds=10,
        max_files=10,
        max_total_bytes=250_000,
        max_file_bytes=100_000,
    )
    if "Authorization" not in client.headers:
        pytest.fail("The authenticated GitHub client did not configure a bearer header")

    pull_request = await client.fetch_pull_request(live_github_pr_url)

    assert pull_request.ref == expected_ref
    assert pull_request.title.strip()
    assert re.fullmatch(r"[a-fA-F0-9]{40,64}", pull_request.head_sha)
    assert 1 <= len(pull_request.files) <= 10
    assert sum(len(item.content.encode("utf-8")) for item in pull_request.files) <= 250_000
    assert any(
        item.status == "added" or bool(changed_line_ranges(item.patch))
        for item in pull_request.files
    )
    for item in pull_request.files:
        path = PurePosixPath(item.path)
        assert not path.is_absolute()
        assert ".." not in path.parts
        assert item.content
        assert len(item.content.encode("utf-8")) <= 100_000
