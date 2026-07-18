# Open-source readiness record

Date: 2026-07-18

Release target: `0.1.0`

## Outcome

PatchScope is prepared for an initial public open-source release as a local, single-user code review
workbench. A new contributor can install it from a checkout, start both services with one command,
run a credential-free example, understand the trust boundary, and follow documented extension
contracts.

This record describes the repository state. It does not claim that a GitHub release, hosted service,
private-repository integration, or provider-backed review has been published or validated.

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

## Local verification

| Layer | Result |
|---|---|
| Release gate | Ruff, strict mypy, offline tests, Bandit, dependency audit, lock check, build, and wheel-only launcher smoke passed |
| Automated tests | 278 passed, 2 opt-in tests deselected; 89.32% branch coverage |
| Browser E2E | 2 Playwright tests passed for review, refactor, triage, history readback, and mobile overflow |
| Manual browser review | Core flow checked at desktop and 390 x 844 mobile sizes; no new browser errors after service startup |
| Local launcher | API and workbench started together on alternate ports and stopped together with Ctrl+C |
| Container contract | Compose configuration, image build, and in-network API/workbench health checks passed |

## Remaining publication steps

The repository still needs a personal GitHub remote, hosted CI execution, and an optional `v0.1.0`
release. Provider-backed synthesis and authenticated GitHub reads remain opt-in integrations; the
credential-free offline path is the supported first-run experience.
