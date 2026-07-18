# PatchScope support

PatchScope is an open-source, local-first project maintained on a best-effort basis. The fastest
path to help depends on what you need.

## Choose the right channel

| Need | Where to go |
|---|---|
| Installation or usage question | Open a **Question** issue |
| Reproducible defect | Open a **Bug report** issue |
| Product or extension proposal | Open a **Feature request** issue |
| Vulnerability or sensitive security concern | Follow [SECURITY.md](SECURITY.md); never use a public issue |
| Contribution help | Read [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/extending.md](docs/extending.md) |

Search existing issues before opening a new one. Keep examples minimal and use synthetic source;
public issues are not a safe place for proprietary code, access tokens, database contents, or
private pull-request details.

## Include useful diagnostics

For a bug or setup question, include:

- PatchScope version or commit;
- operating system, Python version, and install method (`uv`, virtual environment, or Docker);
- input type (paste, file, ZIP, or public PR) without attaching sensitive source;
- exact steps and the sanitized error shown by PatchScope;
- analyzer availability from `patchscope analyzers`.

Do not post `.env`, tokens, full database files, raw private source, or unredacted logs.

## Quick checks

- **Workbench cannot reach the API:** start both services with `uv run patchscope start`, then open
  `http://127.0.0.1:8501`.
- **A port is already in use:** stop the conflicting local process or run
  `uv run patchscope start --help` to see port overrides.
- **An analyzer is unavailable:** run `uv run patchscope analyzers`. Ruff and mypy are Python-only;
  Semgrep is optional.
- **A public PR is rate-limited:** wait for GitHub's public limit to reset or configure a scoped
  server-side `PATCHSCOPE_GITHUB_TOKEN`.
- **Docker services are stale:** run `docker compose down`, then `docker compose up --build`.

The included service is a single-user local reference. Public hosting, tenant isolation, custom
deployment infrastructure, and private-repository integrations are outside the supported `0.1.x`
contract.
