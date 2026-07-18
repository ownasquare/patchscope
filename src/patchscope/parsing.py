"""Safe Tree-sitter parsing with deterministic multi-language fallbacks."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, cast

from patchscope.intake import SourceFile, infer_language
from patchscope.languages import LANGUAGE_REGISTRY

ParserFactory = Callable[[str], Any | None]


class ParseStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FALLBACK = "fallback"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ParseIssue:
    code: str
    message: str
    line: int | None = None
    column: int | None = None

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SyntaxSymbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    start_column: int = 1
    end_column: int = 1
    fingerprint: str = ""

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParseSummary:
    path: str
    language: str
    parser: str
    status: ParseStatus
    sha256: str
    line_count: int
    symbols: tuple[SyntaxSymbol, ...] = ()
    imports: tuple[str, ...] = ()
    issues: tuple[ParseIssue, ...] = ()
    truncated: bool = False

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        return {
            "path": self.path,
            "language": self.language,
            "parser": self.parser,
            "status": self.status.value,
            "sha256": self.sha256,
            "line_count": self.line_count,
            "symbols": [symbol.model_dump() for symbol in self.symbols],
            "imports": list(self.imports),
            "issues": [issue.model_dump() for issue in self.issues],
            "truncated": self.truncated,
        }


_FUNCTION_NODE_TYPES = frozenset(
    {
        "arrow_function",
        "constructor_declaration",
        "function_declaration",
        "function_definition",
        "function_expression",
        "function_item",
        "method",
        "method_declaration",
        "method_definition",
    }
)
_TYPE_NODE_KINDS = {
    "class_declaration": "class",
    "class_definition": "class",
    "class_specifier": "class",
    "enum_declaration": "enum",
    "enum_item": "enum",
    "impl_item": "implementation",
    "interface_declaration": "interface",
    "module_declaration": "module",
    "namespace_definition": "namespace",
    "object_declaration": "object",
    "protocol_declaration": "protocol",
    "record_declaration": "record",
    "struct_declaration": "struct",
    "struct_item": "struct",
    "struct_specifier": "struct",
    "trait_declaration": "trait",
}
_IMPORT_NODE_TYPES = frozenset(
    {
        "import_declaration",
        "import_from_statement",
        "import_statement",
        "include_directive",
        "package_clause",
        "require_call",
        "use_declaration",
        "using_directive",
    }
)
_IDENTIFIER_NODE_TYPES = frozenset(
    {
        "constant",
        "field_identifier",
        "identifier",
        "name",
        "operator_name",
        "property_identifier",
        "type_identifier",
    }
)

_HEURISTIC_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "python": (
        ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
        ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)\b")),
    ),
    "javascript": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        (
            "function",
            re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=.*=>"),
        ),
        ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ),
    "typescript": (
        ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
        (
            "function",
            re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=.*=>"),
        ),
        ("class", re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
        ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=")),
    ),
    "go": (
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
        ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
    ),
    "rust": (
        ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
        ("type", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
        ("implementation", re.compile(r"^\s*impl(?:<[^>]+>)?\s+([^\s<{]+)")),
    ),
    "ruby": (
        ("function", re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)")),
        ("class", re.compile(r"^\s*(?:class|module)\s+([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)")),
    ),
    "php": (
        (
            "function",
            re.compile(
                r"^\s*(?:(?:public|private|protected|static)\s+)*function\s+&?([A-Za-z_]\w*)", re.I
            ),
        ),
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)", re.I)),
    ),
    "swift": (
        (
            "function",
            re.compile(
                r"^\s*(?:(?:public|private|internal|open|static|class)\s+)*func\s+([A-Za-z_]\w*)"
            ),
        ),
        (
            "type",
            re.compile(
                r"^\s*(?:(?:public|private|internal|open)\s+)*(?:class|struct|enum|protocol)\s+([A-Za-z_]\w*)"
            ),
        ),
    ),
    "kotlin": (
        (
            "function",
            re.compile(
                r"^\s*(?:(?:public|private|internal|protected|suspend|inline)\s+)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)"
            ),
        ),
        (
            "class",
            re.compile(
                r"^\s*(?:(?:public|private|internal|sealed|data|open|abstract)\s+)*(?:class|interface|object)\s+([A-Za-z_]\w*)"
            ),
        ),
    ),
    "bash": (
        ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")),
    ),
    "sql": (
        (
            "function",
            re.compile(
                r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+([A-Za-z_][\w.]*)", re.I
            ),
        ),
        ("view", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+([A-Za-z_][\w.]*)", re.I)),
    ),
}

_C_STYLE_TYPE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|abstract|final|static|sealed|partial)\s+)*"
    r"(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)"
)
_C_STYLE_FUNCTION = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|final|inline|constexpr)\s+)*"
    r"(?:[A-Za-z_][\w:<>,.?\[\]*&\s]+\s+)+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:\{|throws\b)"
)


def detect_language(path: str) -> str:
    language, is_patch = infer_language(path)
    if is_patch:
        return "diff"
    return language or "text"


class TreeSitterParser:
    """Parse source bytes only; imported code is never loaded or executed."""

    def __init__(
        self,
        *,
        parser_factory: ParserFactory | None = None,
        max_nodes: int = 100_000,
        max_symbols: int = 2_000,
        max_issues: int = 50,
    ) -> None:
        if min(max_nodes, max_symbols, max_issues) <= 0:
            raise ValueError("parser limits must be positive")
        self._factory = parser_factory or self._default_parser_factory
        self._parsers: dict[str, Any | None] = {}
        self._max_nodes = max_nodes
        self._max_symbols = max_symbols
        self._max_issues = max_issues

    def parse(self, source: SourceFile) -> ParseSummary:
        language = source.language_hint or detect_language(source.path)
        content = source.content
        patch_issue: ParseIssue | None = None
        if source.is_patch:
            inferred, content = self._patch_content(source.content)
            if source.language_hint is None and inferred is not None:
                language = inferred
            patch_issue = ParseIssue(
                code="patch_partial",
                message="Only added patch lines were parsed; unchanged context is not represented.",
            )
            return self._heuristic_summary(
                source,
                language,
                content,
                status=ParseStatus.FALLBACK,
                issue=patch_issue,
            )

        parser_name = LANGUAGE_REGISTRY.parser_name(language)
        if parser_name is None:
            return self._heuristic_summary(
                source,
                language,
                content,
                status=ParseStatus.FALLBACK,
                issue=ParseIssue(
                    code="grammar_not_configured",
                    message=(
                        "No Tree-sitter grammar is configured; deterministic symbol matching "
                        "was used."
                    ),
                ),
            )
        parser = self._parser_for(parser_name)
        if parser is None:
            return self._heuristic_summary(
                source,
                language,
                content,
                status=ParseStatus.UNAVAILABLE,
                issue=ParseIssue(
                    code="parser_unavailable",
                    message=(
                        "The Tree-sitter grammar is unavailable; deterministic symbol matching "
                        "was used."
                    ),
                ),
            )
        encoded = content.encode("utf-8")
        try:
            tree = parser.parse(encoded)
            root = tree.root_node
            symbols, imports, issues, truncated = self._walk_tree(root, encoded)
        except (AttributeError, LookupError, OSError, TypeError, ValueError):
            return self._heuristic_summary(
                source,
                language,
                content,
                status=ParseStatus.ERROR,
                issue=ParseIssue(
                    code="parse_failed",
                    message=(
                        "Tree-sitter could not parse the source; deterministic symbol matching "
                        "was used."
                    ),
                ),
            )
        return ParseSummary(
            path=source.path,
            language=language,
            parser="tree-sitter",
            status=ParseStatus.SUCCEEDED,
            sha256=source.sha256,
            line_count=_line_count(content),
            symbols=tuple(symbols),
            imports=tuple(imports),
            issues=tuple(issues),
            truncated=truncated,
        )

    def parse_many(self, sources: list[SourceFile]) -> tuple[ParseSummary, ...]:
        return tuple(self.parse(source) for source in sorted(sources, key=lambda item: item.path))

    def _parser_for(self, parser_name: str) -> Any | None:
        if parser_name not in self._parsers:
            try:
                self._parsers[parser_name] = self._factory(parser_name)
            except (ImportError, LookupError, OSError, TypeError, ValueError):
                self._parsers[parser_name] = None
        return self._parsers[parser_name]

    @staticmethod
    def _default_parser_factory(parser_name: str) -> Any | None:
        try:
            from tree_sitter_language_pack import SupportedLanguage, get_parser
        except ImportError:
            return None
        return get_parser(cast(SupportedLanguage, parser_name))

    def _walk_tree(
        self, root: Any, encoded: bytes
    ) -> tuple[list[SyntaxSymbol], list[str], list[ParseIssue], bool]:
        symbols: list[SyntaxSymbol] = []
        imports: set[str] = set()
        issues: list[ParseIssue] = []
        stack = [root]
        visited = 0
        truncated = False
        seen_symbols: set[tuple[str, str, int, int]] = set()
        while stack:
            node = stack.pop()
            visited += 1
            if visited > self._max_nodes:
                truncated = True
                issues.append(
                    ParseIssue(
                        code="node_limit",
                        message="The syntax tree exceeded the bounded node limit.",
                    )
                )
                break
            node_type = str(getattr(node, "type", ""))
            start_line, start_column = _point(getattr(node, "start_point", (0, 0)))
            end_line, end_column = _point(getattr(node, "end_point", (0, 0)))
            if (node_type == "ERROR" or bool(getattr(node, "is_error", False))) and len(
                issues
            ) < self._max_issues:
                issues.append(
                    ParseIssue(
                        code="syntax_error",
                        message="Tree-sitter found an invalid syntax region.",
                        line=start_line,
                        column=start_column,
                    )
                )
            elif bool(getattr(node, "is_missing", False)) and len(issues) < self._max_issues:
                issues.append(
                    ParseIssue(
                        code="missing_syntax",
                        message="Tree-sitter inferred a missing syntax token.",
                        line=start_line,
                        column=start_column,
                    )
                )
            if node_type in _IMPORT_NODE_TYPES and len(imports) < 500:
                text = _node_text(node, encoded, limit=500)
                if text:
                    imports.add(" ".join(text.split()))
            kind = self._symbol_kind(node_type, node)
            if kind is not None and len(symbols) < self._max_symbols:
                name = _symbol_name(node, encoded)
                if name:
                    key = (name, kind, start_line, end_line)
                    if key not in seen_symbols:
                        seen_symbols.add(key)
                        symbols.append(
                            _make_symbol(
                                name,
                                kind,
                                start_line,
                                end_line,
                                start_column,
                                end_column,
                            )
                        )
            children = list(getattr(node, "named_children", ()) or ())
            stack.extend(reversed(children))
        symbols.sort(key=lambda item: (item.start_line, item.end_line, item.kind, item.name))
        return symbols, sorted(imports), issues[: self._max_issues], truncated

    @staticmethod
    def _symbol_kind(node_type: str, node: Any) -> str | None:
        if node_type in _FUNCTION_NODE_TYPES:
            ancestor = getattr(node, "parent", None)
            while ancestor is not None:
                ancestor_type = str(getattr(ancestor, "type", ""))
                if _TYPE_NODE_KINDS.get(ancestor_type) in {
                    "class",
                    "implementation",
                    "object",
                    "trait",
                }:
                    return "method"
                if ancestor_type in _FUNCTION_NODE_TYPES:
                    break
                ancestor = getattr(ancestor, "parent", None)
            return "function"
        return _TYPE_NODE_KINDS.get(node_type)

    def _heuristic_summary(
        self,
        source: SourceFile,
        language: str,
        content: str,
        *,
        status: ParseStatus,
        issue: ParseIssue,
    ) -> ParseSummary:
        symbols = _heuristic_symbols(language, content, self._max_symbols)
        return ParseSummary(
            path=source.path,
            language=language,
            parser="heuristic",
            status=status,
            sha256=source.sha256,
            line_count=_line_count(content),
            symbols=tuple(symbols),
            issues=(issue,),
            truncated=len(symbols) >= self._max_symbols,
        )

    @staticmethod
    def _patch_content(content: str) -> tuple[str | None, str]:
        inferred: str | None = None
        added: list[str] = []
        for line in content.splitlines():
            if line.startswith("+++ "):
                candidate = line[4:].strip()
                if candidate != "/dev/null":
                    candidate = candidate[2:] if candidate.startswith("b/") else candidate
                    language = detect_language(PurePosixPath(candidate).as_posix())
                    if language not in {"diff", "text"}:
                        inferred = language
                continue
            if line.startswith("+") and not line.startswith("+++"):
                added.append(line[1:])
        return inferred, "\n".join(added)


def _heuristic_symbols(language: str, content: str, limit: int) -> list[SyntaxSymbol]:
    patterns = list(_HEURISTIC_PATTERNS.get(language, ()))
    if language == "tsx":
        patterns = list(_HEURISTIC_PATTERNS["typescript"])
    if language in {"c", "cpp", "csharp", "java", "scala"}:
        patterns.extend((("type", _C_STYLE_TYPE), ("function", _C_STYLE_FUNCTION)))
    symbols: list[SyntaxSymbol] = []
    seen: set[tuple[str, str, int]] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = " ".join(match.group(1).split())[:200]
            key = (name, kind, line_number)
            if name and key not in seen:
                seen.add(key)
                symbols.append(
                    _make_symbol(
                        name,
                        kind,
                        line_number,
                        line_number,
                        match.start(1) + 1,
                        match.end(1) + 1,
                    )
                )
            if len(symbols) >= limit:
                return symbols
    return symbols


def _point(value: Any) -> tuple[int, int]:
    row = getattr(value, "row", None)
    column = getattr(value, "column", None)
    if isinstance(row, int) and isinstance(column, int):
        return row + 1, column + 1
    try:
        row_value, column_value = value
        return int(row_value) + 1, int(column_value) + 1
    except (TypeError, ValueError):
        return 1, 1


def _node_text(node: Any, encoded: bytes, *, limit: int) -> str:
    start = getattr(node, "start_byte", None)
    end = getattr(node, "end_byte", None)
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        return ""
    try:
        return encoded[start : min(end, start + limit)].decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _symbol_name(node: Any, encoded: bytes) -> str | None:
    candidate = None
    child_by_field_name = getattr(node, "child_by_field_name", None)
    if callable(child_by_field_name):
        for field in ("name", "declarator", "type"):
            candidate = child_by_field_name(field)
            if candidate is not None:
                break
    if candidate is None:
        return None
    if str(getattr(candidate, "type", "")) not in _IDENTIFIER_NODE_TYPES:
        stack = [candidate]
        candidate = None
        while stack:
            item = stack.pop()
            if str(getattr(item, "type", "")) in _IDENTIFIER_NODE_TYPES:
                candidate = item
                break
            stack.extend(reversed(list(getattr(item, "named_children", ()) or ())))
    if candidate is None:
        return None
    value = " ".join(_node_text(candidate, encoded, limit=300).split())
    if not value or len(value) > 200:
        return None
    return value


def _make_symbol(
    name: str,
    kind: str,
    start_line: int,
    end_line: int,
    start_column: int,
    end_column: int,
) -> SyntaxSymbol:
    identity = f"{kind}\x00{name}\x00{start_line}\x00{end_line}".encode()
    return SyntaxSymbol(
        name=name,
        kind=kind,
        start_line=max(start_line, 1),
        end_line=max(end_line, start_line, 1),
        start_column=max(start_column, 1),
        end_column=max(end_column, 1),
        fingerprint=hashlib.sha256(identity).hexdigest(),
    )


def _line_count(content: str) -> int:
    return max(len(content.splitlines()), 1 if content else 0)


__all__ = [
    "ParseIssue",
    "ParseStatus",
    "ParseSummary",
    "SyntaxSymbol",
    "TreeSitterParser",
    "detect_language",
]
