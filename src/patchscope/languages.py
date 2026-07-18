"""Typed source-language registry shared by intake, parsing, API, and UI."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One accepted source language and its parser/display metadata."""

    name: str
    display_name: str
    suffixes: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()
    filename_prefixes: tuple[str, ...] = ()
    parser_name: str | None = None

    def __post_init__(self) -> None:
        name = self.name.strip().casefold()
        display_name = self.display_name.strip()
        if not name or not display_name:
            raise ValueError("language name and display name cannot be blank")
        suffixes = tuple(suffix.strip().casefold() for suffix in self.suffixes)
        if any(not suffix.startswith(".") or len(suffix) < 2 for suffix in suffixes):
            raise ValueError("language suffixes must begin with a dot")
        filenames = tuple(filename.strip().casefold() for filename in self.filenames)
        prefixes = tuple(prefix.strip().casefold() for prefix in self.filename_prefixes)
        if any(not value or "/" in value or "\\" in value for value in (*filenames, *prefixes)):
            raise ValueError("language filenames must be non-empty basenames")
        parser_name = self.parser_name.strip().casefold() if self.parser_name else None
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "suffixes", suffixes)
        object.__setattr__(self, "filenames", filenames)
        object.__setattr__(self, "filename_prefixes", prefixes)
        object.__setattr__(self, "parser_name", parser_name)


class LanguageRegistry:
    """Validated, immutable lookup surface for every accepted source type."""

    __slots__ = (
        "_by_filename",
        "_by_name",
        "_by_prefix",
        "_by_suffix",
        "_patch_suffixes",
        "_specs",
    )

    def __init__(
        self,
        specs: Iterable[LanguageSpec],
        *,
        patch_suffixes: Iterable[str] = (".diff", ".patch"),
    ) -> None:
        self._specs = tuple(specs)
        if not self._specs:
            raise ValueError("language registry cannot be empty")
        self._by_name: dict[str, LanguageSpec] = {}
        self._by_suffix: dict[str, LanguageSpec] = {}
        self._by_filename: dict[str, LanguageSpec] = {}
        self._by_prefix: dict[str, LanguageSpec] = {}
        for spec in self._specs:
            self._insert(self._by_name, spec.name, spec, "language name")
            for suffix in spec.suffixes:
                self._insert(self._by_suffix, suffix, spec, "source suffix")
            for filename in spec.filenames:
                self._insert(self._by_filename, filename, spec, "source filename")
            for prefix in spec.filename_prefixes:
                self._insert(self._by_prefix, prefix, spec, "source filename prefix")
        normalized_patches = tuple(sorted({suffix.strip().casefold() for suffix in patch_suffixes}))
        if any(not suffix.startswith(".") or len(suffix) < 2 for suffix in normalized_patches):
            raise ValueError("patch suffixes must begin with a dot")
        if set(normalized_patches) & self._by_suffix.keys():
            raise ValueError("patch suffixes cannot also be source suffixes")
        self._patch_suffixes = normalized_patches

    @staticmethod
    def _insert(
        target: dict[str, LanguageSpec],
        key: str,
        spec: LanguageSpec,
        description: str,
    ) -> None:
        if key in target:
            raise ValueError(f"duplicate {description}: {key}")
        target[key] = spec

    @property
    def specs(self) -> tuple[LanguageSpec, ...]:
        return self._specs

    @property
    def language_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    @property
    def parser_language_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs if spec.parser_name is not None)

    @property
    def source_extensions(self) -> tuple[str, ...]:
        return tuple(sorted(suffix.removeprefix(".") for suffix in self._by_suffix))

    @property
    def review_extensions(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    *self.source_extensions,
                    *(suffix.removeprefix(".") for suffix in self._patch_suffixes),
                }
            )
        )

    def language_for_path(self, path: str) -> str | None:
        candidate = PurePosixPath(path)
        filename = candidate.name.casefold()
        exact = self._by_filename.get(filename)
        if exact is not None:
            return exact.name
        for prefix, spec in self._by_prefix.items():
            if filename.startswith(prefix):
                return spec.name
        suffix_spec = self._by_suffix.get(candidate.suffix.casefold())
        return suffix_spec.name if suffix_spec is not None else None

    def parser_name(self, language: str) -> str | None:
        spec = self._by_name.get(language.strip().casefold())
        return spec.parser_name if spec is not None else None

    def display_name_for_path(self, path: str) -> str | None:
        if self.is_patch_path(path):
            return "Diff"
        language = self.language_for_path(path)
        spec = self._by_name.get(language) if language is not None else None
        return spec.display_name if spec is not None else None

    def is_patch_path(self, path: str) -> bool:
        return PurePosixPath(path).suffix.casefold() in self._patch_suffixes


