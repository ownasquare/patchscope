# PatchScope security design

## Principle: analyze, never execute

PatchScope reads source as data. It does not import Python files, evaluate JavaScript, load project plugins, run builds/tests/hooks, invoke package managers, or accept repository-owned analyzer configuration. Refactors are string-level previews and are never written back to the submitted source.

## Input controls

- Repository-relative Unicode-normalized paths only
- Traversal, absolute path, backslash ambiguity, control characters, and duplicates rejected
- Symlinks and special ZIP entries rejected
- Sensitive filenames and key/certificate suffixes skipped
- UTF-8 text only; NUL/binary data rejected
- File, archive, expanded-byte, entry-count, path-length, and compression-ratio limits
- Dependency, VCS, build, and generated directories skipped

## Network controls

GitHub input is parsed into owner, repository, and integer PR number. PatchScope does not fetch the submitted URL. It calls only fixed `https://api.github.com/repos/...` paths with redirects disabled, short timeouts, bounded retries, and response caps. Tokens stay server-side.

## Analyzer controls

- Analyzer subprocesses receive fixed argument arrays with `shell=False`; user source never
  becomes an executable name or command argument
- Each review uses a fresh bounded temporary directory
- Project analyzer configuration and plugins are disabled
- Environment inheritance is reduced to an explicit safe allowlist
- Commands have deadlines and output limits
- Imported source is never marked executable
- Missing, timed-out, failed, and malformed analyzers are distinguished from clean scans

Semgrep is installed as an isolated `uv tool`, rather than as an application extra. This keeps
its CLI dependency graph from constraining or weakening PatchScope's web and CLI dependencies.
The two targeted Bandit `nosec` annotations cover only these reviewed fixed-argument launch
sites; subprocess findings remain enabled everywhere else.

These controls reduce risk but are not a kernel sandbox. A production service that accepts arbitrary public uploads should execute analyzers in a separate, patched, non-root, network-disabled container or microVM with CPU, memory, process, and filesystem quotas.

## AI controls

Provider use is optional. API keys are server-only secret settings. Source sent to a provider is bounded. Structured model output is validated against known paths, valid line ranges, and exact source evidence. Model output cannot erase deterministic findings or claim source execution. Provider errors in `auto` mode are recorded as fallback metadata.

## Storage and privacy

The default SQLite database stores source snapshots to support evidence readback and history. Treat `.data/patchscope.db` as sensitive. It is ignored by Git, stored in a named Docker volume, and should be encrypted or moved to an approved database for shared deployments. PatchScope performs no telemetry by default.

## Deployment boundary

The included Compose stack is a hardened local reference, not a public production deployment. Public hosting additionally requires authentication, authorization, tenant isolation, rate limiting, TLS, CSRF review, audit retention, source encryption, sandboxed analyzers, backups, and a formal incident process.
