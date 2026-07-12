"""Wire the pipeline graph and provide the single ``run_pipeline`` entry point.

Graph shape:

    START -> ingest -> plan -> dispatch --(Send per workflow)--> summarize -> dispatch
                                  \\--(waves done)--> reduce -> findings -> gapfill -> compose
    compose --(PDD given)--> extract_requirements -> dispatch_verify
        --(Send per requirement)--> verify_requirement -> evidence_rescue -> compose_compliance -> END
    compose --(no PDD)--> END
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from ..config import Settings, cache_dir
from ..llm import GuardianLLM
from .compliance import ComplianceNodes
from .nodes import PipelineNodes
from .state import GuardianState


def build_graph(settings: Settings, llm: GuardianLLM | None = None, checkpointer=None):
    """Compile the pipeline; ``llm`` is injectable for tests."""
    nodes = PipelineNodes(settings, llm=llm)
    compliance = ComplianceNodes(nodes)

    builder = StateGraph(GuardianState)
    builder.add_node("ingest", nodes.ingest)
    builder.add_node("plan", nodes.plan)
    builder.add_node("dispatch", nodes.dispatch)
    builder.add_node("summarize", nodes.summarize)
    builder.add_node("reduce", nodes.reduce)
    builder.add_node("findings", nodes.findings)
    builder.add_node("gapfill", nodes.gapfill)
    builder.add_node("compose", nodes.compose)
    builder.add_node("extract_requirements", compliance.extract_requirements)
    builder.add_node("dispatch_verify", compliance.dispatch_verify)
    builder.add_node("verify_requirement", compliance.verify_requirement)
    builder.add_node("evidence_rescue", compliance.evidence_rescue)
    builder.add_node("compose_compliance", compliance.compose_compliance)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_edge("plan", "dispatch")
    builder.add_conditional_edges("dispatch", nodes.route_map, ["summarize", "reduce"])
    builder.add_edge("summarize", "dispatch")
    builder.add_edge("reduce", "findings")
    builder.add_edge("findings", "gapfill")
    builder.add_edge("gapfill", "compose")
    builder.add_conditional_edges("compose", nodes.route_compliance, ["extract_requirements", END])
    builder.add_edge("extract_requirements", "dispatch_verify")
    builder.add_conditional_edges(
        "dispatch_verify", compliance.route_verify, ["verify_requirement", "compose_compliance"]
    )
    builder.add_edge("verify_requirement", "evidence_rescue")
    builder.add_edge("evidence_rescue", "compose_compliance")
    builder.add_edge("compose_compliance", END)

    return builder.compile(checkpointer=checkpointer)


def run_pipeline(
    project_root: Path,
    settings: Settings,
    pdd_text: str = "",
    llm: GuardianLLM | None = None,
    on_event: Callable[[str, object], None] | None = None,
    resume: bool = False,
) -> GuardianState:
    """Run the full pipeline and return the final state.

    A SQLite checkpointer records every super-step under ``.guardian_cache/`` so
    a crashed run on a big project can be resumed with ``resume=True``.
    """
    import sqlite3

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite import SqliteSaver

    ckpt_dir = cache_dir(project_root)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    run_file = ckpt_dir / "last_run_id"

    thread_id = None
    if resume and run_file.exists():
        thread_id = run_file.read_text(encoding="utf-8").strip() or None
    if thread_id is None:
        thread_id = uuid.uuid4().hex
        run_file.write_text(thread_id, encoding="utf-8")

    # check_same_thread=False: map-phase nodes run in worker threads.
    conn = sqlite3.connect(str(ckpt_dir / "checkpoints.sqlite"), check_same_thread=False)
    ir_mod = "rpa_code_guardian.model.ir"
    sum_mod = "rpa_code_guardian.model.summaries"
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            (ir_mod, "Argument"), (ir_mod, "Variable"), (ir_mod, "Invocation"),
            (ir_mod, "LogLine"), (ir_mod, "WorkflowIR"), (ir_mod, "ConfigEntry"),
            (ir_mod, "LogDigest"), (ir_mod, "ProjectMeta"), (ir_mod, "CallGraph"),
            (ir_mod, "ProjectInventory"),
            (sum_mod, "AnalysisPlan"), (sum_mod, "WorkflowSummary"),
            (sum_mod, "NarrativeSections"), (sum_mod, "GapAnswer"),
            (sum_mod, "Finding"), (sum_mod, "Requirement"),
            (sum_mod, "RequirementList"), (sum_mod, "ComplianceItem"),
        ]
    )
    try:
        saver = SqliteSaver(conn, serde=serde)
        graph = build_graph(settings, llm=llm, checkpointer=saver)
        config = {
            "configurable": {"thread_id": thread_id},
            "max_concurrency": settings.max_concurrency,
            "recursion_limit": 200,
        }
        inputs: GuardianState | None = {
            "project_root": str(project_root),
            "pdd_text": pdd_text,
        }
        if resume and _has_progress(saver, thread_id):
            inputs = None  # continue from the last checkpoint

        for update in graph.stream(inputs, config=config, stream_mode="updates"):
            for node, payload in update.items():
                if on_event is not None:
                    on_event(node, payload)
        state = graph.get_state(config)
        return dict(state.values) if state and state.values else {}
    finally:
        conn.close()


def _has_progress(saver, thread_id: str) -> bool:
    try:
        return saver.get({"configurable": {"thread_id": thread_id}}) is not None
    except Exception:  # noqa: BLE001
        return False
