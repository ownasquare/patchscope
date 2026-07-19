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
load `PATCHSCOPE_GITHUB_TOKEN` and `PATCHSCOPE_OPENAI_API_KEY` through PatchScope settings, skip only
the integration whose credential is absent, and must never print either value. Full acceptance is
exactly `2 passed` and `0 skipped`.

## Current boundary

| Check | State |
|---|---|
| Normal `make verify` and hosted CI | Passed without live credentials |
| GitHub CLI account health | `ownasquare` and `beladed-sites` sessions both validated; no credential repair is needed |
| Authenticated GitHub fixture | Passed locally against bounded `ownasquare/evalforge` pull request 1: `1 passed in 2.68s` |
| OpenAI model contract | Official model documentation lists `gpt-5-mini` and Structured Outputs as supported |
| OpenAI synthesis | Pending because no `PATCHSCOPE_OPENAI_API_KEY` is available |
| Complete live acceptance | Not yet claimed; requires `2 passed`, `0 skipped` in one opted-in run |

GitHub CLI session health does not substitute for the PatchScope live test, and a skipped provider
test does not count as acceptance. Keep credentials in local secret storage or an untracked `.env`;
never add them to fixtures, logs, screenshots, commits, or pull-request workflows.

The GitHub test used the active `ownasquare` CLI credential only in the child process environment;
the token was neither printed nor written to disk. OpenAI remains a separate unrun proof layer until
`PATCHSCOPE_OPENAI_API_KEY` is supplied locally. The model compatibility check used the
[official GPT-5 mini model reference](https://developers.openai.com/api/docs/models/gpt-5-mini);
it is not a substitute for a request made with the maintainer's API account.
