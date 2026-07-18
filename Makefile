.PHONY: sync format lint type test audit lock build package-smoke verify dev-api dev-ui demo clean

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

audit:
	uv run bandit -q -r src/patchscope
	uv run pip-audit

lock:
	uv lock --check

build:
	uv build

package-smoke:
	uv run --isolated --no-project --refresh-package patchscope --with ./dist/patchscope-0.1.0-py3-none-any.whl patchscope start --help

verify: lint type test audit lock build package-smoke

dev-api:
	uv run patchscope serve --host 127.0.0.1 --port 8787 --reload

dev-ui:
	uv run patchscope ui --host 127.0.0.1 --port 8501

demo:
	uv run patchscope demo

clean:
	uv run python -c "from pathlib import Path; [p.unlink() for p in Path('dist').glob('*') if p.is_file()]"
