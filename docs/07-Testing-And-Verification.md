# 07 — Testing and verification

The suite (18 tests, `pytest`, < 2 s, zero network) runs the entire pipeline —
graph, waves, cache, tools, rendering — against a bundled miniature
REFramework project with a scripted fake model. Source: `tests/`.

## The two enabling pieces

### 1. The injectable fake LLM

`tests/conftest.py: FakeGuardianLLM(GuardianLLM)` overrides the gateway's two
methods (ch. 05):

- `structured()` returns canned instances per schema — and *reads its input*
  where realism matters: the fake `WorkflowSummary` parses the `WORKFLOW:`
  header from the prompt to set its path, marks `SetTransactionStatus` as
  boilerplate, and attaches a smell only to `ExtractInvoice`. The fake
  narrative asks one `open_question` on the first call and none on the second
  — which is precisely what drives the gap-fill + refine path through the
  graph.
- `tool_loop()` actually **invokes the real tools** it is handed
  (`search_project`, then `read_workflow` on `GetTransactionData.xaml`) before
  answering. This is what lets tests assert that evidence recording works end
  to end: the recorder is populated by real tool code, not by the fake.

Injection needs no patching because the pipeline was built for it:
`build_graph(settings, llm=fake)` (decision-by-construction, ch. 02). The fake
also counts `structured_calls`, which the cache test uses as its probe.

### 2. The fixture project

`tests/fixtures/sample-project/` is a hand-written minimal REFramework —
7 workflows, `project.json`, generated `Data/Config.xlsx`, a JSON-lines
execution log. Every file exists to exercise a specific code path:

| Fixture element | Exercises |
|-----------------|-----------|
| `Main.xaml` — StateMachine, 4 REFramework states, transitions, a populated Catch, `TextExpression`/view-state noise blocks | fingerprint (strong signal 1), state extraction, noise pruning, non-empty-catch discrimination |
| `Framework/InitAllSettings.xaml` — `String[]` argument, ForEach + delegate | argument-type cleanup, transparent-wrapper walking |
| `Framework/GetTransactionData.xaml` — `in/io/out` arguments, If/Then/Else, `Config("OrchestratorQueueName")` in an escaped attribute | direction parsing, config-key regex on `&quot;` form |
| `Framework/SetTransactionStatus.xaml` — **empty** Catch (a bare `Sequence`), literal `Delay 00:00:02` | empty-catch detection, hardcoded-delay check |
| `Process.xaml` -> `Business/ExtractInvoice.xaml` | call-graph resolution of `\`-separated paths |
| `Business/ExtractInvoice.xaml` — selectors (`app='chrome.exe'`), literal `C:\Temp\...` path, `Delay 00:00:05`, `Config("InvoiceFolder")` where the workbook has no such key | selector-app extraction, hardcoded-path regex, missing-config-key check |
| `Business/Unused.xaml` — invoked by nothing | orphan detection |
| `Config.xlsx` — `ApplicationPassword` row, unused `ReportPath` row, `Assets` sheet with Name/Asset headers | secret redaction, unused-key check, header-name column resolution |
| `logs/execution.log` — JSON lines, one Error, one Warning | log digestion, level normalization |

The workbook is generated (not hand-edited): `tests/fixtures/make_config_xlsx.py`
rebuilds it reproducibly. The PDD fixture (`sample-pdd.md`) contains five
requirements of which one (the summary email) is deliberately *not*
implemented by the fixture project — giving the compliance path a true
negative to find.

## What each test file proves

- **`test_xaml_parser.py`** — the extraction table of ch. 03, item by item:
  root type + states, invocation targets and bindings, argument
  direction/type cleanup, variables, config keys, selector apps, hardcoded
  delay/path, log messages, empty vs populated Catch, noise exclusion from
  the outline, compression (`raw_chars > len(to_context())`), and the
  never-raise contract on malformed XML.
- **`test_scanner.py`** — inventory census, dependency unwrapping, secret
  redaction (asserts the plaintext appears nowhere in the rendered config
  context), call-graph edges, orphan list, log digest, and the bottom-up
  ordering *property*: for every edge, `wave(callee) < wave(caller)` —
  checked over the whole graph rather than against a hardcoded order.
- **`test_checks.py`** — each deterministic rule fires on the fixture defect
  planted for it; findings arrive sorted by severity.
- **`test_render.py`** — document structure (every section heading + matching
  ToC anchor), argument tables straight from IR, boilerplate grouped into its
  own table, Mermaid block present, and the two hard style guarantees: an
  emoji planted in the fake narrative does not survive, and the redacted
  secret never appears.
- **`test_graph.py`** — three end-to-end runs over the compiled graph:
  1. *No PDD*: every workflow summarized, the gap-fill ran exactly once, and
     its evidence equals the path list the real tools recorded.
  2. *With PDD*: requirements extracted and renumbered; R-01 verdict
     `Compliant`; R-02 goes `Not verifiable` -> evidence rescue -> justified
     `Non-compliant` with recorded evidence — the full three-stage compliance
     path in one assertion chain.
  3. *Cache*: run twice against a tmp copy; the second run performs **zero**
     additional `WorkflowSummary` calls (probed via the fake's call counter)
     and the cache file exists on disk.

## Verification beyond the suite

Two smoke checks were run against the **official
[UiPath/ReFrameWork](https://github.com/UiPath/ReFrameWork) template** (real
Studio-generated XAML, which is far noisier than the fixture):

- Parser only: 13 workflows, **0 parse errors**, REFramework detected with all
  three evidence signals, correct call graph (including
  `Framework/GetAppCredentials.xaml` as a true orphan), 238k -> 32k chars
  (7.5x compression), waves `[9, 3, 1]`.
- Full `run_pipeline` with the fake LLM + fixture PDD: all 13 nodes executed,
  both documents rendered, checkpointer and summary cache written, `--resume`
  on the finished thread returns cleanly, and a re-run with
  `warnings.simplefilter("error")` confirmed the serializer whitelist removed
  every deserialization warning.

## What is *not* covered (deliberately)

Real-model behavior: schema adherence of gemma/gpt-oss, endpoint concurrency,
narrative prose quality. That validation can only happen against the corporate
endpoint — the checklist for it:

1. `pip install -e .`, copy `.env.example` -> `.env`, set the endpoint + both
   model slots.
2. Run against a small real project first; check the `warnings` block in the
   document appendix — structured-output fallbacks (`... fell back ...`)
   appearing frequently means the worker model needs the JSON fallback tuned
   or a different model slot.
3. Then a large project: watch wall-clock vs `GUARDIAN_MAX_CONCURRENCY`, try
   killing the run mid-map and resuming with `--resume`.
4. Then a real PDD: inspect which requirements land `Not verifiable` — that is
   the signal for whether word-overlap relevance selection is good enough
   (decision D2's revisit trigger).
