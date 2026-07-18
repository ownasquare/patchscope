from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest

import patchscope.intake as intake_module
from patchscope.intake import (
    IntakeBundle,
    IntakeError,
    IntakeLimits,
    SourceFile,
    SourceIntake,
    infer_language,
    ingest_files,
    ingest_zip,
    materialize_sources,
    staged_sources,
    validate_relative_path,
)


def _zip_bytes(values: dict[str, str], *, compression: int = zipfile.ZIP_STORED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path, content in values.items():
            archive.writestr(path, content)
    return output.getvalue()


class _ArchiveInfo:
    filename = "src/app.py"
    flag_bits = 0
    external_attr = stat.S_IFREG << 16
    file_size = 1
    compress_size = 1

    @staticmethod
    def is_dir() -> bool:
        return False


class _FakeArchive:
    def __init__(self, info: _ArchiveInfo, payload: bytes = b"x") -> None:
        self.info = info
        self.payload = payload

    def __enter__(self) -> _FakeArchive:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def infolist(self) -> list[_ArchiveInfo]:
        return [self.info]

    def open(self, _info: object, _mode: str) -> io.BytesIO:
        return io.BytesIO(self.payload)


def test_source_file_calculates_and_validates_digest() -> None:
    source = SourceFile.create("src/app.py", "print('safe')\n", language_hint="python")

    assert source.path == "src/app.py"
    assert source.size_bytes == len(source.content)
    assert source.model_dump()["sha256"] == source.sha256

    with pytest.raises(IntakeError, match="does not match"):
        SourceFile("src/app.py", "changed", "0" * 64, "python")


def test_source_file_rejects_invalid_fields_and_normalizes_safe_fields() -> None:
    normalized = SourceFile.create(
        " src/app.py ",
        "pass\n",
        language_hint=" PYTHON ",
    )

    assert normalized.path == "src/app.py"
    assert normalized.language_hint == "python"
    with pytest.raises(TypeError, match="content"):
        SourceFile("src/app.py", 3, "0" * 64)  # type: ignore[arg-type]
    with pytest.raises(IntakeError) as invalid_hash:
        SourceFile("src/app.py", "pass\n", "short")
    assert invalid_hash.value.code == "invalid_sha256"
    with pytest.raises(IntakeError) as invalid_hint:
        SourceFile.create("src/app.py", "pass\n", language_hint="not valid!")
    assert invalid_hint.value.code == "invalid_language_hint"
    with pytest.raises(TypeError, match="boolean"):
        SourceFile.create("src/app.py", "pass\n", is_patch=1)  # type: ignore[arg-type]


def test_limits_and_bundle_models_are_validated_and_serializable() -> None:
    with pytest.raises(ValueError, match="max_files"):
        IntakeLimits(max_files=0)

    source = SourceFile.create("app.py", "pass\n", language_hint="python")
    dumped = IntakeBundle((source,), source.size_bytes, (".env",)).model_dump(mode="json")

    assert dumped["files"][0]["path"] == "app.py"
    assert dumped["skipped_paths"] == [".env"]


@pytest.mark.parametrize(
    "path",
    ["", "../escape.py", "/absolute.py", "safe/../../escape.py", "safe\\app.py", "a/\x01.py"],
)
def test_relative_path_validation_fails_closed(path: str) -> None:
    with pytest.raises(IntakeError) as raised:
        validate_relative_path(path)
    assert raised.value.code == "unsafe_path"


def test_relative_path_rejects_wrong_type_and_oversized_segment() -> None:
    with pytest.raises(TypeError, match="text"):
        validate_relative_path(123)  # type: ignore[arg-type]
    with pytest.raises(IntakeError, match="unsafe segment"):
        validate_relative_path(f"src/{'a' * 256}.py")


def test_language_inference_covers_named_files_variants_and_patches() -> None:
    assert infer_language("Dockerfile") == ("dockerfile", False)
    assert infer_language("Dockerfile.worker") == ("dockerfile", False)
    assert infer_language("Makefile") == ("make", False)
    assert infer_language("queries/report.sql") == ("sql", False)
    assert infer_language("config/service.yaml") == ("yaml", False)
    assert infer_language("change.diff") == (None, True)
    assert infer_language("asset.bin") == (None, False)


def test_mapping_intake_is_ordered_and_skips_sensitive_or_unknown_files() -> None:
    bundle = SourceIntake().from_mapping(
        {
            "src/z.py": "value = 1\n",
            ".env": "PASSWORD=not-reviewed",
            "assets/image.bin": b"not source",
            "node_modules/ignored.py": "pass\n",
            "keys/private.pem": "not-reviewed",
            "src/a.ts": "export const a = 1;\n",
        }
    )

    assert [source.path for source in bundle.files] == ["src/a.ts", "src/z.py"]
    assert set(bundle.skipped_paths) == {
        ".env",
        "assets/image.bin",
        "keys/private.pem",
        "node_modules/ignored.py",
    }


@pytest.mark.parametrize(
    "values",
    [
        {},
        {".env": "PASSWORD=not-reviewed"},
        {"notes.txt": "not a supported source file"},
        {
            ".env": "PASSWORD=not-reviewed",
            "node_modules/ignored.py": "pass\n",
            "notes.txt": "not a supported source file",
        },
    ],
)
def test_mapping_intake_rejects_sources_without_reviewable_files(
    values: dict[str, str],
) -> None:
    with pytest.raises(IntakeError) as raised:
        SourceIntake().from_mapping(values)

    assert raised.value.code == "no_reviewable_files"
    assert "No supported text source files" in str(raised.value)


def test_mapping_accepts_utf8_bom_and_rejects_duplicate_normalized_paths() -> None:
    bundle = SourceIntake().from_mapping({"src/app.py": b"\xef\xbb\xbfpass\n"})

    assert bundle.files[0].content == "pass\n"
    with pytest.raises(IntakeError) as duplicate:
        SourceIntake().from_mapping({"src/Caf\u00e9.py": "pass\n", "src/Cafe\u0301.py": "pass\n"})
    assert duplicate.value.code == "duplicate_path"


def test_mapping_rejects_type_count_and_size_boundary_violations() -> None:
    with pytest.raises(IntakeError) as too_many:
        SourceIntake(IntakeLimits(max_files=1)).from_mapping({"a.py": "a", "b.py": "b"})
    assert too_many.value.code == "too_many_files"

    with pytest.raises(TypeError, match="text or bytes"):
        SourceIntake().from_mapping({"app.py": object()})  # type: ignore[dict-item]

    with pytest.raises(IntakeError) as file_too_large:
        SourceIntake(IntakeLimits(max_file_bytes=2)).from_mapping({"app.py": "abc"})
    assert file_too_large.value.code == "file_too_large"

    limits = IntakeLimits(max_file_bytes=10, max_total_bytes=5)
    with pytest.raises(IntakeError) as total_too_large:
        SourceIntake(limits).from_mapping({"a.py": "abc", "b.py": "def"})
    assert total_too_large.value.code == "upload_too_large"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"pass\x00word", "binary_file"),
        (b"\xff\xfe", "invalid_encoding"),
        (b"\x01\x02plain", "binary_file"),
    ],
)
def test_mapping_rejects_binary_or_non_utf8_source(raw: bytes, code: str) -> None:
    with pytest.raises(IntakeError) as raised:
        SourceIntake().from_mapping({"app.py": raw})
    assert raised.value.code == code


