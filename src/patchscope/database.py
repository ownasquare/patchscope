"""SQLAlchemy schema and engine lifecycle for PatchScope's durable store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative metadata root."""


class ReviewRow(Base):
    __tablename__ = "reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_reference: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    ai_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    request_metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    stage_trace_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    ai_metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    total_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recommendation: Mapped[str] = mapped_column(String(32), nullable=False, default="approve")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_reviews_created_at_id", "created_at", "id"),
        Index("ix_reviews_status_created_at", "status", "created_at"),
    )


class ReviewFileRow(Base):
    __tablename__ = "review_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("review_id", "path", name="uq_review_files_review_path"),
        Index("ix_review_files_review_position", "review_id", "position"),
    )


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(200), nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_column: Mapped[int | None] = mapped_column(Integer)
    end_column: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    analyzer: Mapped[str] = mapped_column(String(120), nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    suggestion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    refactor_diff: Mapped[str | None] = mapped_column(Text)
    triage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    triage_note: Mapped[str | None] = mapped_column(Text)
    triaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("review_id", "fingerprint", name="uq_findings_review_fingerprint"),
        Index("ix_findings_review_position", "review_id", "position"),
        Index("ix_findings_review_triage", "review_id", "triage"),
    )


class AnalyzerRunRow(Base):
    __tablename__ = "analyzer_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    analyzer: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("review_id", "analyzer", name="uq_analyzer_runs_review_analyzer"),
        Index("ix_analyzer_runs_review_position", "review_id", "position"),
    )


def _prepare_sqlite_path(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return
    database = url.database
    if not database or database == ":memory:" or database.startswith("file:"):
        return
    Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite(engine: Engine) -> None:
    def set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    event.listen(engine, "connect", set_sqlite_pragmas)


@dataclass(slots=True)
class Database:
    """Own an engine and short-lived session factory."""

    engine: Engine
    session_factory: sessionmaker[Session]

    @classmethod
    def connect(cls, database_url: str, *, echo: bool = False) -> Database:
        _prepare_sqlite_path(database_url)
        url = make_url(database_url)
        engine_options: dict[str, Any] = {
            "echo": echo,
            "future": True,
            "pool_pre_ping": True,
        }
        if url.get_backend_name() == "sqlite":
            engine_options["connect_args"] = {"check_same_thread": False}
            if not url.database or url.database == ":memory:":
                engine_options["poolclass"] = StaticPool

        engine = create_engine(database_url, **engine_options)
        if url.get_backend_name() == "sqlite":
            _configure_sqlite(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)
        return cls(engine=engine, session_factory=factory)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()


def create_database(
    database_url: str, *, create_schema: bool = True, echo: bool = False
) -> Database:
    """Create a database handle and, by default, initialize its schema."""

    database = Database.connect(database_url, echo=echo)
    if create_schema:
        database.create_schema()
    return database


__all__ = [
    "AnalyzerRunRow",
    "Base",
    "Database",
    "FindingRow",
    "ReviewFileRow",
    "ReviewRow",
    "create_database",
]
