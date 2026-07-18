from __future__ import annotations

from patchscope.languages import LANGUAGE_REGISTRY


def test_language_registry_is_the_complete_source_support_contract() -> None:
    assert LANGUAGE_REGISTRY.language_for_path("src/service.py") == "python"
    assert LANGUAGE_REGISTRY.language_for_path("web/component.jsx") == "javascript"
    assert LANGUAGE_REGISTRY.language_for_path("queries/report.sql") == "sql"
    assert LANGUAGE_REGISTRY.language_for_path("Dockerfile.worker") == "dockerfile"
    assert LANGUAGE_REGISTRY.language_for_path("Gemfile") == "ruby"
    assert LANGUAGE_REGISTRY.language_for_path("assets/blob.bin") is None

    assert LANGUAGE_REGISTRY.parser_name("python") == "python"
    assert LANGUAGE_REGISTRY.parser_name("sql") == "sql"
    assert LANGUAGE_REGISTRY.parser_name("yaml") is None
    assert LANGUAGE_REGISTRY.display_name_for_path("src/main.cpp") == "C++"


def test_language_registry_exposes_unique_ui_extensions_and_language_names() -> None:
    extensions = LANGUAGE_REGISTRY.source_extensions
    names = LANGUAGE_REGISTRY.language_names

    assert extensions == tuple(sorted(set(extensions)))
    assert names == tuple(dict.fromkeys(names))
    assert {"py", "tsx", "kt", "scala", "sql", "swift", "yaml"} <= set(extensions)
    assert {"python", "tsx", "kotlin", "scala", "sql", "swift", "yaml"} <= set(names)


def test_registry_rejects_duplicate_suffixes() -> None:
    from patchscope.languages import LanguageRegistry, LanguageSpec

    duplicate = (
        LanguageSpec("one", "One", suffixes=(".same",)),
        LanguageSpec("two", "Two", suffixes=(".same",)),
    )

    try:
        LanguageRegistry(duplicate)
    except ValueError as error:
        assert "duplicate source suffix" in str(error)
    else:  # pragma: no cover - makes a registry collision an explicit contract failure
        raise AssertionError("duplicate suffixes must be rejected")
