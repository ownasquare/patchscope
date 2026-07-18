from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from patchscope.intake import SourceFile
from patchscope.parsing import (
    ParseStatus,
    TreeSitterParser,
    _node_text,
    _point,
    detect_language,
)


class _Node:
    def __init__(
        self,
        node_type: str,
        *,
        start: int = 0,
        end: int = 0,
        start_point: object = (0, 0),
        end_point: object = (0, 0),
        children: list[_Node] | None = None,
        fields: dict[str, _Node] | None = None,
        is_error: bool = False,
        is_missing: bool = False,
    ) -> None:
        self.type = node_type
        self.start_byte = start
        self.end_byte = end
        self.start_point = start_point
        self.end_point = end_point
        self.named_children = children or []
        self._fields = fields or {}
        self.is_error = is_error
        self.is_missing = is_missing
        self.parent: _Node | None = None
        for child in [*self.named_children, *self._fields.values()]:
            child.parent = self

    def child_by_field_name(self, field: str) -> _Node | None:
        return self._fields.get(field)


class _Parser:
    def __init__(self, root: _Node | None = None, error: Exception | None = None) -> None:
        self.root = root
        self.error = error

    def parse(self, _encoded: bytes) -> Any:
        if self.error is not None:
            raise self.error
        return SimpleNamespace(root_node=self.root)


def _span(content: str, value: str) -> tuple[int, int]:
    start = content.index(value)
    return start, start + len(value)


def test_detect_language_covers_common_source_types() -> None:
    assert detect_language("src/app.py") == "python"
    assert detect_language("web/view.tsx") == "tsx"
    assert detect_language("cmd/main.go") == "go"
    assert detect_language("queries/report.sql") == "sql"
    assert detect_language("config/service.yaml") == "yaml"
    assert detect_language("change.patch") == "diff"
    assert detect_language("assets/blob.unknown") == "text"


def test_unavailable_tree_sitter_uses_deterministic_python_symbols() -> None:
    source = SourceFile.create(
        "src/service.py",
        "class Service:\n    async def run(self) -> None:\n        return None\n",
        language_hint="python",
    )
    parser = TreeSitterParser(parser_factory=lambda _language: None)

    summary = parser.parse(source)

    assert summary.status is ParseStatus.UNAVAILABLE
    assert [(symbol.kind, symbol.name) for symbol in summary.symbols] == [
        ("class", "Service"),
        ("function", "run"),
    ]
    assert summary.model_dump()["status"] == "unavailable"


def test_typescript_fallback_extracts_functions_types_and_interfaces() -> None:
    source = SourceFile.create(
        "src/client.ts",
        "export interface Client {}\nexport type ID = string\nexport const load = async () => 1\n",
        language_hint="typescript",
    )

    summary = TreeSitterParser(parser_factory=lambda _language: None).parse(source)

    assert [(symbol.kind, symbol.name) for symbol in summary.symbols] == [
        ("interface", "Client"),
        ("type", "ID"),
        ("function", "load"),
    ]


def test_patch_parsing_reviews_added_lines_and_infers_target_language() -> None:
    source = SourceFile.create(
        "review.diff",
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n+def added():\n+    return 1\n",
        is_patch=True,
    )

    summary = TreeSitterParser(parser_factory=lambda _language: None).parse(source)

    assert summary.status is ParseStatus.FALLBACK
    assert summary.language == "python"
    assert [symbol.name for symbol in summary.symbols] == ["added"]
    assert summary.issues[0].code == "patch_partial"


def test_real_tree_sitter_parser_collects_imports_and_nested_symbols() -> None:
    source = SourceFile.create(
        "src/service.py",
        "import os\n\nclass Service:\n    def run(self) -> str:\n        return os.name\n",
        language_hint="python",
    )

    summary = TreeSitterParser().parse(source)

    assert summary.status is ParseStatus.SUCCEEDED
    assert summary.parser == "tree-sitter"
    assert "import os" in summary.imports
    assert ("class", "Service") in {(item.kind, item.name) for item in summary.symbols}
    assert ("method", "run") in {(item.kind, item.name) for item in summary.symbols}


def test_synthetic_tree_reports_syntax_issues_imports_and_nested_identifier() -> None:
    content = "import os\nclass Service:\n    def run(self):\n        pass\n"
    import_start, import_end = _span(content, "import os")
    class_start, class_end = _span(content, "class Service")
    service_start, service_end = _span(content, "Service")
    run_start, run_end = _span(content, "run")
    wrapper = _Node(
        "declarator",
        children=[_Node("type_identifier", start=service_start, end=service_end)],
    )
    method = _Node(
        "function_definition",
        start=content.index("    def run"),
        end=len(content),
        start_point=SimpleNamespace(row=2, column=4),
        end_point=SimpleNamespace(row=3, column=12),
        fields={"name": _Node("identifier", start=run_start, end=run_end)},
    )
    class_node = _Node(
        "class_definition",
        start=class_start,
        end=class_end,
        start_point=(1, 0),
        end_point=(3, 12),
        children=[method],
        fields={"name": wrapper},
    )
    root = _Node(
        "module",
        end=len(content),
        children=[
            _Node("import_statement", start=import_start, end=import_end),
            class_node,
            _Node("ERROR", start_point=(4, 2), end_point=(4, 3)),
            _Node("identifier", start_point=(5, 0), end_point=(5, 1), is_missing=True),
        ],
    )
    source = SourceFile.create("src/service.py", content, language_hint="python")

    summary = TreeSitterParser(parser_factory=lambda _language: _Parser(root)).parse(source)

    assert summary.status is ParseStatus.SUCCEEDED
    assert summary.imports == ("import os",)
    assert [(item.kind, item.name) for item in summary.symbols] == [
        ("class", "Service"),
        ("method", "run"),
    ]
    assert [issue.code for issue in summary.issues] == ["syntax_error", "missing_syntax"]
    assert summary.model_dump()["issues"][0]["line"] == 5


