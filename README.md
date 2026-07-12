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
   for changed workflows.
   |
   v
 reduce (LLM)        process narrative from summaries only (hierarchical
   |                 degradation keeps it inside the context budget)
   v
 gap-fill (LLM agent, bounded tool loop)
   answers the narrative's open questions with read-only project tools;
   evidence is recorded from actual tool reads, not model claims
   |
   v
 compose (deterministic)
   Markdown skeleton, ToC, argument/config/dependency tables, Mermaid
   invocation graph, prioritized findings (static checks + LLM smells),
   emoji-free corporate style enforced by lint
   |
   +--(if --pdd)--> compliance subgraph:
        extract requirements -> verify each in parallel (isolated context)
        -> agentic evidence rescue for 'Not verifiable' items
        -> traceability matrix
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

Fill in `.env`:

| Var | Meaning |
|-----|---------|
| `OPENAI_BASE_URL` | Your OpenAI-compatible endpoint (ends in `/v1`). |
| `OPENAI_API_KEY` | Any non-empty string if the endpoint ignores it. |
| `GUARDIAN_WORKER_MODEL` | Model for the per-workflow map phase (e.g. `gemma`). |
| `GUARDIAN_LEAD_MODEL` | Model for planning/narrative/compliance (e.g. `gpt-oss`). |
| `GUARDIAN_MAX_CONCURRENCY` | Parallel LLM calls in the map phase (default 4). |
| `GUARDIAN_AGENT_MAX_ITERATIONS` | Tool-loop cap for the evidence agents (default 8). |
| `GUARDIAN_IR_MAX_CHARS` | Char budget per workflow IR (default 12000). |

Both models must support OpenAI-style **tool calling**; if a structured call
fails, the client retries once and then falls back to JSON parsing.

## Usage

```bash
# documentation only
rpa-guardian analyze /path/to/UiPathProject -o out/

# documentation + PDD compliance report
rpa-guardian analyze /path/to/UiPathProject --pdd PDD.md -o out/

# useful flags
rpa-guardian analyze ... --resume          # continue an interrupted run
rpa-guardian analyze ... --no-cache        # ignore cached workflow summaries
rpa-guardian analyze ... --lead-model gpt-oss --worker-model gemma
rpa-guardian analyze ... -v                # show every pipeline event
```

Outputs land in `out/` as `<Project>-Documentation.md` and
`<Project>-Compliance.md`; drop them into any Obsidian vault. The PDD must be
Markdown or plain text.

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
LLM's per-workflow observations. Secret-looking Config values are redacted
before they ever reach the LLM or the document.

## Development

```bash
pip install -e ".[dev]"
pytest          # 18 tests: parser, scanner, checks, renderer, full graph (fake LLM)
```

The test suite runs the entire pipeline against a bundled minimal REFramework
fixture using an injected fake LLM — no endpoint needed. The parser is also
validated against the official
[UiPath/ReFrameWork](https://github.com/UiPath/ReFrameWork) template.

## Project layout

```
src/rpa_code_guardian/
  config.py            # typed settings from .env / CLI
  llm.py               # single LLM gateway: structured output + tool loops
  checks.py            # deterministic quality checks over the IR
  tools.py             # bounded read-only project tools for evidence agents
  ingest/              # scanner, XAML->IR parser, project.json, Config.xlsx, logs
  model/               # IR models + LLM structured-output schemas
  graph/               # LangGraph state, nodes, compliance subgraph, builder, cache
  render/              # deterministic md assembly + corporate style lint
  cli.py               # typer entrypoint (rpa-guardian)
tests/                 # fixture REFramework project + fake-LLM pipeline tests
```
