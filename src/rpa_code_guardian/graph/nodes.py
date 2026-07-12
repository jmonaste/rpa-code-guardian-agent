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
    NarrativeSections,
    WorkflowSummary,
)
from ..render.document import render_documentation
from ..tools import build_project_tools
from .cache import SummaryCache
from .state import GuardianState, MapPayload

NARRATIVE_CONTEXT_BUDGET = 28_000  # chars of workflow summaries shown to the reduce node
MAX_GAP_QUESTIONS = 5

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
        summary.path = path  # canonical, never trusted from the model
        if not summary.one_liner:
            summary.one_liner = summary.purpose.split(". ")[0][:140]
        if self.cache is not None:
            self.cache.put(payload["content_hash"], summary)
        return {"summaries": {path: summary}}

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
        for path, summary in sorted(state.get("summaries", {}).items()):
            for smell in summary.smells:
                key = (path, smell[:60])
                if key in seen:
                    continue
                seen.add(key)
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
        return {"findings": results}

    # --------------------------------------------------------------- compose

    def compose(self, state: GuardianState) -> dict:
        md = render_documentation(state)
        return {"documentation_md": md}

    def route_compliance(self, state: GuardianState):
        return "extract_requirements" if state.get("pdd_text") else END
