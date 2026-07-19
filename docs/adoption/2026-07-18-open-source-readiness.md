# Open-source readiness record

Date: 2026-07-18

Release target: `0.1.1`

## Outcome

PatchScope is published at [github.com/ownasquare/patchscope](https://github.com/ownasquare/patchscope)
as a public, local-first code review workbench. A new contributor can install it from a checkout,
start both services with one command, run a credential-free example, understand the trust boundary,
and follow documented extension contracts.

The `v0.1.1` release record defines a wheel, source archive, and checksum manifest built from one
verified release commit. GitHub Releases are PatchScope's supported distribution channel.
This record does not claim that a hosted application, private-repository integration, or
provider-backed review has been published or validated.

## Adoption path

1. `uv sync`
2. `uv run patchscope start`
3. Open the workbench, choose **Load example review**, and select **Run review**.

The first result replaces the intake screen and leads with the decision, risk, findings, evidence,
and preview-only refactors. Optional names, filters, analyzer details, and exports remain available
without competing with the primary workflow.

## Public project surface

- Outcome-first README with uv, standard virtual-environment, and Docker paths
- One-command API and workbench launcher with clean shared shutdown
- Results-first desktop and mobile workbench with concise contextual help
- Shared typed language registry used by intake, parsing, GitHub filtering, and the UI
- Extension guide for analyzers, rules, languages, refactors, workflow stages, exports, and API fields
- Contribution, support, security, conduct, issue-template, and pull-request guidance
- Explicit local-only, credential, source-execution, and preview-only boundaries
- Fail-closed public-repository intake before GitHub file or source requests
- Auditable whole-prompt and completion limits that preserve deterministic findings
- Minimal launcher child environments, disabled model tracing, and offline tests with TCP blocked

## Verification

| Layer | Result |
|---|---|
| Release gate | Ruff, strict mypy, offline tests, Bandit, dependency audit, lock check, build, and wheel-only launcher smoke passed |
| Automated tests | 313 passed, 4 separately opted-in tests deselected; 89.36% branch coverage |
| Browser E2E | 2 Playwright tests passed for review, refactor, triage, history readback, and mobile overflow |
| Manual browser review | Core flow checked at desktop and 390 x 844 mobile sizes; no new browser errors after service startup |
| Local launcher | API and workbench started together on alternate ports and stopped together with Ctrl+C |
| Container contract | Compose configuration, image build, and in-network API/workbench health checks passed |
| Hosted CI | Public GitHub Actions passed verification, container, and Playwright E2E jobs on `main` |
| Optional live integrations | Authenticated GitHub passed; OpenAI remains pending; full acceptance requires exactly 2 passed and 0 skipped |
| Optional PostgreSQL | Driver extra installs and imports; no live database lifecycle or migration support is claimed |

## Live-integration follow-up

`make verify` and normal CI remain offline and credential-free. Maintainers can opt into
`make test-live`, which uses `https://github.com/ownasquare/evalforge/pull/1` as its bounded GitHub
fixture. PatchScope's authenticated GitHub test passed with its credential held only in the child
process environment. OpenAI acceptance remains pending because no
`PATCHSCOPE_OPENAI_API_KEY` is available. See the
[live-integration acceptance record](../validation/2026-07-18-live-integrations.md).

## Publication state

The repository is public, `main` tracks the personal GitHub remote, hosted CI has passed, and
PatchScope is pinned on the `ownasquare` profile. `v0.1.1` is the prepared patch target; PatchScope
is not published to PyPI. Provider-backed synthesis and authenticated GitHub reads remain opt-in
integrations, while the credential-free offline path is the supported first-run experience. The
prior `v0.1.0` release remains available for reproducible installs.
