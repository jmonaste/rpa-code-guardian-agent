# 04 — Context engineering

How the agent handles a 5-workflow demo and a 500-workflow monster with the
same code. The framing follows the four levers described in
[Anthropic's "Effective context engineering for AI agents"](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
and [Schmid's context-engineering series](https://www.philschmid.de/context-engineering-part-2):
**compress** (make things smaller), **isolate** (give sub-tasks their own
clean window), **write** (persist context outside the window), **select**
(retrieve only what's needed now).

## Compress — XAML to IR

Chapter 03 in one sentence: a deterministic parser removes ~90 % designer
noise before any token is spent, and structural facts move out of prompts
entirely (they are rendered from the IR by the composer).

The second compression stage is *summaries as currency*: after the map phase,
no stage ever looks at an IR again unless it explicitly asks (gap-fill tools).
The reduce node reasons over `WorkflowSummary` objects; the compliance
verifier reasons over summaries; callers see callee **one-liners**. Each hop
up the hierarchy trades detail for coverage — the classic hierarchical
summarization pattern.

## Isolate — one workflow, one window

The map phase (`dispatch`/`summarize`, ch. 02) is the isolation lever:

- Each `Send` payload is self-contained (IR text + callee digests + plan
  notes). The node receives *the payload instead of the state*, so a branch
  physically cannot read what it should not depend on.
- Bottom-up ordering makes the isolation *sufficient*: by the time a caller
  is summarized, its callees are already one-line digests. The caller's
  context holds everything relevant about its subtree at ~100 chars per
  callee instead of ~10k.

The same shape repeats in compliance verification: one requirement per
`Send`, with only the process overview, the 4 most relevant summaries and a
config excerpt (ch. 06).

The gap-fill and evidence-rescue agents are isolated differently: each
question gets a **fresh tool loop** (fresh message list, fresh evidence
recorder), so exploration for question 1 cannot pollute the context of
question 2. Sub-agent isolation without sub-agent infrastructure.

## Write — two persistence layers

- **`SummaryCache`** (`graph/cache.py`): `sha256(file):model:PROMPT_VERSION ->
  WorkflowSummary`, JSON on disk, thread-safe via a lock (map branches run
  concurrently), persisted by `dispatch` between waves and by `reduce` at the
  end. Bump `PROMPT_VERSION` when the map prompt changes — that is the cache's
  correctness contract: a summary is a pure function of (content, prompt,
  model).
- **SQLite checkpointer** (`run_pipeline`): LangGraph
  [persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
  of every super-step, enabling `--resume`. See ch. 02 for the serializer
  whitelist details.

Both live under `<project>/.guardian_cache/` — next to the analyzed project,
not the tool, so caches never mix across projects and deleting the directory
is a full reset.

## Select — bounded tools, recorded evidence

`tools.py: build_project_tools()` gives the agents a read-only, capped view of
the inventory (obsidian-agent's vault-tool philosophy):

| Tool | Purpose | Bound |
|------|---------|-------|
| `list_workflows` | orientation | 80 lines + `(showing N of M)` footer |
| `read_workflow` | the parsed IR of one workflow | `GUARDIAN_IR_MAX_CHARS` (12k) |
| `search_project` | grep over IR renderings + config | 25 hits, then an explicit "narrow the query" footer |
| `read_config` | the workbook | 120 entries |
| `read_logs` | the digest | already bounded at ingestion |
| `read_xaml_source` | raw XAML window | 6k chars/page, explicit `[more: offset=...]` pagination |

Design points:

- Every bound is *visible to the model* ("showing 80 of 213"), so it knows
  results were capped and can narrow instead of assuming completeness — the
  single most effective trick carried over from obsidian-agent.
- `read_workflow` accepts fuzzy paths (basename match) and answers a miss with
  candidate suggestions, because local models frequently mangle exact paths.
- The `recorder` closure appends every workflow actually read; evidence in
  `GapAnswer` and `ComplianceItem` comes from there, not from model claims
  (decision D8).

## Every budget in the system

All limits in one table — these are the knobs to revisit when the corporate
endpoint's real context size is known:

| Constant | Value | Where | Bounds |
|----------|-------|-------|--------|
| `GUARDIAN_IR_MAX_CHARS` | 12 000 | settings | one workflow IR in a map prompt / `read_workflow` |
| `MAX_OUTLINE_NODES` / `MAX_OUTLINE_DEPTH` | 350 / 12 | xaml_parser | outline size inside the IR |
| per-list caps (variables 40, logs 25, ...) | — | `WorkflowIR.to_context` | any single IR section |
| `NARRATIVE_CONTEXT_BUDGET` | 28 000 | nodes.py | summaries shown to `reduce` |
| `MAX_GAP_QUESTIONS` | 5 | nodes.py | gap-fill loops per run |
| `GUARDIAN_AGENT_MAX_ITERATIONS` | 8 | settings | tool calls per agent loop |
| `LIST_MAX` / `SEARCH_MAX` / `RAW_WINDOW_CHARS` | 80 / 25 / 6 000 | tools.py | tool outputs |
| `PDD_CHUNK_CHARS` | 20 000 | compliance.py | PDD slice per extraction call |
| `MAX_REQUIREMENTS` | 60 | compliance.py | requirement list size |
| `RELEVANT_SUMMARIES` | 4 | compliance.py | summaries per verification |
| rescue cap | 8 | compliance.py | tool loops in evidence rescue |
| `GUARDIAN_MAX_CONCURRENCY` | 4 | settings | parallel LLM calls |
| `recursion_limit` | 200 | builder.py | graph super-steps (~95 waves) |

## Hierarchical degradation in `reduce`

The one budget that needs an algorithm rather than a constant. The reduce
prompt must mention *every* workflow while fitting 28k chars, so
`_summaries_context` degrades in stages:

1. **Full block** (purpose, steps, I/O, errors, externals) for business
   workflows.
2. **One-liner** for anything the plan or the summary itself marked as
   REFramework boilerplate — template code adds nothing to a narrative.
3. If still over budget, the largest full blocks are **demoted to one-liners**
   (largest first, so the fewest workflows lose detail), with an explicit
   `(N summaries shown as one-liners to fit the context budget)` marker so
   the model — and anyone reading the prompt in a trace — knows degradation
   happened.

The same idea, one level down, appears in `dispatch`: if a callee has no
summary yet (cache miss + failure), its digest falls back to the IR's
deterministic `one_liner()` — degraded, but never absent.

## What was measured

On [UiPath/ReFrameWork](https://github.com/UiPath/ReFrameWork) (13 workflows):
238 238 chars of XAML -> 31 899 chars of IR (7.5x) before any LLM call; the
biggest single file (`Main.xaml`, 55k) compressed 8.3x. Business-heavy
workflows with selectors and view state compress far more. The appendix of
every generated document reports the same coverage numbers for its project —
the system measures itself on every run.
