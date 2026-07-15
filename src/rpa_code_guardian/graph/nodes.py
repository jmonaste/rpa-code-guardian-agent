"""Pipeline nodes: ingest -> plan -> map (waves) -> reduce -> gap-fill -> compose.

Context strategy in one paragraph: the ingest node compresses the project
deterministically (XAML -> IR); the map phase summarizes one workflow at a time
in an isolated context, walking the call graph bottom-up so callers see their
callees as one-line digests instead of code; the reduce phase sees only
summaries, never files; the gap-fill agent selectively retrieves evidence for
the few questions the summaries could not answer. Every LLM node degrades to a
deterministic fallback and records a warning instead of failing the run.
"""

from __future__ import annotations

import re
from pathlib import Path

from langgraph.graph import END
from langgraph.types import Send

from ..checks import run_checks
from ..config import Settings, cache_dir
from ..ingest.scanner import scan_project
from ..llm import GuardianLLM
from ..model.ir import ProjectInventory
from ..model.summaries import (
    AnalysisPlan,
    Finding,
    GapAnswer,
    NarrativeAudit,
    NarrativeSections,
    WorkflowSummary,
)
from ..render.document import render_documentation
from ..tools import build_project_tools
from .cache import SummaryCache
from .state import GuardianState, MapPayload

NARRATIVE_CONTEXT_BUDGET = 28_000  # chars of workflow summaries shown to the reduce node
MAX_GAP_QUESTIONS = 5
MAX_SMELL_CHECKS = 12  # adversarial verification loops per run (cost bound)
SMELL_LOOP_ITERATIONS = 4
MIN_PURPOSE_CHARS = 40  # quality gate: shorter purposes look like non-answers

_XAML_CITE_RE = re.compile(r"[\w][\w\-./\\]*\.xaml", re.IGNORECASE)

PLAN_SYSTEM = """\
You are a senior RPA architect. You are given the inventory of a UiPath project
(workflow list, call graph, configuration). Produce a short analysis plan for
documenting it: what kind of process it is, what deserves attention, and which
workflows look like unmodified REFramework template boilerplate. Be specific to
THIS project; do not restate generic best practices."""

MAP_SYSTEM = """\
You are a senior RPA developer writing the technical documentation of a UiPath
project. You are given the parsed representation of ONE workflow (arguments,
variables, invocations, log messages, selectors, activity outline) plus one-line
digests of the workflows it invokes. Describe what the workflow actually does,
grounded strictly in the given content — never invent activities or systems that
are not shown. Write plain, professional English prose. No emojis."""

REDUCE_SYSTEM = """\
You are a senior RPA architect writing the technical documentation of a UiPath
process for a corporate audience. You are given the project inventory and a
summary of every workflow. Write the requested sections in plain, professional
English prose (no emojis, no marketing language). Ground every statement in the
provided summaries; reference workflows by their path in parentheses. If
something important cannot be determined from the summaries, add a precise
question to open_questions instead of guessing."""

GAP_SYSTEM = """\
You answer one specific question about a UiPath project using the provided
read-only tools. Explore selectively: search first, then read only what is
needed. Ground the answer strictly in what the tools return; if the project
does not contain the answer, say so plainly. Answer in 2-5 sentences of
professional English prose."""

CRITIC_SYSTEM = """\
You are auditing the draft documentation of a UiPath process for grounding.
You are given the workflow summaries (the only source of truth) and the draft
sections. List every factual claim in the sections that is NOT supported by the
summaries: invented systems, invented behavior, numbers or rules that appear
nowhere. Report at most 5, each as one precise sentence quoting the claim. If
everything is grounded, return an empty list. Audit factual support only, not
style or completeness."""

SMELL_VERIFY_SYSTEM = """\
You are skeptically verifying one reported code-quality issue in a UiPath
project. Use the read-only tools to inspect the workflow in question. Then
answer with exactly one line starting with either CONFIRMED: or REFUTED:,
followed by a one-sentence justification grounded in what you actually read.
If the tools do not show enough evidence to confirm the issue, answer REFUTED."""

