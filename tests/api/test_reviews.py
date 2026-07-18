from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from typing import Any
from unittest.mock import Mock

from fastapi.testclient import TestClient
from httpx import Response

from patchscope import __version__
from patchscope.api.app import create_app


class BlockingCountingWorkflow:
    """Hold the first analysis open so an identical request overlaps it."""

    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.calls = 0
        self.started = Event()
        self.release = Event()
        self._calls_lock = Lock()

    def invoke(self, *, files: list[object], metadata: dict[str, object]) -> dict[str, object]:
        with self._calls_lock:
            self.calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("The concurrency test did not release the workflow")
        return self.delegate.invoke(files=files, metadata=metadata)


class ExplodingWorkflow:
    def invoke(self, *, files: list[object], metadata: dict[str, object]) -> dict[str, object]:
        del files, metadata
        raise RuntimeError("sensitive provider detail")


def test_health_and_capabilities_report_truth(api_client: TestClient) -> None:
    health = api_client.get("/health")
    capabilities = api_client.get("/api/v1/capabilities")

    assert health.status_code == 200
    assert health.json()["version"] == __version__
    assert health.json()["source_execution"] is False
    assert health.headers["x-request-id"]
    assert capabilities.status_code == 200
    assert capabilities.json()["source_execution"] is False
    semgrep = next(item for item in capabilities.json()["analyzers"] if item["name"] == "semgrep")
    assert semgrep["status"] in {"available", "unavailable"}


def test_readiness_reports_database_failure_without_claiming_ready(
    api_client: TestClient,
    monkeypatch: Any,
) -> None:
    service: Any = api_client.app.state.review_service
    monkeypatch.setattr(service.repository, "ping", lambda: False)

    health = api_client.get("/health")
    readiness = api_client.get("/ready")

    assert health.status_code == 200
    assert health.json()["database"] == "unavailable"
    assert readiness.status_code == 503
    assert readiness.json() == {"status": "not_ready"}


def test_factory_owned_service_is_built_and_closed_by_lifespan(
    api_client: TestClient,
    monkeypatch: Any,
) -> None:
    service: Any = api_client.app.state.review_service
    close = Mock(wraps=service.close)
    monkeypatch.setattr(service, "close", close)
    build = Mock(return_value=service)
    monkeypatch.setattr("patchscope.container.build_service", build)

    app = create_app(settings=service.settings)
    with TestClient(app) as owned_client:
        assert owned_client.get("/health").status_code == 200

    build.assert_called_once_with(service.settings)
    close.assert_called_once_with()


