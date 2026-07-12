"""Compliance subgraph: PDD requirements -> per-requirement verdicts -> matrix.

The PDD is chunked for extraction if it is large; each requirement is then
verified in its own isolated context (Send API) containing only the process
narrative and the workflow summaries most relevant to that requirement. When
the model cannot decide from summaries alone, it gets one bounded tool loop to
look for evidence in the parsed project.
"""

from __future__ import annotations

import re

from langgraph.types import Send

from ..model.summaries import ComplianceItem, GapAnswer, Requirement, RequirementList
from ..render.document import render_compliance
from ..tools import build_project_tools
from .nodes import PipelineNodes
from .state import GuardianState, VerifyPayload

PDD_CHUNK_CHARS = 20_000
MAX_REQUIREMENTS = 60
RELEVANT_SUMMARIES = 4

EXTRACT_SYSTEM = """\
You extract testable requirements from a Process Definition Document (PDD) of an
RPA automation. A requirement is anything the built automation could comply with
or violate: processing rules, inputs/outputs, systems to use, exception and retry
behavior, reporting, notifications, performance or scheduling constraints.
Ignore document boilerplate, revision tables and organizational descriptions.
Number them R-01, R-02, ... in document order. Keep each requirement specific:
preserve concrete values, field names and system names."""

VERIFY_SYSTEM = """\
You are auditing whether a UiPath implementation complies with one specific PDD
requirement. You are given the requirement and the relevant technical analysis
of the implementation. Judge strictly from the provided material:
- Compliant: the implementation clearly covers the requirement.
- Partially compliant: covered with gaps or deviations (state them).
- Non-compliant: the implementation contradicts or omits the requirement.
- Not verifiable: the provided material does not show enough to decide.
Cite the workflow paths that support your judgement. Professional English, no
speculation."""

VERIFY_TOOL_SYSTEM = """\
You are auditing one PDD requirement against a UiPath project. Use the read-only
tools to find evidence: search first, read only what is needed. Then state, in
2-5 sentences, what the project actually implements regarding this requirement,
citing workflow paths. If nothing relevant exists, say so plainly."""

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,}")


