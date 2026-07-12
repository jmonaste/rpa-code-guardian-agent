# 05 — The LLM gateway

`llm.py: GuardianLLM` is the only module that talks to a model. Every
robustness measure for local models lives here once, instead of being
scattered through the pipeline. Nodes call two methods: `structured()` and
`tool_loop()`.

## Why a gateway class

- **Local models fail more.** gemma/gpt-oss behind an OpenAI-compatible server
  will occasionally return malformed tool calls, wrap JSON in prose, or drop a
  required field. Handling that per-node would spread try/except everywhere;
  handling it here means a node's failure path is one honest fallback (D7).
- **Testability.** The pipeline nodes hold a `GuardianLLM` reference; tests
  inject `FakeGuardianLLM` (a subclass overriding both methods) through
  `build_graph(settings, llm=...)` — no patching, no network (ch. 07).
- **Model slots.** `_chat_model(role)` lazily builds one
  [`ChatOpenAI`](https://python.langchain.com/docs/integrations/chat/openai/)
  per role against `OPENAI_BASE_URL`: `worker`
  (`GUARDIAN_WORKER_MODEL`) and `lead` (`GUARDIAN_LEAD_MODEL`), each defaulting
  to the other. Temperature defaults to 0 — the deliverable is a document;
  reproducibility beats creativity.

## Structured output: the retry ladder

Every analysis artifact (plan, workflow summary, narrative, requirements,
verdicts) is a pydantic schema from `model/summaries.py`. Getting a local
model to emit valid instances is the gateway's main job:

```
attempt 1: with_structured_output(schema, method="function_calling")
attempt 2: same, with the error appended to the prompt
attempt 3: plain completion -> extract JSON block -> model_validate_json
   else  : raise GuardianLLMError  (the calling node degrades per D7)
```

Details worth knowing:

1. **`method="function_calling"`** makes LangChain bind the schema as a single
   tool and force the model to "call" it — the most widely supported
   structured-output mechanism on OpenAI-compatible servers (unlike
   `json_schema` response formats, which many local servers do not implement).
   Reference: [structured output how-to](https://python.langchain.com/docs/how_to/structured_output/).
2. **The retry embeds the failure**: `"Your previous attempt failed with:
   <error>. Respond again, satisfying the schema exactly."` — cheap, and
   effective with schema-shaped errors (missing field, wrong type).
3. **The JSON fallback** sends the schema's `model_json_schema()` inline and
   asks for raw JSON, then regex-extracts the first `{...}` block before
   validating — tolerating models that wrap JSON in prose or code fences.
4. **Field descriptions are prompt text.** With function calling, pydantic
   `Field(description=...)` strings are what the model actually reads —
   which is why the schemas in `model/summaries.py` carry precise, imperative
   descriptions ("Copy it exactly from the input", "Empty if none"). Treat
   them as prompts when editing.

Two schema-design conventions used throughout:

- **Never trust identity fields.** `WorkflowSummary.path` and
  `ComplianceItem.requirement_id` are overwritten by the calling node from the
  payload after validation. The model is asked to copy them (it helps
  grounding) but the pipeline does not depend on it.
- **Lists of objects are wrapped** (`RequirementList` wraps
  `list[Requirement]`) because a top-level object survives function calling
  far more reliably than a bare JSON array.

## The tool loop

`tool_loop(system, user, tools, role, max_iterations)` is the think-act-observe
loop used by the gap-fill node and the compliance evidence rescue. It is a
plain Python loop, deliberately *not* a nested LangGraph graph:

```
messages = [system, user]
repeat up to max_iterations (default GUARDIAN_AGENT_MAX_ITERATIONS = 8):
    reply = llm.bind_tools(tools).invoke(messages)
    if reply has no tool_calls: return (reply.text, transcript)
    for each call: run tool (errors become "ERROR: ..." ToolMessages)
out of budget: append "Tool budget exhausted. Answer now with what you have."
               and take one final un-tooled completion
```

Why a plain loop where obsidian-agent used a `StateGraph` + `ToolNode`:
here the loop is a *leaf operation inside a node* of the outer graph. Nesting
a graph would add checkpoint noise (every inner step becomes a super-step)
for zero benefit — the loop is bounded, private, and its only output is a
string plus the evidence recorder's side effects. The outer pipeline is where
graph semantics pay off; the inner loop is just control flow.
(The pattern itself — bind tools, execute, feed `ToolMessage`s back — is the
standard LangChain [tool-calling loop](https://python.langchain.com/docs/how_to/tool_calling/).)

Failure semantics mirror the tools: an unknown tool name or a tool exception
is returned to the model as an `ERROR:` observation rather than raised — the
model can recover (retry with a fixed path, try another tool), and often does.

## Prompts

The system prompts live next to the nodes that use them (`graph/nodes.py`,
`graph/compliance.py`) rather than in a prompt library — when reading a node,
its contract with the model should be one screen away. Shared conventions:

- Role + task + grounding rule ("never invent activities or systems not
  shown") + output register ("plain professional English, no emojis" — belt;
  the lint pass is the suspenders).
- Escalation instead of guessing: reduce is told to put unanswerable points in
  `open_questions`; the verifier has an explicit `Not verifiable` verdict.
  Giving the model a *legitimate way out* is the cheapest hallucination
  countermeasure.
- The map prompt receives callee digests and plan notes as clearly labeled
  blocks (`INVOKED WORKFLOWS (digests):`, `PROJECT CONTEXT:`) — local models
  attend better to labeled sections than to prose-blended context.

`PROMPT_VERSION` in `graph/cache.py` must be bumped when the map prompt or
`WorkflowSummary` schema changes, or stale cached summaries will leak into new
runs.