def test_text_review_persists_findings_refactor_and_readback(api_client: TestClient) -> None:
    created = api_client.post(
        "/api/v1/reviews/text",
        json={"name": "Checkout", "filename": "checkout.py", "content": "eval(value)\n"},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["summary"]["by_category"]["security"] == 1
    assert payload["summary"]["recommendation"] == "request_changes"
    assert payload["findings"][0]["evidence"] == "eval(value)"
    assert payload["findings"][0]["refactor_diff"].startswith("--- a/checkout.py")
    assert payload["stage_trace"] == ["parse", "analyze", "synthesize", "refactor", "score"]

    readback = api_client.get(f"/api/v1/reviews/{payload['id']}")
    assert readback.status_code == 200
    assert readback.json() == payload

    repeated = api_client.post(
        "/api/v1/reviews/text",
        json={"name": "Different title", "filename": "checkout.py", "content": "eval(value)\n"},
    )
    assert repeated.status_code == 201
    assert repeated.json()["id"] == payload["id"]


def test_concurrent_identical_reviews_execute_workflow_once(api_client: TestClient) -> None:
    service: Any = api_client.app.state.review_service
    workflow = BlockingCountingWorkflow(service.workflow)
    service.workflow = workflow
    request_barrier = Barrier(3)

    def create_review() -> Response:
        request_barrier.wait(timeout=5)
        return api_client.post(
            "/api/v1/reviews/text",
            json={"filename": "checkout.py", "content": "eval(value)\n"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create_review)
        second = executor.submit(create_review)
        request_barrier.wait(timeout=5)
        assert workflow.started.wait(timeout=5)
        workflow.release.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.status_code for response in responses] == [201, 201]
    assert workflow.calls == 1
    assert responses[0].json()["id"] == responses[1].json()["id"]

    persisted = api_client.get(f"/api/v1/reviews/{responses[0].json()['id']}")
    assert persisted.status_code == 200
    assert persisted.json() == responses[0].json() == responses[1].json()


def test_review_inbox_triage_and_exports(api_client: TestClient) -> None:
    review = api_client.post(
        "/api/v1/reviews/text",
        json={"filename": "checkout.py", "content": "eval(value)\n"},
    ).json()
    review_id = review["id"]
    fingerprint = review["findings"][0]["fingerprint"]

    inbox = api_client.get("/api/v1/reviews?limit=10")
    assert inbox.status_code == 200
    assert inbox.json()["total"] == 1

    triage = api_client.patch(
        f"/api/v1/reviews/{review_id}/findings/{fingerprint}",
        json={"status": "resolved", "note": "Covered by a regression test."},
    )
    assert triage.status_code == 200
    assert triage.json()["triage"] == "fixed"

    markdown = api_client.get(f"/api/v1/reviews/{review_id}/exports/markdown")
    sarif = api_client.get(f"/api/v1/reviews/{review_id}/exports/sarif")
    assert markdown.status_code == 200
    assert b"Unsafe dynamic evaluation" in markdown.content
    assert sarif.status_code == 200
    assert sarif.json()["version"] == "2.1.0"


def test_review_list_rejects_limit_above_100(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/reviews?limit=101")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert "query.limit" in response.json()["detail"]["fields"]


def test_file_and_github_intake(api_client: TestClient) -> None:
    upload = api_client.post(
        "/api/v1/reviews/file",
        files={"file": ("service.py", b"eval(value)\n", "text/x-python")},
        data={"name": "Uploaded service"},
    )
    assert upload.status_code == 201
    assert upload.json()["request"]["source_kind"] == "file"

    github = api_client.post(
        "/api/v1/reviews/github",
        json={"url": "https://github.com/acme/shop/pull/7"},
    )
    assert github.status_code == 201
    assert github.json()["request"]["metadata"]["head_sha"] == "a" * 40


def test_oversized_upload_is_rejected_before_review_execution(api_client: TestClient) -> None:
    service: Any = api_client.app.state.review_service
    service.settings.max_review_bytes = 4

    response = api_client.post(
        "/api/v1/reviews/file",
        files={"file": ("too-large.py", b"12345", "text/x-python")},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_source"
    assert "4-byte review limit" in response.json()["message"]


def test_sources_without_reviewable_files_use_clean_intake_error(api_client: TestClient) -> None:
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr(".env", "SECRET=not-reviewed")
        archive.writestr("notes.txt", "not a supported source file")

    responses = [
        api_client.post(
            "/api/v1/reviews/text",
            json={"filename": "notes.txt", "content": "not a supported source file"},
        ),
        api_client.post(
            "/api/v1/reviews/file",
            files={"file": (".env", b"SECRET=not-reviewed\n", "text/plain")},
        ),
        api_client.post(
            "/api/v1/reviews/file",
            files={"file": ("notes.txt", b"not a supported source file\n", "text/plain")},
        ),
        api_client.post(
            "/api/v1/reviews/file",
            files={"file": ("sources.zip", archive_buffer.getvalue(), "application/zip")},
        ),
    ]

    for response in responses:
        assert response.status_code == 422
        assert response.json()["code"] == "invalid_source"
        assert response.json()["detail"]["intake_code"] == "no_reviewable_files"
        assert "No supported text source files" in response.json()["message"]


def test_invalid_export_format_uses_public_error_contract(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/reviews/rev_missing/exports/html")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_source"
    assert response.json()["detail"] == {}


def test_unexpected_failure_is_sanitized_and_keeps_request_id(
    api_client: TestClient,
    monkeypatch: Any,
) -> None:
    service: Any = api_client.app.state.review_service
    service.workflow = ExplodingWorkflow()
    monkeypatch.setattr(api_client._transport, "raise_server_exceptions", False)

    response = api_client.post(
        "/api/v1/reviews/text",
        json={"filename": "explode.py", "content": "raise RuntimeError\n"},
    )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["message"] == "PatchScope could not complete the request."
    assert response.json()["detail"] == {"error_type": "RuntimeError"}
    assert "sensitive provider detail" not in response.text
    assert response.json()["request_id"]
    assert response.headers["x-request-id"] == response.json()["request_id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_invalid_input_and_missing_review_use_sanitized_errors(api_client: TestClient) -> None:
    invalid = api_client.post(
        "/api/v1/reviews/text",
        json={"filename": "../escape.py", "content": "pass\n"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_source"
    assert "traceback" not in str(invalid.json()).casefold()

    missing = api_client.get("/api/v1/reviews/rev_missing")
    assert missing.status_code == 404
    assert missing.json()["code"] == "review_not_found"