class ComplianceNodes:
    """Compliance node implementations sharing the pipeline's LLM gateway."""

    def __init__(self, pipeline: PipelineNodes) -> None:
        self.pipeline = pipeline
        self.llm = pipeline.llm
        self.settings = pipeline.settings

    # ------------------------------------------------------------- extract

    def extract_requirements(self, state: GuardianState) -> dict:
        pdd = state.get("pdd_text", "")
        chunks = [pdd[i : i + PDD_CHUNK_CHARS] for i in range(0, len(pdd), PDD_CHUNK_CHARS)] or [""]
        requirements: list[Requirement] = []
        warnings: list[str] = []
        for n, chunk in enumerate(chunks, start=1):
            user = f"PDD (part {n} of {len(chunks)}):\n\n{chunk}"
            try:
                result = self.llm.structured(RequirementList, EXTRACT_SYSTEM, user, role="lead")
                requirements.extend(result.requirements)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"requirement extraction failed on PDD part {n}: {exc}")

        # Renumber sequentially and dedupe near-identical texts.
        seen: set[str] = set()
        unique: list[Requirement] = []
        for req in requirements:
            key = req.text.strip().lower()[:120]
            if key in seen:
                continue
            seen.add(key)
            req.id = f"R-{len(unique) + 1:02d}"
            unique.append(req)
            if len(unique) >= MAX_REQUIREMENTS:
                warnings.append(f"requirement list capped at {MAX_REQUIREMENTS}")
                break
        return {"requirements": unique, "warnings": warnings}

    # ------------------------------------------------------------ dispatch

    def dispatch_verify(self, state: GuardianState) -> dict:
        """Build one isolated verification context per requirement."""
        narrative = state.get("narrative")
        process_desc = narrative.process_description if narrative else ""
        payloads: list[VerifyPayload] = []
        for req in state.get("requirements", []):
            context = (
                f"PROCESS OVERVIEW:\n{process_desc[:6000]}\n\n"
                f"RELEVANT WORKFLOW ANALYSIS:\n{self._relevant_summaries(state, req)}\n\n"
                f"CONFIGURATION:\n{state['inventory'].config_context(max_entries=40)}"
            )
            payloads.append(
                VerifyPayload(
                    requirement_id=req.id,
                    requirement_text=req.text,
                    requirement_kind=req.kind,
                    context=context,
                )
            )
        return {"current_verify": payloads}

    def route_verify(self, state: GuardianState):
        payloads = state.get("current_verify") or []
        if payloads:
            return [Send("verify_requirement", p) for p in payloads]
        return "compose_compliance"

    def _relevant_summaries(self, state: GuardianState, req: Requirement) -> str:
        """Select the summaries that share the most vocabulary with the requirement."""
        req_words = {w.lower() for w in _WORD_RE.findall(req.text)}
        scored: list[tuple[int, str, str]] = []
        for path, s in state.get("summaries", {}).items():
            text = f"{s.purpose} {' '.join(s.key_logic)} {' '.join(s.external_systems)}"
            words = {w.lower() for w in _WORD_RE.findall(text)}
            score = len(req_words & words)
            block = f"### {path}\n{s.purpose}\n" + ("Steps: " + "; ".join(s.key_logic) if s.key_logic else "")
            scored.append((score, path, block))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return "\n".join(block for _, _, block in scored[:RELEVANT_SUMMARIES]) or "(no summaries)"

    # -------------------------------------------------------------- verify

    def verify_requirement(self, payload: VerifyPayload) -> dict:
        req_id = payload["requirement_id"]
        user = (
            f"REQUIREMENT {req_id} ({payload['requirement_kind']}):\n"
            f"{payload['requirement_text']}\n\n{payload['context']}"
        )
        try:
            item = self.llm.structured(ComplianceItem, VERIFY_SYSTEM, user, role="worker")
        except Exception as exc:  # noqa: BLE001
            item = ComplianceItem(
                requirement_id=req_id,
                verdict="Not verifiable",
                justification="The verification call failed; this requirement must be checked manually.",
            )
            return {"compliance": {req_id: item}, "warnings": [f"verification failed for {req_id}: {exc}"]}
        item.requirement_id = req_id
        return {"compliance": {req_id: item}}

    # ----------------------------------------------------- evidence rescue

    def evidence_rescue(self, state: GuardianState) -> dict:
        """Give 'Not verifiable' verdicts one bounded agentic evidence pass."""
        pending = [
            (rid, item) for rid, item in sorted(state.get("compliance", {}).items())
            if item.verdict == "Not verifiable"
        ][:MAX_REQUIREMENTS]
        if not pending:
            return {}
        inv = state["inventory"]
        reqs = {r.id: r for r in state.get("requirements", [])}
        updates: dict[str, ComplianceItem] = {}
        answers: list[GapAnswer] = []
        warnings: list[str] = []
        for rid, item in pending[:8]:  # cap the expensive loops per run
            req = reqs.get(rid)
            if req is None:
                continue
            recorder: list[str] = []
            tools = build_project_tools(inv, recorder, self.settings.ir_max_chars)
            try:
                evidence_text, _ = self.llm.tool_loop(
                    VERIFY_TOOL_SYSTEM, f"Requirement {rid}: {req.text}", tools, role="lead"
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"evidence pass failed for {rid}: {exc}")
                continue
            answers.append(GapAnswer(question=f"{rid}: {req.text}", answer=evidence_text, evidence=recorder))
            user = (
                f"REQUIREMENT {rid} ({req.kind}):\n{req.text}\n\n"
                f"EVIDENCE FOUND IN THE PROJECT:\n{evidence_text}\n\n"
                f"WORKFLOWS CONSULTED: {', '.join(recorder) or '(none)'}"
            )
            try:
                revised = self.llm.structured(ComplianceItem, VERIFY_SYSTEM, user, role="lead")
                revised.requirement_id = rid
                if not revised.evidence:
                    revised.evidence = recorder
                updates[rid] = revised
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"re-verification failed for {rid}: {exc}")
        result: dict = {}
        if updates:
            result["compliance"] = updates
        if warnings:
            result["warnings"] = warnings
        return result

    # -------------------------------------------------------------- compose

    def compose_compliance(self, state: GuardianState) -> dict:
        return {"compliance_md": render_compliance(state)}
