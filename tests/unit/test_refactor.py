from __future__ import annotations

from dataclasses import dataclass

import pytest

from patchscope.intake import SourceFile
from patchscope.refactor import RefactorEngine


@dataclass
class _DataclassFinding:
    id: str
    path: str
    rule_id: str
    start_line: int


class _ModelFinding:
    def __init__(self, values: object) -> None:
        self.values = values

    def model_dump(self) -> object:
        return self.values


def test_bare_except_preview_is_non_mutating_and_reviewable() -> None:
    source = SourceFile.create(
        "src/app.py",
        "try:\n    run()\nexcept:\n    recover()\n",
        language_hint="python",
    )

    preview = RefactorEngine().preview(
        source,
        {
            "id": "finding-1",
            "path": source.path,
            "rule_id": "patchscope.python.bare-except",
            "start_line": 3,
            "end_line": 3,
        },
    )

    assert preview.applicable is True
    assert "except Exception:" in preview.revised
    assert "except:" in source.content
    assert "--- a/src/app.py" in preview.unified_diff
    assert preview.model_dump()["finding_id"] == "finding-1"


def test_explicit_safe_range_replacement_is_applied_only_to_preview() -> None:
    source = SourceFile.create("src/app.py", "value == None\n", language_hint="python")
    preview = RefactorEngine().preview(
        source,
        {
            "id": "ruff-1",
            "path": source.path,
            "rule_id": "E711",
            "start_line": 1,
            "end_line": 1,
            "autofix_safe": True,
            "suggested_replacement": " is None",
            "properties": {
                "replacement_start_line": 1,
                "replacement_end_line": 1,
                "replacement_start_column": 6,
                "replacement_end_column": 14,
            },
        },
    )

    assert preview.applicable is True
    assert preview.revised == "value is None\n"
    assert source.content == "value == None\n"


def test_unknown_or_patch_refactors_fail_closed() -> None:
    source = SourceFile.create("src/app.py", "eval(value)\n", language_hint="python")
    unknown = RefactorEngine().preview(
        source,
        {"id": "security", "path": source.path, "rule_id": "dynamic-execution", "start_line": 1},
    )
    patch = SourceFile.create("change.patch", "+eval(value)\n", is_patch=True)
    patch_preview = RefactorEngine().preview(
        patch,
        {"id": "patch", "path": patch.path, "rule_id": "dynamic-execution", "start_line": 1},
    )

    assert unknown.applicable is False
    assert unknown.original == ""
    assert patch_preview.applicable is False


def test_mutable_default_preview_creates_a_fresh_value_per_call() -> None:
    source = SourceFile.create(
        "src/cart.py",
        "def total(discounts: list[float] = []) -> float:\n    return sum(discounts)\n",
        language_hint="python",
    )

    preview = RefactorEngine().preview(
        source,
        {
            "id": "mutable-default",
            "path": source.path,
            "rule_id": "patchscope.python.mutable-default",
            "start_line": 1,
        },
    )

    assert preview.applicable is True
    assert "discounts: list[float] | None = None" in preview.revised
    assert "if discounts is None:" in preview.revised
    assert "discounts = []" in preview.revised
    assert source.content.startswith("def total(discounts: list[float] = [])")


def test_network_timeout_preview_is_bounded_and_non_mutating() -> None:
    source = SourceFile.create(
        "src/client.py",
        'response = httpx.get(f"https://example.test/{region}")\n',
        language_hint="python",
    )

    preview = RefactorEngine().preview(
        source,
        {
            "id": "network-timeout",
            "path": source.path,
            "rule_id": "patchscope.python.network-timeout",
            "start_line": 1,
        },
    )

    assert preview.applicable is True
    assert "timeout=10.0" in preview.revised
    assert "timeout" not in source.content


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            "payload = json.loads(requests.get(url).text)\n",
            "payload = json.loads(requests.get(url, timeout=10.0).text)\n",
        ),
        (
            "payload = requests.get(build_url(region)).json()\n",
            "payload = requests.get(build_url(region), timeout=10.0).json()\n",
        ),
        (
            "payload = retry(httpx.post(url, json={'items': [build()]}), timeout=2)\n",
            (
                "payload = retry(httpx.post(url, json={'items': [build()]}, "
                "timeout=10.0), timeout=2)\n"
            ),
        ),
    ],
)
def test_network_timeout_refactor_targets_the_http_call_closing_delimiter(
    expression: str,
    expected: str,
) -> None:
    source = SourceFile.create("client.py", expression, language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "nested-network-timeout",
            "path": source.path,
            "rule_id": "patchscope.python.network-timeout",
            "start_line": 1,
        },
    )

    assert preview.applicable is True
    assert preview.revised == expected
    compile(preview.revised, source.path, "exec")


