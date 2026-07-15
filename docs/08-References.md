# 08 — References

Annotated links to the primary documentation behind every technology and
technique in this project. Grouped by what you want to study.

## LangGraph

- [LangGraph documentation](https://docs.langchain.com/oss/python/langgraph/overview)
  — current home of the docs. The concepts used here: graph API (nodes, edges,
  conditional edges), state with reducers, Send, subgraphs, persistence.
- [Graph API: state, nodes, edges, reducers](https://docs.langchain.com/oss/python/langgraph/graph-api)
  — how `Annotated[..., reducer]` state channels merge parallel writes; the
  mechanism behind `summaries`/`compliance`/`warnings` in `graph/state.py`.
- [Map-reduce with the Send API](https://langchain-ai.github.io/langgraph/how-tos/map-reduce/)
  — the fan-out pattern used by the map phase and compliance verification;
  explains why `Send` payloads replace the node's state input (our isolation
  guarantee).
- [Persistence: checkpointers and threads](https://docs.langchain.com/oss/python/langgraph/persistence)
  — thread IDs, checkpoints per super-step, resuming with `inputs=None`; the
  basis of `run_pipeline`'s `--resume`.
- [Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
  — read to understand what we deliberately did *not* do: the compliance
  stage shares the parent state, so it is plain nodes in the same graph, and
  the agent loops are plain Python (ch. 05, "Why a plain loop").
- [Scaling LangGraph agents: parallelization, subgraphs, map-reduce trade-offs](https://medium.com/@linafaik/scaling-langgraph-agents-parallelization-subgraphs-and-map-reduce-trade-offs-5af5c357b995)
  (Lina Faik) — a good practitioner treatment of when each construct pays off.
- [LangGraph Map-Reduce: parallel execution](https://machinelearningplus.com/gen-ai/langgraph-map-reduce-parallel-execution/)
  — worked Send-API example with timing comparisons.

## LangChain (model & tool layer)

- [Structured output](https://python.langchain.com/docs/how_to/structured_output/)
  — `with_structured_output` and the `method="json_schema"` /
  `"function_calling"` distinction that drives the method ladder in `llm.py`.
- [Tool calling](https://python.langchain.com/docs/how_to/tool_calling/)
  — `bind_tools`, `tool_calls` on `AIMessage`, `ToolMessage` responses: the
  raw loop implemented in `GuardianLLM.tool_loop`.
- [Custom tools with @tool](https://python.langchain.com/docs/how_to/custom_tools/)
  — docstrings become the model-facing tool descriptions; why the docstrings
  in `tools.py` are written as instructions.
- [ChatOpenAI](https://python.langchain.com/docs/integrations/chat/openai/)
  — the `base_url` parameter that points everything at a local
  OpenAI-compatible endpoint.

## Context engineering

- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
  — the canonical treatment: context as a finite resource, compaction,
  sub-agent isolation, "the smallest set of high-signal tokens".
- [Phil Schmid — Context engineering, part 2](https://www.philschmid.de/context-engineering-part-2)
  — the write / select / compress / isolate taxonomy that chapter 04 maps
  onto this codebase.
- [LangChain blog — Beyond RAG: agent search](https://www.langchain.com/blog/beyond-rag-implementing-agent-search-with-langgraph-for-smarter-knowledge-retrieval)
  — the argument for tool-driven navigation over embeddings on structured
  corpora (decision D2).

## UiPath and XAML

- [UiPath/ReFrameWork on GitHub](https://github.com/UiPath/ReFrameWork)
  — the template this agent is tuned for; its
  [documentation PDF](https://github.com/UiPath/ReFrameWork/blob/master/Documentation/REFramework%20documentation.pdf)
  explains the four states and the standard `Framework/` workflows that the
  fingerprint in `scanner.py` detects.
- [UiPath Studio — Workflow Analyzer](https://docs.uipath.com/studio/standalone/2023.10/user-guide/about-workflow-analyzer)
  — UiPath's own static-analysis rules; `checks.py` reimplements the spirit of
  several (naming aside) directly over the IR.
- [Windows Workflow Foundation](https://learn.microsoft.com/en-us/dotnet/framework/windows-workflow-foundation/)
  — the runtime whose serialized activity trees `.xaml` files are.
- [XAML services overview](https://learn.microsoft.com/en-us/dotnet/desktop/xaml-services/overview)
  — the XAML language itself: `x:` namespace, `x:Members`, attached
  properties (which is what `sap2010:Annotation.AnnotationText` is).
- [UiPath docs — selectors](https://docs.uipath.com/studio/standalone/2023.10/user-guide/about-selectors)
  — the XML mini-language inside `Selector` attributes from which
  `selector_apps` extracts `app=`/`title=` tokens.

## Python stack

- [pydantic](https://docs.pydantic.dev/latest/) — every IR model and every
  LLM schema; `model_json_schema()` powers the JSON fallback,
  `Field(description=...)` powers the function-calling prompts.
- [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
  — typed `.env` config with aliases (`GUARDIAN_*`), the `Settings` class.
- [openpyxl](https://openpyxl.readthedocs.io/) — `read_only`/`data_only`
  workbook reading in `config_xlsx.py`.
- [typer](https://typer.tiangolo.com/) — the CLI; note the callback trick in
  `cli.py` that keeps `rpa-guardian analyze` as an explicit subcommand.
- [rich](https://rich.readthedocs.io/) — CLI progress output.
- [`xml.etree.ElementTree`](https://docs.python.org/3/library/xml.etree.elementtree.html)
  — stdlib XML parsing; the namespace-in-tag (`{uri}local`) convention that
  `_local()`/`_ns()` handle.

## Models

- [gpt-oss](https://huggingface.co/openai/gpt-oss-20b) — OpenAI's open-weight
  models; tool calling supported, served locally (lead slot by default).
- [Gemma](https://ai.google.dev/gemma) — Google's open models (worker slot by
  default).

## Output formats

- [Mermaid flowcharts](https://mermaid.js.org/syntax/flowchart.html) — the
  invocation-graph syntax emitted by `_mermaid()`; rendered natively by
  Obsidian and GitHub.
- [Obsidian — basic formatting syntax](https://help.obsidian.md/syntax) — what
  "Obsidian-ready Markdown" means in practice (standard headings, tables,
  fenced blocks; anchors as generated by `md_anchor`).
- [Requirements traceability](https://en.wikipedia.org/wiki/Requirements_traceability)
  — the practice the compliance matrix implements.

## Lineage

- [jmonaste/obsidian-agent](https://github.com/jmonaste/obsidian-agent) — the
  predecessor project. What carried over: LangGraph + `ChatOpenAI(base_url)`,
  bounded read-only tools with visible caps, deterministic
  reference/evidence collection from tool messages, pydantic-settings +
  typer + rich, pip/venv-first corporate install, GPL-3. What is new here:
  structured pipeline over an agent loop, deterministic ingestion/compression,
  Send-API map-reduce, disk caches and checkpointing, and the compliance
  subgraph.
