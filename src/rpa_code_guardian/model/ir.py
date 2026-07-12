"""Intermediate representation (IR) of a parsed UiPath project.

Raw ``.xaml`` is Windows Workflow Foundation XML where most bytes are designer
noise (view state, geometry, namespace clutter). The ingestion layer reduces
every workflow to a compact :class:`WorkflowIR` *deterministically* — before a
single LLM token is spent — and the whole project to a :class:`ProjectInventory`.
Everything the LLM sees downstream is rendered from these models.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Argument(BaseModel):
    """A workflow argument declared in ``x:Members``."""

    name: str
    direction: str  # "in" | "out" | "io" | "property"
    type: str
    annotation: str = ""


class Variable(BaseModel):
    """A variable declared inside the workflow."""

    name: str
    type: str
    default: str = ""


class Invocation(BaseModel):
    """An ``InvokeWorkflowFile`` call from one workflow to another."""

    target: str  # normalized project-relative path, or the raw expression if dynamic
    dynamic: bool = False  # True when the file name is an expression, not a literal
    arguments: dict[str, str] = Field(default_factory=dict)  # name -> binding expression


class LogLine(BaseModel):
    """A ``LogMessage`` activity found in the workflow."""

    level: str
    message: str


class WorkflowIR(BaseModel):
    """Compact, LLM-ready representation of one ``.xaml`` workflow."""

    path: str  # project-relative path with forward slashes
    display_name: str
    root_type: str = ""  # Sequence | Flowchart | StateMachine | ...
    annotation: str = ""  # top-level annotation, if the developer wrote one
    arguments: list[Argument] = Field(default_factory=list)
    variables: list[Variable] = Field(default_factory=list)
    invocations: list[Invocation] = Field(default_factory=list)
    log_messages: list[LogLine] = Field(default_factory=list)
    selector_apps: list[str] = Field(default_factory=list)  # apps/titles targeted by selectors
    outline: str = ""  # indented activity outline (type + display name)
    states: list[str] = Field(default_factory=list)  # state names when root is a StateMachine
    activity_count: int = 0
    max_depth: int = 0
    try_catch_count: int = 0
    empty_catches: int = 0
    disabled_activities: int = 0  # CommentOut blocks left in the code
    hardcoded_delays: list[str] = Field(default_factory=list)  # literal Delay durations
    hardcoded_paths: list[str] = Field(default_factory=list)  # literal absolute paths
    config_keys_used: list[str] = Field(default_factory=list)  # Config("...") lookups
    raw_chars: int = 0  # size of the original XAML, to report compression
    content_hash: str = ""  # sha256 of the raw file, keys the summary cache
    parse_error: str = ""  # non-empty when the file could not be fully parsed

    def one_liner(self) -> str:
        """A single line used in listings and as callee digest fallback."""
        note = self.annotation.splitlines()[0][:120] if self.annotation else ""
        return f"{self.path} ({self.root_type}, {self.activity_count} activities){': ' + note if note else ''}"

    def to_context(self, max_chars: int = 12_000) -> str:
        """Render the IR as the compact text block handed to the LLM."""
        lines: list[str] = [f"WORKFLOW: {self.path}"]
        lines.append(f"Root: {self.root_type or 'unknown'} | activities: {self.activity_count} | raw XAML: {self.raw_chars} chars")
        if self.annotation:
            lines.append(f"Developer annotation: {self.annotation.strip()}")
        if self.states:
            lines.append("States: " + ", ".join(self.states))
        if self.arguments:
            lines.append("Arguments:")
            lines += [f"  - {a.direction} {a.name}: {a.type}" + (f" — {a.annotation}" if a.annotation else "") for a in self.arguments]
        if self.variables:
            lines.append("Variables:")
            lines += [f"  - {v.name}: {v.type}" + (f" = {v.default}" if v.default else "") for v in self.variables[:40]]
            if len(self.variables) > 40:
                lines.append(f"  (showing 40 of {len(self.variables)})")
        if self.invocations:
            lines.append("Invokes:")
            for inv in self.invocations:
                args = ", ".join(f"{k}={v}" for k, v in list(inv.arguments.items())[:8])
                lines.append(f"  - {inv.target}{' [dynamic]' if inv.dynamic else ''}" + (f" ({args})" if args else ""))
        if self.config_keys_used:
            lines.append("Config keys used: " + ", ".join(sorted(set(self.config_keys_used))[:30]))
        if self.selector_apps:
            lines.append("UI targets (from selectors): " + ", ".join(sorted(set(self.selector_apps))[:15]))
        if self.log_messages:
            lines.append("Log messages:")
            lines += [f"  - [{m.level}] {m.message[:160]}" for m in self.log_messages[:25]]
            if len(self.log_messages) > 25:
                lines.append(f"  (showing 25 of {len(self.log_messages)})")
        if self.try_catch_count:
            lines.append(f"TryCatch blocks: {self.try_catch_count} (empty catches: {self.empty_catches})")
        if self.hardcoded_delays:
            lines.append("Hardcoded delays: " + ", ".join(self.hardcoded_delays[:10]))
        if self.hardcoded_paths:
            lines.append("Hardcoded paths: " + ", ".join(self.hardcoded_paths[:10]))
        if self.parse_error:
            lines.append(f"PARSE WARNING: {self.parse_error}")
        if self.outline:
            lines.append("Activity outline:")
            lines.append(self.outline)
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n[outline truncated to fit the context budget]"
        return text


class ConfigEntry(BaseModel):
    """One row of the REFramework ``Config.xlsx``."""

    sheet: str  # Settings | Constants | Assets | <other>
    name: str
    value: str
    description: str = ""


class LogDigest(BaseModel):
    """Bounded digest of execution log files found in the project."""

    files: list[str] = Field(default_factory=list)
    total_lines: int = 0
    level_counts: dict[str, int] = Field(default_factory=dict)
    first_timestamp: str = ""
    last_timestamp: str = ""
    error_samples: list[str] = Field(default_factory=list)  # bounded error/warn lines

    def to_context(self) -> str:
        if not self.files:
            return "(no log files found)"
        lines = [f"Log files: {', '.join(self.files)}", f"Total lines: {self.total_lines}"]
        if self.level_counts:
            lines.append("Levels: " + ", ".join(f"{k}={v}" for k, v in sorted(self.level_counts.items())))
        if self.first_timestamp or self.last_timestamp:
            lines.append(f"Span: {self.first_timestamp} .. {self.last_timestamp}")
        if self.error_samples:
            lines.append("Error/warning samples:")
            lines += [f"  - {s[:220]}" for s in self.error_samples]
        return "\n".join(lines)


class ProjectMeta(BaseModel):
    """Metadata read from ``project.json``."""

    name: str = ""
    description: str = ""
    main: str = "Main.xaml"
    project_version: str = ""
    studio_version: str = ""
    schema_version: str = ""
    target_framework: str = ""
    expression_language: str = ""
    dependencies: dict[str, str] = Field(default_factory=dict)


class CallGraph(BaseModel):
    """Static invocation graph built from ``InvokeWorkflowFile`` edges."""

    edges: dict[str, list[str]] = Field(default_factory=dict)  # caller -> callees (paths)
    entry: str = "Main.xaml"
    orphans: list[str] = Field(default_factory=list)  # workflows unreachable from entry
    dynamic_calls: list[str] = Field(default_factory=list)  # callers with dynamic targets

    def callees(self, path: str) -> list[str]:
        return self.edges.get(path, [])

    def callers(self, path: str) -> list[str]:
        return [src for src, dsts in self.edges.items() if path in dsts]

    def bottom_up_order(self, paths: list[str]) -> list[list[str]]:
        """Group ``paths`` into waves: every workflow appears after its callees.

        Wave N contains workflows whose (known) callees are all in waves < N.
        Cycles are broken by flushing the remaining strongly-connected workflows
        into a final wave, so the method always terminates.
        """
        remaining = set(paths)
        done: set[str] = set()
        waves: list[list[str]] = []
        while remaining:
            wave = sorted(
                p for p in remaining
                if all(c in done or c not in remaining for c in self.edges.get(p, []))
            )
            if not wave:  # cycle: flush the rest
                wave = sorted(remaining)
            waves.append(wave)
            done |= set(wave)
            remaining -= set(wave)
        return waves

    def to_context(self, max_edges: int = 80) -> str:
        lines = [f"Entry point: {self.entry}"]
        count = 0
        for src in sorted(self.edges):
            for dst in self.edges[src]:
                lines.append(f"  {src} -> {dst}")
                count += 1
                if count >= max_edges:
                    lines.append(f"  (edge list truncated at {max_edges})")
                    break
            if count >= max_edges:
                break
        if self.orphans:
            lines.append("Unreachable from entry: " + ", ".join(self.orphans[:20]))
        if self.dynamic_calls:
            lines.append("Workflows with dynamic invocations: " + ", ".join(self.dynamic_calls[:10]))
        return "\n".join(lines)


class ProjectInventory(BaseModel):
    """Everything the pipeline knows about the project after ingestion."""

    root: str  # absolute path of the project directory
    meta: ProjectMeta = Field(default_factory=ProjectMeta)
    workflows: dict[str, WorkflowIR] = Field(default_factory=dict)  # path -> IR
    call_graph: CallGraph = Field(default_factory=CallGraph)
    config_entries: list[ConfigEntry] = Field(default_factory=list)
    config_file: str = ""
    log_digest: LogDigest = Field(default_factory=LogDigest)
    is_reframework: bool = False
    reframework_evidence: list[str] = Field(default_factory=list)
    other_files: dict[str, int] = Field(default_factory=dict)  # extension -> count
    skipped_files: list[str] = Field(default_factory=list)  # unreadable / too large
    total_xaml_chars: int = 0

    def census(self) -> str:
        """Compact project overview used by the plan node."""
        lines = [
            f"Project: {self.meta.name or '(unnamed)'} — {self.meta.description or 'no description'}",
            f"Studio {self.meta.studio_version} | {self.meta.target_framework} | expressions: {self.meta.expression_language}",
            f"Workflows: {len(self.workflows)} (total raw XAML: {self.total_xaml_chars} chars)",
            f"REFramework detected: {self.is_reframework}"
            + (f" ({'; '.join(self.reframework_evidence)})" if self.reframework_evidence else ""),
        ]
        if self.meta.dependencies:
            deps = ", ".join(f"{k} {v}" for k, v in sorted(self.meta.dependencies.items()))
            lines.append(f"Dependencies: {deps}")
        if self.config_entries:
            lines.append(f"Config: {self.config_file} ({len(self.config_entries)} entries)")
        lines.append("Workflow list:")
        lines += [f"  - {ir.one_liner()}" for _, ir in sorted(self.workflows.items())]
        return "\n".join(lines)

    def config_context(self, max_entries: int = 120) -> str:
        if not self.config_entries:
            return "(no Config workbook found)"
        lines = [f"Config workbook: {self.config_file}"]
        for e in self.config_entries[:max_entries]:
            lines.append(f"  [{e.sheet}] {e.name} = {e.value}" + (f" — {e.description}" if e.description else ""))
        if len(self.config_entries) > max_entries:
            lines.append(f"  (showing {max_entries} of {len(self.config_entries)})")
        return "\n".join(lines)
