# 03 — XAML ingestion

The ingestion layer is the foundation of the whole design: it converts an
arbitrary UiPath project into the compact, typed, deterministic
`ProjectInventory` that every later stage consumes. Source: `ingest/`,
`model/ir.py`, `checks.py`.

## Anatomy of a UiPath `.xaml`

A workflow file is [XAML](https://learn.microsoft.com/en-us/dotnet/desktop/xaml-services/overview)
serializing a .NET
[Windows Workflow Foundation](https://learn.microsoft.com/en-us/dotnet/framework/windows-workflow-foundation/)
activity tree. A trimmed real example, annotated:

```xml
<Activity x:Class="ExtractInvoice"                         <!-- root wrapper -->
   xmlns="http://schemas.microsoft.com/netfx/2009/xaml/activities"
   xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
   xmlns:sap2010="http://schemas.microsoft.com/netfx/2010/xaml/activities/presentation"
   xmlns:ui="http://schemas.uipath.com/workflow/activities">
  <x:Members>                                              <!-- SIGNAL: arguments -->
    <x:Property Name="in_InvoiceId" Type="InArgument(x:String)"
                sap2010:Annotation.AnnotationText="Identifier of the invoice." />
  </x:Members>
  <TextExpression.NamespacesForImplementation> ... </...>  <!-- NOISE: VB imports -->
  <sap2010:WorkflowViewState.IdRef> ... </...>             <!-- NOISE: designer ids -->
  <Sequence DisplayName="Extract Invoice"
            sap2010:Annotation.AnnotationText="Downloads the invoice PDF...">
    <Sequence.Variables>                                   <!-- SIGNAL: variables -->
      <Variable x:TypeArguments="x:String" Name="InvoicePath" />
    </Sequence.Variables>
    <sap2010:WorkflowViewState.ViewState> ... </...>       <!-- NOISE: geometry -->
    <ui:InvokeWorkflowFile WorkflowFileName="Framework\Sub.xaml"> <!-- SIGNAL: edge -->
      <ui:InvokeWorkflowFile.Arguments>
        <InArgument x:TypeArguments="x:String" x:Key="in_X">[expr]</InArgument>
      </ui:InvokeWorkflowFile.Arguments>
    </ui:InvokeWorkflowFile>
    <ui:TypeInto Text="[in_InvoiceId]">
      <ui:TypeInto.Target>
        <ui:Target Selector="&lt;html app='chrome.exe' title='Invoice Entry'/&gt;" />
      </ui:TypeInto.Target>                                <!-- SIGNAL: UI target -->
    </ui:TypeInto>
  </Sequence>
</Activity>
```

Key namespaces (constants at the top of `ingest/xaml_parser.py`):

| Prefix | Namespace | Meaning | Treatment |
|--------|-----------|---------|-----------|
| (default) | `.../netfx/2009/xaml/activities` | WF activities (`Sequence`, `If`, `TryCatch`, `StateMachine`, ...) | signal |
| `x` | `.../winfx/2006/xaml` | XAML language (`x:Members`, `x:Property`, `x:Reference`) | arguments = signal; rest skipped |
| `ui` | `schemas.uipath.com/workflow/activities` | UiPath activities (`InvokeWorkflowFile`, `LogMessage`, `TypeInto`, ...) | signal |
| `sap`/`sap2010` | `.../activities/presentation` | designer view state, annotations | noise, **except** `Annotation.AnnotationText` |
| `sads` | `.../activities/debugger` | debug symbols | noise |

The ratio matters: on the official
[UiPath/ReFrameWork](https://github.com/UiPath/ReFrameWork) template, 238k
characters of XAML reduce to 32k of IR (7.5x); business workflows with heavy
selectors and view state routinely compress 20–50x.

## The parser: `parse_xaml()`

Design rules:

1. **Never raise.** Any failure lands in `WorkflowIR.parse_error` with
   whatever could still be salvaged (the regex extractions below run on the
   raw text before XML parsing, so even a malformed file yields config keys
   and hardcoded paths). A corrupt workflow becomes a *finding*, not a crash.
2. **`xml.etree.ElementTree` from the stdlib**, not `lxml`. The files are
   machine-generated and well-formed; the stdlib parser removes a native
   dependency that can be painful to install on locked-down Windows machines
   (D10 in ch. 01).
3. **Names over namespaces.** Elements are matched by *local name*
   (`_local(tag)`), with namespace checks only to exclude the x/sap/sads
   families. UiPath has moved activities between namespace versions over the
   years; local names are stable.

### What is extracted, and from where

| IR field | Source | Detail |
|----------|--------|--------|
| `arguments` | `x:Members/x:Property` | `Type="InArgument(x:String)"` parsed by regex into direction (`in/out/io/property`) + inner type; `_clean_type` strips prefixes (`scg:Dictionary(x:String, x:Object)` -> `Dictionary(String, Object)`) |
| `variables` | any `Variable` element | name, cleaned `x:TypeArguments`, truncated default |
| `annotation` | `sap2010:Annotation.AnnotationText` attribute | taken from the `Activity` root or the root activity — this is where good developers document intent, so it is first-class |
| `invocations` | `InvokeWorkflowFile` | `WorkflowFileName` normalized (`\` -> `/`); a value starting with `[` is a VB/C# expression, flagged `dynamic=True`; per-argument bindings from the `.Arguments` property children keyed by `x:Key` |
| `log_messages` | `LogMessage` | level (`[LogLevel.Info]` -> `Info`) + message text, unwrapped from `["..."]` expression syntax |
| `selector_apps` | any `Selector` attribute | regex `(?:app|title|cls)='...'` over the (XML-unescaped) selector string — the cheapest reliable answer to "which applications does this automate?" |
| `states` | `State` elements | REFramework state names for the fingerprint and the architecture section |
| `try_catch_count` / `empty_catches` | `TryCatch` / `Catch` | a catch is *empty* if it contains no activity beyond structural wrappers (`Sequence`, `Comment`, `ActivityAction`, delegate args) — the classic swallowed-exception smell |
| `hardcoded_delays` | `Delay` | literal `Duration` matching `hh:mm:ss` (expressions in `[...]` are fine — they usually read Config) |
| `hardcoded_paths` | raw-text regex | `[A-Za-z]:\...` literals, excluding UiPath/Microsoft installation paths |
| `config_keys_used` | raw-text regex | `Config("Key")` lookups, matched in both raw and XML-escaped (`&quot;`) form — feeds the missing/unused-key checks |
| `outline` | recursive walk | see below |
| `content_hash` | sha256 of the raw file | the summary-cache key (ch. 04) |

### The outline walk (`_OutlineWalker`)

The outline is the LLM's view of the workflow's *logic*. The walker descends
the activity tree with three element categories:

- **Property elements** (local name contains `.`, e.g. `TryCatch.Catches`):
  structural containers. The walker recurses through them *without* emitting a
  node — unless the prefix is in `_NOISE_PREFIXES`
  (`WorkflowViewState`, `VirtualizedContainerService`, `DebugSymbol`,
  `TextExpression`, `Annotation`), in which case the entire subtree is pruned.
- **Transparent wrappers** (`FlowStep`, `ActivityAction`,
  `CancellationScope`): recursed through invisibly, so the outline shows
  *what happens*, not WF plumbing.
- **Non-activities** (`Variable`, `InArgument`, delegate args, literals,
  anything in the x/sap/sads namespaces): skipped entirely.

Everything else is an activity: it increments `activity_count`, updates
`max_depth`, triggers per-activity extraction (`_observe`), and — within the
caps `MAX_OUTLINE_DEPTH = 12`, `MAX_OUTLINE_NODES = 350` — emits an indented
line:

```
- Sequence "Extract Invoice"  // Downloads the invoice PDF...
  - Assign "Build invoice path"
  - OpenBrowser "Open invoicing system"
  - TypeInto "Type invoice number"
```

Counting continues past the render caps, so `activity_count` stays exact for
the size checks even when the outline is truncated.

### `WorkflowIR.to_context()`

The single rendering of an IR for LLM consumption: header, annotation,
arguments, variables (cap 40), invocations with bindings, config keys,
UI targets, log messages (cap 25), error-handling stats, smells, outline —
hard-truncated to the caller's budget (default `GUARDIAN_IR_MAX_CHARS`,
12 000 chars) with an explicit `[outline truncated ...]` marker. Every list
inside is individually capped so one pathological workflow (400 variables)
cannot crowd out the rest.

## The scanner: `scan_project()`

One `rglob` walk with:

- **Skip dirs**: `.git`, `.local`, `.settings`, `.objects`, `.screenshots`,
  etc. (`SKIP_DIRS`) — UiPath's `.local` in particular contains caches that
  can dwarf the project.
- **Classification**: `.xaml` -> parse (files > 8 MB are recorded in
  `skipped_files` instead — a size that no hand-made workflow reaches);
  root `project.json` -> `parse_project_json`; `*config*.xlsx` ->
  config candidates (conventional `Data/Config.xlsx` preferred; Excel lock
  files `~$...` ignored); `*.log` -> log digestion; everything else -> an
  extension census in `other_files`.

### `project.json` (`ingest/project_json.py`)

Read with `utf-8-sig` (Studio writes a BOM), tolerating schema drift across
Studio versions (`targetFramework` and `expressionLanguage` moved in and out
of `designOptions` over the years). Dependency versions are unwrapped from
UiPath's `"[1.0.0]"` bracket syntax.

### `Config.xlsx` (`ingest/config_xlsx.py`)

`openpyxl` in `read_only` + `data_only` mode (values, not formulas). Column
positions are resolved from the header row by name (`Name`/`Value`/
`Description`, with `Asset` as fallback for the Assets sheet), so reordered
columns don't break parsing. Secret-looking names are redacted **here**, at
the entry point (decision D9).

### Logs (`ingest/logs.py`)

Bounded by construction: max 5 files, last 2 MB of each, max 15 error/warning
samples. Each line is tried as JSON (UiPath robot logs are JSON lines with
`level`/`timeStamp`/`message`) and falls back to a level-keyword regex for
plain-text logs. The output is a `LogDigest` — counts, time span, samples —
never the log itself.

## The call graph (`build_call_graph()`)

Edges come from each IR's non-dynamic invocations. Target resolution tries,
in order: the path as written (project-root-relative — UiPath's convention),
then caller-relative with manual `..` normalization (some teams use it), then
gives up and keeps the string (it will show as an unresolved edge rather than
silently vanish). Dynamic invocations (`WorkflowFileName` is an expression)
are recorded per caller in `dynamic_calls` — they matter because they
invalidate dead-code reasoning, so the unused-config check disables itself
when any exist.

Reachability (iterative DFS from the entry point) yields `orphans` — workflows
no invocation path reaches, excluding test folders. On the official
REFramework this correctly flags `Framework/GetAppCredentials.xaml`, which the
template ships but nothing invokes.

`bottom_up_order(paths)` produces the map-phase waves: wave N holds workflows
whose known callees all sit in earlier waves; if no progress can be made
(an invocation cycle), the remainder is flushed as a final wave so the
algorithm always terminates. Property verified by test:
every callee's wave index < its caller's.

## REFramework fingerprint (`detect_reframework()`)

Evidence-based, not boolean-by-vibes. Three signals:

1. The entry workflow is a `StateMachine` whose states include at least 3 of
   `Initialization`, `Get Transaction Data`, `Process Transaction`,
   `End Process` (strong).
2. At least 3 of the 9 standard `Framework/` template files are present by
   name (strong).
3. The config workbook has `Settings` + `Constants` sheets (weak).

`is_reframework` requires a strong signal; all found evidence strings are kept
and printed in the document's overview table, so the reader can audit the
classification.

## Deterministic checks (`checks.py`)

Rules in the spirit of UiPath's own
[Workflow Analyzer](https://docs.uipath.com/studio/standalone/2023.10/user-guide/about-workflow-analyzer),
computed from the IR alone (no LLM, therefore never wrong about the facts):

| Rule | Severity | IR source |
|------|----------|-----------|
| Empty Catch blocks | High | `empty_catches` |
| Config key used but missing from the workbook | High | `config_keys_used` vs config entries |
| Unparseable workflow | Medium | `parse_error` |
| Hardcoded `Delay` | Medium | `hardcoded_delays` |
| Hardcoded absolute paths | Medium | `hardcoded_paths` |
| Very large workflow (> 150 activities) | Medium | `activity_count` |
| No top-level annotation | Low | `annotation` |
| Disabled (commented-out) activities left in | Low | `disabled_activities` |
| Deep nesting (> 9) | Low | `max_depth` |
| No logging in a non-trivial workflow | Low | `log_messages` |
| Unreachable workflow (dead code) | Low | call graph orphans |
| Config entries never referenced | Low | union of `config_keys_used` |

The findings node (ch. 02) merges these with the LLM's per-workflow `smells`;
deterministic findings always survive dedup because they are inserted first.
