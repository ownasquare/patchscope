from __future__ import annotations

from patchscope.analyzers.heuristics import HeuristicAnalyzer
from patchscope.intake import SourceFile


def test_heuristics_cover_security_bugs_performance_and_readability(tmp_path) -> None:
    files = [
        SourceFile.create(
            "app.py",
            "async def load():\n"
            "    time.sleep(1)\n\n"
            "def fetch():\n"
            "    print('fetching')\n"
            "    return httpx.get('https://example.test')\n\n"
            "try:\n"
            "    run()\n"
            "except:\n"
            "    pass\n",
            language_hint="python",
        ),
        SourceFile.create(
            "app.ts",
            "const token = 'super-secret-value';\nif (left == right) eval(input);\n",
            language_hint="typescript",
        ),
        SourceFile.create("query.sql", "SELECT * FROM users;\n", language_hint="sql"),
    ]

    first = HeuristicAnalyzer().analyze(files, tmp_path)
    second = HeuristicAnalyzer().analyze(files, tmp_path)
    rules = {finding.rule_id for finding in first.findings}

    assert "patchscope.python.blocking-in-async" in rules
    assert "patchscope.python.bare-except" in rules
    assert "patchscope.python.network-timeout" in rules
    assert "patchscope.python.debug-print" in rules
    assert "patchscope.generic.hardcoded-secret" in rules
    assert "patchscope.javascript.loose-equality" in rules
    assert "patchscope.sql.select-star" in rules
    assert [item.fingerprint for item in first.findings] == [
        item.fingerprint for item in second.findings
    ]
    secret = next(item for item in first.findings if item.rule_id.endswith("hardcoded-secret"))
    assert secret.snippet == "[redacted credential-like source line]"


def test_patch_findings_use_target_file_line_numbers(tmp_path) -> None:
    patch = SourceFile.create(
        "app.py",
        "@@ -10,1 +10,2 @@\n context\n+eval(value)\n",
        language_hint="python",
        is_patch=True,
    )

    run = HeuristicAnalyzer().analyze([patch], tmp_path)
    finding = next(item for item in run.findings if item.rule_id.endswith("dynamic-execution"))

    assert finding.start_line == 11
