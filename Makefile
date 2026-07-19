LIVE_GITHUB_PR_URL ?= https://github.com/ownasquare/evalforge/pull/1
PACKAGE_VERSION := $(shell uv version --short)
PACKAGE_WHEEL := ./dist/patchscope-$(PACKAGE_VERSION)-py3-none-any.whl

.PHONY: sync format lint type test test-live audit lock build package-smoke verify dev-api dev-ui demo clean

sync:
	uv sync --extra e2e

format:
	uv run ruff format .

lint:
	uv run ruff format --check .
	uv run ruff check .

type:
	uv run mypy src/patchscope

test:
	uv run pytest -m "not e2e and not live" --cov=patchscope --cov-branch --cov-report=term-missing

test-live:
	uv run pytest -q -m live tests/live --force-enable-socket --live-github-pr-url "$(LIVE_GITHUB_PR_URL)"

audit:
	uv run bandit -q -r src/patchscope
	uv run pip-audit

lock:
	uv lock --check

build:
	uv build

package-smoke: build
	uv run --isolated --no-project --refresh-package patchscope --with $(PACKAGE_WHEEL) patchscope start --help

verify: lint type test audit lock build package-smoke

dev-api:
	uv run patchscope serve --host 127.0.0.1 --port 8787 --reload

dev-ui:
	uv run patchscope ui --host 127.0.0.1 --port 8501

demo:
	uv run patchscope demo

clean:
	uv run python -c "from pathlib import Path; [p.unlink() for p in Path('dist').glob('*') if p.is_file()]"
