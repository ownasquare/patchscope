from __future__ import annotations

import json

from patchscope.analyzers.base import AnalyzerStatus
from patchscope.analyzers.mypy import MypyAnalyzer
from patchscope.analyzers.process import ProcessResult
from patchscope.analyzers.ruff import RuffAnalyzer
from patchscope.analyzers.semgrep import SemgrepAnalyzer
from patchscope.intake import SourceFile, materialize_sources


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls = []

    def run(self, executable, arguments, *, cwd, timeout_seconds):
        self.calls.append((executable, arguments, cwd, timeout_seconds))
        return self.result


def test_ruff_adapter_normalizes_json_and_uses_isolated_arguments(tmp_path) -> None:
    source = SourceFile.create("app.py", "print(missing)\n", language_hint="python")
    materialize_sources([source], tmp_path)
    payload = [
        {
            "code": "F821",
            "filename": str(tmp_path / "app.py"),
            "location": {"row": 1, "column": 7},
            "end_location": {"row": 1, "column": 14},
            "message": "Undefined name `missing`",
            "fix": None,
        }
    ]
    runner = FakeRunner(
        ProcessResult(AnalyzerStatus.SUCCEEDED, ("ruff",), 4, json.dumps(payload), exit_code=0)
    )

    run = RuffAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert run.findings[0].rule_id == "F821"
    assert "--isolated" in runner.calls[0][1]
    assert "--exit-zero" in runner.calls[0][1]


def test_mypy_adapter_accepts_type_error_exit_and_ignores_project_config(tmp_path) -> None:
    source = SourceFile.create("app.py", "value: int = 'bad'\n", language_hint="python")
    materialize_sources([source], tmp_path)
    output = f"{tmp_path / 'app.py'}:1:14: error: Incompatible types [assignment]\n"
    runner = FakeRunner(ProcessResult(AnalyzerStatus.SUCCEEDED, ("mypy",), 5, output, exit_code=1))

    run = MypyAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert run.findings[0].rule_id == "assignment"
    assert "--no-site-packages" in runner.calls[0][1]
    assert "--follow-imports" in runner.calls[0][1]


def test_semgrep_adapter_normalizes_results_and_disables_network_metrics(tmp_path) -> None:
    source = SourceFile.create("app.py", "eval(value)\n", language_hint="python")
    materialize_sources([source], tmp_path)
    payload = {
        "results": [
            {
                "check_id": "patchscope.semgrep.python-dynamic-execution",
                "path": str(tmp_path / "app.py"),
                "start": {"line": 1, "col": 1},
                "end": {"line": 1, "col": 12},
                "extra": {
                    "message": "Dynamic execution",
                    "severity": "ERROR",
                    "metadata": {"category": "security", "confidence": "high"},
                },
            }
        ],
        "errors": [],
    }
    runner = FakeRunner(
        ProcessResult(AnalyzerStatus.SUCCEEDED, ("semgrep",), 8, json.dumps(payload), exit_code=0)
    )

    run = SemgrepAnalyzer(runner=runner).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.SUCCEEDED
    assert run.findings[0].category.value == "security"
    assert "--metrics=off" in runner.calls[0][1]
    assert "--disable-version-check" in runner.calls[0][1]


def test_adapter_preserves_explicit_unavailable_state(tmp_path) -> None:
    source = SourceFile.create("app.py", "pass\n", language_hint="python")
    result = ProcessResult(AnalyzerStatus.UNAVAILABLE, ("ruff",), 0, message="Unavailable")

    run = RuffAnalyzer(runner=FakeRunner(result)).analyze([source], tmp_path)

    assert run.status is AnalyzerStatus.UNAVAILABLE
    assert run.findings == ()
