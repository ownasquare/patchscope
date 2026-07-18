from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_container_runs_as_non_root() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "USER 10001:10001" in dockerfile
    assert "USER root" not in dockerfile


def test_compose_has_hardened_service_contracts() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert "read_only: true" in compose
    assert compose.count('cap_drop: ["ALL"]') == 2
    assert compose.count('security_opt: ["no-new-privileges:true"]') == 2
    assert "docker.sock" not in compose
    assert "patchscope-data:/data" in compose
    assert compose.count("healthcheck:") == 2


def test_compose_publishes_only_on_host_loopback() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    assert '"127.0.0.1:8787:8787"' in compose
    assert '"127.0.0.1:8501:8501"' in compose


def test_compose_does_not_claim_a_hosted_environment() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert "production" not in compose.casefold()
    assert "deploy:" not in compose


def test_compose_forwards_documented_review_resource_controls() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    for setting in (
        "PATCHSCOPE_OPENAI_MODEL",
        "PATCHSCOPE_MAX_FILE_BYTES",
        "PATCHSCOPE_MAX_REVIEW_BYTES",
        "PATCHSCOPE_MAX_FILES",
        "PATCHSCOPE_ANALYZER_TIMEOUT_SECONDS",
        "PATCHSCOPE_GITHUB_TIMEOUT_SECONDS",
    ):
        assert setting in compose

    assert "PATCHSCOPE_API_HOST" not in compose
    assert "PATCHSCOPE_LOG_LEVEL" not in compose


def test_ci_starts_and_health_checks_the_built_compose_stack() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "docker compose up --detach --wait" in workflow
    assert "http://127.0.0.1:8787/ready" in workflow
    assert "http://127.0.0.1:8501/_stcore/health" in workflow
    assert "docker compose down --volumes" in workflow
    assert "- run: uv lock\n" not in workflow