def test_parser_failure_and_factory_failure_fall_back_without_repeated_loading() -> None:
    source = SourceFile.create("src/app.py", "def run():\n    pass\n", language_hint="python")
    failed_parse = TreeSitterParser(
        parser_factory=lambda _language: _Parser(error=ValueError("bad tree"))
    ).parse(source)
    calls = 0

    def unavailable_factory(_language: str) -> None:
        nonlocal calls
        calls += 1
        raise ImportError("missing grammar")

    unavailable = TreeSitterParser(parser_factory=unavailable_factory)

    assert failed_parse.status is ParseStatus.ERROR
    assert failed_parse.issues[0].code == "parse_failed"
    assert unavailable.parse(source).status is ParseStatus.UNAVAILABLE
    assert unavailable.parse(source).status is ParseStatus.UNAVAILABLE
    assert calls == 1


def test_unconfigured_grammar_and_patch_without_target_fail_to_bounded_fallbacks() -> None:
    css = SourceFile.create("styles/app.css", ".card { color: red; }\n", language_hint="css")
    deleted_patch = SourceFile.create(
        "review.patch",
        "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-print('gone')\n",
        is_patch=True,
    )

    css_summary = TreeSitterParser(parser_factory=lambda _language: None).parse(css)
    patch_summary = TreeSitterParser(parser_factory=lambda _language: None).parse(deleted_patch)

    assert css_summary.status is ParseStatus.FALLBACK
    assert css_summary.issues[0].code == "grammar_not_configured"
    assert patch_summary.language == "diff"
    assert patch_summary.symbols == ()


def test_patch_language_hint_wins_and_parse_many_is_path_ordered() -> None:
    patch = SourceFile.create(
        "review.diff",
        "+++ b/client.ts\n+export const load = () => 1\n",
        language_hint="javascript",
        is_patch=True,
    )
    first = SourceFile.create("a.py", "def a():\n    pass\n", language_hint="python")
    last = SourceFile.create("z.py", "def z():\n    pass\n", language_hint="python")
    parser = TreeSitterParser(parser_factory=lambda _language: None)

    summaries = parser.parse_many([last, first])

    assert parser.parse(patch).language == "javascript"
    assert [summary.path for summary in summaries] == ["a.py", "z.py"]


def test_node_and_symbol_limits_are_reported_instead_of_overrunning() -> None:
    content = "def one():\n    pass\ndef two():\n    pass\n"
    root = _Node("module", children=[_Node("identifier"), _Node("identifier")])
    source = SourceFile.create("app.py", content, language_hint="python")

    tree_summary = TreeSitterParser(
        parser_factory=lambda _language: _Parser(root), max_nodes=1
    ).parse(source)
    fallback_summary = TreeSitterParser(parser_factory=lambda _language: None, max_symbols=1).parse(
        source
    )

    assert tree_summary.truncated is True
    assert tree_summary.issues[0].code == "node_limit"
    assert fallback_summary.truncated is True
    assert [item.name for item in fallback_summary.symbols] == ["one"]


@pytest.mark.parametrize("keyword", ["max_nodes", "max_symbols", "max_issues"])
def test_parser_rejects_non_positive_limits(keyword: str) -> None:
    with pytest.raises(ValueError, match="limits"):
        TreeSitterParser(**{keyword: 0})


def test_heuristic_parser_covers_tsx_and_c_style_declarations() -> None:
    tsx = SourceFile.create(
        "view.tsx",
        "export const View = () => <main />\n",
        language_hint="tsx",
    )
    java = SourceFile.create(
        "Service.java",
        "public class Service {\npublic String load() {\n}\n",
        language_hint="java",
    )
    parser = TreeSitterParser(parser_factory=lambda _language: None)

    assert [item.name for item in parser.parse(tsx).symbols] == ["View"]
    assert [item.name for item in parser.parse(java).symbols] == ["Service", "load"]


def test_point_and_node_text_helpers_fail_closed_on_malformed_values() -> None:
    assert _point(SimpleNamespace(row=2, column=3)) == (3, 4)
    assert _point(("4", "5")) == (5, 6)
    assert _point(object()) == (1, 1)
    assert _node_text(SimpleNamespace(start_byte=-1, end_byte=2), b"abc", limit=3) == ""
    assert _node_text(SimpleNamespace(start_byte=0, end_byte=1), b"\xff", limit=3) == ""
