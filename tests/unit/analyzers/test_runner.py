from __future__ import annotations

from patchscope.analyzers.base import AnalyzerRun, AnalyzerStatus
from patchscope.analyzers.runner import AnalyzerRunner
from patchscope.intake import SourceFile


class SuccessfulAnalyzer:
    name = "successful"

    def analyze(self, files, root):
        assert (root / files[0].path).is_file()
        return AnalyzerRun(self.name, AnalyzerStatus.SUCCEEDED)


class FailingAnalyzer:
    name = "failing"

    def analyze(self, files, root):
        raise RuntimeError("source-controlled detail must not escape")


def test_runner_stages_once_and_isolates_analyzer_failures() -> None:
    source = SourceFile.create("app.py", "pass\n", language_hint="python")

    runs = AnalyzerRunner([SuccessfulAnalyzer(), FailingAnalyzer()]).analyze([source])

    assert [run.analyzer for run in runs] == ["successful", "failing"]
    assert [run.status for run in runs] == [AnalyzerStatus.SUCCEEDED, AnalyzerStatus.ERROR]
    assert "source-controlled" not in runs[1].message
