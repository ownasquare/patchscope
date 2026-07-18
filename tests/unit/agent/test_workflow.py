from __future__ import annotations

from dataclasses import dataclass

from patchscope.agent.model import EvidenceSynthesizer
from patchscope.agent.workflow import ReviewWorkflow, WorkflowDependencies


@dataclass
class Source:
    path: str
    content: str


class Parser:
    def parse(self, source: Source) -> dict[str, object]:
        return {"path": source.path, "language": "python", "syntax_errors": 0}


class Runner:
    def analyze(self, _files: list[Source]) -> list[dict[str, object]]:
        return [
            {
                "analyzer": "heuristics",
                "status": "completed",
                "findings": [
                    {
                        "rule_id": "PS001",
                        "path": "app.py",
                        "start_line": 1,
                        "category": "security",
                        "severity": "high",
                        "message": "Unsafe dynamic evaluation.",
                        "evidence": "eval(value)",
                        "suggestion": "Use a strict parser.",
                    }
                ],
            }
        ]


class Refactorer:
    def preview(self, source: Source, finding: dict[str, object]) -> dict[str, object]:
        return {
            "path": source.path,
            "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-eval(value)\n+parse(value)",
            "applied": False,
            "finding_fingerprint": finding.get("fingerprint"),
        }


def test_workflow_executes_fixed_stages_and_scores_risk() -> None:
    workflow = ReviewWorkflow(
        WorkflowDependencies(
            parser=Parser(),
            analyzer_runner=Runner(),
            refactor_engine=Refactorer(),
            synthesizer=EvidenceSynthesizer(mode="offline"),
        )
    )
    result = workflow.invoke(files=[Source("app.py", "eval(value)")], metadata={})
    assert result["stage_trace"] == ["parse", "analyze", "synthesize", "refactor", "score"]
    assert result["summary"]["recommendation"] == "request_changes"
    assert result["summary"]["risk_score"] == 20
    assert result["ai_metadata"]["mode"] == "offline"
    assert result["refactors"][0]["applied"] is False


def test_workflow_excludes_findings_outside_changed_pull_request_lines() -> None:
    workflow = ReviewWorkflow(
        WorkflowDependencies(
            parser=Parser(),
            analyzer_runner=Runner(),
            refactor_engine=Refactorer(),
            synthesizer=EvidenceSynthesizer(mode="offline"),
        )
    )

    result = workflow.invoke(
        files=[Source("app.py", "eval(value)\nsafe()\n")],
        metadata={"changed_line_ranges": {"app.py": [[2, 2]]}},
    )

    assert result["findings"] == []
    assert result["analyzer_runs"][0]["findings"] == []
    assert result["summary"]["recommendation"] == "approve"
    assert result["refactors"] == []
