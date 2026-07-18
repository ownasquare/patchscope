"""Bounded, non-executing source intake for PatchScope.

The intake layer treats filenames, archives, and source text as hostile data.  It
normalizes repository-relative paths, rejects links and archive traversal, bounds
decompression, and decodes only UTF-8 text.  It never imports source, invokes a
package manager, or runs repository-controlled commands.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from patchscope.languages import LANGUAGE_REGISTRY

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_LANGUAGE_HINT_RE = re.compile(r"[a-z][a-z0-9_+#.-]{0,31}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_IGNORED_PARTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "vendor",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "netrc",
        "secrets.yml",
        "secrets.yaml",
    }
)
_SENSITIVE_SUFFIXES = frozenset({".der", ".jks", ".key", ".p12", ".pem", ".pfx"})


class IntakeError(ValueError):
    """A stable, display-safe source-intake failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One validated UTF-8 source file or unified patch."""

    path: str
    content: str
    sha256: str
    language_hint: str | None = None
    is_patch: bool = False

    def __post_init__(self) -> None:
        normalized_path = validate_relative_path(self.path)
        object.__setattr__(self, "path", normalized_path)
        if not isinstance(self.content, str):
            raise TypeError("content must be text")
        normalized_hash = self.sha256.casefold()
        if not _SHA256_RE.fullmatch(normalized_hash):
            raise IntakeError("invalid_sha256", "Source SHA-256 must be 64 hexadecimal characters.")
        calculated = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if calculated != normalized_hash:
            raise IntakeError("sha256_mismatch", "Source content does not match its SHA-256.")
        object.__setattr__(self, "sha256", normalized_hash)
        if self.language_hint is not None:
            normalized_hint = self.language_hint.strip().casefold()
            if not _LANGUAGE_HINT_RE.fullmatch(normalized_hint):
                raise IntakeError("invalid_language_hint", "The language hint is invalid.")
            object.__setattr__(self, "language_hint", normalized_hint)
        if not isinstance(self.is_patch, bool):
            raise TypeError("is_patch must be a boolean")

    @classmethod
    def create(
        cls,
        path: str,
        content: str,
        *,
        language_hint: str | None = None,
        is_patch: bool = False,
    ) -> SourceFile:
        """Build a source record and calculate its canonical content digest."""

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return cls(
            path=path,
            content=content,
            sha256=digest,
            language_hint=language_hint,
            is_patch=is_patch,
        )

    @property
    def size_bytes(self) -> int:
        return len(self.content.encode("utf-8"))

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return asdict(self)


@dataclass(frozen=True, slots=True)
class IntakeLimits:
    """Resource limits applied before any source reaches an analyzer."""

    max_files: int = 500
    max_file_bytes: int = 1_000_000
    max_total_bytes: int = 20_000_000
    max_archive_bytes: int = 25_000_000
    max_compression_ratio: int = 100
    max_path_length: int = 500

    def __post_init__(self) -> None:
        for field_name, value in asdict(self).items():
            if not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class IntakeBundle:
    """Deterministically ordered source records and bounded intake evidence."""

    files: tuple[SourceFile, ...]
    total_bytes: int
    skipped_paths: tuple[str, ...] = ()

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "files": [source.model_dump() for source in self.files],
            "total_bytes": self.total_bytes,
            "skipped_paths": list(self.skipped_paths),
        }


def validate_relative_path(value: str, *, max_length: int = 500) -> str:
    """Return a normalized repository-relative POSIX path or fail closed."""

    if not isinstance(value, str):
        raise TypeError("source path must be text")
    normalized = unicodedata.normalize("NFC", value.strip())
    if (
        not normalized
        or len(normalized) > max_length
        or "\\" in normalized
        or _CONTROL_RE.search(normalized)
    ):
        raise IntakeError("unsafe_path", "The source path is invalid.")
    path = PurePosixPath(normalized)
    if path.is_absolute() or not path.parts:
        raise IntakeError("unsafe_path", "The source path must be repository-relative.")
    for part in path.parts:
        if part in {"", ".", ".."} or len(part.encode("utf-8")) > 255:
            raise IntakeError("unsafe_path", "The source path contains an unsafe segment.")
    return path.as_posix()


def infer_language(path: str) -> tuple[str | None, bool]:
    """Infer a conservative language hint and whether the record is a patch."""

    if LANGUAGE_REGISTRY.is_patch_path(path):
        return None, True
    return LANGUAGE_REGISTRY.language_for_path(path), False


def should_ignore_source_path(path: str) -> bool:
    """Return whether a source path is excluded from every intake channel."""

    candidate = PurePosixPath(path)
    folded_parts = {part.casefold() for part in candidate.parts}
    name = candidate.name.casefold()
    if folded_parts & _IGNORED_PARTS:
        return True
    if name in _SENSITIVE_NAMES or candidate.suffix.casefold() in _SENSITIVE_SUFFIXES:
        return True
    return name.startswith(".env.")


class SourceIntake:
    """Validate mappings and ZIP archives into immutable source records."""

    def __init__(self, limits: IntakeLimits | None = None) -> None:
        self.limits = limits or IntakeLimits()

    def from_mapping(self, values: Mapping[str, str | bytes]) -> IntakeBundle:
        if len(values) > self.limits.max_files:
            raise IntakeError("too_many_files", "The upload contains too many files.")
        records: list[SourceFile] = []
        skipped: list[str] = []
        seen: set[str] = set()
        total = 0
        for original_path in sorted(values, key=lambda item: unicodedata.normalize("NFC", item)):
            path = validate_relative_path(original_path, max_length=self.limits.max_path_length)
            duplicate_key = path.casefold()
            if duplicate_key in seen:
                raise IntakeError("duplicate_path", "The upload contains duplicate source paths.")
            seen.add(duplicate_key)
            if self._skip_path(path):
                skipped.append(path)
                continue
            raw = values[original_path]
            if isinstance(raw, str):
                encoded = raw.encode("utf-8")
                text = raw
            elif isinstance(raw, bytes):
                encoded = raw
                text = self._decode_text(raw)
            else:
                raise TypeError("source values must be text or bytes")
            self._check_file_size(len(encoded))
            total += len(encoded)
            self._check_total_size(total)
            language_hint, is_patch = infer_language(path)
            if language_hint is None and not is_patch:
                skipped.append(path)
                continue
            records.append(
                SourceFile.create(
                    path,
                    text,
                    language_hint=language_hint,
                    is_patch=is_patch,
                )
            )
        if not records:
            raise IntakeError(
                "no_reviewable_files",
                "No supported text source files were found in the submitted source.",
            )
        return IntakeBundle(
            tuple(sorted(records, key=lambda item: item.path)), total, tuple(skipped)
        )

    def from_zip(self, archive: bytes) -> IntakeBundle:
        if not isinstance(archive, bytes):
            raise TypeError("archive must be bytes")
        if len(archive) > self.limits.max_archive_bytes:
            raise IntakeError("archive_too_large", "The compressed archive is too large.")
        try:
            container = zipfile.ZipFile(io.BytesIO(archive))
        except (zipfile.BadZipFile, OSError) as exc:
            raise IntakeError("invalid_archive", "The ZIP archive could not be read.") from exc
        with container:
            infos = container.infolist()
            if len(infos) > self.limits.max_files * 2:
                raise IntakeError("too_many_entries", "The ZIP archive contains too many entries.")
            values: dict[str, bytes] = {}
            declared_total = 0
            seen: set[str] = set()
            for info in sorted(infos, key=lambda item: unicodedata.normalize("NFC", item.filename)):
                if info.is_dir():
                    continue
                path = validate_relative_path(info.filename, max_length=self.limits.max_path_length)
                duplicate_key = path.casefold()
                if duplicate_key in seen:
                    raise IntakeError("duplicate_path", "The ZIP archive contains duplicate paths.")
                seen.add(duplicate_key)
                if info.flag_bits & 0x1:
                    raise IntakeError("encrypted_entry", "Encrypted ZIP entries are not supported.")
                self._reject_non_regular_entry(info)
                self._check_file_size(info.file_size)
                declared_total += info.file_size
                self._check_total_size(declared_total)
                if info.file_size and info.compress_size == 0:
                    raise IntakeError(
                        "unsafe_compression", "A ZIP entry has an unsafe compression ratio."
                    )
                if (
                    info.compress_size
                    and info.file_size / info.compress_size > self.limits.max_compression_ratio
                ):
                    raise IntakeError(
                        "unsafe_compression", "A ZIP entry has an unsafe compression ratio."
                    )
                values[path] = self._read_bounded(container, info)
            if len(values) > self.limits.max_files:
                raise IntakeError("too_many_files", "The ZIP archive contains too many files.")
        return self.from_mapping(values)

    def _read_bounded(self, container: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
        chunks: list[bytes] = []
        size = 0
        try:
            with container.open(info, "r") as stream:
                while True:
                    chunk = stream.read(min(65_536, self.limits.max_file_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.limits.max_file_bytes:
                        raise IntakeError("file_too_large", "A source file exceeds the size limit.")
        except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
            raise IntakeError(
                "invalid_archive_entry", "A ZIP entry could not be read safely."
            ) from exc
        return b"".join(chunks)

    @staticmethod
    def _reject_non_regular_entry(info: zipfile.ZipInfo) -> None:
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type and file_type != stat.S_IFREG:
            raise IntakeError(
                "non_regular_entry", "Links and special ZIP entries are not supported."
            )

    @staticmethod
    def _decode_text(raw: bytes) -> str:
        if b"\x00" in raw:
            raise IntakeError("binary_file", "Binary files are not accepted for review.")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise IntakeError("invalid_encoding", "Source files must use UTF-8 encoding.") from exc
        disallowed = sum(
            1 for character in text if ord(character) < 32 and character not in "\n\r\t\f"
        )
        if text and disallowed / len(text) > 0.01:
            raise IntakeError("binary_file", "The file appears to contain binary control data.")
        return text

    def _check_file_size(self, size: int) -> None:
        if size > self.limits.max_file_bytes:
            raise IntakeError("file_too_large", "A source file exceeds the size limit.")

    def _check_total_size(self, size: int) -> None:
        if size > self.limits.max_total_bytes:
            raise IntakeError(
                "upload_too_large", "The extracted source exceeds the total size limit."
            )

    @staticmethod
    def _skip_path(path: str) -> bool:
        return should_ignore_source_path(path)


def ingest_files(
    values: Mapping[str, str | bytes], *, limits: IntakeLimits | None = None
) -> IntakeBundle:
    return SourceIntake(limits).from_mapping(values)


def ingest_zip(archive: bytes, *, limits: IntakeLimits | None = None) -> IntakeBundle:
    return SourceIntake(limits).from_zip(archive)


def materialize_sources(files: Iterable[SourceFile], root: Path) -> tuple[Path, ...]:
    """Write validated full-file sources into an owned analyzer staging root."""

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise IntakeError("unsafe_stage", "The analyzer staging root is invalid.")
    resolved_root = root.resolve(strict=True)
    created: list[Path] = []
    seen: set[str] = set()
    for source in sorted(files, key=lambda item: item.path):
        if source.is_patch:
            continue
        key = source.path.casefold()
        if key in seen:
            raise IntakeError("duplicate_path", "Duplicate source paths cannot be staged.")
        seen.add(key)
        relative = PurePosixPath(validate_relative_path(source.path))
        destination = resolved_root.joinpath(*relative.parts)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve(strict=True)
        if resolved_root != resolved_parent and resolved_root not in resolved_parent.parents:
            raise IntakeError("unsafe_stage", "A source path escaped the analyzer staging root.")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(destination, flags, 0o600)
        try:
            payload = source.content.encode("utf-8")
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        created.append(destination)
    return tuple(created)


@contextmanager
def staged_sources(files: Iterable[SourceFile]) -> Iterator[Path]:
    """Materialize source into a private temporary directory for fixed-argv tools."""

    with tempfile.TemporaryDirectory(prefix="patchscope-analysis-") as temporary:
        root = Path(temporary)
        os.chmod(root, 0o700)
        materialize_sources(files, root)
        yield root


__all__ = [
    "IntakeBundle",
    "IntakeError",
    "IntakeLimits",
    "SourceFile",
    "SourceIntake",
    "infer_language",
    "ingest_files",
    "ingest_zip",
    "materialize_sources",
    "should_ignore_source_path",
    "staged_sources",
    "validate_relative_path",
]
