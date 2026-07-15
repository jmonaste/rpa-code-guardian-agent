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
  to the other. Each slot gets a custom `httpx.Client` honoring
  `GUARDIAN_VERIFY_SSL` (self-signed corporate endpoints) and
  `GUARDIAN_REQUEST_TIMEOUT`, plus an explicit
  `max_tokens=GUARDIAN_MAX_TOKENS` — reasoning models (gpt-oss) spend budget
  thinking before answering, and an endpoint's low default silently truncates
  their JSON mid-object. Temperature defaults to 0 — the deliverable is a
  document; reproducibility beats creativity.

## Structured output: the method ladder

Every analysis artifact (plan, workflow summary, narrative, requirements,
verdicts) is a pydantic schema from `model/summaries.py`. Getting a local
model to emit valid instances is the gateway's main job:

```
rung 1: with_structured_output(schema, method="json_schema")     one attempt
rung 2: with_structured_output(schema, method="function_calling")
        + one retry with the error appended to the prompt
rung 3: plain completion -> tolerant JSON extraction -> model_validate
 else : raise GuardianLLMError  (the calling node degrades per D7)
```

Details worth knowing:

1. **`json_schema` first.** Guided JSON constrains decoding to the schema on
   endpoints that support it (vLLM and friends) — when it works, the output
   is correct by construction. Many local servers do not implement it, in
   which case the single attempt fails fast and the ladder falls to tool
   calling. Reference:
   [structured output how-to](https://python.langchain.com/docs/how_to/structured_output/).
2. **`function_calling`** binds the schema as a single tool and forces the
   model to "call" it — the most widely supported mechanism on
   OpenAI-compatible servers. Its retry embeds the failure: `"Your previous
   attempt failed with: <error>. Respond again, satisfying the schema
   exactly."` — cheap, and effective with schema-shaped errors.
3. **The JSON fallback is deliberately tolerant** of everything local models
   actually do: it strips `<think>…</think>` reasoning blocks, scans for
   *every* balanced `{...}` object with a brace counter that ignores braces
   inside strings (a greedy regex grabs the wrong object), and validates each
   candidate *and its one-level-nested dicts* — recovering payloads wrapped
   under a schema-named key (`{"WorkflowSummary": {...}}`) or preceded by an
   echo of the schema itself.
4. **Field descriptions are prompt text.** With function calling, pydantic
   `Field(description=...)` strings are what the model actually reads —
   which is why the schemas in `model/summaries.py` carry precise, imperative
   descriptions ("Copy it exactly from the input", "Empty if none"). Treat
   them as prompts when editing.

Which rung actually serves a given endpoint/model pair is empirical — the
`rpa-guardian tune` command (`tune.py`) probes each method once per model and
reports what works, along with truncation and concurrency measurements.

## Transient errors: retry with backoff

Every invoke — structured, fallback, and each tool-loop turn — goes through
`_invoke_with_retry`:

- `_is_retryable` classifies the failure by walking the exception *cause
  chain*, matching both exception types (`RateLimitError`, `APITimeoutError`,
  ...) and message markers (`429`, `bad gateway`, `timed out`, ...). Local
  endpoints under parallel load produce these routinely.
- Transient errors are retried with exponential backoff:
  `GUARDIAN_LLM_RETRY_BASE_DELAY` (2s) doubling per attempt, capped at 60s,
  up to `GUARDIAN_LLM_RETRIES` (3) retries. Each wait is logged as a warning
  (`_error_summary` collapses gateway HTML error pages to one clean line).
- Exhaustion raises `GuardianLLMUnavailable` — a distinct subclass that
  **aborts the method ladder**: falling to a more permissive method cannot fix
  a dead transport and would just burn more slow, failing calls. The calling
  node still degrades per D7.
- Non-transient errors (validation, 400s) propagate immediately and are *not*
  counted as endpoint failures — the endpoint answered; the content was
  unusable, which is the ladder's job to handle.

## Live call stats

`LLMCallStats` (attached to every `GuardianLLM`) keeps thread-safe counters —
`ok` / `retried` / `failed` under the semantics above — and notifies an
optional observer after every update. The CLI registers an observer that
redraws the in-place progress line (`map: 7/19 workflows analyzed · llm: 23
ok, 2 retried`), which is how a user watching a long run can tell "slow model"
from "hung endpoint". Observer exceptions are swallowed: display must never
break a run.

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
    reply = _invoke_with_retry(llm.bind_tools(tools), messages)
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
