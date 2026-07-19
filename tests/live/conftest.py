from __future__ import annotations

import pytest

DEFAULT_LIVE_GITHUB_PR_URL = "https://github.com/ownasquare/evalforge/pull/1"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-github-pr-url",
        action="store",
        default=DEFAULT_LIVE_GITHUB_PR_URL,
        help="Controlled public pull request used by the authenticated GitHub live test.",
    )


@pytest.fixture
def live_github_pr_url(request: pytest.FixtureRequest) -> str:
    return str(request.config.getoption("--live-github-pr-url"))
