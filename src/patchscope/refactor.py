"""Conservative, review-only refactor previews.

This module never writes source files.  It applies only small deterministic edits
whose exact range is known, then returns a unified diff for human review.
"""

from __future__ import annotations

import difflib
import io
import re
import tokenize
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, cast

from patchscope.intake import SourceFile


@dataclass(frozen=True, slots=True)
class RefactorPreview:
    path: str
    finding_id: str
    applicable: bool
    confidence: str
    rationale: str
    original: str = ""
    revised: str = ""
    unified_diff: str = ""
    start_line: int | None = None
    end_line: int | None = None
    safety_notes: tuple[str, ...] = ()

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]:
        del mode
        result = asdict(self)
        result["safety_notes"] = list(self.safety_notes)
        return result


class RefactorEngine:
    """Create bounded previews without mutating or executing imported code."""

    def __init__(self, *, max_source_bytes: int = 1_000_000, max_diff_chars: int = 200_000) -> None:
        if min(max_source_bytes, max_diff_chars) <= 0:
            raise ValueError("refactor limits must be positive")
        self.max_source_bytes = max_source_bytes
        self.max_diff_chars = max_diff_chars

    def preview(self, source: SourceFile, finding: object) -> RefactorPreview:
        data = _object_mapping(finding)
        finding_id = _text(data.get("id"), "finding")
        path = _text(data.get("path"), source.path)
        if path != source.path:
            return self._not_applicable(
                source,
                finding_id,
                "The finding does not belong to this source file.",
            )
        if source.is_patch:
            return self._not_applicable(
                source,
                finding_id,
                (
                    "Patch fragments are incomplete, so PatchScope will not synthesize a "
                    "full-file edit."
                ),
            )
        if source.size_bytes > self.max_source_bytes:
            return self._not_applicable(
                source,
                finding_id,
                "The file exceeds the bounded refactor-preview size.",
            )

        rule_id = _text(data.get("rule_id"), "")
        start_line = _positive_int(data.get("start_line"), 1)
        end_line = max(_positive_int(data.get("end_line"), start_line), start_line)
        raw_properties = data.get("properties")
        properties: Mapping[object, object] = (
            raw_properties if isinstance(raw_properties, Mapping) else {}
        )
        replacement = data.get("suggested_replacement")
        if data.get("autofix_safe") is True and isinstance(replacement, str):
            safe_revised = _apply_exact_replacement(source.content, replacement, properties)
            if safe_revised is not None:
                return self._completed(
                    source,
                    finding_id,
                    safe_revised,
                    start_line,
                    end_line,
                    "Applied the analyzer's explicitly safe, range-bound edit to a preview only.",
                    "high",
                    (
                        "Re-run the originating analyzer and focused tests before applying "
                        "this patch.",
                    ),
                )

        revised: str | None = None
        rationale = ""
        confidence = "medium"
        notes: tuple[str, ...] = (
            "This is a preview only; confirm behavior and run focused tests before applying it.",
        )
        if rule_id == "W291":
            revised = _replace_line(source.content, start_line, lambda line: line.rstrip(" \t"))
            rationale = "Removed trailing whitespace without changing executable tokens."
            confidence = "high"
        elif rule_id == "W292":
            revised = source.content if source.content.endswith("\n") else f"{source.content}\n"
            rationale = "Added the missing final newline."
            confidence = "high"
        elif rule_id == "E711":
            revised = _replace_line(source.content, start_line, _replace_none_comparison)
            rationale = "Replaced a None equality comparison with Python identity syntax."
            confidence = "high"
        elif rule_id == "patchscope.python.bare-except":
            revised = _replace_line(
                source.content,
                start_line,
                lambda line: re.sub(r"\bexcept\s*:", "except Exception:", line, count=1),
            )
            rationale = (
                "Narrowed a bare except so shutdown and cancellation signals are not swallowed."
            )
            notes = (
                "Replace Exception with the narrow expected exception after reviewing the "
                "protected operation.",
                "Run failure-path and cancellation tests before applying this patch.",
            )
        elif rule_id == "patchscope.python.mutable-default":
            revised = _replace_mutable_default(source.content, start_line)
            rationale = (
                "Replaced the shared mutable default with None and initialized a fresh value "
                "per call."
            )
            confidence = "high"
            notes = (
                "Confirm callers do not intentionally rely on state shared between calls.",
                "Run focused tests for omitted, explicit None, and explicit collection arguments.",
            )
        elif rule_id == "patchscope.python.network-timeout":
            revised = _replace_line(source.content, start_line, _add_http_timeout)
            rationale = "Added an explicit request timeout to bound network wait time."
            confidence = "medium"
            notes = (
                "Tune connect and read timeouts for the service-level objective before applying.",
                "Exercise the timeout and retry path with a focused test.",
            )
        elif rule_id == "patchscope.javascript.loose-equality":
            revised = _replace_line(source.content, start_line, _replace_loose_equality)
            rationale = "Replaced one loose equality operator with strict equality in the preview."
            confidence = "low"
            notes = (
                "Strict equality can change behavior when operand types differ; verify the "
                "intended types first.",
                "Run the affected JavaScript or TypeScript tests before applying this patch.",
            )

        if revised is None or revised == source.content:
            return self._not_applicable(
                source,
                finding_id,
                "No conservative deterministic edit is available for this finding.",
            )
        return self._completed(
            source,
            finding_id,
            revised,
            start_line,
            end_line,
            rationale,
            confidence,
            notes,
        )

    def _completed(
        self,
        source: SourceFile,
        finding_id: str,
        revised: str,
        start_line: int,
        end_line: int,
        rationale: str,
        confidence: str,
        notes: tuple[str, ...],
    ) -> RefactorPreview:
        patch = "".join(
            difflib.unified_diff(
                source.content.splitlines(keepends=True),
                revised.splitlines(keepends=True),
                fromfile=f"a/{source.path}",
                tofile=f"b/{source.path}",
                lineterm="\n",
            )
        )
        if len(patch) > self.max_diff_chars:
            return self._not_applicable(
                source,
                finding_id,
                "The proposed preview exceeded the bounded diff size.",
            )
        return RefactorPreview(
            path=source.path,
            finding_id=finding_id,
            applicable=True,
            confidence=confidence,
            rationale=rationale,
            original=source.content,
            revised=revised,
            unified_diff=patch,
            start_line=start_line,
            end_line=end_line,
            safety_notes=notes,
        )

    @staticmethod
    def _not_applicable(source: SourceFile, finding_id: str, rationale: str) -> RefactorPreview:
        return RefactorPreview(
            path=source.path,
            finding_id=finding_id,
            applicable=False,
            confidence="none",
            rationale=rationale,
            safety_notes=("The original source was not changed.",),
        )


