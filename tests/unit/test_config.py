from pathlib import Path

import pytest
from pydantic import ValidationError

from patchscope.config import Settings


def test_settings_default_to_local_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATCHSCOPE_DATA_DIR", str(tmp_path))

    settings = Settings(_env_file=None)

    assert settings.database_url.startswith("sqlite+pysqlite:///")
    assert settings.database_url.endswith("patchscope.db")
    assert settings.ai_mode == "auto"
    assert settings.max_file_bytes == 500_000
    assert settings.max_review_bytes == 2_000_000
    assert settings.data_dir == tmp_path


def test_database_override_wins_over_data_directory(tmp_path: Path) -> None:
    override = f"sqlite+pysqlite:///{tmp_path / 'override.db'}"

    settings = Settings(
        data_dir=tmp_path / "ignored",
        database_url_override=override,
        _env_file=None,
    )

    assert settings.database_url == override


def test_plain_postgresql_url_uses_the_packaged_psycopg_driver() -> None:
    settings = Settings(
        database_url_override="postgresql://user:pass@db.example/patchscope",
        _env_file=None,
    )

    assert settings.database_url == ("postgresql+psycopg://user:pass@db.example/patchscope")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_file_bytes", 0),
        ("max_review_bytes", -1),
        ("max_files", 0),
        ("analyzer_timeout_seconds", 0),
    ],
)
def test_settings_reject_non_positive_resource_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value}, _env_file=None)


def test_settings_reject_file_limit_larger_than_review_limit() -> None:
    with pytest.raises(ValidationError, match="max_file_bytes"):
        Settings(max_file_bytes=20, max_review_bytes=10, _env_file=None)
