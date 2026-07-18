"""Credential-free deterministic review heuristics across common languages."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from patchscope.analyzers.base import (
    AnalyzerRun,
    AnalyzerStatus,
    Finding,
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    finding_id,
)
from patchscope.intake import SourceFile
from patchscope.parsing import detect_language


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    languages: frozenset[str]
    pattern: re.Pattern[str]
    category: FindingCategory
    severity: FindingSeverity
    message: str
    suggestion: str
    confidence: FindingConfidence = FindingConfidence.MEDIUM
    redact_snippet: bool = False


_SCRIPT_LANGUAGES = frozenset({"javascript", "tsx", "typescript"})
_C_FAMILY = frozenset({"c", "cpp"})
_RULES: tuple[_Rule, ...] = (
    _Rule(
        "patchscope.generic.hardcoded-secret",
        frozenset({"*"}),
        re.compile(
            r"(?i)\b(?:api[_-]?key|auth[_-]?token|client[_-]?secret|password|secret|token)\b"
            r"\s*[:=]\s*[\"'][^\"'\r\n]{8,}[\"']"
        ),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "A credential-like value appears to be hard-coded.",
        "Move the value to an approved secret store and rotate it if it was ever active.",
        FindingConfidence.HIGH,
        True,
    ),
    _Rule(
        "patchscope.generic.todo",
        frozenset({"*"}),
        re.compile(r"(?i)\b(?:FIXME|TODO)\b"),
        FindingCategory.READABILITY,
        FindingSeverity.INFO,
        "An unresolved maintenance marker remains in the reviewed source.",
        "Replace the marker with a tracked issue reference or complete the work before merging.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.dynamic-execution",
        frozenset({"python"}),
        re.compile(r"(?<![\w.])(?:eval|exec)\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Dynamic Python execution can run attacker-controlled code.",
        "Use a typed parser or an allowlisted operation instead of dynamic execution.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.shell-true",
        frozenset({"python"}),
        re.compile(
            r"\bsubprocess\.(?:call|check_call|check_output|Popen|run)\s*\(.*\bshell\s*=\s*True\b"
        ),
        FindingCategory.SECURITY,
        FindingSeverity.CRITICAL,
        "A subprocess enables shell parsing, which can turn data into commands.",
        "Pass a fixed argument list with shell disabled and validate every dynamic value.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.bare-except",
        frozenset({"python"}),
        re.compile(r"^\s*except\s*:"),
        FindingCategory.BUG,
        FindingSeverity.MEDIUM,
        "A bare except clause also catches shutdown and cancellation signals.",
        "Catch the narrow expected exception, or at minimum Exception, and handle it explicitly.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.mutable-default",
        frozenset({"python"}),
        re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\([^)]*=\s*(?:\[\]|\{\}|set\(\))"),
        FindingCategory.BUG,
        FindingSeverity.HIGH,
        "A mutable default argument is shared between calls.",
        "Use None as the default and create the collection inside the function.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.network-timeout",
        frozenset({"python"}),
        re.compile(
            r"\b(?:httpx|requests)\.(?:get|post|put|patch|delete)\s*\("
            r"(?![^\r\n]*\btimeout\s*=)"
        ),
        FindingCategory.PERFORMANCE,
        FindingSeverity.MEDIUM,
        "A network request has no explicit timeout and can block indefinitely.",
        "Set a bounded connect/read timeout and handle timeout failures explicitly.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.debug-print",
        frozenset({"python"}),
        re.compile(r"^\s*print\s*\("),
        FindingCategory.READABILITY,
        FindingSeverity.LOW,
        "A direct print call bypasses the application's structured logging contract.",
        "Use the configured logger with an appropriate level and non-sensitive context.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.python.sql-interpolation",
        frozenset({"python"}),
        re.compile(r"\.execute(?:many)?\s*\(\s*(?:f[\"']|[\"'][^\"']*%|[^,]+\.format\s*\()"),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "A database query appears to interpolate values into SQL text.",
        "Use the database driver's parameter binding instead of string interpolation.",
        FindingConfidence.MEDIUM,
    ),
    _Rule(
        "patchscope.javascript.dynamic-execution",
        _SCRIPT_LANGUAGES,
        re.compile(r"(?<![\w.])(?:eval|Function)\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Dynamic JavaScript execution can run attacker-controlled code.",
        "Use a typed parser or an explicit operation allowlist.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.javascript.inner-html",
        _SCRIPT_LANGUAGES,
        re.compile(r"\.(?:innerHTML|outerHTML)\s*="),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Direct HTML assignment can create a cross-site scripting path.",
        "Render text safely or sanitize through a reviewed HTML policy before assignment.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.javascript.loose-equality",
        _SCRIPT_LANGUAGES,
        re.compile(r"(?<![=!])==(?!=)|(?<![!])!=(?!=)"),
        FindingCategory.BUG,
        FindingSeverity.LOW,
        "Loose equality performs implicit type coercion.",
        "Use strict equality after confirming both operands have the intended type.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.javascript.async-foreach",
        _SCRIPT_LANGUAGES,
        re.compile(r"\.forEach\s*\(\s*async\b"),
        FindingCategory.BUG,
        FindingSeverity.HIGH,
        "forEach does not await an asynchronous callback.",
        (
            "Use a for...of loop for sequential work or Promise.all over map for "
            "bounded parallel work."
        ),
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.go.shell-command",
        frozenset({"go"}),
        re.compile(
            r"\bexec\.Command(?:Context)?\s*\(\s*[`\"'](?:sh|bash|zsh|cmd)(?:\.exe)?[`\"']\s*,\s*[`\"'](?:-c|/c)[`\"']"
        ),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "A Go subprocess explicitly invokes a command shell.",
        "Call the intended executable directly with a validated argument slice.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.go.ignored-error",
        frozenset({"go"}),
        re.compile(r"^\s*_\s*=\s*[A-Za-z_][\w.]*\s*\("),
        FindingCategory.BUG,
        FindingSeverity.MEDIUM,
        "A returned error may be intentionally discarded.",
        "Handle the error or document why ignoring it is safe at this boundary.",
        FindingConfidence.MEDIUM,
    ),
    _Rule(
        "patchscope.rust.unwrap",
        frozenset({"rust"}),
        re.compile(r"\.(?:expect|unwrap)\s*\("),
        FindingCategory.BUG,
        FindingSeverity.LOW,
        "This operation can panic instead of returning a recoverable error.",
        "Propagate or handle the error unless the invariant is proven and documented.",
        FindingConfidence.MEDIUM,
    ),
    _Rule(
        "patchscope.jvm.runtime-exec",
        frozenset({"java", "kotlin", "scala"}),
        re.compile(r"\bRuntime\.getRuntime\s*\(\s*\)\.exec\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Runtime.exec creates a command-injection boundary.",
        "Use ProcessBuilder with a fixed executable and separately validated arguments.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.c.unsafe-string-copy",
        _C_FAMILY,
        re.compile(r"(?<![\w])(?:gets|strcat|strcpy|sprintf|vsprintf)\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "An unbounded C string operation can overwrite memory.",
        "Use a size-aware operation and prove destination capacity at the call site.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.php.dynamic-execution",
        frozenset({"php"}),
        re.compile(r"(?i)(?<![\w])(?:eval|shell_exec|system|passthru)\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Dynamic PHP or shell execution creates a code-injection boundary.",
        "Replace dynamic execution with an allowlisted operation and fixed arguments.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.ruby.dynamic-execution",
        frozenset({"ruby"}),
        re.compile(r"(?<![\w.])(?:eval|system)\s*\("),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Dynamic Ruby or process execution may turn data into code.",
        "Use an allowlisted operation and pass process arguments without a shell.",
        FindingConfidence.MEDIUM,
    ),
    _Rule(
        "patchscope.shell.eval",
        frozenset({"bash"}),
        re.compile(r"^\s*eval\s+"),
        FindingCategory.SECURITY,
        FindingSeverity.HIGH,
        "Shell eval reparses data as commands.",
        "Use arrays, explicit dispatch, or a case statement instead of eval.",
        FindingConfidence.HIGH,
    ),
    _Rule(
        "patchscope.sql.select-star",
        frozenset({"sql"}),
        re.compile(r"(?i)\bSELECT\s+\*\s+FROM\b"),
        FindingCategory.PERFORMANCE,
        FindingSeverity.LOW,
        "SELECT * can fetch unnecessary data and creates a brittle result contract.",
        "Select only the columns consumed by the caller.",
        FindingConfidence.HIGH,
    ),
)


class HeuristicAnalyzer:
    name = "patchscope-heuristics"

    def analyze(self, files: list[SourceFile], root: Path) -> AnalyzerRun:
        del root
        started = time.monotonic()
        findings: list[Finding] = []
        for source in sorted(files, key=lambda item: item.path):
            language = source.language_hint or detect_language(source.path)
            for line_number, line in _review_lines(source):
                stripped = line.lstrip()
                comment_only = _is_comment_only(language, stripped)
                for rule in _RULES:
                    if "*" not in rule.languages and language not in rule.languages:
                        continue
                    if comment_only and rule.id != "patchscope.generic.todo":
                        continue
                    match = rule.pattern.search(line)
                    if match is None:
                        continue
                    snippet = (
                        "[redacted credential-like source line]"
                        if rule.redact_snippet
                        else line[:500]
                    )
                    findings.append(
                        Finding(
                            id=finding_id(
                                self.name, rule.id, source.path, line_number, rule.message
                            ),
                            analyzer=self.name,
                            rule_id=rule.id,
                            category=rule.category,
                            severity=rule.severity,
                            message=rule.message,
                            path=source.path,
                            start_line=line_number,
                            end_line=line_number,
                            start_column=match.start() + 1,
                            end_column=max(match.end() + 1, match.start() + 2),
                            confidence=rule.confidence,
                            snippet=snippet,
                            suggestion=rule.suggestion,
                        )
                    )
                if len(line) > 140 and not _looks_like_generated_or_url(line):
                    message = "This line exceeds 140 characters and is difficult to review."
                    rule_id = "patchscope.generic.long-line"
                    findings.append(
                        Finding(
                            id=finding_id(self.name, rule_id, source.path, line_number, message),
                            analyzer=self.name,
                            rule_id=rule_id,
                            category=FindingCategory.READABILITY,
                            severity=FindingSeverity.INFO,
                            message=message,
                            path=source.path,
                            start_line=line_number,
                            end_line=line_number,
                            start_column=141,
                            end_column=len(line) + 1,
                            confidence=FindingConfidence.HIGH,
                            snippet=line[:500],
                            suggestion=(
                                "Split the expression or data while preserving the language's "
                                "formatting conventions."
                            ),
                        )
                    )
            findings.extend(_contextual_findings(source, language, self.name))
        unique = {finding.fingerprint: finding for finding in findings}
        ordered = tuple(
            sorted(
                unique.values(),
                key=lambda item: (item.path, item.start_line, item.start_column, item.rule_id),
            )
        )
        return AnalyzerRun(
            analyzer=self.name,
            status=AnalyzerStatus.SUCCEEDED,
            findings=ordered,
            duration_ms=max(int((time.monotonic() - started) * 1_000), 0),
            message="Deterministic source heuristics completed without executing imported code.",
            version="1",
        )


def _review_lines(source: SourceFile) -> list[tuple[int, str]]:
    if not source.is_patch:
        return list(enumerate(source.content.splitlines(), start=1))
    reviewed: list[tuple[int, str]] = []
    target_line: int | None = None
    hunk_header = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for line in source.content.splitlines():
        header = hunk_header.match(line)
        if header is not None:
            target_line = int(header.group(1))
            continue
        if target_line is None or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if target_line >= 1:
                reviewed.append((target_line, line[1:]))
            target_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            target_line += 1
    return reviewed


def _contextual_findings(source: SourceFile, language: str, analyzer: str) -> list[Finding]:
    if language != "python" or source.is_patch:
        return []
    findings: list[Finding] = []
    async_indents: list[int] = []
    lines = source.content.splitlines()
    for line_number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        async_indents = [level for level in async_indents if indent > level]
        if re.match(r"async\s+def\s+", stripped):
            async_indents.append(indent)
            continue
        if async_indents and re.search(
            r"\b(?:requests\.(?:get|post|put|delete)|time\.sleep)\s*\(", stripped
        ):
            message = "A blocking call appears inside an async function."
            rule_id = "patchscope.python.blocking-in-async"
            match = re.search(r"\b(?:requests\.|time\.sleep)", line)
            column = match.start() + 1 if match else 1
            findings.append(
                Finding(
                    id=finding_id(analyzer, rule_id, source.path, line_number, message),
                    analyzer=analyzer,
                    rule_id=rule_id,
                    category=FindingCategory.PERFORMANCE,
                    severity=FindingSeverity.MEDIUM,
                    message=message,
                    path=source.path,
                    start_line=line_number,
                    end_line=line_number,
                    start_column=column,
                    end_column=column + 1,
                    confidence=FindingConfidence.MEDIUM,
                    snippet=line[:500],
                    suggestion=(
                        "Use an asynchronous client or move the blocking call to a bounded "
                        "worker thread."
                    ),
                )
            )
    return findings


def _is_comment_only(language: str, stripped: str) -> bool:
    prefixes = {
        "bash": ("#",),
        "python": ("#",),
        "ruby": ("#",),
        "sql": ("--",),
    }.get(language, ("//", "/*", "*"))
    return stripped.startswith(prefixes)


def _looks_like_generated_or_url(line: str) -> bool:
    stripped = line.strip()
    return (
        "http://" in stripped
        or "https://" in stripped
        or stripped.startswith(("// Code generated", "# Code generated"))
        or re.fullmatch(r"[A-Za-z0-9+/=]{120,}", stripped) is not None
    )


__all__ = ["HeuristicAnalyzer"]
