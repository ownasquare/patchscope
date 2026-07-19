"""Bounded, SSRF-safe public GitHub pull-request intake."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from patchscope import __version__
from patchscope.errors import GitHubError, GitHubRateLimitError, IntakeError
from patchscope.intake import should_ignore_source_path
from patchscope.languages import LANGUAGE_REGISTRY

_REPOSITORY_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


@dataclass(frozen=True, slots=True)
class GitHubPullRequestRef:
    owner: str
    repository: str
    number: int

    @property
    def canonical_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}/pull/{self.number}"


@dataclass(frozen=True, slots=True)
class GitHubSource:
    path: str
    content: str
    status: str
    additions: int
    deletions: int
    patch: str | None = None
    is_patch: bool = False


@dataclass(frozen=True, slots=True)
class GitHubPullRequest:
    ref: GitHubPullRequestRef
    title: str
    author: str
    head_sha: str
    base_branch: str
    head_branch: str
    files: tuple[GitHubSource, ...]
    skipped_files: tuple[str, ...] = field(default_factory=tuple)


def parse_pull_request_url(url: str) -> GitHubPullRequestRef:
    """Parse only canonical github.com pull-request URLs.

    Accepting structured owner/repository/number values rather than fetching the
    supplied URL prevents the URL field from becoming a general-purpose proxy.
    """

    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise IntakeError("Use an https://github.com/<owner>/<repo>/pull/<number> URL")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise IntakeError("The GitHub pull request URL contains unsupported parts")
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 4 or segments[2] != "pull" or not segments[3].isdigit():
        raise IntakeError("Use an https://github.com/<owner>/<repo>/pull/<number> URL")
    owner, repository = segments[0], segments[1].removesuffix(".git")
    if not _REPOSITORY_PART.fullmatch(owner) or not _REPOSITORY_PART.fullmatch(repository):
        raise IntakeError("The GitHub owner or repository name is invalid")
    number = int(segments[3])
    if not 1 <= number <= 2_147_483_647:
        raise IntakeError("The pull request number is invalid")
    return GitHubPullRequestRef(owner=owner, repository=repository, number=number)


class GitHubClient:
    """Read public PR metadata and source through fixed GitHub REST endpoints."""

    def __init__(
        self,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        max_files: int = 100,
        max_total_bytes: int = 2_000_000,
        max_file_bytes: int = 500_000,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 5.0))
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._max_file_bytes = max_file_bytes
        self._transport = transport

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"PatchScope/{__version__}",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def fetch_pull_request(self, value: str | GitHubPullRequestRef) -> GitHubPullRequest:
        ref = parse_pull_request_url(value) if isinstance(value, str) else value
        async with httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=self.headers,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            metadata = await self._request_json(
                client,
                f"/repos/{quote(ref.owner)}/{quote(ref.repository)}/pulls/{ref.number}",
            )
            if not isinstance(metadata, dict):
                raise GitHubError("GitHub returned an invalid pull request response")
            _require_public_repository(metadata)
            changed_files = _as_nonnegative_int(metadata.get("changed_files"))
            if changed_files > self._max_files:
                raise IntakeError(
                    f"This pull request exceeds the {self._max_files}-file review limit"
                )
            files_data = await self._list_files(client, ref)
            return await self._hydrate(client, ref, metadata, files_data)

    async def _list_files(
        self,
        client: httpx.AsyncClient,
        ref: GitHubPullRequestRef,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._request_json(
                client,
                f"/repos/{quote(ref.owner)}/{quote(ref.repository)}/pulls/{ref.number}/files",
                params={"per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise GitHubError("GitHub returned an invalid file list")
            batch = [item for item in payload if isinstance(item, dict)]
            collected.extend(batch)
            if len(collected) > self._max_files:
                raise IntakeError(
                    f"This pull request exceeds the {self._max_files}-file review limit"
                )
            if len(batch) < 100:
                break
            page += 1
        return collected

    async def _hydrate(
        self,
        client: httpx.AsyncClient,
        ref: GitHubPullRequestRef,
        metadata: dict[str, Any],
        files_data: list[dict[str, Any]],
    ) -> GitHubPullRequest:
        raw_head = metadata.get("head")
        raw_base = metadata.get("base")
        raw_user = metadata.get("user")
        head: Mapping[str, Any] = raw_head if isinstance(raw_head, dict) else {}
        base: Mapping[str, Any] = raw_base if isinstance(raw_base, dict) else {}
        user: Mapping[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        head_sha = str(head.get("sha", ""))
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", head_sha):
            raise GitHubError("GitHub did not return a valid head revision")

        sources: list[GitHubSource] = []
        skipped: list[str] = []
        total_bytes = 0
        for item in files_data:
            path = str(item.get("filename", ""))
            status = str(item.get("status", "modified"))
            if (
                not _safe_repo_path(path)
                or should_ignore_source_path(path)
                or LANGUAGE_REGISTRY.language_for_path(path) is None
            ):
                skipped.append(path or "<unnamed>")
                continue
            patch = item.get("patch") if isinstance(item.get("patch"), str) else None
            if patch is None and status != "added":
                skipped.append(path)
                continue
            content: str | None = None
            is_patch = False
            if status != "removed":
                content = await self._fetch_content(client, ref, path, head_sha)
            if content is None and patch:
                content = patch
                is_patch = True
            if content is None:
                skipped.append(path)
                continue
            encoded_size = len(content.encode("utf-8"))
            if encoded_size > self._max_file_bytes:
                skipped.append(path)
                continue
            total_bytes += encoded_size
            if total_bytes > self._max_total_bytes:
                raise IntakeError(
                    f"This pull request exceeds the {self._max_total_bytes}-byte review limit"
                )
            sources.append(
                GitHubSource(
                    path=path,
                    content=content,
                    status=status,
                    additions=_as_nonnegative_int(item.get("additions")),
                    deletions=_as_nonnegative_int(item.get("deletions")),
                    patch=patch,
                    is_patch=is_patch,
                )
            )
        if not sources:
            raise IntakeError("No supported text source files were found in this pull request")
        return GitHubPullRequest(
            ref=ref,
            title=str(metadata.get("title") or f"Pull request #{ref.number}"),
            author=str(user.get("login") or "unknown"),
            head_sha=head_sha,
            base_branch=str(base.get("ref") or "unknown"),
            head_branch=str(head.get("ref") or "unknown"),
            files=tuple(sources),
            skipped_files=tuple(skipped),
        )

    async def _fetch_content(
        self,
        client: httpx.AsyncClient,
        ref: GitHubPullRequestRef,
        path: str,
        head_sha: str,
    ) -> str | None:
        encoded_path = "/".join(quote(part, safe="") for part in PurePosixPath(path).parts)
        endpoint = f"/repos/{quote(ref.owner)}/{quote(ref.repository)}/contents/{encoded_path}"
        try:
            response = await self._request(
                client,
                endpoint,
                params={"ref": head_sha},
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except GitHubError:
            return None
        if len(response.content) > self._max_file_bytes or b"\x00" in response.content:
            return None
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            return None

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> object:
        response = await self._request(client, endpoint, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise GitHubError("GitHub returned a response that was not valid JSON") from exc

    async def _request(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        *,
        params: Mapping[str, str | int | float | bool | None] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                response = await client.get(endpoint, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == 2:
                    raise GitHubError(
                        "GitHub could not be reached within the request timeout"
                    ) from exc
                await asyncio.sleep(0.05 * (2**attempt))
                continue
            if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
                raise GitHubRateLimitError("GitHub's request limit was reached; try again later")
            if response.status_code == 404:
                raise GitHubError("The pull request or source file was not found")
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
                continue
            if response.status_code >= 400:
                raise GitHubError(
                    "GitHub refused the pull request request",
                    detail={"status_code": response.status_code},
                )
            return response
        raise GitHubError("GitHub could not be reached")


def _safe_repo_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return bool(path) and not candidate.is_absolute() and ".." not in candidate.parts


def _require_public_repository(metadata: Mapping[str, Any]) -> None:
    raw_base = metadata.get("base")
    base: Mapping[str, Any] = raw_base if isinstance(raw_base, dict) else {}
    raw_repository = base.get("repo")
    repository: Mapping[str, Any] = raw_repository if isinstance(raw_repository, dict) else {}
    visibility = repository.get("visibility")
    if repository.get("private") is not False or (
        visibility is not None and visibility != "public"
    ):
        raise IntakeError("PatchScope accepts pull requests from public GitHub repositories only")


def _as_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def changed_line_ranges(patch: str | None) -> tuple[tuple[int, int], ...]:
    """Return compact target-file ranges for lines added by a unified diff."""

    if not patch:
        return ()
    added_lines: list[int] = []
    target_line: int | None = None
    for line in patch.splitlines():
        header = _HUNK_HEADER.match(line)
        if header is not None:
            target_line = int(header.group("start"))
            continue
        if target_line is None or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if target_line >= 1:
                added_lines.append(target_line)
            target_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            target_line += 1

    ranges: list[tuple[int, int]] = []
    for line_number in added_lines:
        if ranges and ranges[-1][1] + 1 == line_number:
            ranges[-1] = (ranges[-1][0], line_number)
        else:
            ranges.append((line_number, line_number))
    return tuple(ranges)