def test_engine_rejects_invalid_limits_mismatched_paths_and_oversized_sources() -> None:
    with pytest.raises(ValueError, match="limits"):
        RefactorEngine(max_diff_chars=0)

    source = SourceFile.create("src/app.py", "pass\n", language_hint="python")
    mismatch = RefactorEngine().preview(
        source,
        {"id": "other", "path": "src/other.py", "rule_id": "W292"},
    )
    oversized = RefactorEngine(max_source_bytes=2).preview(
        source,
        {"id": "large", "path": source.path, "rule_id": "W292"},
    )

    assert mismatch.applicable is False
    assert "does not belong" in mismatch.rationale
    assert oversized.applicable is False
    assert "size" in oversized.rationale


def test_whitespace_and_final_newline_refactors_handle_line_endings_and_noops() -> None:
    whitespace = SourceFile.create("app.py", "value = 1  \r\n", language_hint="python")
    missing_newline = SourceFile.create("other.py", "value = 2", language_hint="python")
    complete = SourceFile.create("complete.py", "value = 3\n", language_hint="python")

    whitespace_preview = RefactorEngine().preview(
        whitespace,
        {"id": "w291", "path": whitespace.path, "rule_id": "W291", "start_line": 1},
    )
    newline_preview = RefactorEngine().preview(
        missing_newline,
        {"id": "w292", "path": missing_newline.path, "rule_id": "W292"},
    )
    noop = RefactorEngine().preview(
        complete,
        {"id": "noop", "path": complete.path, "rule_id": "W292"},
    )

    assert whitespace_preview.revised == "value = 1\r\n"
    assert whitespace_preview.confidence == "high"
    assert newline_preview.revised.endswith("\n")
    assert noop.applicable is False


@pytest.mark.parametrize(
    ("expression", "replacement"),
    [("value == None\n", "value is None\n"), ("value != None\n", "value is not None\n")],
)
def test_none_comparison_refactors_both_operators(expression: str, replacement: str) -> None:
    source = SourceFile.create("app.py", expression, language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {"id": "e711", "path": source.path, "rule_id": "E711", "start_line": 1},
    )

    assert preview.revised == replacement


def test_invalid_lines_or_already_narrow_exception_fail_closed() -> None:
    source = SourceFile.create("app.py", "except Exception:\n    pass\n", language_hint="python")

    invalid_line = RefactorEngine().preview(
        source,
        {"id": "line", "path": source.path, "rule_id": "W291", "start_line": 20},
    )
    already_narrow = RefactorEngine().preview(
        source,
        {
            "id": "except",
            "path": source.path,
            "rule_id": "patchscope.python.bare-except",
            "start_line": 1,
        },
    )

    assert invalid_line.applicable is False
    assert already_narrow.applicable is False


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        ("def collect(items={}):\n    return items\n", "items = None"),
        (
            "def collect(items: set[str] | None = set()):\n    return items\n",
            "items: set[str] | None = None",
        ),
    ],
)
def test_mutable_default_refactor_supports_unannotated_and_optional_defaults(
    signature: str,
    expected: str,
) -> None:
    source = SourceFile.create("app.py", signature, language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "mutable",
            "path": source.path,
            "rule_id": "patchscope.python.mutable-default",
            "start_line": 1,
        },
    )

    assert preview.applicable is True
    assert expected in preview.revised
    assert "if items is None:" in preview.revised


def test_mutable_default_refactor_preserves_method_indentation_and_rejects_non_signature() -> None:
    source = SourceFile.create(
        "app.py",
        "class Cart:\n    def add(self, items=[]):\n        return items\n",
        language_hint="python",
    )
    invalid = SourceFile.create("invalid.py", "items = []\n", language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "mutable",
            "path": source.path,
            "rule_id": "patchscope.python.mutable-default",
            "start_line": 2,
        },
    )
    unavailable = RefactorEngine().preview(
        invalid,
        {
            "id": "invalid",
            "path": invalid.path,
            "rule_id": "patchscope.python.mutable-default",
            "start_line": 1,
        },
    )

    assert "        if items is None:\n            items = []" in preview.revised
    assert unavailable.applicable is False


