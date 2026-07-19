from __future__ import annotations

import json

import httpx
import pytest

from patchscope.errors import GitHubRateLimitError, IntakeError
from patchscope.github import GitHubClient, changed_line_ranges, parse_pull_request_url


def test_parse_pull_request_url_accepts_canonical_url() -> None:
    ref = parse_pull_request_url("https://github.com/acme/widgets/pull/42/")
    assert (ref.owner, ref.repository, ref.number) == ("acme", "widgets", 42)
    assert ref.canonical_url == "https://github.com/acme/widgets/pull/42"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets/pull/42",
        "https://example.test/acme/widgets/pull/42",
        "https://github.com@127.0.0.1/acme/widgets/pull/42",
        "https://github.com/acme/widgets/issues/42",
        "https://github.com/acme/widgets/pull/not-a-number",
        "https://github.com/acme/widgets/pull/42?redirect=http://127.0.0.1",
    ],
)
def test_parse_pull_request_url_rejects_noncanonical_input(url: str) -> None:
    with pytest.raises(IntakeError):
        parse_pull_request_url(url)


def test_changed_line_ranges_map_added_diff_lines_to_target_file() -> None:
    patch = "@@ -8,3 +8,4 @@\n context\n-old\n+new\n+more\n context\n@@ -19,0 +21,1 @@\n+tail\n"

    assert changed_line_ranges(patch) == ((9, 10), (21, 21))


@pytest.mark.asyncio
async def test_fetch_pull_request_hydrates_supported_sources() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(
                200,
                json={
                    "title": "Safer checkout",
                    "user": {"login": "sam"},
                    "head": {"sha": "a" * 40, "ref": "secure-checkout"},
                    "base": {
                        "ref": "main",
                        "repo": {"private": False, "visibility": "public"},
                    },
                },
            )
        if request.url.path.endswith("/pulls/7/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/checkout.py",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-eval(value)\n+parse(value)",
                    },
                    {
                        "filename": "queries/report.sql",
                        "status": "added",
                        "additions": 1,
                        "deletions": 0,
                        "patch": "@@ -0,0 +1 @@\n+SELECT id FROM reports;",
                    },
                    {
                        "filename": "credentials.json",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    },
                    {"filename": "assets/logo.png", "status": "modified"},
                ],
            )
        if request.url.path.endswith("/contents/src/checkout.py"):
            return httpx.Response(200, content=b"def checkout(value):\n    return eval(value)\n")
        if request.url.path.endswith("/contents/queries/report.sql"):
            return httpx.Response(200, content=b"SELECT id FROM reports;\n")
        raise AssertionError(request.url)

    client = GitHubClient(token="test-token", transport=httpx.MockTransport(handler))
    result = await client.fetch_pull_request("https://github.com/acme/shop/pull/7")

    assert result.title == "Safer checkout"
    assert result.author == "sam"
    assert [source.path for source in result.files] == [
        "src/checkout.py",
        "queries/report.sql",
    ]
    assert all(source.is_patch is False for source in result.files)
    assert result.skipped_files == ("credentials.json", "assets/logo.png")
    assert all(request.headers.get("authorization") == "Bearer test-token" for request in calls)


@pytest.mark.asyncio
async def test_fetch_pull_request_uses_patch_when_content_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/3"):
            return httpx.Response(
                200,
                json={
                    "title": "Delete old code",
                    "user": {"login": "sam"},
                    "head": {"sha": "b" * 40, "ref": "cleanup"},
                    "base": {
                        "ref": "main",
                        "repo": {"private": False, "visibility": "public"},
                    },
                },
            )
        if request.url.path.endswith("/pulls/3/files"):
            return httpx.Response(
                200,
                content=json.dumps(
                    [
                        {
                            "filename": "legacy.py",
                            "status": "removed",
                            "additions": 0,
                            "deletions": 1,
                            "patch": "@@ -1 +0,0 @@\n-print('old')",
                        }
                    ]
                ).encode(),
            )
        raise AssertionError(request.url)

    result = await GitHubClient(transport=httpx.MockTransport(handler)).fetch_pull_request(
        "https://github.com/acme/shop/pull/3"
    )
    assert result.files[0].is_patch is True


@pytest.mark.asyncio
async def test_fetch_pull_request_reports_rate_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    with pytest.raises(GitHubRateLimitError):
        await GitHubClient(transport=httpx.MockTransport(handler)).fetch_pull_request(
            "https://github.com/acme/shop/pull/3"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base",
    [
        None,
        {"ref": "main", "repo": {"private": True, "visibility": "private"}},
        {"ref": "main"},
        {"ref": "main", "repo": []},
        {"ref": "main", "repo": {}},
        {"ref": "main", "repo": {"private": "false"}},
        {"ref": "main", "repo": {"private": 0}},
        {"ref": "main", "repo": {"private": False, "visibility": "internal"}},
        {"ref": "main", "repo": {"private": False, "visibility": 0}},
    ],
)
async def test_fetch_pull_request_rejects_nonpublic_or_ambiguous_repository(
    base: object,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/pulls/11"):
            return httpx.Response(
                200,
                json={
                    "title": "Hidden change",
                    "head": {"sha": "d" * 40, "ref": "hidden"},
                    "base": base,
                },
            )
        raise AssertionError(f"Unexpected request after metadata: {request.url.path}")

    with pytest.raises(IntakeError, match="public GitHub repositories"):
        await GitHubClient(transport=httpx.MockTransport(handler)).fetch_pull_request(
            "https://github.com/acme/shop/pull/11"
        )

    assert len(calls) == 1
    assert calls[0].url.path.endswith("/pulls/11")


@pytest.mark.asyncio
async def test_file_pagination_fetches_a_sentinel_page_at_the_exact_limit() -> None:
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/9"):
            return httpx.Response(
                200,
                json={
                    "title": "Large change",
                    "head": {"sha": "c" * 40, "ref": "large"},
                    "base": {
                        "ref": "main",
                        "repo": {"private": False, "visibility": "public"},
                    },
                },
            )
        if request.url.path.endswith("/pulls/9/files"):
            page = int(request.url.params["page"])
            requested_pages.append(page)
            count = 100 if page == 1 else 1
            return httpx.Response(
                200,
                json=[{"filename": f"src/file-{page}-{index}.py"} for index in range(count)],
            )
        raise AssertionError(request.url)

    with pytest.raises(IntakeError, match="100-file review limit"):
        await GitHubClient(transport=httpx.MockTransport(handler)).fetch_pull_request(
            "https://github.com/acme/shop/pull/9"
        )

    assert requested_pages == [1, 2]
