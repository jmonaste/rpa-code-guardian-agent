"""Shared pipeline state.

The state deliberately carries only compact artifacts (IR, summaries, section
prose) — never raw project files. Heavy text lives on disk; what flows between
nodes is what an LLM might actually need to see. Map-phase nodes run in
parallel via the Send API, so their keys use merge reducers.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..model.ir import ProjectInventory
from ..model.summaries import (
    AnalysisPlan,
    ComplianceItem,
    Finding,
    GapAnswer,
    NarrativeSections,
    Requirement,
    WorkflowSummary,
)


def merge_dicts(left: dict, right: dict) -> dict:
    """Reducer for parallel map results."""
    return {**left, **right}


class GuardianState(TypedDict, total=False):
    # inputs
    project_root: str
    pdd_text: str

    # ingestion artifacts (deterministic)
    inventory: ProjectInventory

    # analysis artifacts
    plan: AnalysisPlan
    waves: list[list[str]]  # bottom-up waves still to summarize
    current_wave: list["MapPayload"]  # isolated payloads being summarized this super-step
    summaries: Annotated[dict[str, WorkflowSummary], merge_dicts]
    narrative: NarrativeSections
    gap_answers: list[GapAnswer]
    findings: list[Finding]

    # compliance artifacts
    requirements: list[Requirement]
    current_verify: list["VerifyPayload"]  # isolated payloads for the verify map
    compliance: Annotated[dict[str, ComplianceItem], merge_dicts]

    # outputs
    documentation_md: str
    compliance_md: str
    warnings: Annotated[list[str], operator.add]


class MapPayload(TypedDict):
    """Isolated input for one map-phase summarization (built by dispatch)."""

    path: str
    ir_context: str
    callee_digests: str
    plan_notes: str
    content_hash: str


class VerifyPayload(TypedDict):
    """Isolated input for one compliance verification."""

    requirement_id: str
    requirement_text: str
    requirement_kind: str
    context: str