def test_network_timeout_refactor_leaves_existing_timeout_and_unknown_client_unchanged() -> None:
    configured = SourceFile.create(
        "client.py",
        'requests.post("https://example.test", timeout=2)\n',
        language_hint="python",
    )
    unsupported = SourceFile.create(
        "other.py",
        'client.get("https://example.test")\n',
        language_hint="python",
    )

    configured_preview = RefactorEngine().preview(
        configured,
        {
            "id": "configured",
            "path": configured.path,
            "rule_id": "patchscope.python.network-timeout",
            "start_line": 1,
        },
    )
    unsupported_preview = RefactorEngine().preview(
        unsupported,
        {
            "id": "unsupported",
            "path": unsupported.path,
            "rule_id": "patchscope.python.network-timeout",
            "start_line": 1,
        },
    )

    assert configured_preview.applicable is False
    assert unsupported_preview.applicable is False


@pytest.mark.parametrize(
    ("expression", "replacement"),
    [("left == right;\n", "left === right;\n"), ("left != right;\n", "left !== right;\n")],
)
def test_javascript_loose_equality_refactor_covers_both_operators(
    expression: str,
    replacement: str,
) -> None:
    source = SourceFile.create("app.js", expression, language_hint="javascript")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "equality",
            "path": source.path,
            "rule_id": "patchscope.javascript.loose-equality",
            "start_line": 1,
        },
    )

    assert preview.revised == replacement
    assert preview.confidence == "low"


def test_strict_equality_is_not_rewritten_again() -> None:
    source = SourceFile.create("app.js", "left === right;\n", language_hint="javascript")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "strict",
            "path": source.path,
            "rule_id": "patchscope.javascript.loose-equality",
            "start_line": 1,
        },
    )

    assert preview.applicable is False


def test_exact_replacement_fails_closed_for_missing_reversed_or_out_of_range_coordinates() -> None:
    source = SourceFile.create("app.py", "first\nsecond\n", language_hint="python")
    coordinate_sets = [
        {},
        {
            "replacement_start_line": 1,
            "replacement_end_line": 1,
            "replacement_start_column": 99,
            "replacement_end_column": 100,
        },
        {
            "replacement_start_line": 2,
            "replacement_end_line": 1,
            "replacement_start_column": 3,
            "replacement_end_column": 2,
        },
    ]

    for index, properties in enumerate(coordinate_sets):
        preview = RefactorEngine().preview(
            source,
            {
                "id": f"invalid-{index}",
                "path": source.path,
                "rule_id": "unknown",
                "autofix_safe": True,
                "suggested_replacement": "changed",
                "properties": properties,
            },
        )
        assert preview.applicable is False


def test_multiline_exact_replacement_uses_one_based_coordinates() -> None:
    source = SourceFile.create("app.py", "first\nsecond\nthird\n", language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "safe",
            "path": source.path,
            "rule_id": "external.safe",
            "start_line": 2,
            "end_line": 2,
            "autofix_safe": True,
            "suggested_replacement": "SECOND",
            "properties": {
                "replacement_start_line": 2,
                "replacement_end_line": 2,
                "replacement_start_column": 1,
                "replacement_end_column": 7,
            },
        },
    )

    assert preview.applicable is True
    assert preview.revised == "first\nSECOND\nthird\n"


def test_safe_replacement_with_non_mapping_properties_can_use_rule_fallback() -> None:
    source = SourceFile.create("app.py", "pass", language_hint="python")

    preview = RefactorEngine().preview(
        source,
        {
            "id": "fallback",
            "path": source.path,
            "rule_id": "W292",
            "autofix_safe": True,
            "suggested_replacement": "ignored",
            "properties": [],
        },
    )

    assert preview.revised == "pass\n"


def test_preview_accepts_dataclass_and_model_dump_findings_but_rejects_invalid_dump() -> None:
    source = SourceFile.create("app.py", "value = 1  \n", language_hint="python")
    dataclass_preview = RefactorEngine().preview(
        source,
        _DataclassFinding("dataclass", source.path, "W291", 1),
    )
    model_preview = RefactorEngine().preview(
        source,
        _ModelFinding({"id": "model", "path": source.path, "rule_id": "W291", "start_line": 1}),
    )
    invalid_dump = RefactorEngine().preview(source, _ModelFinding([]))

    assert dataclass_preview.applicable is True
    assert model_preview.applicable is True
    assert invalid_dump.finding_id == "finding"
    assert invalid_dump.applicable is False


def test_diff_limit_prevents_returning_an_unbounded_preview() -> None:
    source = SourceFile.create("app.py", "value = 1", language_hint="python")

    preview = RefactorEngine(max_diff_chars=1).preview(
        source,
        {"id": "diff", "path": source.path, "rule_id": "W292"},
    )

    assert preview.applicable is False
    assert "diff size" in preview.rationale
