"""Deterministic orchestration for local static analyzers."""

from __future__ import annotations

import time
from collections.abc import Iterable

from patchscope.analyzers.base import AnalyzerAdapter, AnalyzerRun, AnalyzerStatus
from patchscope.analyzers.heuristics import HeuristicAnalyzer
from patchscope.analyzers.mypy import MypyAnalyzer
from patchscope.analyzers.ruff import RuffAnalyzer
from patchscope.analyzers.semgrep import SemgrepAnalyzer
from patchscope.intake import IntakeError, SourceFile, staged_sources


class AnalyzerRunner:
    """Stage validated text once and run analyzers sequentially in a stable order."""

    def __init__(self, analyzers: Iterable[AnalyzerAdapter] | None = None) -> None:
        configured = tuple(
            analyzers
            if analyzers is not None
            else (HeuristicAnalyzer(), RuffAnalyzer(), MypyAnalyzer(), SemgrepAnalyzer())
        )
        names = [analyzer.name for analyzer in configured]
        if len(names) != len(set(names)):
            raise ValueError("analyzer names must be unique")
        self.analyzers = configured

    def analyze(self, files: list[SourceFile]) -> list[AnalyzerRun]:
        """Return one explicit terminal run per configured analyzer."""

        if not isinstance(files, list) or any(not isinstance(item, SourceFile) for item in files):
            raise TypeError("files must be a list of SourceFile objects")
        paths = [source.path.casefold() for source in files]
        if len(paths) != len(set(paths)):
            raise IntakeError("duplicate_path", "Duplicate source paths cannot be analyzed.")
        with staged_sources(files) as root:
            runs: list[AnalyzerRun] = []
            for analyzer in self.analyzers:
                started = time.monotonic()
                try:
                    run = analyzer.analyze(files, root)
                    if run.analyzer != analyzer.name:
                        raise ValueError("analyzer returned a mismatched name")
                except Exception:  # Analyzer isolation boundary; raw errors are never exposed.
                    run = AnalyzerRun(
                        analyzer=analyzer.name,
                        status=AnalyzerStatus.ERROR,
                        duration_ms=max(int((time.monotonic() - started) * 1_000), 0),
                        message="The analyzer failed inside its isolated review boundary.",
                    )
                runs.append(run)
        return runs


__all__ = ["AnalyzerRunner"]
