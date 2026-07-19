"""Typed, local-safe configuration for PatchScope services."""

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from ``PATCHSCOPE_*`` environment keys."""

    model_config = SettingsConfigDict(
        env_prefix="PATCHSCOPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    data_dir: Path = Path(".data")
    database_url_override: str | None = None

    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:8501",
        "http://localhost:8501",
    )

    ai_mode: Literal["auto", "offline", "openai"] = "auto"
    openai_model: str = "gpt-5-mini"
    openai_api_key: SecretStr | None = None
    openai_max_prompt_chars: int = Field(default=120_000, ge=4_000, le=1_000_000)
    openai_max_completion_tokens: int = Field(default=4_096, ge=256, le=16_384)
    github_token: SecretStr | None = None

    max_file_bytes: int = Field(default=500_000, gt=0)
    max_review_bytes: int = Field(default=2_000_000, gt=0)
    max_files: int = Field(default=100, gt=0, le=1_000)
    analyzer_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    github_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("database_url_override")
    @classmethod
    def validate_database_override(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        if normalized.startswith("postgresql://"):
            normalized = normalized.replace("postgresql://", "postgresql+psycopg://", 1)
        supported = ("sqlite://", "sqlite+pysqlite://", "postgresql+psycopg://")
        if not normalized.startswith(supported):
            raise ValueError("database_url_override must use SQLite or PostgreSQL")
        return normalized

    @field_validator("allowed_origins")
    @classmethod
    def validate_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(origin.strip().rstrip("/") for origin in value))
        if not normalized or any(
            not origin.startswith(("http://", "https://")) for origin in normalized
        ):
            raise ValueError("allowed_origins must contain HTTP or HTTPS origins")
        return normalized

    @model_validator(mode="after")
    def validate_resource_limits(self) -> Self:
        if self.max_file_bytes > self.max_review_bytes:
            raise ValueError("max_file_bytes cannot exceed max_review_bytes")
        return self

    @property
    def database_url(self) -> str:
        """Return the configured database URL without touching the filesystem."""

        if self.database_url_override:
            return self.database_url_override
        database_path = (self.data_dir.expanduser().resolve() / "patchscope.db").as_posix()
        return f"sqlite+pysqlite:///{database_path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return one process-local settings snapshot."""

    return Settings()


__all__ = ["Settings", "get_settings"]