LANGUAGE_REGISTRY = LanguageRegistry(
    (
        LanguageSpec("python", "Python", suffixes=(".py", ".pyi"), parser_name="python"),
        LanguageSpec(
            "javascript",
            "JavaScript",
            suffixes=(".js", ".jsx", ".mjs"),
            parser_name="javascript",
        ),
        LanguageSpec("typescript", "TypeScript", suffixes=(".ts",), parser_name="typescript"),
        LanguageSpec("tsx", "TSX", suffixes=(".tsx",), parser_name="tsx"),
        LanguageSpec("java", "Java", suffixes=(".java",), parser_name="java"),
        LanguageSpec("go", "Go", suffixes=(".go",), parser_name="go"),
        LanguageSpec("rust", "Rust", suffixes=(".rs",), parser_name="rust"),
        LanguageSpec("c", "C", suffixes=(".c", ".h"), parser_name="c"),
        LanguageSpec(
            "cpp",
            "C++",
            suffixes=(".cc", ".cpp", ".cxx", ".hh", ".hpp"),
            parser_name="cpp",
        ),
        LanguageSpec("csharp", "C#", suffixes=(".cs",), parser_name="csharp"),
        LanguageSpec(
            "ruby",
            "Ruby",
            suffixes=(".rb",),
            filenames=("gemfile",),
            parser_name="ruby",
        ),
        LanguageSpec("php", "PHP", suffixes=(".php",), parser_name="php"),
        LanguageSpec(
            "bash",
            "Bash",
            suffixes=(".bash", ".sh", ".zsh"),
            parser_name="bash",
        ),
        LanguageSpec("css", "CSS", suffixes=(".css",)),
        LanguageSpec("html", "HTML", suffixes=(".htm", ".html")),
        LanguageSpec("json", "JSON", suffixes=(".json", ".jsonc")),
        LanguageSpec("kotlin", "Kotlin", suffixes=(".kt", ".kts"), parser_name="kotlin"),
        LanguageSpec("markdown", "Markdown", suffixes=(".md", ".mdx")),
        LanguageSpec("scala", "Scala", suffixes=(".scala",), parser_name="scala"),
        LanguageSpec("sql", "SQL", suffixes=(".sql",), parser_name="sql"),
        LanguageSpec("swift", "Swift", suffixes=(".swift",), parser_name="swift"),
        LanguageSpec("terraform", "Terraform", suffixes=(".tf",)),
        LanguageSpec("toml", "TOML", suffixes=(".toml",)),
        LanguageSpec("yaml", "YAML", suffixes=(".yaml", ".yml")),
        LanguageSpec(
            "dockerfile",
            "Dockerfile",
            filenames=("dockerfile",),
            filename_prefixes=("dockerfile.",),
        ),
        LanguageSpec("make", "Makefile", filenames=("makefile",)),
        LanguageSpec("text", "Text", filenames=("procfile",)),
    )
)


__all__ = ["LANGUAGE_REGISTRY", "LanguageRegistry", "LanguageSpec"]
