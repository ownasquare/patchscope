"""Focused contract tests for the workbench HTTP boundary."""

from __future__ import annotations

import json

import httpx
import pytest

from patchscope.ui.client import ApiError, PatchScopeClient, client_from_environment


def test_client_unwraps_review_history_and_posts_named_text_review() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{"id": "rev-1", "title": "Review"}]})
        return httpx.Response(201, json={"id": "rev-1", "status": "completed"})

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))

    assert client.list_reviews() == [{"id": "rev-1", "title": "Review"}]
    created = client.create_text_review(filename="demo.py", content="print('ok')", name="Demo")

    assert created["id"] == "rev-1"
    assert json.loads(requests[-1].content) == {
        "filename": "demo.py",
        "content": "print('ok')",
        "name": "Demo",
    }


def test_client_surfaces_sanitized_nested_api_problem() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "review_not_found", "message": "Review not found"}},
        )

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError, match="Review not found") as captured:
        client.get_review("missing")

    assert captured.value.status_code == 404
    assert captured.value.code == "review_not_found"
    assert captured.value.retryable is False


def test_client_returns_download_metadata_without_interpreting_export() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/exports/sarif")
        return httpx.Response(
            200,
            content=b'{"version":"2.1.0"}',
            headers={
                "content-type": "application/sarif+json; charset=utf-8",
                "content-disposition": 'attachment; filename="review.sarif.json"',
            },
        )

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))
    artifact = client.download_export("rev-1", "sarif")

    assert artifact.content == b'{"version":"2.1.0"}'
    assert artifact.filename == "review.sarif.json"
    assert artifact.media_type == "application/sarif+json"


def test_client_covers_all_review_mutations_and_escapes_path_segments() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/v1/capabilities":
            return httpx.Response(200, json={"source_execution": False})
        return httpx.Response(200, json={"id": "rev-1"})

    client = PatchScopeClient("http://testserver/", transport=httpx.MockTransport(handler))

    assert client.base_url == "http://testserver"
    assert client.health() == {"status": "ok"}
    assert client.capabilities() == {"source_execution": False}
    client.create_file_review(
        filename="demo.py",
        content=b"pass\n",
        content_type="",
    )
    client.create_github_review(url="https://github.com/acme/shop/pull/1", name="PR")
    client.update_finding(
        review_id="rev/1",
        fingerprint="finding/1",
        status="fixed",
        note="Covered",
    )

    assert requests[2].headers["content-type"].startswith("multipart/form-data")
    assert b"text/plain" in requests[2].content
    assert json.loads(requests[3].content) == {
        "url": "https://github.com/acme/shop/pull/1",
        "name": "PR",
    }
    assert requests[4].url.raw_path.decode() == ("/api/v1/reviews/rev%2F1/findings/finding%2F1")
    assert json.loads(requests[4].content) == {"status": "fixed", "note": "Covered"}


@pytest.mark.parametrize(
    ("limit", "status", "expected_query"),
    [
        (0, None, "limit=1"),
        (500, "all", "limit=100"),
        (25, "failed", "limit=25&status=failed"),
    ],
)
def test_review_list_clamps_limit_and_applies_optional_status(
    limit: int,
    status: str | None,
    expected_query: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.query.decode() == expected_query
        return httpx.Response(200, json={"reviews": [{"id": "rev-1"}, "invalid"]})

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))

    assert client.list_reviews(limit=limit, status=status) == [{"id": "rev-1"}]


def test_review_list_returns_empty_for_invalid_items_shape() -> None:
    client = PatchScopeClient(
        "http://testserver",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"items": {"id": "not-a-list"}})
        ),
    )

    assert client.list_reviews() == []


@pytest.mark.parametrize(
    ("response", "message", "status_code", "code", "retryable"),
    [
        (
            httpx.Response(422, json={"detail": {"message": "Bad source", "code": "bad"}}),
            "Bad source",
            422,
            "bad",
            False,
        ),
        (
            httpx.Response(409, json={"message": "Still running", "code": "conflict"}),
            "Still running",
            409,
            "conflict",
            True,
        ),
        (
            httpx.Response(503, text="upstream HTML"),
            "PatchScope encountered a temporary service error.",
            503,
            None,
            True,
        ),
        (
            httpx.Response(400, json=["unexpected"]),
            "The request could not be completed.",
            400,
            None,
            False,
        ),
    ],
)
def test_client_error_contract_is_sanitized_and_classifies_retryability(
    response: httpx.Response,
    message: str,
    status_code: int,
    code: str | None,
    retryable: bool,
) -> None:
    client = PatchScopeClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _request: response),
    )

    with pytest.raises(ApiError, match=message.replace(".", r"\.")) as captured:
        client.health()

    assert captured.value.message == message
    assert captured.value.status_code == status_code
    assert captured.value.code == code
    assert captured.value.retryable is retryable


@pytest.mark.parametrize("exception_type", [httpx.ConnectError, httpx.ReadTimeout])
def test_client_converts_network_failures_to_retryable_service_error(
    exception_type: type[httpx.HTTPError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception_type("network failed", request=request)

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))

    with pytest.raises(ApiError, match="service is unavailable") as captured:
        client.health()

    assert captured.value.status_code is None
    assert captured.value.code == "service_unavailable"
    assert captured.value.retryable is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"not-json", "response the workbench could not read"),
        (b"[]", "unexpected response shape"),
    ],
)
def test_success_response_must_be_a_json_object(payload: bytes, message: str) -> None:
    client = PatchScopeClient(
        "http://testserver",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=payload,
                headers={"content-type": "application/json"},
            )
        ),
    )

    with pytest.raises(ApiError, match=message):
        client.health()


def test_export_validates_format_and_sanitizes_download_filename() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"report",
            headers={"content-disposition": 'attachment; filename="folder/report.md"'},
        )

    client = PatchScopeClient("http://testserver", transport=httpx.MockTransport(handler))

    artifact = client.download_export("review/1", "MARKDOWN")
    assert artifact.filename == "folder-report.md"
    assert artifact.media_type == "application/octet-stream"
    with pytest.raises(ValueError, match="Unsupported PatchScope export format"):
        client.download_export("review/1", "html")


def test_export_uses_safe_fallback_when_disposition_is_missing_or_invalid() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"first"),
            httpx.Response(200, content=b"second", headers={"content-disposition": "inline"}),
        ]
    )
    client = PatchScopeClient(
        "http://testserver",
        transport=httpx.MockTransport(lambda _request: next(responses)),
    )

    assert client.download_export("review/1", "markdown").filename == "patchscope-review%2F1.md"
    assert client.download_export("review/1", "sarif").filename == (
        "patchscope-review%2F1.sarif.json"
    )


def test_environment_client_precedence_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PATCHSCOPE_API_URL", raising=False)
    assert client_from_environment().base_url == "http://127.0.0.1:8787"

    monkeypatch.setenv("PATCHSCOPE_API_URL", "http://configured:8000/")
    assert client_from_environment().base_url == "http://configured:8000"
