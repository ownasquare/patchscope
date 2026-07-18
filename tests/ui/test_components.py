"""Pure presentation contract tests for review filtering and source checks."""

from __future__ import annotations

import tomllib
from pathlib import Path

from patchscope.ui.components import (
    STATUS_COLORS,
    filter_findings,
    github_pr_identity,
    language_for_filename,
    refactor_diff,
    review_name,
    text_preflight,
    upload_preflight,
)
from patchscope.ui.theme import APP_STYLES


def test_review_name_and_refactor_support_the_domain_response_shape() -> None:
    review = {
        "request": {
            "title": "Checkout review",
            "files": [{"path": "checkout.py"}],
        }
    }
    finding = {"refactor_diff": "--- a/checkout.py\n+++ b/checkout.py"}

    assert review_name(review) == "Checkout review"
    assert refactor_diff(finding) == "--- a/checkout.py\n+++ b/checkout.py"


def test_finding_filters_include_domain_triage_and_source_fields() -> None:
    findings = [
        {
            "title": "Unsafe eval",
            "path": "checkout.py",
            "rule_id": "PS001",
            "severity": "critical",
            "category": "security",
            "triage": "open",
        },
        {
            "title": "Missing annotation",
            "path": "checkout.py",
            "rule_id": "PS210",
            "severity": "medium",
            "category": "readability",
            "triage": "ignored",
        },
    ]

    matched = filter_findings(
        findings,
        query="PS001",
        severities=["critical"],
        categories=["security"],
        statuses=["open"],
    )

    assert [finding["title"] for finding in matched] == ["Unsafe eval"]

    ignored = filter_findings(findings, statuses=["ignored"])
    assert [finding["title"] for finding in ignored] == ["Missing annotation"]
    assert STATUS_COLORS["acknowledged"] == "green"
    assert STATUS_COLORS["ignored"] == "gray"


def test_source_preflight_is_bounded_and_github_identity_is_canonical() -> None:
    preflight = text_preflight(
        name="Authentication review",
        filename="auth.py",
        content="def authenticate():\n    return True\n",
    )

    assert preflight["language"] == "Python"
    assert language_for_filename("queries/report.sql") == "SQL"
    assert language_for_filename("Dockerfile.worker") == "Dockerfile"
    assert language_for_filename("change.patch") == "Diff"
    assert preflight["line_count"] == 2
    assert github_pr_identity("https://github.com/acme/widgets/pull/42") == (
        "acme",
        "widgets",
        42,
    )
    assert github_pr_identity("https://user@github.com/acme/widgets/pull/42") is None
    assert github_pr_identity("https://www.github.com/acme/widgets/pull/42") is None


def test_zip_preflight_defers_archive_contents_to_the_bounded_service() -> None:
    preflight = upload_preflight(
        name="Repository snapshot",
        filename="checkout.zip",
        content=b"PK\x03\x04bounded archive bytes",
        content_type="application/zip",
    )

    assert preflight["file_count"] == "From archive"
    assert preflight["line_count"] == "From archive"
    assert preflight["language"] == "Detected per file"


def test_theme_contract_supports_focus_responsive_reflow_and_both_native_themes() -> None:
    assert "focus-visible" in APP_STYLES
    assert "prefers-reduced-motion" in APP_STYLES
    assert "max-width: 48rem" in APP_STYLES
    assert "gradient" not in APP_STYLES.casefold()

    config_path = Path(__file__).parents[2] / ".streamlit" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["theme"]["light"]["backgroundColor"] == "#F4F6F7"
    assert config["theme"]["dark"]["backgroundColor"] == "#111719"
    assert config["server"]["enableXsrfProtection"] is True
    assert config["client"]["showErrorDetails"] == "none"