@pytest.mark.parametrize("path", ["../escape.py", "/absolute.py", "safe/../../escape.py"])
def test_mapping_intake_rejects_path_traversal(path: str) -> None:
    with pytest.raises(IntakeError, match="path"):
        SourceIntake().from_mapping({path: "pass\n"})


def test_zip_intake_rejects_links_and_compression_bombs() -> None:
    linked = io.BytesIO()
    with zipfile.ZipFile(linked, "w") as archive:
        info = zipfile.ZipInfo("src/link.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "target.py")
    with pytest.raises(IntakeError, match="Links"):
        SourceIntake().from_zip(linked.getvalue())

    compressed = io.BytesIO()
    with zipfile.ZipFile(compressed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("src/large.py", "a" * 20_000)
    limits = IntakeLimits(max_compression_ratio=2)
    with pytest.raises(IntakeError, match="compression ratio"):
        SourceIntake(limits).from_zip(compressed.getvalue())


def test_zip_intake_accepts_sources_and_rejects_invalid_container_boundaries() -> None:
    bundle = ingest_zip(_zip_bytes({"src/app.py": "pass\n", "notes.txt": "skip"}))

    assert [source.path for source in bundle.files] == ["src/app.py"]
    with pytest.raises(TypeError, match="bytes"):
        SourceIntake().from_zip("not-bytes")  # type: ignore[arg-type]
    with pytest.raises(IntakeError) as archive_too_large:
        SourceIntake(IntakeLimits(max_archive_bytes=2)).from_zip(b"123")
    assert archive_too_large.value.code == "archive_too_large"
    with pytest.raises(IntakeError) as invalid:
        SourceIntake().from_zip(b"not a zip")
    assert invalid.value.code == "invalid_archive"


def test_zip_intake_rejects_archives_without_reviewable_files() -> None:
    archive = _zip_bytes(
        {
            ".env": "PASSWORD=not-reviewed",
            "node_modules/ignored.py": "pass\n",
            "notes.txt": "not a supported source file",
        }
    )

    with pytest.raises(IntakeError) as raised:
        SourceIntake().from_zip(archive)

    assert raised.value.code == "no_reviewable_files"


def test_zip_intake_rejects_entry_count_duplicate_and_declared_size_limits() -> None:
    too_many = io.BytesIO()
    with zipfile.ZipFile(too_many, "w") as archive:
        archive.writestr("one/", "")
        archive.writestr("two/", "")
        archive.writestr("three/", "")
    with pytest.raises(IntakeError) as entry_count:
        SourceIntake(IntakeLimits(max_files=1)).from_zip(too_many.getvalue())
    assert entry_count.value.code == "too_many_entries"

    with pytest.raises(IntakeError) as duplicate:
        SourceIntake().from_zip(_zip_bytes({"A.py": "a", "a.py": "b"}))
    assert duplicate.value.code == "duplicate_path"

    with pytest.raises(IntakeError) as too_large:
        SourceIntake(IntakeLimits(max_file_bytes=2)).from_zip(_zip_bytes({"app.py": "abc"}))
    assert too_large.value.code == "file_too_large"

    values = {"a.py": "abc", "b.py": "def"}
    with pytest.raises(IntakeError) as total:
        SourceIntake(IntakeLimits(max_file_bytes=10, max_total_bytes=5)).from_zip(
            _zip_bytes(values)
        )
    assert total.value.code == "upload_too_large"


def test_zip_intake_rejects_encryption_zero_size_ratio_and_read_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = _ArchiveInfo()
    encrypted.flag_bits = 0x1
    monkeypatch.setattr(intake_module.zipfile, "ZipFile", lambda _buffer: _FakeArchive(encrypted))
    with pytest.raises(IntakeError) as encrypted_error:
        SourceIntake().from_zip(b"zip")
    assert encrypted_error.value.code == "encrypted_entry"

    unsafe = _ArchiveInfo()
    unsafe.compress_size = 0
    monkeypatch.setattr(intake_module.zipfile, "ZipFile", lambda _buffer: _FakeArchive(unsafe))
    with pytest.raises(IntakeError) as ratio_error:
        SourceIntake().from_zip(b"zip")
    assert ratio_error.value.code == "unsafe_compression"

    class BrokenArchive(_FakeArchive):
        def open(self, _info: object, _mode: str) -> io.BytesIO:
            raise OSError("broken member")

    regular = _ArchiveInfo()
    monkeypatch.setattr(
        intake_module.zipfile,
        "ZipFile",
        lambda _buffer: BrokenArchive(regular),
    )
    with pytest.raises(IntakeError) as read_error:
        SourceIntake().from_zip(b"zip")
    assert read_error.value.code == "invalid_archive_entry"


def test_bounded_zip_reader_rejects_stream_larger_than_declared() -> None:
    intake = SourceIntake(IntakeLimits(max_file_bytes=2))

    with pytest.raises(IntakeError) as raised:
        intake._read_bounded(_FakeArchive(_ArchiveInfo(), b"abc"), zipfile.ZipInfo("app.py"))
    assert raised.value.code == "file_too_large"


def test_materialize_sources_writes_only_full_files(tmp_path) -> None:
    source = SourceFile.create("src/app.py", "value = 1\n", language_hint="python")
    patch = SourceFile.create("change.patch", "+value = 2\n", is_patch=True)

    created = materialize_sources([patch, source], tmp_path)

    assert created == (tmp_path / "src/app.py",)
    assert (tmp_path / "src/app.py").read_text() == source.content
    assert not (tmp_path / "change.patch").exists()


def test_materialization_rejects_symlink_roots_parents_duplicates_and_overwrite(
    tmp_path: Path,
) -> None:
    source = SourceFile.create("src/app.py", "pass\n", language_hint="python")
    actual_root = tmp_path / "actual"
    actual_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(IntakeError) as root_error:
        materialize_sources([source], linked_root)
    assert root_error.value.code == "unsafe_stage"

    stage = tmp_path / "stage"
    stage.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (stage / "src").symlink_to(outside, target_is_directory=True)
    with pytest.raises(IntakeError) as parent_error:
        materialize_sources([source], stage)
    assert parent_error.value.code == "unsafe_stage"

    duplicate_root = tmp_path / "duplicate"
    upper = SourceFile.create("App.py", "one\n", language_hint="python")
    lower = SourceFile.create("app.py", "two\n", language_hint="python")
    with pytest.raises(IntakeError) as duplicate_error:
        materialize_sources([upper, lower], duplicate_root)
    assert duplicate_error.value.code == "duplicate_path"

    existing_root = tmp_path / "existing"
    existing_root.mkdir()
    (existing_root / "app.py").write_text("original\n")
    with pytest.raises(FileExistsError):
        materialize_sources([lower], existing_root)
    assert (existing_root / "app.py").read_text() == "original\n"


def test_staged_sources_and_ingest_wrapper_have_bounded_lifetimes() -> None:
    bundle = ingest_files({"app.py": "pass\n"})
    staged_root: Path

    with staged_sources(bundle.files) as root:
        staged_root = root
        assert (root / "app.py").read_text() == "pass\n"
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    assert not staged_root.exists()