def _apply_exact_replacement(
    content: str,
    replacement: str,
    properties: Mapping[object, object],
) -> str | None:
    start_line = _positive_int(properties.get("replacement_start_line"), 0)
    end_line = _positive_int(properties.get("replacement_end_line"), 0)
    start_column = _positive_int(properties.get("replacement_start_column"), 0)
    end_column = _positive_int(properties.get("replacement_end_column"), 0)
    if min(start_line, end_line, start_column, end_column) <= 0:
        return None
    start = _offset(content, start_line, start_column)
    end = _offset(content, end_line, end_column)
    if start is None or end is None or end < start:
        return None
    return f"{content[:start]}{replacement}{content[end:]}"


def _offset(content: str, line: int, column: int) -> int | None:
    lines = content.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        return None
    line_value = lines[line - 1]
    content_width = len(line_value.rstrip("\r\n"))
    if not 1 <= column <= content_width + 1:
        return None
    return sum(len(value) for value in lines[: line - 1]) + column - 1


def _replace_line(content: str, line_number: int, transform: Any) -> str | None:
    lines = content.splitlines(keepends=True)
    if not 1 <= line_number <= len(lines):
        return None
    original = lines[line_number - 1]
    ending = "\r\n" if original.endswith("\r\n") else "\n" if original.endswith("\n") else ""
    body = original[: -len(ending)] if ending else original
    lines[line_number - 1] = f"{transform(body)}{ending}"
    return "".join(lines)


def _replace_none_comparison(line: str) -> str:
    line = re.sub(r"\s*==\s*None\b", " is None", line, count=1)
    return re.sub(r"\s*!=\s*None\b", " is not None", line, count=1)


