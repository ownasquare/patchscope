from __future__ import annotations

from pathlib import Path

import pytest

from patchscope.analyzers.utils import (
    bounded_message,
    display_command,
    normalize_reported_path,
    source_snippet,
)
from patchscope.intake import SourceFile


def test_normalize_reported_path_accepts_relative_and_in_workspace_absolute_paths(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "src" / "app.py"

    assert normalize_reported_path("src/app.py", tmp_path) == "src/app.py"
    assert normalize_reported_path(str(nested), tmp_path) == "src/app.py"


@pytest.mark.parametrize(
    "reported",
    [None, 7, "", "bad\x00.py", "../outside.py", "src/../outside.py", "src\\app.py"],
)
def test_normalize_reported_path_rejects_invalid_or_traversing_values(
    reported: object,
    tmp_path: Path,
) -> None:
    assert normalize_reported_path(reported, tmp_path) is None


def test_normalize_reported_path_rejects_absolute_path_outside_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"

    assert normalize_reported_path(str(outside), tmp_path) is None


def test_normalize_reported_path_handles_missing_workspace_for_absolute_report(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "missing-root"
    absolute_report = tmp_path / "app.py"

    assert normalize_reported_path(str(absolute_report), missing_root) is None


def test_source_snippet_returns_bounded_matching_line() -> None:
    source = SourceFile.create(
        "src/app.py",
        f"{'x' * 600}\nsecond line\n",
        language_hint="python",
    )
    other = SourceFile.create("other.py", "pass\n", language_hint="python")

    assert source_snippet([other, source], "src/app.py", 1) == "x" * 500
    assert source_snippet([source], "src/app.py", 2) == "second line"
    assert source_snippet([source], "src/app.py", 0) is None
    assert source_snippet([source], "src/app.py", 3) is None
    assert source_snippet([source], "missing.py", 1) is None


def test_bounded_message_normalizes_untrusted_text_and_uses_fallbacks() -> None:
    assert bounded_message(None, fallback="fallback") == "fallback"
    assert bounded_message(" \x00 \n\t", fallback="fallback") == "fallback"
    assert bounded_message("  first\x00\n second  ", fallback="fallback") == "first second"
    assert bounded_message("abcdefgh", fallback="fallback", limit=5) == "abcde"


def test_display_command_redacts_workspace_and_temporary_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    temporary = tmp_path / "private-rules.yml"
    command = (
        "semgrep",
        str(workspace),
        f"--config={temporary}",
        str(workspace / "src" / "app.py"),
        "relative-value",
    )

    assert display_command(command, workspace, (temporary,)) == (
        "semgrep",
        "<workspace>",
        "--config=<temporary-1>",
        "<workspace>/src/app.py",
        "relative-value",
    )
