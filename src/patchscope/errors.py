"""PatchScope's public, sanitized error hierarchy."""

from __future__ import annotations


class PatchScopeError(Exception):
    """Base class for expected application errors."""

    code = "patchscope_error"
    status_code = 400

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class IntakeError(PatchScopeError):
    """Submitted source could not be accepted safely."""

    code = "invalid_source"
    status_code = 422


class ReviewNotFoundError(PatchScopeError):
    """The requested persisted review does not exist."""

    code = "review_not_found"
    status_code = 404


class GitHubError(PatchScopeError):
    """A public GitHub pull request could not be loaded."""

    code = "github_error"
    status_code = 502


class GitHubRateLimitError(GitHubError):
    """GitHub refused the request because its request budget was exhausted."""

    code = "github_rate_limited"
    status_code = 429


class AnalyzerError(PatchScopeError):
    """An analyzer failed outside its normal degraded result contract."""

    code = "analyzer_error"
    status_code = 500
