# Documentation

Study-oriented documentation of how rpa-code-guardian is designed and built.
It is written to be read next to the source code: every chapter names the
modules, classes and functions it explains, and every non-obvious decision is
justified. Read in order the first time; each chapter also stands alone.

| # | Chapter | What it covers |
|---|---------|----------------|
| 01 | [Requirements and design decisions](01-Requirements-And-Design.md) | The problem, the requirements analysis, and every architectural decision with its rationale and the alternatives that were rejected. |
| 02 | [Pipeline architecture](02-Pipeline-Architecture.md) | The LangGraph graph: state, reducers, every node, the wave loop, control flow, concurrency, checkpointing. |
| 03 | [XAML ingestion](03-XAML-Ingestion.md) | Anatomy of UiPath XAML, what is noise vs signal, how the parser builds the IR, the call graph, the REFramework fingerprint, Config.xlsx and log digestion. |
| 04 | [Context engineering](04-Context-Engineering.md) | How the agent handles projects of any size: the compress / isolate / write / select levers mapped to concrete code, and every context budget in the system. |
| 05 | [LLM gateway](05-LLM-Gateway.md) | The single point of contact with the model: structured output over tool calling, the retry ladder, the JSON fallback, the tool loop, and local-model quirks. |
| 06 | [Compliance subgraph](06-Compliance-Subgraph.md) | PDD requirement extraction, per-requirement isolated verification, the evidence-rescue agent, and the traceability matrix. |
| 07 | [Testing and verification](07-Testing-And-Verification.md) | The injectable fake LLM, what each fixture file exercises, what each test proves, and the smoke run against the official REFramework. |
| 08 | [References](08-References.md) | Annotated links to the primary documentation of every technology and technique used. |

## The system in one diagram

```
 project dir
    |
    v
 ingest        deterministic: XAML -> IR, project.json, Config.xlsx,
    |          log digest, call graph, REFramework fingerprint
    v
 plan          LLM (lead): what this process is, what to focus on
    |
    v
 dispatch <------------------+     bottom-up waves over the call graph
    | Send() per workflow    |
    v                        |
 summarize (parallel) -------+     LLM (worker): one workflow per isolated
    |                              context; disk-cached by content hash
    | (waves exhausted)
    v
 reduce        LLM (lead): narrative sections from summaries only
    |
    v
 findings      deterministic: static checks + LLM smells, deduped, ranked
    |
    v
 gapfill       LLM agent (lead): bounded tool loop answers the narrative's
    |          open questions; evidence recorded from actual tool reads
    v
 compose       deterministic: Markdown skeleton, tables, Mermaid, lint
    |
    +--(no PDD)--> END
    |
    +--(PDD given)--> extract_requirements -> dispatch_verify
                          | Send() per requirement
                          v
                      verify_requirement (parallel, worker)
                          |
                          v
                      evidence_rescue (lead, tool loop for 'Not verifiable')
                          |
                          v
                      compose_compliance -> END
```

## Source map

```
src/rpa_code_guardian/
  config.py            Settings (pydantic-settings), cache_dir()
  llm.py               GuardianLLM: structured(), tool_loop()          -> ch. 05
  checks.py            run_checks(): deterministic findings            -> ch. 03
  tools.py             build_project_tools(): bounded read-only tools  -> ch. 04, 06
  ingest/
    scanner.py         scan_project(), build_call_graph(), fingerprint -> ch. 03
    xaml_parser.py     parse_xaml(), _OutlineWalker                    -> ch. 03
    project_json.py    parse_project_json()                            -> ch. 03
    config_xlsx.py     parse_config_xlsx() (+ secret redaction)        -> ch. 03
    logs.py            digest_logs()                                   -> ch. 03
  model/
    ir.py              WorkflowIR, ProjectInventory, CallGraph, ...    -> ch. 03
    summaries.py       LLM structured-output schemas                   -> ch. 05
  graph/
    state.py           GuardianState, reducers, Send payloads          -> ch. 02
    nodes.py           PipelineNodes: main pipeline nodes + prompts    -> ch. 02, 04
    compliance.py      ComplianceNodes                                 -> ch. 06
    builder.py         build_graph(), run_pipeline(), checkpointer     -> ch. 02
    cache.py           SummaryCache (content-hash disk cache)          -> ch. 04
  render/
    document.py        render_documentation(), render_compliance()    -> ch. 02
    lint.py            lint_markdown(), md_cell(), md_anchor()         -> ch. 02
  cli.py               typer entry point (rpa-guardian analyze)
tests/                                                                 -> ch. 07
```
