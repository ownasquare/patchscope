"""Small normalization helpers shared by analyzer adapters."""

from __future__ import annotations

from pathlib import Path

from patchscope.intake import SourceFile, validate_relative_path


def normalize_reported_path(value: object, root: Path) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = Path(value)
    try:
        if candidate.is_absolute():
            relative = candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
            normalized = relative.as_posix()
        else:
            normalized = candidate.as_posix()
        return validate_relative_path(normalized)
    except (OSError, ValueError):
        return None


def source_snippet(files: list[SourceFile], path: str, line: int) -> str | None:
    for source in files:
        if source.path != path:
            continue
        lines = source.content.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1][:500]
        return None
    return None


def bounded_message(value: object, *, fallback: str, limit: int = 1_000) -> str:
    if not isinstance(value, str):
        return fallback
    compact = " ".join(value.replace("\x00", "").split())
    if not compact:
        return fallback
    return compact[:limit]


def display_command(
    argv: tuple[str, ...], root: Path, extra_paths: tuple[Path, ...] = ()
) -> tuple[str, ...]:
    replacements = {str(root): "<workspace>"}
    replacements.update(
        {str(path): f"<temporary-{index}>" for index, path in enumerate(extra_paths, 1)}
    )
    displayed: list[str] = []
    for argument in argv:
        value = argument
        for original, replacement in replacements.items():
            value = value.replace(original, replacement)
        displayed.append(value)
    return tuple(displayed)


__all__ = ["bounded_message", "display_command", "normalize_reported_path", "source_snippet"]
