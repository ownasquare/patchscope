# Live-integration acceptance

Date: 2026-07-18

## Contract

PatchScope's release gate and normal CI are deliberately credential-free. Maintainers may run the
separate opt-in gate with:

```bash
make test-live
```

The command checks authenticated GitHub pull-request intake against
`https://github.com/ownasquare/evalforge/pull/1` and OpenAI-backed structured synthesis. The tests
load `PATCHSCOPE_GITHUB_TOKEN` and `PATCHSCOPE_OPENAI_API_KEY` through PatchScope settings, fail the
relevant check before its network call when a credential is absent, and must never print either
value. Full acceptance is exactly `2 passed` and `0 skipped`.

## Current boundary

| Check | State |
|---|---|
| Local `make verify` | Passed without live credentials: `313 passed`, `4 deselected`, 89.36% coverage, plus lint, typing, audit, lock, build, and isolated wheel smoke |
| Browser E2E | Passed locally against the combined launcher: `2 passed in 5.31s` |
| PostgreSQL extra | Driver installation and import passed; no live database lifecycle is claimed |
| Missing-credential guard | Opted-in gate failed before network calls with both required setting names, as designed |
| Authenticated GitHub fixture | Passed locally against bounded `ownasquare/evalforge` pull request 1: `1 passed in 2.71s` |
| OpenAI model contract | Official model documentation lists `gpt-5-mini` and Structured Outputs as supported |
| OpenAI synthesis | Pending because no `PATCHSCOPE_OPENAI_API_KEY` is available |
| Complete live acceptance | Not yet claimed; requires `2 passed`, `0 skipped` in one opted-in run |

A missing credential must fail an explicitly opted-in acceptance run. Keep credentials in local
secret storage or an untracked `.env`; never add them to fixtures, logs, screenshots, commits, or
pull-request workflows.

The GitHub test used the active `ownasquare` CLI credential only in the child process environment;
the token was neither printed nor written to disk. OpenAI remains a separate unrun proof layer until
`PATCHSCOPE_OPENAI_API_KEY` is supplied locally. The model compatibility check used the
[official GPT-5 mini model reference](https://developers.openai.com/api/docs/models/gpt-5-mini);
it is not a substitute for a request made with the maintainer's API account.
