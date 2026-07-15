# 01 — Requirements and design decisions

## The problem

Given the full directory of a UiPath process (usually built on the
[REFramework](https://github.com/UiPath/ReFrameWork) template: `Main.xaml`,
`project.json`, `Data/Config.xlsx`, `Framework/*.xaml`, ad-hoc business
workflows, execution logs), produce with a **local LLM**:

1. Professional technical documentation as Obsidian-ready Markdown — corporate
   tone, English, no emojis, improvement suggestions at the end.
2. Optionally, given the PDD (Process Definition Document, as Markdown/text), a
   compliance report stating whether the code fulfils it.

Constraints that shaped everything:

- **Projects of any size.** A real enterprise process can have hundreds of
  workflows totalling tens of megabytes of XAML. Nothing may assume "it fits in
  the context window".
- **Local models.** The endpoint serves gemma and gpt-oss over an
  OpenAI-compatible API. They support tool calling, but they are less reliable
  than hosted frontier models at following schemas, so every LLM interaction
  needs a fallback.
- **Corporate machines.** Installation must work with plain `pip` + `venv`
  behind a proxy (the same constraint the predecessor project,
  [obsidian-agent](https://github.com/jmonaste/obsidian-agent), already solved).

## Requirements analysis

| # | Requirement | Design consequence |
|---|-------------|--------------------|
| R1 | Input is a whole project directory | A deterministic scanner classifies every file; raw files never reach the LLM (ch. 03) |
| R2 | The agent "reads everything, then decides what/how to analyze" | Ingestion builds a full inventory + call graph first; a `plan` node (LLM) decides focus areas before any per-workflow analysis (ch. 02) |
| R3 | REFramework-aware | Fingerprint detection with explicit evidence; boilerplate template workflows are documented as a group, not expanded (ch. 03) |
| R4 | Any project size | Layered context strategy: compress, isolate, write, select (ch. 04) |
| R5 | Professional, emoji-free, Obsidian-ready output | The LLM writes *prose sections* only; the document skeleton, tables, ToC and Mermaid diagram are assembled deterministically, and a lint pass enforces style (ch. 02) |
| R6 | Optional PDD -> compliance verdicts | A separate subgraph with per-requirement isolated verification and deterministic evidence collection (ch. 06) |
| R7 | Local LLM via OpenAI-compatible endpoint | One gateway class owns all model I/O: structured output with a retry ladder, two model slots (ch. 05) |

## Architectural decisions

Each decision below records the context, the choice, the rejected
alternatives, and where to see it in code.

### D1 — Deterministic parsing before any LLM call

**Decision.** A UiPath `.xaml` file is Windows Workflow Foundation XML in which
roughly 90 % of the bytes are designer metadata (view state, geometry, debug
symbols, namespace declarations). `ingest/xaml_parser.py` reduces each file to
a compact typed IR (`model/ir.py: WorkflowIR`) with zero LLM involvement —
typically 8–50x smaller than the raw file.

**Why.** Three reasons. (a) *Cost/latency*: tokens are the scarce resource with
a local model; spending them on `<sap2010:WorkflowViewState.IdRef>` is pure
waste. (b) *Reliability*: structural facts (argument names, types, invocation
targets) extracted by a parser are always exact; the same facts "read" by an
LLM can be hallucinated. The renderer therefore builds argument and config
tables from the IR, never from model output. (c) *Determinism*: two runs over
the same project produce the same IR, which makes caching by content hash
possible (D6).

**Rejected alternative.** Feeding raw XAML with a big-context model. Even with
a 128k window, a medium project does not fit, attention degrades long before
the window is full, and every run would re-pay the full token cost.

### D2 — Agentic exploration instead of embeddings/RAG

**Decision.** No vector database, no embeddings. The pipeline is a *structured*
analysis (every workflow is visited exactly once); a small bounded ReAct-style
agent with read-only tools (`tools.py`) is used only afterwards, to answer the
specific questions the structured pass could not (`gapfill` node, and the
compliance `evidence_rescue`).

**Why.** The corpus is not an unstructured document pile: it has an explicit
structure (the invocation graph) that tells us exactly what to read and in what
order. Retrieval adds an embedding model dependency (another service on the
corporate endpoint), an index lifecycle, and chunking artifacts — for no gain
when full coverage is required anyway. This mirrors the obsidian-agent finding
that Claude-Code-style tool navigation beats RAG for structured corpora; see
[LangChain, "Beyond RAG: agent search"](https://www.langchain.com/blog/beyond-rag-implementing-agent-search-with-langgraph-for-smarter-knowledge-retrieval).

**Trade-off accepted.** Token-overlap scoring (not semantic similarity) selects
the summaries shown to each compliance verification. It is crude but
transparent, dependency-free, and errors degrade gracefully: the verifier can
still say "Not verifiable", which triggers the agentic rescue pass.

### D3 — Bottom-up map-reduce over the call graph, in waves

**Decision.** Per-workflow summarization (the *map* phase) walks the call graph
leaves-first (`CallGraph.bottom_up_order`). Each wave is dispatched with the
LangGraph **Send API** so its members run in parallel; a caller is only
summarized after all its callees, and its context contains their **one-line
digests**, not their code.

**Why.** A workflow's meaning depends on what it invokes. Two naive orders
fail: top-down means summarizing `Main.xaml` while knowing nothing about the
13 workflows it orchestrates; random order means callers see raw callee IRs
(context explosion) or nothing. Bottom-up with digests gives each map call a
small, complete context — the classic map-reduce shape recommended for
LangGraph fan-out work (see
[Send API / map-reduce](https://langchain-ai.github.io/langgraph/how-tos/map-reduce/) and
[scaling trade-offs](https://medium.com/@linafaik/scaling-langgraph-agents-parallelization-subgraphs-and-map-reduce-trade-offs-5af5c357b995)).

**Implementation note.** LangGraph's `Send` dispatches everything in one
super-step, so strict ordering *between* waves is implemented as a loop:
`dispatch` pops the next wave and the conditional edge either fans out
`Send("summarize", payload)` or routes to `reduce` when no waves remain
(ch. 02). Cycles in the invocation graph (rare but legal) are broken by
flushing the remaining strongly-connected workflows into a final wave, so the
loop always terminates.

### D4 — The LLM writes sections; Python writes the document

**Decision.** `render/document.py` owns the output. The LLM produces typed
prose sections (`NarrativeSections`) and per-workflow summaries
(`WorkflowSummary`); the composer deterministically assembles title, ToC,
metadata/dependency/argument/config tables, the Mermaid invocation graph,
findings tables and appendix, then `render/lint.py` strips emojis and
normalizes whitespace.

**Why.** (a) Structural data must be exact (D1). (b) Style requirements
("no emojis, corporate") are *enforced*, not requested — a prompt instruction
is a suggestion; a lint pass is a guarantee. (c) Document structure stays
stable across runs and models, which matters for a deliverable people diff and
review.

### D5 — Two model slots (worker / lead)

**Decision.** `Settings` exposes `GUARDIAN_WORKER_MODEL` (map phase and
per-requirement verification: many small calls) and `GUARDIAN_LEAD_MODEL`
(plan, reduce, refine, requirement extraction, evidence rescue: few hard
calls). Either defaults to the other, so a single-model setup needs zero
configuration.

**Why.** The map phase is embarrassingly parallel and latency-bound — a
smaller/faster model (gemma) there can halve wall-clock time on big projects,
while the synthesis steps benefit from the stronger model (gpt-oss). The cache
key includes the model name, so switching models invalidates only what it
should.

### D6 — Content-hash summary cache + SQLite checkpointer

**Decision.** Two independent persistence layers under
`<project>/.guardian_cache/`:

- `SummaryCache` (`graph/cache.py`): per-workflow summaries keyed by
  `sha256(xaml):model:PROMPT_VERSION`. A re-run only re-summarizes changed
  workflows.
- A LangGraph `SqliteSaver` checkpointer (`graph/builder.py: run_pipeline`)
  records every super-step; `--resume` continues an interrupted run from the
  last checkpoint.

**Why two layers?** They answer different questions. The cache survives
*across* runs and models the fact that summaries are pure functions of
(file content, prompt, model). The checkpointer survives *within* a run and
models pipeline progress (useful when a 300-workflow map phase dies at 80 %).
The cache alone would already make a crashed re-run cheap; the checkpointer
makes it free.

**Note on serialization.** LangGraph's msgpack serializer refuses to
deserialize arbitrary classes by default (a deliberate security measure);
`run_pipeline` explicitly registers every pydantic model in
`allowed_msgpack_modules`. The SQLite connection is created with
`check_same_thread=False` because map-phase nodes run in worker threads.

### D7 — Every LLM node degrades, none aborts

**Decision.** Each LLM-calling node wraps the call and, on failure, substitutes
a deterministic fallback and appends to `state["warnings"]`: `plan` falls back
to a default plan, `summarize` to a "not analyzed" stub, `reduce` to a listing
of per-workflow purposes, `gapfill`/`refine` keep the previous narrative,
compliance verdicts fall back to "Not verifiable". Warnings surface in the
document appendix and on the CLI.

**Why.** A documentation run over 200 workflows must not die at workflow 173
because the endpoint hiccuped once. Truthfulness is preserved because every
fallback *states what happened* instead of pretending — a document that says
"this workflow could not be analyzed" is honest; a crashed run 40 minutes in
is useless.

### D8 — Evidence is recorded, never trusted

**Decision.** When an agent (gap-fill or compliance rescue) claims something
about the project, the workflow paths cited as evidence come from a *recorder*:
`build_project_tools(inv, recorder)` appends the path of every workflow the
agent actually read. The model's own citations are ignored.

**Why.** Inherited directly from obsidian-agent, where references were parsed
from `read_note` tool messages rather than from the answer text. Models cite
plausible-looking paths they never opened; instrumenting the tools makes the
citation a side effect of the read, which cannot be hallucinated.

### D9 — Secrets are redacted at the parsing boundary

**Decision.** `parse_config_xlsx` replaces the value of any config entry whose
*name* matches `password|secret|token|api[_-]?key|credential` with
`(redacted)` — before the entry enters the inventory.

**Why.** Everything in the inventory eventually flows to two sinks: LLM
prompts and the generated document. Redacting at the single entry point means
no prompt, no cache file, no checkpoint and no deliverable can leak a value,
regardless of what any later code does. (Real deployments should keep secrets
in Orchestrator assets anyway — the docs generator just refuses to make a
leak worse.)

### D10 — Stack continuity with obsidian-agent

**Decision.** Same foundations as the first agent: LangGraph `StateGraph`
(hand-built, no prebuilt `create_react_agent`), `ChatOpenAI(base_url=...)`
against the corporate endpoint, `pydantic-settings` for typed `.env` config,
`typer` + `rich` CLI, `pip/venv`-first installation, GPL-3.

**Why.** That stack is *proven on the target machines* — endpoint quirks,
proxy issues and install paths were already debugged once. The graph is
hand-built because the pipeline is a static workflow with one dynamic fan-out,
not an open-ended agent loop; explicit nodes and edges are easier to test,
checkpoint and reason about than a prebuilt agent abstraction.

### D11 — Unverified model output never ships

**Decision.** Model output that would land in a deliverable passes a
verification layer first, each part independently switchable in settings:
a *critic* pass audits the narrative for claims the summaries do not support
(they become gap-fill questions) and for cited workflow paths that do not
exist; every model-reported code smell must survive an adversarial skeptic
tool-loop (CONFIRM or REFUTE from actual reads) before becoming a finding;
compliance evidence citations are validated against the inventory, and
compliant verdicts whose evidence fails validation are re-checked by the
rescue agent. A cheap quality gate also retries weak worker summaries with
the lead model.

**Why.** D7 handles the model *failing*; D11 handles the model *succeeding
convincingly and being wrong* — the dangerous case for an audit deliverable.
Local models invent paths, overstate smells and claim compliance without
grounds; verification converts each of those from a silent error into either
a corrected statement or an explicit warning. Fail-open everywhere: when a
verification pass itself fails, the original content is kept — losing a real
issue is worse than keeping a doubtful one.

### D12 — The gateway absorbs endpoint flakiness

**Decision.** All transport robustness lives in `GuardianLLM`: structured
output negotiates a method ladder (guided JSON → tool calling → tolerant
plain-JSON parsing); transient endpoint errors (429, 502/503/504, timeouts)
are retried with exponential backoff (`GUARDIAN_LLM_RETRIES`,
`GUARDIAN_LLM_RETRY_BASE_DELAY`); a dead endpoint raises
`GuardianLLMUnavailable`, which aborts the ladder instead of burning more
calls; and every call outcome feeds thread-safe live counters
(`LLMCallStats`) that the CLI renders in real time. The `rpa-guardian tune`
command probes all of this against the configured endpoint on demand.

**Why.** Local endpoints under parallel load produce 429s and gateway
timeouts routinely; handled per-node this would be scattered try/except and
lost summaries, handled in the gateway it is one policy every call inherits.
The distinction between "the endpoint answered something unusable" (fall down
the ladder) and "the transport is dead" (stop trying) is what keeps a flaky
run slow-but-complete and a dead-endpoint run fast-failing instead of hanging.

## What was deliberately left out (v0.1)

- **PDD in .docx/.pdf** — the PDD is accepted as Markdown/text only; document
  conversion is a separate concern (and the company PDDs can be exported).
- **Embeddings/semantic retrieval** — see D2; revisit only if compliance
  relevance selection proves too weak on real PDDs.
- **`.cs` coded workflows** — modern UiPath projects can contain C# coded
  workflows; the scanner counts them as "other files" but does not parse them.
- **Cross-project analysis** (libraries referenced as packages) — dependencies
  are documented from `project.json`, not fetched and analyzed.
