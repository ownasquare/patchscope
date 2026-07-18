# Extending PatchScope

PatchScope is designed to be extended through reviewed source changes with explicit tests. It does
not load third-party, uploaded, or repository-owned plugins at runtime; that boundary prevents
submitted code from becoming executable configuration.

Start with [CONTRIBUTING.md](../CONTRIBUTING.md), then choose the smallest extension point below.

## Add an analyzer

Analyzer contracts live in `src/patchscope/analyzers/base.py`. An adapter has a stable `name` and an
`analyze(files, root)` method that returns one explicit `AnalyzerRun`.

```python
from pathlib import Path

from patchscope.analyzers.base import (
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingSeverity,
)
from patchscope.intake import SourceFile


class ExampleAnalyzer:
    name = "example"

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun:
        del root
        findings: list[Finding] = []
        for source in files:
            if "unsafe_call(" in source.content:
                findings.append(
                    Finding(
                        id="example-unsafe-call",
                        analyzer=self.name,
                        rule_id="example.unsafe-call",
                        category=FindingCategory.SECURITY,
                        severity=FindingSeverity.HIGH,
                        message="Review this unsafe call.",
                        path=source.path,
                        start_line=1,
                        end_line=1,
                        snippet="unsafe_call(",
                        suggestion="Replace it with a bounded safe operation.",
                    )
                )
        return AnalyzerRun(
            analyzer=self.name,
            status=AnalyzerStatus.SUCCEEDED,
            findings=tuple(findings),
        )
```

Then:

1. Register the adapter in `src/patchscope/container.py` and, when it belongs in the default runner,
   `src/patchscope/analyzers/runner.py`.
2. Export it from `src/patchscope/analyzers/__init__.py` when it is part of the public source API.
3. Add focused tests under `tests/unit/analyzers/` for findings and every terminal state.
4. Update capability and user documentation when availability or language coverage changes.

An analyzer must return `UNAVAILABLE`, `NOT_APPLICABLE`, `TIMEOUT`, or `ERROR` when appropriate. It
must never turn a failed or missing tool into a successful empty scan. External tools must use the
bounded process runner in `analyzers/process.py`: fixed argument arrays, no shell, isolated
configuration, restricted environment inheritance, output limits, and a timeout.

## Add a deterministic rule

Built-in cross-language rules live in `src/patchscope/analyzers/heuristics.py`.

- Give each rule a stable, namespaced ID such as `patchscope.python.mutable-default`.
- Map it to an existing category and severity contract.
- Point to the smallest accurate source range and include exact evidence.
- Write a concrete remediation that does not claim a fix was validated by execution.
- Cover positive, negative, boundary, and false-positive cases in
  `tests/unit/analyzers/test_heuristics.py`.

Prefer a narrow high-confidence rule over a broad pattern that creates review noise.

## Add a refactor preview

Preview generation lives in `src/patchscope/refactor.py`. Refactors are conservative transformations,
not autonomous edits.

1. Match a stable analyzer rule ID.
2. Validate the declared path and source range.
3. Return no preview when syntax, bounds, or intent is ambiguous.
4. Produce a bounded unified diff without changing the submitted source.
5. Add positive, malformed-input, and no-change tests in `tests/unit/test_refactor.py`.

Every preview needs a rationale, confidence level, and safety notes telling the user what to verify.

## Change the review workflow

The fixed LangGraph flow lives in `src/patchscope/agent/workflow.py`, and its typed state lives in
`src/patchscope/agent/state.py`. Add a node only when it represents a distinct, testable review
stage; wire it explicitly, preserve the bounded `parse → analyze → synthesize → refactor → score`
contract, and add transition and failure tests under `tests/unit/agent/`. Model-produced content
must still pass the same path, range, and exact-evidence validation as deterministic findings.

## Add or improve language support

Accepted languages, display names, filenames, suffixes, and optional Tree-sitter grammar names live
in the shared registry at `src/patchscope/languages.py`. Add or update one `LanguageSpec` in
`LANGUAGE_REGISTRY`; intake, parsing, capabilities, and the workbench all consume that registry.

Add registry tests in `tests/unit/test_languages.py`, intake tests for accepted and skipped paths,
and parser tests for symbols, imports, malformed syntax, and fallback behavior. When a language has
no available grammar, leave `parser_name` unset and preserve the explicit fallback status. Do not
label fallback extraction as a successful Tree-sitter parse.

## Add an export format

Export serializers live in `src/patchscope/exports.py`. Keep them pure: they should transform a
persisted review without rerunning analysis or reading submitted files. Register a new format in the
service dispatch and capability response (`service.py`), the route allowlist (`api/routes.py`), the
UI client and Report view, and the API documentation. Cover serialization in
`tests/unit/test_exports.py`, content type and filename behavior in `tests/api/`, and download
readback in `tests/ui/`.

## Extend the API or workbench

FastAPI is the system of record; Streamlit is a client of its public contract.

For a public field or route, update these layers together:

1. domain and persistence models in `domain.py`, `database.py`, and `repository.py` as needed;
2. request and response models in `schemas.py`;
3. service behavior in `service.py` and routes under `api/`;
4. the client in `ui/client.py` before rendering it under `ui/`;
5. API, persistence, UI, and export tests;
6. [docs/api.md](api.md) and the root README when the user workflow changes.

Keep errors sanitized and stable. New mutation routes need durable readback proof, and new fields must
survive persistence rather than appearing only in the immediate response.

## Validate an extension

Run a focused test while iterating, then the complete local gate:

```bash
uv run pytest tests/unit/analyzers/test_heuristics.py -q
make verify
```

For UI changes, install Chromium, start PatchScope, and run Playwright:

```bash
uv run playwright install chromium
uv run patchscope start
# In another terminal:
uv run pytest -m e2e tests/e2e \
  --browser chromium \
  --base-url http://127.0.0.1:8501
```

Before opening a pull request, confirm that fixtures contain only synthetic source, analyzer status
is truthful, resource limits still fail closed, and documentation describes the public behavior
rather than internal implementation history.