def _replace_mutable_default(content: str, line_number: int) -> str | None:
    lines = content.splitlines(keepends=True)
    if not 1 <= line_number <= len(lines):
        return None
    line = lines[line_number - 1]
    pattern = re.compile(
        r"(?P<name>[A-Za-z_]\w*)"
        r"(?P<annotation>\s*:\s*[^=,)]+)?"
        r"\s*=\s*(?P<default>\[\]|\{\}|set\(\))"
    )
    match = pattern.search(line)
    if match is None or not line.rstrip().endswith(":"):
        return None
    name = match.group("name")
    annotation = (match.group("annotation") or "").rstrip()
    if annotation and "None" not in annotation:
        annotation = f"{annotation} | None"
    replacement = f"{name}{annotation} = None"
    lines[line_number - 1] = f"{line[: match.start()]}{replacement}{line[match.end() :]}"
    function_indent = len(line) - len(line.lstrip(" "))
    body_indent = " " * (function_indent + 4)
    default_value = match.group("default")
    initialization = f"{body_indent}if {name} is None:\n{body_indent}    {name} = {default_value}\n"
    lines.insert(line_number, initialization)
    return "".join(lines)


def _add_http_timeout(line: str) -> str:
    call_span = _http_call_without_timeout(line)
    if call_span is None:
        return line
    opening_end, closing_start = call_span
    separator = ", " if line[opening_end:closing_start].strip() else ""
    return f"{line[:closing_start]}{separator}timeout=10.0{line[closing_start:]}"


_HTTP_CLIENTS = frozenset({"httpx", "requests"})
_HTTP_METHODS = frozenset({"delete", "get", "patch", "post", "put"})
_OPENING_DELIMITERS = {"(": ")", "[": "]", "{": "}"}
_CLOSING_DELIMITERS = frozenset(_OPENING_DELIMITERS.values())
_IGNORED_TOKEN_TYPES = frozenset(
    {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
)


def _http_call_without_timeout(line: str) -> tuple[int, int] | None:
    """Locate one literal HTTP client call whose own arguments omit ``timeout``."""

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (IndentationError, tokenize.TokenError):
        return None

    for index in range(len(tokens) - 3):
        client, dot, method, opening = tokens[index : index + 4]
        if not (
            client.type == tokenize.NAME
            and client.string in _HTTP_CLIENTS
            and dot.type == tokenize.OP
            and dot.string == "."
            and method.type == tokenize.NAME
            and method.string in _HTTP_METHODS
            and opening.type == tokenize.OP
            and opening.string == "("
        ):
            continue
        closing_index = _matching_delimiter(tokens, index + 3)
        if closing_index is None or _call_has_timeout(tokens, index + 3, closing_index):
            continue
        return opening.end[1], tokens[closing_index].start[1]
    return None


def _matching_delimiter(tokens: list[tokenize.TokenInfo], opening_index: int) -> int | None:
    stack: list[str] = []
    for index in range(opening_index, len(tokens)):
        token_value = tokens[index]
        if token_value.type != tokenize.OP:
            continue
        if token_value.string in _OPENING_DELIMITERS:
            stack.append(token_value.string)
            continue
        if token_value.string not in _CLOSING_DELIMITERS:
            continue
        if not stack or _OPENING_DELIMITERS[stack[-1]] != token_value.string:
            return None
        stack.pop()
        if not stack:
            return index
    return None


def _call_has_timeout(
    tokens: list[tokenize.TokenInfo], opening_index: int, closing_index: int
) -> bool:
    stack: list[str] = []
    for index in range(opening_index + 1, closing_index):
        token_value = tokens[index]
        if token_value.type == tokenize.OP and token_value.string in _OPENING_DELIMITERS:
            stack.append(token_value.string)
            continue
        if token_value.type == tokenize.OP and token_value.string in _CLOSING_DELIMITERS:
            if not stack or _OPENING_DELIMITERS[stack[-1]] != token_value.string:
                return False
            stack.pop()
            continue
        if stack or token_value.type != tokenize.NAME or token_value.string != "timeout":
            continue
        for following in tokens[index + 1 : closing_index]:
            if following.type in _IGNORED_TOKEN_TYPES:
                continue
            return following.type == tokenize.OP and following.string == "="
    return False


def _replace_loose_equality(line: str) -> str:
    return (
        re.sub(r"(?<![=!])==(?!=)", "===", line, count=1)
        if re.search(r"(?<![=!])==(?!=)", line)
        else re.sub(r"(?<![!])!=(?!=)", "!==", line, count=1)
    )


def _object_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    if is_dataclass(value) and not isinstance(value, type):
        dumped_dataclass: dict[str, Any] = asdict(cast(Any, value))
        return dumped_dataclass
    return {}


def _text(value: object, default: str) -> str:
    return value.strip()[:1_000] if isinstance(value, str) and value.strip() else default


def _positive_int(value: object, default: int) -> int:
    return value if isinstance(value, int) and value > 0 else default


__all__ = ["RefactorEngine", "RefactorPreview"]
