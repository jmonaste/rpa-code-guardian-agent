# 02 — Pipeline architecture

This chapter walks the LangGraph graph node by node. Source:
`graph/state.py`, `graph/nodes.py`, `graph/builder.py`, `render/`.

## Why a graph at all?

The pipeline could be a plain Python script — most steps are sequential. It is
a LangGraph `StateGraph` for three properties that a script does not give:

1. **Native parallel fan-out.** The map phase and the compliance verification
   use the [Send API](https://langchain-ai.github.io/langgraph/how-tos/map-reduce/)
   to run N model calls concurrently with per-branch state isolation and
   automatic result merging via reducers.
2. **Checkpointing.** With a
   [checkpointer](https://docs.langchain.com/oss/python/langgraph/persistence)
   attached, every super-step is durable; `--resume` continues a crashed run.
3. **Observability.** `graph.stream(..., stream_mode="updates")` yields one
   event per node execution — the CLI progress display is just a consumer of
   that stream.

## State

`graph/state.py` defines a single `GuardianState` (a `TypedDict` with
`total=False`, so nodes only mention the keys they touch):

| Key | Type | Written by | Notes |
|-----|------|-----------|-------|
| `project_root`, `pdd_text` | `str` | caller | inputs |
| `inventory` | `ProjectInventory` | `ingest` | the compact parsed project (ch. 03) |
| `plan` | `AnalysisPlan` | `plan` | focus areas, boilerplate list |
| `waves` | `list[list[str]]` | `ingest`, `dispatch` | remaining bottom-up waves |
| `current_wave` | `list[MapPayload]` | `dispatch` | isolated payloads for this super-step |
| `summaries` | `dict[path, WorkflowSummary]` | `summarize` (parallel) | reducer: `merge_dicts` |
| `narrative` | `NarrativeSections` | `reduce`, `gapfill` | prose sections |
| `gap_answers` | `list[GapAnswer]` | `gapfill` | verified Q&A with evidence |
| `findings` | `list[Finding]` | `findings` | static checks + LLM smells |
| `requirements` | `list[Requirement]` | `extract_requirements` | from the PDD |
| `current_verify` | `list[VerifyPayload]` | `dispatch_verify` | isolated payloads |
| `compliance` | `dict[id, ComplianceItem]` | `verify_requirement`, `evidence_rescue` | reducer: `merge_dicts` |
| `documentation_md`, `compliance_md` | `str` | `compose`, `compose_compliance` | outputs |
| `warnings` | `list[str]` | any node | reducer: `operator.add` |

**Reducers** are the key detail. When several `summarize` branches finish in
the same super-step, LangGraph merges their `{"summaries": {path: summary}}`
returns with the `merge_dicts` reducer declared in the `Annotated` type. The
same mechanism accumulates `warnings` from every node without any node knowing
about the others. Reference:
[graph state and reducers](https://docs.langchain.com/oss/python/langgraph/graph-api).

The state deliberately carries only **compact artifacts** — IR, summaries,
prose sections — never raw file contents. This keeps checkpoints small and is
the reason the whole state can be serialized to SQLite cheaply.

## Graph wiring

`graph/builder.py: build_graph()`:

```python
START -> ingest -> plan -> dispatch
dispatch --route_map--> [Send("summarize", p) ...]   # wave not empty
                          summarize -> dispatch       # loop
dispatch --route_map--> reduce                        # waves exhausted
reduce -> findings -> gapfill -> compose
compose --route_compliance--> extract_requirements   # pdd_text present
compose --route_compliance--> END                     # no PDD
extract_requirements -> dispatch_verify
dispatch_verify --route_verify--> [Send("verify_requirement", p) ...]
verify_requirement -> evidence_rescue -> compose_compliance -> END
```

Nodes are methods of `PipelineNodes` / `ComplianceNodes` (classes closed over
`Settings` and the `GuardianLLM` gateway) rather than free functions — this is
what makes the LLM injectable in tests (ch. 07) without any patching.

## The wave loop (map phase)

The subtle part of the graph is strict *inter*-wave ordering with full
*intra*-wave parallelism:

- `Send` fans out everything it is given in **one** super-step; it cannot
  express "run A after B". Ordering therefore lives in the loop:
  `dispatch` pops `waves[0]`, builds one `MapPayload` per workflow, and the
  conditional edge `route_map` returns a list of `Send("summarize", payload)`.
- Every `summarize` branch has a static edge back to `dispatch`. LangGraph
  waits for all branches of a super-step before running the next node once —
  so `dispatch` re-executes exactly once per wave, sees the merged
  `summaries`, and can build the next wave's callee digests from them.
- When `waves` is empty, `route_map` returns `"reduce"` and the loop exits.

A `MapPayload` is **self-contained**: the workflow's rendered IR, its callees'
one-line digests, the plan notes, and the content hash for the cache. The
`summarize` node receives *only* the payload (Send replaces the node input), so
a map branch cannot accidentally read the whole state — isolation by
construction, not by convention.

Two costs to know about:

- `recursion_limit` counts super-steps. Each wave costs 2 (dispatch +
  summarize), so `run_pipeline` sets it to 200 — enough for ~95 waves, far
  beyond any real call-graph depth.
- Concurrency is bounded by `config={"max_concurrency": N}` at invocation
  (from `GUARDIAN_MAX_CONCURRENCY`), which is how you match the endpoint's
  parallel capacity without touching the graph.

## Node-by-node

### `ingest` (deterministic)

Calls `scan_project` (ch. 03), fails fast with `ValueError` if the directory
contains no workflows, instantiates the `SummaryCache`, filters out
`TestCases/`/`Tests/` workflows, and computes the bottom-up waves. Everything
downstream reads the project only through `state["inventory"]`.

### `plan` (LLM, lead)

Input: `inventory.census()` (one line per workflow), the call-graph edge list,
and a 40-entry config excerpt. Output: `AnalysisPlan` — what kind of process
this is, 3–6 focus areas, which workflows look like unmodified template
boilerplate. The plan is threaded into every map prompt ("PROJECT CONTEXT:
..."), giving each isolated summarization a sense of the whole without paying
for the whole. Fallback (D7): a generic plan + warning.

### `dispatch` / `summarize` (LLM, worker)

Described above. `summarize` details worth reading in code:

- Cache check happens *before* any prompt is built (`cache.get(content_hash)`).
- `summary.path` is overwritten with the payload's path after the call — the
  model is asked to copy it, but never trusted (same philosophy as D8).
- A missing `one_liner` is synthesized from the first sentence of `purpose`,
  because the next wave's digests depend on it.
- Failure produces an honest stub summary + warning, never an abort.

### `reduce` (LLM, lead)

Builds the narrative from summaries only. The context assembly
(`_summaries_context`) implements hierarchical degradation under a 28k-char
budget (ch. 04). Output: `NarrativeSections`, including `open_questions` —
up to 5 things the model could *not* determine from summaries, which is the
explicit hand-off contract to the gap-fill agent (the model is told to ask
instead of guessing).

### `findings` (deterministic)

Merges `checks.run_checks(inventory)` (static rules, ch. 03) with the `smells`
collected by every workflow summary, dedupes on `(location, description)`,
and sorts High > Medium > Low.

### `gapfill` (LLM agent, lead)

For each open question (max 5): builds a fresh tool set with a fresh evidence
recorder, runs `GuardianLLM.tool_loop` (bounded think-act-observe, ch. 05),
and stores a `GapAnswer(question, answer, evidence=recorder)`. If any answers
were produced, one `refine` call re-emits the narrative with the verified
information merged in. If refine fails, the original narrative is kept and the
Q&A still lands in the document appendix — information is never lost, only
less integrated.

### `compose` (deterministic)

`render/document.py: render_documentation`. Section order, ToC anchors,
tables, the Mermaid invocation graph (capped at 60 edges; larger graphs are
reduced to the entry point's depth-2 neighborhood), boilerplate workflows
collapsed into a single table, and the appendix (coverage stats, verified
clarifications, warnings). `render/lint.py` then strips emoji/pictograph
ranges and normalizes blank lines — the style guarantee of D4.

### Compliance nodes

Ch. 06. Structurally: `extract_requirements` (chunked extraction) →
`dispatch_verify` (one isolated `VerifyPayload` per requirement) → parallel
`verify_requirement` → `evidence_rescue` (bounded tool loops for the
`Not verifiable` subset) → `compose_compliance`.

## `run_pipeline` and persistence

`run_pipeline` (called by the CLI) owns the run lifecycle:

1. Opens `<project>/.guardian_cache/checkpoints.sqlite`
   (`sqlite3.connect(..., check_same_thread=False)` — map branches run in
   worker threads) and wraps it in a `SqliteSaver` with a `JsonPlusSerializer`
   whose `allowed_msgpack_modules` explicitly whitelists every pydantic model
   in `model/ir.py` and `model/summaries.py`. Without the whitelist, LangGraph
   (correctly) refuses to deserialize unknown classes from a checkpoint.
2. Generates a `thread_id` per run and stores it in
   `.guardian_cache/last_run_id`; `--resume` reads it back and, if that thread
   has checkpoints, invokes the graph with `inputs=None`, which is LangGraph's
   convention for "continue from the last checkpoint".
3. Streams `updates` events to the CLI's `on_event` callback and finally reads
   the terminal state with `graph.get_state(config)`.

File writing happens in `cli.py`, not in graph nodes — nodes stay pure
(state in, state out), which keeps them replayable from checkpoints without
side-effect duplication.
