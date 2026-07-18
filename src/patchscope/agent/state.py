"""Typed shared state for the fixed PatchScope review graph."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ReviewState(TypedDict, total=False):
    files: list[Any]
    metadata: dict[str, Any]
    parse_summaries: list[dict[str, Any]]
    analyzer_runs: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    refactors: list[dict[str, Any]]
    summary: dict[str, Any]
    ai_metadata: dict[str, Any]
    stage_trace: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
