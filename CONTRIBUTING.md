# Contributing to PatchScope

Thanks for helping improve PatchScope. Focused bug fixes, documentation, tests, analyzers, rules,
and workflow improvements are welcome when they preserve evidence quality and safe, bounded
execution.

Please use the [support guide](SUPPORT.md) for questions, an issue template for bugs or proposals,
and a private GitHub Security Advisory for vulnerabilities. Participation is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Development setup

Use Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra e2e
uv run patchscope demo
uv run patchscope start
```

The workbench opens at `http://127.0.0.1:8501`, and the API reference is at
`http://127.0.0.1:8787/docs`.

For browser tests, install Chromium once:

```bash
uv run playwright install chromium
```

## Make a change

1. Open or choose an issue for non-trivial behavior changes so the intended contract is clear.
2. Write a focused failing test.
3. Implement the smallest complete behavior.
4. Run the focused test, then the project checks.
5. Update public documentation when behavior, configuration, or an API contract changes.

Keep source in `src/patchscope/` and tests in the matching `tests/` area. See
[docs/extending.md](docs/extending.md) before adding an analyzer, rule, parser mapping, refactor, or
public API field.

## Validate locally

```bash
make verify
```

That gate runs formatting, linting, strict typing, the offline test suite with branch coverage,
security and dependency audits, lock validation, package builds, and an isolated package smoke test.

Live integration checks are an explicit maintainer opt-in and never run as part of `make verify` or
normal CI. Put only the credentials you intend to test in an untracked `.env`, then run:

```bash
make test-live
```

The GitHub check defaults to the small public pull request at
`https://github.com/ownasquare/evalforge/pull/1`; override `LIVE_GITHUB_PR_URL` when another public
fixture is required. Each check skips when its `PATCHSCOPE_GITHUB_TOKEN` or
`PATCHSCOPE_OPENAI_API_KEY` is missing. A complete live acceptance result is exactly `2 passed`
with `0 skipped`. See the [live-integration record](docs/validation/2026-07-18-live-integrations.md)
for the proof boundary.

Browser E2E is separate. With `patchscope start` running in another terminal, use:

```bash
uv run pytest -m e2e tests/e2e \
  --browser chromium \
  --base-url http://127.0.0.1:8501
```

Never put real credentials, proprietary source, or private pull-request content in tests, fixtures,
issues, logs, or screenshots.

## Safety contract

Changes must not:

- execute imported source, repository hooks, builds, tests, plugins, or package managers;
- accept arbitrary remote URLs or follow unbounded redirects;
- present a missing, failed, or timed-out analyzer as a clean scan;
- let model output bypass path, line-range, and evidence validation;
- apply refactors automatically or hide that a change is preview-only;
- weaken file, archive, network, process, time, or output limits without documented justification.

If a proposal needs a different trust boundary, explain it in the issue before implementation.

## Pull requests

Keep each pull request focused. In the description, explain the user-visible outcome, the safety or
compatibility impact, and the exact validation performed. Complete the repository pull-request
template and respond to review feedback with new tests when a boundary condition is found.

Use concise imperative commit subjects such as `feat: add patch-only PR review` or
`fix: preserve finding triage on rerun`.
