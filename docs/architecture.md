# PatchScope architecture

## Product boundary

PatchScope is a review workstation. It does not provide repository chat, semantic search, autonomous code writes, test execution, or CI orchestration. Its job is to convert bounded source changes into review evidence that a developer can inspect, triage, persist, and export.

## Components

### Intake

`intake.py` validates UTF-8 source and ZIP archives before analysis. It enforces repository-relative paths, rejects symlinks and sensitive files, bounds compressed and expanded bytes, limits compression ratios, ignores dependency/build directories, and records skipped paths. `github.py` accepts only canonical GitHub PR identities and calls fixed REST endpoints.

### Parsing

`languages.py` is the canonical registry for accepted suffixes, named files, display labels, and optional Tree-sitter grammar names. `parsing.py` consumes that registry and extracts symbols, imports, syntax errors, line counts, and truncation state. Unsupported or unavailable grammars fall back to deterministic symbol extraction with an explicit status. Patch-only inputs are labeled partial.

### Analysis

Analyzer adapters share serialization-friendly `Finding` and `AnalyzerRun` contracts. The built-in heuristic analyzer covers high-signal cross-language patterns. Ruff and mypy run only on Python sources. Semgrep is optional. External tools run in a temporary directory with fixed arguments, isolated configuration, no shell, bounded output, restricted inherited environment, and a deadline.

### Agent workflow

The graph is intentionally fixed:

```mermaid
flowchart LR
    A["Parse source"] --> B["Run analyzers"]
    B --> C["Normalize and synthesize"]
    C --> D["Build refactor previews"]
    D --> E["Score review"]
```

The offline synthesizer deduplicates findings and preserves provenance. In OpenAI mode, LangChain requests a Pydantic structure within one total prompt-character ceiling and an explicit completion-token ceiling. Metadata records deterministic section truncation; the complete local static finding set is preserved outside the provider prompt. A model finding is accepted only when its path exists, its line range is valid, and its evidence appears inside that declared source range. GitHub PR findings are additionally restricted to added target-file lines from the unified patch. `auto` mode falls back to the deterministic set without deleting it.

### Persistence

FastAPI is the system of record. SQLAlchemy stores reviews, immutable input snapshots, findings, analyzer runs, summary state, AI provenance, and triage. SQLite uses WAL and foreign keys by default. Request and finding fingerprints make reruns idempotent and preserve human triage.

#### Optional PostgreSQL storage

PostgreSQL is a checkout-only evaluation option, not a Compose or production-readiness claim. Install
its driver and set the server-side URL before starting PatchScope:

```bash
uv sync --extra postgres
PATCHSCOPE_DATABASE_URL_OVERRIDE=postgresql://user:password@localhost/patchscope \
  uv run patchscope start
```

For a standard virtual environment, install the current checkout with
`python -m pip install '.[postgres]'`. PatchScope creates its current schema in an empty database at
startup. It does not provide schema migrations, upgrade orchestration, high availability, tenant
isolation, or a live PostgreSQL acceptance suite. The included Docker Compose image does not install
the PostgreSQL extra and intentionally remains on the default SQLite backend.

### Presentation

Streamlit is a client of the public API, not a second business-logic process. The workbench uses New review and Reviews as its primary routes. Review detail presents a summary, filterable findings, code evidence, unified refactor diffs, finding-status actions, exports, and collapsed technical details.

## Failure semantics

- A parser failure produces a labeled fallback summary.
- A missing external analyzer produces `unavailable`.
- A deadline produces `timed_out`.
- Nonzero or malformed analyzer output produces `failed` or `degraded`, depending on whether trusted findings were recovered.
- An `auto` AI failure produces `offline` metadata plus a bounded fallback reason.
- A forced provider failure leaves the review failed rather than claiming completion.
- A review failure is persisted with a sanitized error type and no source or secret disclosure.

## Trust boundaries

| Boundary | Trusted | Untrusted |
|---|---|---|
| Browser to API | Route/schema definitions | Form values and uploads |
| GitHub intake | Fixed `api.github.com` host and local limits | PR metadata, paths, patches, source |
| Analyzer runner | Fixed executable and arguments | Source and analyzer output |
| Model synthesis | Pydantic response schema and local evidence validator | Provider output |
| Persistence | Domain validation and repository transaction | Serialized external findings |

## Scalability path

The synchronous local workflow is appropriate for portfolio and single-user use. A production multi-user deployment can move graph execution to a worker queue, use PostgreSQL, store source snapshots in object storage, add authenticated tenant boundaries, and run analyzers inside a hardened network-disabled sandbox without changing the API or domain contracts.
