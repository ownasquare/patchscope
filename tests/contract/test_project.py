from __future__ import annotations

import tomllib
from pathlib import Path

from patchscope.api.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_public_project_files_are_present() -> None:
    required = {
        "README.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        "compose.yaml",
        "docs/adoption/2026-07-18-open-source-readiness.md",
        "docs/architecture.md",
        "docs/api.md",
        "docs/assets/patchscope-workbench.svg",
        "docs/extending.md",
        "docs/security.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/question.yml",
        ".github/pull_request_template.md",
    }
    assert not [path for path in sorted(required) if not (ROOT / path).is_file()]


def test_release_artifacts_preserve_streamlit_and_docker_context() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]
    sdist_includes = set(sdist["include"])

    assert wheel["force-include"][".streamlit/config.toml"] == ("patchscope/.streamlit/config.toml")
    assert {"/.streamlit", "/.dockerignore", "/uv.lock"} <= sdist_includes
    assert "/docs" not in sdist_includes
    assert {
        "/CODE_OF_CONDUCT.md",
        "/SUPPORT.md",
        "/docs/adoption",
        "/docs/api.md",
        "/docs/architecture.md",
        "/docs/assets",
        "/docs/extending.md",
        "/docs/security.md",
    } <= sdist_includes

    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY --chown=patchscope:patchscope .streamlit ./.streamlit" in dockerfile


def test_package_metadata_points_to_the_public_project() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["project"]["urls"] == {
        "Homepage": "https://github.com/ownasquare/patchscope",
        "Repository": "https://github.com/ownasquare/patchscope",
        "Issues": "https://github.com/ownasquare/patchscope/issues",
    }


def test_secret_and_runtime_data_are_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text()
    for value in (
        ".env",
        ".data",
        "*.db",
        ".venv",
        "docs/handoffs/",
        "docs/superpowers/",
    ):
        assert value in gitignore
    assert not (ROOT / ".env").exists()


def test_environment_example_contains_only_consumed_server_settings() -> None:
    example = (ROOT / ".env.example").read_text()

    for unused_launcher_setting in (
        "PATCHSCOPE_API_BASE_URL",
        "PATCHSCOPE_API_HOST",
        "PATCHSCOPE_API_PORT",
        "PATCHSCOPE_UI_PORT",
        "PATCHSCOPE_LOG_LEVEL",
    ):
        assert unused_launcher_setting not in example


def test_openapi_contract_exposes_complete_review_flow() -> None:
    openapi = create_app().openapi()
    paths = openapi["paths"]
    expected = {
        "/health",
        "/ready",
        "/api/v1/capabilities",
        "/api/v1/reviews/text",
        "/api/v1/reviews/file",
        "/api/v1/reviews/github",
        "/api/v1/reviews",
        "/api/v1/reviews/{review_id}",
        "/api/v1/reviews/{review_id}/findings/{fingerprint}",
        "/api/v1/reviews/{review_id}/exports/{export_format}",
    }
    assert expected <= set(paths)
    assert paths["/api/v1/reviews/text"]["post"]["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/ReviewDetail")
    assert paths["/api/v1/reviews"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/ReviewPage")


def test_global_react_e2e_policy_is_not_violated() -> None:
    assert not list(ROOT.rglob("cypress.config.*"))
    assert not list((ROOT / "cypress").glob("e2e/**/*")) if (ROOT / "cypress").exists() else True


def test_default_pytest_run_excludes_live_and_e2e_suites() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    addopts = project["tool"]["pytest"]["ini_options"]["addopts"]

    assert "not e2e" in addopts
    assert "not live" in addopts


def test_make_verify_includes_lock_build_and_isolated_wheel_smoke() -> None:
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "verify: lint type test audit lock build package-smoke" in makefile
    assert "uv lock --check" in makefile
    assert "uv run --isolated --no-project --refresh-package patchscope --with" in makefile
    assert "patchscope start --help" in makefile
    assert "- run: make package-smoke" in workflow


def test_ci_uses_current_node24_action_releases() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert workflow.count("actions/checkout@v7.0.0") == 3
    assert workflow.count("astral-sh/setup-uv@v8.3.2") == 3
