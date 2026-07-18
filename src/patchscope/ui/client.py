"""Small, resilient HTTP client used only by the Streamlit presentation layer."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

JsonObject = dict[str, Any]


class ApiError(RuntimeError):
    """A sanitized API failure suitable for direct user presentation."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ExportArtifact:
    """Download-ready review evidence returned by the API."""

    content: bytes
    filename: str
    media_type: str


def _json_object(response: httpx.Response) -> JsonObject:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ApiError(
            "PatchScope returned a response the workbench could not read.",
            status_code=response.status_code,
            retryable=response.status_code >= 500,
        ) from exc
    if not isinstance(payload, dict):
        raise ApiError(
            "PatchScope returned an unexpected response shape.",
            status_code=response.status_code,
            retryable=response.status_code >= 500,
        )
    return payload


def _problem_message(payload: object, default: str) -> tuple[str, str | None]:
    if not isinstance(payload, dict):
        return default, None
    code = payload.get("code")
    error = payload.get("error")
    if isinstance(error, dict):
        nested_message = error.get("message") or error.get("detail")
        nested_code = error.get("code")
        if isinstance(nested_message, str) and nested_message.strip():
            return (
                nested_message.strip(),
                str(nested_code or code) if nested_code or code else None,
            )
    detail = payload.get("detail")
    if isinstance(detail, dict):
        nested_message = detail.get("message") or detail.get("detail")
        nested_code = detail.get("code")
        if isinstance(nested_message, str) and nested_message.strip():
            return nested_message.strip(), str(nested_code or code) if nested_code or code else None
    for key in ("message", "detail", "error", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), str(code) if code else None
    return default, str(code) if code else None


def _filename_from_disposition(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', value, flags=re.IGNORECASE)
    if not match:
        return fallback
    candidate = match.group(1).strip().replace("/", "-").replace("\\", "-")
    return candidate or fallback


class PatchScopeClient:
    """Typed-ish boundary around PatchScope's JSON and download endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds))
        self.transport = transport

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                headers={"Accept": "application/json", "User-Agent": "PatchScope-Workbench/1"},
            ) as client:
                response = client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ApiError(
                "The PatchScope service is unavailable. Start the API, then try again.",
                code="service_unavailable",
                retryable=True,
            ) from exc
        if response.is_error:
            try:
                payload: object = response.json()
            except ValueError:
                payload = None
            default = (
                "The request could not be completed."
                if response.status_code < 500
                else "PatchScope encountered a temporary service error."
            )
            message, code = _problem_message(payload, default)
            raise ApiError(
                message,
                status_code=response.status_code,
                code=code,
                retryable=response.status_code in {408, 409, 425, 429}
                or response.status_code >= 500,
            )
        return response

    def health(self) -> JsonObject:
        return _json_object(self._request("GET", "/health"))

    def capabilities(self) -> JsonObject:
        return _json_object(self._request("GET", "/api/v1/capabilities"))

    def list_reviews(self, *, limit: int = 100, status: str | None = None) -> list[JsonObject]:
        params: dict[str, str | int] = {"limit": max(1, min(limit, 100))}
        if status and status != "all":
            params["status"] = status
        payload = _json_object(self._request("GET", "/api/v1/reviews", params=params))
        raw_items = payload.get("items", payload.get("reviews", []))
        if not isinstance(raw_items, list):
            return []
        return [item for item in raw_items if isinstance(item, dict)]

    def get_review(self, review_id: str) -> JsonObject:
        safe_id = quote(review_id, safe="")
        return _json_object(self._request("GET", f"/api/v1/reviews/{safe_id}"))

    def create_text_review(
        self, *, filename: str, content: str, name: str | None = None
    ) -> JsonObject:
        payload: JsonObject = {"filename": filename, "content": content}
        if name:
            payload["name"] = name
        return _json_object(self._request("POST", "/api/v1/reviews/text", json=payload))

    def create_file_review(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str,
        name: str | None = None,
    ) -> JsonObject:
        data = {"name": name} if name else None
        files = {"file": (filename, content, content_type or "text/plain")}
        return _json_object(self._request("POST", "/api/v1/reviews/file", data=data, files=files))

    def create_github_review(self, *, url: str, name: str | None = None) -> JsonObject:
        payload: JsonObject = {"url": url}
        if name:
            payload["name"] = name
        return _json_object(self._request("POST", "/api/v1/reviews/github", json=payload))

    def update_finding(
        self,
        *,
        review_id: str,
        fingerprint: str,
        status: str,
        note: str | None = None,
    ) -> JsonObject:
        safe_review_id = quote(review_id, safe="")
        safe_fingerprint = quote(fingerprint, safe="")
        payload: JsonObject = {"status": status}
        if note:
            payload["note"] = note
        return _json_object(
            self._request(
                "PATCH",
                f"/api/v1/reviews/{safe_review_id}/findings/{safe_fingerprint}",
                json=payload,
            )
        )

    def download_export(self, review_id: str, export_format: str) -> ExportArtifact:
        normalized = export_format.casefold()
        if normalized not in {"markdown", "sarif"}:
            raise ValueError("Unsupported PatchScope export format")
        safe_id = quote(review_id, safe="")
        response = self._request("GET", f"/api/v1/reviews/{safe_id}/exports/{normalized}")
        suffix = "md" if normalized == "markdown" else "sarif.json"
        fallback = f"patchscope-{safe_id}.{suffix}"
        return ExportArtifact(
            content=response.content,
            filename=_filename_from_disposition(
                response.headers.get("content-disposition"), fallback
            ),
            media_type=response.headers.get("content-type", "application/octet-stream").split(
                ";", 1
            )[0],
        )


def client_from_environment() -> PatchScopeClient:
    """Build a client without exposing environment contents to the page or logs."""

    base_url = os.environ.get("PATCHSCOPE_API_URL") or "http://127.0.0.1:8787"
    return PatchScopeClient(base_url)


__all__ = [
    "ApiError",
    "ExportArtifact",
    "JsonObject",
    "PatchScopeClient",
    "client_from_environment",
]
