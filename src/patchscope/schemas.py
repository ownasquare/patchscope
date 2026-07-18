"""HTTP request and response-only schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TextReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=2_000_000)
    name: str | None = Field(default=None, max_length=160)

    @field_validator("filename")
    @classmethod
    def normalize_filename(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("filename cannot be blank")
        return normalized

    @field_validator("content")
    @classmethod
    def preserve_nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content cannot be blank")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class GitHubReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=20, max_length=500)
    name: str | None = Field(default=None, max_length=160)

    @field_validator("url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class FindingTriageRequest(BaseModel):
    status: Literal[
        "open",
        "acknowledged",
        "accepted",
        "fixed",
        "resolved",
        "ignored",
        "dismissed",
    ]
    note: str | None = Field(default=None, max_length=1_000)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    context: dict[str, object] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class Capability(BaseModel):
    name: str
    status: Literal["available", "unavailable", "degraded", "not_applicable"]
    detail: str


class CapabilitiesResponse(BaseModel):
    version: str
    ai_mode: str
    source_execution: Literal[False] = False
    inputs: list[str]
    languages: list[str]
    analyzers: list[Capability]
    exports: list[str]
