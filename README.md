# rpa-code-guardian-agent

A LangGraph agent that ingests a **complete UiPath project** (REFramework-aware),
analyzes it with a **local LLM** behind an OpenAI-compatible endpoint (gemma,
gpt-oss, ...), and generates:

1. **`<Project>-Documentation.md`** — professional technical documentation,
   Obsidian-ready, with improvement suggestions at the end.
2. **`<Project>-Compliance.md`** (optional, when a PDD is provided) — a
   requirements traceability matrix stating whether the code complies with the
   Process Definition Document.

Successor to [obsidian-agent](https://github.com/jmonaste/obsidian-agent), built
on the same stack that already works on restricted corporate machines:
LangGraph + `ChatOpenAI(base_url=...)` + typed settings + typer CLI.

> **Studying the implementation?** [`docs/`](docs/README.md) contains full
> engineering documentation: requirements and design decisions, pipeline
> architecture, the XAML ingestion deep dive, context engineering, the LLM
> gateway, the compliance subgraph, the testing strategy, and annotated
> references for every technology used.

## How it works

Projects can be arbitrarily large, so no raw file is ever handed to the LLM.
The pipeline applies the four context-engineering levers (compress / isolate /
write / select):

```
project dir
   |
   v
 ingest (deterministic, no LLM)
   XAML -> compact IR (arguments, variables, invocations, logs, selectors,
   activity outline; designer noise stripped: typically 10-50x smaller)
   + project.json, Config.xlsx, execution-log digest, call graph,
   REFramework fingerprint
   |
   v
 plan (LLM)          what kind of process this is, what to focus on
   |
   v
 map (LLM, parallel Send API)
   one workflow per isolated context, walking the call graph bottom-up:
   callers see their callees as one-line digests, never as code.
   Summaries are cached on disk by content hash - re-runs only pay
   for changed workflows. A weak worker answer (empty key logic,
   generic purpose) is retried once with the lead model.
   |
   v
 reduce (LLM)        process narrative from summaries only (hierarchical
   |                 degradation keeps it inside the context budget)
   v
 critic (LLM + deterministic)
   grounding audit of the draft: cited workflow paths must exist in the
   inventory; claims the summaries do not support become open questions
   |
   v
 findings (deterministic + LLM skeptics)
   static checks merge with the model's per-workflow smells; each smell
   is adversarially verified with the project tools - a skeptic loop must
   CONFIRM or REFUTE it from actual reads, refuted smells are dropped
   |
   v
 gap-fill (LLM agent, bounded tool loop)
   answers the narrative's open questions with read-only project tools;
   evidence is recorded from actual tool reads, not model claims
   |
   v
 compose (deterministic)
   Markdown skeleton, ToC, argument/config/dependency tables, Mermaid
   invocation graph, prioritized findings, model prose normalized
   (headings demoted, block spacing fixed), emoji-free corporate style
   enforced by lint
   |
   +--(if --pdd)--> compliance subgraph:
        extract requirements -> verify each in parallel (isolated context)
        -> agentic evidence rescue for 'Not verifiable' verdicts AND for
           compliant verdicts whose cited evidence fails validation
        -> traceability matrix (hallucinated citations dropped)
```

A SQLite checkpointer under `<project>/.guardian_cache/` records every
super-step, so an interrupted run on a big project resumes with `--resume`.

## Setup

Requires Python 3.12+. Pick whichever package manager your machine allows.

### Option A — pip + venv (works on restricted / corporate laptops)

```bash
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate

python -m pip install --upgrade pip
pip install -e .

cp .env.example .env              # then edit .env  (Windows: copy .env.example .env)
```

For development extras (tests): `pip install -e ".[dev]"`.

> **Behind a corporate proxy / internal index?** Point pip at your mirror, e.g.
> `pip install -e . --index-url https://<your-artifactory>/api/pypi/pypi/simple`.
> If SSL inspection breaks TLS, add `--trusted-host <host>`. To make it permanent
> put those under `[global]` in `pip.conf` (`~/.config/pip/pip.conf` on
> macOS/Linux, `%APPDATA%\pip\pip.ini` on Windows).

### Option B — conda / miniforge

```bash
conda create -n rpa-guardian python=3.12 -y
conda activate rpa-guardian
pip install -e .
cp .env.example .env
```

### Option C — uv

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env
```

Fill in `.env` (run `rpa-guardian tune` to find the right values for your
endpoint — see below):

**Endpoint**

| Var | Meaning |
|-----|---------|
| `OPENAI_BASE_URL` | Your OpenAI-compatible endpoint (ends in `/v1`). |
| `OPENAI_API_KEY` | Any non-empty string if the endpoint ignores it. |
| `GUARDIAN_WORKER_MODEL` | Model for the per-workflow map phase (e.g. `gemma`). |
| `GUARDIAN_LEAD_MODEL` | Model for planning/narrative/compliance (e.g. `gpt-oss`). |
| `GUARDIAN_TEMPERATURE` | Sampling temperature (default 0, keep it for reproducible docs). |
| `GUARDIAN_VERIFY_SSL` | Verify the endpoint's TLS certificate (default `true`). Set `false` only for a trusted local endpoint with a self-signed certificate. |
| `GUARDIAN_REQUEST_TIMEOUT` | HTTP timeout in seconds per LLM call (default 120; local models can be slow). |
| `GUARDIAN_MAX_TOKENS` | Max completion tokens per call (default 8192). Reasoning models (gpt-oss) spend tokens thinking first; too low a budget truncates the JSON and validation fails. |
| `GUARDIAN_LLM_RETRIES` | Retries per call on transient endpoint errors — 429 rate limits, 502/503/504, timeouts (default 3, exponential backoff). |
| `GUARDIAN_LLM_RETRY_BASE_DELAY` | Initial backoff in seconds; doubles per retry, capped at 60s (default 2). |

**Pipeline**

| Var | Meaning |
|-----|---------|
| `GUARDIAN_MAX_CONCURRENCY` | Parallel LLM calls in the map phase (default 4). |
| `GUARDIAN_AGENT_MAX_ITERATIONS` | Tool-loop cap for the evidence agents (default 8). |
| `GUARDIAN_IR_MAX_CHARS` | Char budget per workflow IR (default 12000). |
| `GUARDIAN_USE_CACHE` | Reuse cached per-workflow summaries when the file has not changed (default `true`). |

**Agentic verification passes** (each costs extra LLM calls; disable to go faster)

| Var | Meaning |
|-----|---------|
| `GUARDIAN_AUDIT_NARRATIVE` | Critic pass over the narrative: unsupported claims become gap-fill questions (default `true`, one extra lead call). |
| `GUARDIAN_VERIFY_SMELLS` | Adversarially verify model-reported code smells before they become findings; unconfirmed smells are dropped (default `true`). |
| `GUARDIAN_ESCALATE_WEAK_SUMMARIES` | Retry a workflow summary with the lead model when the worker's output fails a cheap quality gate (default `true`). |

Structured output is negotiated automatically per call: guided JSON
(`json_schema`, the most reliable when the endpoint supports it, e.g. vLLM),
then OpenAI-style tool calling, then plain-completion JSON parsing that
tolerates reasoning blocks, schema-named wrappers and code fences. Transient
endpoint errors are retried with exponential backoff; a dead endpoint fails
fast instead of burning calls, and the affected node degrades to a
deterministic fallback rather than crashing the run.

## Usage

### `rpa-guardian analyze` — document and audit a project

```bash
# documentation only
rpa-guardian analyze /path/to/UiPathProject -o out/

# documentation + PDD compliance report
rpa-guardian analyze /path/to/UiPathProject --pdd PDD.md -o out/
```

| Flag | Meaning |
|------|---------|
| `--pdd PDD.md` | Also generate the compliance report against this PDD (Markdown or plain text). |
| `-o, --output DIR` | Where the Markdown files are written (default `out/`). |
| `--resume` | Continue the previous interrupted run of this project (SQLite checkpoint). |
| `--no-cache` | Ignore cached per-workflow summaries. |
| `--base-url`, `--worker-model`, `--lead-model`, `--max-concurrency` | Override the corresponding `.env` values for this run. |
| `-v, --verbose` | Show every pipeline event. |
| `--log-level LEVEL` | Console log level: `debug`, `info`, `warning` (default) or `error`. `warning` surfaces endpoint trouble (429/5xx retries); `debug` shows every LLM call with timing and which structured-output method served it. |
| `--log-file PATH` | Also write **full debug logs with timestamps** to a file, regardless of the console level — review a long run afterwards. |

While running, the console shows live progress, including a **real-time
endpoint counter** so you can see at a glance whether the LLM is responding:

```
ingest: 21 workflows (19 to analyze in 3 waves), REFramework=True
plan done
map: 7/19 workflows analyzed · llm: 23 ok, 2 retried
reduce done
critic: narrative grounded
findings: 9 issue(s) (2 high, 6 medium, 1 low)
gapfill: 3 open question(s) answered with project evidence
compose done
Documentation written: out/MyProcess-Documentation.md
Done in 4m 12s — 21 workflows analyzed, 63 llm calls ok, 2 retried, 0 warning(s)
```

`llm:` counts endpoint outcomes live: **ok** (green) — the endpoint returned a
completion; **retried** (yellow) — a transient 429/5xx/timeout triggered a
backoff retry; **failed** (red) — the endpoint stayed down through every retry.
If the ok counter stops ticking, a call is hanging or the endpoint is gone.

Outputs land in `out/` as `<Project>-Documentation.md` and
`<Project>-Compliance.md`; drop them into any Obsidian vault.

### `rpa-guardian tune` — probe the endpoint, recommend parameters

Run it on demand — after setting up `.env`, after switching models, or whenever
runs feel slow or flaky. It probes the configured endpoint **through the same
LLM gateway the pipeline uses** and prints a recommended configuration:

```bash
rpa-guardian tune                              # full probe + concurrency sweep
rpa-guardian tune --skip-sweep                 # quick check (no sweep)
rpa-guardian tune --levels 1,2,4 --calls 8     # custom sweep
rpa-guardian tune --log-level debug            # see each probe call
```

What it checks:

1. **Connectivity** — `GET /models`: is the endpoint reachable, which model ids
   does it serve, and are your configured worker/lead models among them.
2. **Structured output support per model** — tries `json_schema` and
   `function_calling` once each plus the plain-JSON fallback, with latencies:
   shows which method your calls will actually be served by.
3. **Truncation** — detects whether the configured `GUARDIAN_MAX_TOKENS` cuts
   completions short (`finish_reason=length`) and suggests raising it.
4. **Concurrency sweep** — fires probe calls at increasing parallelism
   (`--levels`, `--calls` per level) and reports errors, retries, average
   latency and throughput per level.

It ends with a ready-to-paste **Recommended .env** block:
`GUARDIAN_MAX_CONCURRENCY` (the highest level that stays clean and still
improves throughput ≥15%), plus `GUARDIAN_MAX_TOKENS` /
`GUARDIAN_REQUEST_TIMEOUT` when the probes indicate them.

## Generated document structure

**Documentation**: executive summary, project overview (metadata + dependency
tables), process description, architecture (REFramework states + Mermaid
invocation graph), configuration tables, per-workflow reference (purpose,
argument tables, key steps, error handling), exception handling and logging,
external systems, execution-log observations, prioritized improvement
suggestions, and an appendix with coverage stats and verified clarifications.

**Compliance**: scope and method, verdict summary, requirements traceability
matrix (`Compliant / Partially compliant / Non-compliant / Not verifiable`,
each with workflow-path evidence), gaps and deviations, conclusion.

Findings merge two sources: deterministic Workflow-Analyzer-style checks
(empty catches, hardcoded delays/paths, missing/unused Config keys, orphan
workflows, oversized workflows, missing annotations or logging) and the
LLM's per-workflow observations — the latter only after an adversarial
verification loop confirms them from actual reads of the project. Workflow
paths cited as compliance evidence are validated against the inventory;
hallucinated citations never reach the report. Secret-looking Config values
are redacted before they ever reach the LLM or the document.

## Development

```bash
pip install -e ".[dev]"
pytest          # 61 tests: parser, scanner, checks, renderer, LLM gateway,
                # agentic passes, CLI, tune utility, full graph (fake LLM)
```

The test suite runs the entire pipeline against a bundled minimal REFramework
fixture using an injected fake LLM — no endpoint needed. The parser is also
validated against the official
[UiPath/ReFrameWork](https://github.com/UiPath/ReFrameWork) template.

## Project layout

```
src/rpa_code_guardian/
  config.py            # typed settings from .env / CLI
  llm.py               # single LLM gateway: structured-output ladder, retry with
                       #   backoff, live call stats, tool loops
  checks.py            # deterministic quality checks over the IR
  tools.py             # bounded read-only project tools for evidence agents
  tune.py              # endpoint probes + parameter recommendation (rpa-guardian tune)
  ingest/              # scanner, XAML->IR parser, project.json, Config.xlsx, logs
  model/               # IR models + LLM structured-output schemas
  graph/               # LangGraph state, nodes (incl. critic + smell verification),
                       #   compliance subgraph, builder, cache
  render/              # deterministic md assembly, prose normalization, style lint
  cli.py               # typer entrypoint (rpa-guardian analyze / tune)
tests/                 # fixture REFramework project + fake-LLM pipeline tests
```