REFINE_SYSTEM = """\
You are revising the sections of a technical document about a UiPath process.
You are given the current sections and verified answers to the open questions
they raised. Re-emit ALL sections, improved with the new information where it is
relevant and otherwise unchanged. Keep the same professional tone. Leave
open_questions empty unless something important is still genuinely unknown."""


class PipelineNodes:
    """Node implementations, closed over settings and the LLM gateway."""

    def __init__(self, settings: Settings, llm: GuardianLLM | None = None) -> None:
        self.settings = settings
        self.llm = llm or GuardianLLM(settings)
        self.cache: SummaryCache | None = None

    # ---------------------------------------------------------------- ingest

    def ingest(self, state: GuardianState) -> dict:
        root = Path(state["project_root"]).resolve()
        inventory = scan_project(root)
        if not inventory.workflows:
            raise ValueError(f"no .xaml workflows found under {root}")
        self.cache = SummaryCache(
            cache_dir(root) / "summaries.json",
            model=self.settings.resolved_worker_model(),
            enabled=self.settings.use_cache,
        )
        to_analyze = [
            p for p in inventory.workflows
            if not any(part.lower() in ("testcases", "tests") for part in Path(p).parts)
        ]
        waves = inventory.call_graph.bottom_up_order(to_analyze)
        return {"inventory": inventory, "waves": waves, "warnings": []}

    # ------------------------------------------------------------------ plan

    def plan(self, state: GuardianState) -> dict:
        inv = state["inventory"]
        user = (
            f"{inv.census()}\n\nCALL GRAPH:\n{inv.call_graph.to_context()}\n\n"
            f"CONFIG:\n{inv.config_context(max_entries=40)}"
        )
        try:
            plan = self.llm.structured(AnalysisPlan, PLAN_SYSTEM, user, role="lead")
        except Exception as exc:  # noqa: BLE001 - degrade, don't fail the run
            plan = AnalysisPlan(
                process_type="Unknown (analysis plan unavailable)",
                focus_areas=["Document each workflow from its parsed content."],
            )
            return {"plan": plan, "warnings": [f"plan node fell back to defaults: {exc}"]}
        return {"plan": plan}

    # ------------------------------------------------------- map (in waves)

    def dispatch(self, state: GuardianState) -> dict:
        """Prepare isolated payloads for the next bottom-up wave."""
        if self.cache is not None:
            self.cache.save()  # persist completed waves before starting the next
        waves = list(state.get("waves") or [])
        if not waves:
            return {"current_wave": []}
        wave, rest = waves[0], waves[1:]
        inv = state["inventory"]
        summaries = state.get("summaries", {})
        plan = state.get("plan")
        plan_notes = ""
        if plan is not None:
            plan_notes = f"Process: {plan.process_type}. Focus: {'; '.join(plan.focus_areas)}"

        payloads: list[MapPayload] = []
        for path in wave:
            ir = inv.workflows[path]
            digests = []
            for callee in inv.call_graph.callees(path):
                s = summaries.get(callee)
                if s is not None and s.one_liner:
                    digests.append(f"  - {callee}: {s.one_liner}")
                elif callee in inv.workflows:
                    digests.append(f"  - {inv.workflows[callee].one_liner()}")
            payloads.append(
                MapPayload(
                    path=path,
                    ir_context=ir.to_context(max_chars=self.settings.ir_max_chars),
                    callee_digests="\n".join(digests),
                    plan_notes=plan_notes,
                    content_hash=ir.content_hash,
                )
            )
        return {"waves": rest, "current_wave": payloads}

    def route_map(self, state: GuardianState):
        wave = state.get("current_wave") or []
        if wave:
            return [Send("summarize", payload) for payload in wave]
        return "reduce"

    def summarize(self, payload: MapPayload) -> dict:
        path = payload["path"]
        if self.cache is not None:
            cached = self.cache.get(payload["content_hash"])
            if cached is not None:
                return {"summaries": {path: cached}}
        user = payload["ir_context"]
        if payload["callee_digests"]:
            user += f"\n\nINVOKED WORKFLOWS (digests):\n{payload['callee_digests']}"
        if payload["plan_notes"]:
            user += f"\n\nPROJECT CONTEXT: {payload['plan_notes']}"
        try:
            summary = self.llm.structured(WorkflowSummary, MAP_SYSTEM, user, role="worker")
        except Exception as exc:  # noqa: BLE001
            summary = WorkflowSummary(
                path=path,
                purpose="Not analyzed: the language model call failed for this workflow.",
                key_logic=[],
                one_liner="(analysis unavailable)",
            )
            return {"summaries": {path: summary}, "warnings": [f"summary failed for {path}: {exc}"]}
        summary = self._escalate_if_weak(summary, user)
        summary.path = path  # canonical, never trusted from the model
        if not summary.one_liner:
            summary.one_liner = summary.purpose.split(". ")[0][:140]
        if self.cache is not None:
            self.cache.put(payload["content_hash"], summary)
        return {"summaries": {path: summary}}

    def _weak_summary(self, summary: WorkflowSummary) -> bool:
        """Cheap quality gate: a real workflow should yield substance."""
        if summary.is_boilerplate:
            return False
        return len(summary.purpose.strip()) < MIN_PURPOSE_CHARS or not summary.key_logic

    def _escalate_if_weak(self, summary: WorkflowSummary, user: str) -> WorkflowSummary:
        """Retry a weak worker summary with the lead model (pay for quality only
        where the cheap model failed). Keeps the worker's answer if the lead's is
        no better or the retry fails."""
        if (
            not self.settings.escalate_weak_summaries
            or not self._weak_summary(summary)
            or self.settings.resolved_lead_model() == self.settings.resolved_worker_model()
        ):
            return summary
        try:
            retry = self.llm.structured(WorkflowSummary, MAP_SYSTEM, user, role="lead")
        except Exception:  # noqa: BLE001 - keep the worker summary
            return summary
        return retry if not self._weak_summary(retry) else summary

    # ---------------------------------------------------------------- reduce

    def reduce(self, state: GuardianState) -> dict:
        if self.cache is not None:
            self.cache.save()
        inv = state["inventory"]
        user = (
            f"{inv.census()}\n\nCALL GRAPH:\n{inv.call_graph.to_context()}\n\n"
            f"CONFIG:\n{inv.config_context(max_entries=60)}\n\n"
            f"LOGS:\n{inv.log_digest.to_context()}\n\n"
            f"WORKFLOW SUMMARIES:\n{self._summaries_context(state)}"
        )
        try:
            narrative = self.llm.structured(NarrativeSections, REDUCE_SYSTEM, user, role="lead")
        except Exception as exc:  # noqa: BLE001
            narrative = self._fallback_narrative(state)
            return {"narrative": narrative, "warnings": [f"reduce node fell back to listing: {exc}"]}
        return {"narrative": narrative}

    def _summaries_context(self, state: GuardianState) -> str:
        """Hierarchical degradation: full summaries for business workflows,
        one-liners for boilerplate, truncation as the last resort."""
        inv = state["inventory"]
        summaries = state.get("summaries", {})
        plan = state.get("plan")
        boilerplate = set(plan.boilerplate_workflows) if plan else set()

        blocks: list[tuple[bool, str]] = []  # (is_full, text)
        for path in sorted(summaries):
            s = summaries[path]
            if s.is_boilerplate or path in boilerplate:
                blocks.append((False, f"- {path}: {s.one_liner or s.purpose[:120]} [standard template code]"))
            else:
                text = (
                    f"### {path}\n{s.purpose}\n"
                    + ("Steps: " + "; ".join(s.key_logic) + "\n" if s.key_logic else "")
                    + (f"I/O: {s.inputs_outputs}\n" if s.inputs_outputs else "")
                    + (f"Errors: {s.error_handling}\n" if s.error_handling else "")
                    + (f"External: {', '.join(s.external_systems)}\n" if s.external_systems else "")
                )
                blocks.append((True, text))

        text = "\n".join(b for _, b in blocks)
        if len(text) <= NARRATIVE_CONTEXT_BUDGET:
            return text
        # Over budget: demote the largest full blocks to one-liners until it fits.
        blocks.sort(key=lambda b: (b[0], len(b[1])))  # one-liners first, then small blocks
        kept: list[str] = []
        used = 0
        demoted = 0
        for is_full, block in blocks:
            if used + len(block) > NARRATIVE_CONTEXT_BUDGET and is_full:
                first_line = block.splitlines()[0].lstrip("# ")
                s = summaries.get(first_line)
                kept.append(f"- {first_line}: {s.one_liner if s else '(demoted)'}")
                demoted += 1
                used += len(kept[-1])
            else:
                kept.append(block)
                used += len(block)
        if demoted:
            kept.append(f"({demoted} summaries shown as one-liners to fit the context budget)")
        return "\n".join(kept)

    def _fallback_narrative(self, state: GuardianState) -> NarrativeSections:
        inv = state["inventory"]
        summaries = state.get("summaries", {})
        listing = "\n\n".join(
            f"{p}: {s.purpose}" for p, s in sorted(summaries.items())
        )
        return NarrativeSections(
            executive_summary=(
                f"Automated documentation of the UiPath project '{inv.meta.name}'. "
                "The narrative synthesis step was unavailable; this document presents "
                "the per-workflow analysis directly."
            ),
            process_description=listing or "(no workflow summaries available)",
            architecture=f"REFramework detected: {inv.is_reframework}. "
            + "; ".join(inv.reframework_evidence),
            exception_strategy="See the per-workflow reference below.",
        )

    # ---------------------------------------------------------------- critic

    def critic(self, state: GuardianState) -> dict:
        """Grounding audit of the draft narrative.

        Deterministic layer: every workflow path the prose cites must exist in
        the inventory. Agentic layer: a lead-model pass lists factual claims the
        summaries do not support; those become open questions so the existing
        gap-fill agent verifies them against the project before the document is
        composed.
        """
        narrative = state.get("narrative")
        if narrative is None:
            return {}
        inv = state["inventory"]
        warnings: list[str] = []

        prose = "\n".join(
            (
                narrative.executive_summary, narrative.process_description,
                narrative.architecture, narrative.exception_strategy,
                narrative.logging_observability, narrative.external_systems,
            )
        )
        known = {p.lower() for p in inv.workflows}
        for cited in sorted({m.group(0) for m in _XAML_CITE_RE.finditer(prose)}):
            if cited.replace("\\", "/").lower() not in known:
                warnings.append(f"narrative cites nonexistent workflow: {cited}")

        if not self.settings.audit_narrative:
            return {"warnings": warnings} if warnings else {}

        user = (
            f"WORKFLOW SUMMARIES:\n{self._summaries_context(state)}\n\n"
            f"DRAFT SECTIONS:\n{narrative.model_dump_json(indent=1)}"
        )
        try:
            audit = self.llm.structured(NarrativeAudit, CRITIC_SYSTEM, user, role="lead")
        except Exception as exc:  # noqa: BLE001 - degrade, don't fail the run
            warnings.append(f"narrative audit skipped: {exc}")
            return {"warnings": warnings} if warnings else {}

        if audit.unsupported_claims:
            existing = set(narrative.open_questions)
            for claim in audit.unsupported_claims[:MAX_GAP_QUESTIONS]:
                question = f"Verify or correct this draft statement against the project: {claim}"
                if question not in existing:
                    narrative.open_questions.append(question)
            warnings.append(
                f"narrative audit flagged {len(audit.unsupported_claims)} unsupported claim(s) for gap-fill"
            )
            return {"narrative": narrative, "warnings": warnings}
        return {"warnings": warnings} if warnings else {}

    # -------------------------------------------------------------- gap fill

    def gapfill(self, state: GuardianState) -> dict:
        narrative = state.get("narrative")
        if narrative is None or not narrative.open_questions:
            return {"gap_answers": []}
        inv = state["inventory"]
        answers: list[GapAnswer] = []
        warnings: list[str] = []
        for question in narrative.open_questions[:MAX_GAP_QUESTIONS]:
            recorder: list[str] = []
            tools = build_project_tools(inv, recorder, self.settings.ir_max_chars)
            try:
                text, _ = self.llm.tool_loop(GAP_SYSTEM, question, tools, role="lead")
                answers.append(GapAnswer(question=question, answer=text, evidence=recorder))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"gap-fill failed for {question!r}: {exc}")

        if not answers:
            return {"gap_answers": [], "warnings": warnings}

        qa = "\n\n".join(f"Q: {a.question}\nA: {a.answer}" for a in answers)
        user = (
            f"CURRENT SECTIONS:\n{narrative.model_dump_json(indent=1)}\n\n"
            f"VERIFIED ANSWERS:\n{qa}"
        )
        try:
            refined = self.llm.structured(NarrativeSections, REFINE_SYSTEM, user, role="lead")
            return {"gap_answers": answers, "narrative": refined, "warnings": warnings}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"refine step kept the original narrative: {exc}")
            return {"gap_answers": answers, "warnings": warnings}

    # -------------------------------------------------------------- findings

    def findings(self, state: GuardianState) -> dict:
        inv = state["inventory"]
        results = run_checks(inv)
        seen = {(f.location, f.description[:60]) for f in results}
        candidates: list[tuple[str, str]] = []
        for path, summary in sorted(state.get("summaries", {}).items()):
            for smell in summary.smells:
                key = (path, smell[:60])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append((path, smell))
        kept, warnings = self._verify_smells(inv, candidates)
        for path, smell in kept:
            results.append(
                Finding(
                    severity="Medium",
                    category="Code quality",
                    location=path,
                    description=smell,
                    recommendation="Review and address as part of routine maintenance.",
                )
            )
        order = {"High": 0, "Medium": 1, "Low": 2}
        results.sort(key=lambda f: (order.get(f.severity, 3), f.location))
        out: dict = {"findings": results}
        if warnings:
            out["warnings"] = warnings
        return out

    def _verify_smells(
        self, inv: ProjectInventory, candidates: list[tuple[str, str]]
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Adversarially verify model-reported smells before they become findings.

        Each smell gets one short skeptic tool-loop that must CONFIRM or REFUTE
        it from actual reads. Refuted smells are dropped (the model invented or
        overstated them); on any ambiguity or verification failure the smell is
        kept — losing a real issue is worse than keeping a doubtful one.
        """
        if not self.settings.verify_smells or not candidates:
            return candidates, []
        kept: list[tuple[str, str]] = []
        warnings: list[str] = []
        dropped = 0
        for path, smell in candidates[:MAX_SMELL_CHECKS]:
            tools = build_project_tools(inv, None, self.settings.ir_max_chars)
            try:
                answer, _ = self.llm.tool_loop(
                    SMELL_VERIFY_SYSTEM,
                    f"Reported issue in workflow {path}: {smell}",
                    tools,
                    role="worker",
                    max_iterations=SMELL_LOOP_ITERATIONS,
                )
            except Exception as exc:  # noqa: BLE001 - fail open
                kept.append((path, smell))
                warnings.append(f"smell verification unavailable for {path}: {exc}")
                continue
            text = answer.upper()
            if "REFUTED" in text and "CONFIRMED" not in text:
                dropped += 1
            else:
                kept.append((path, smell))
        kept.extend(candidates[MAX_SMELL_CHECKS:])  # beyond the cost cap: keep unverified
        if len(candidates) > MAX_SMELL_CHECKS:
            warnings.append(
                f"{len(candidates) - MAX_SMELL_CHECKS} smell(s) kept unverified (over the {MAX_SMELL_CHECKS}-check budget)"
            )
        if dropped:
            warnings.append(f"{dropped} unconfirmed code smell(s) dropped after adversarial verification")
        return kept, warnings

    # --------------------------------------------------------------- compose

    def compose(self, state: GuardianState) -> dict:
        md = render_documentation(state)
        return {"documentation_md": md}

    def route_compliance(self, state: GuardianState):
        return "extract_requirements" if state.get("pdd_text") else END
