"""Read-only, bounded project tools for the evidence-gathering agents.

Same philosophy as obsidian-agent's vault tools: everything is sandboxed to the
parsed inventory, output is capped so a huge project cannot overflow a limited
context window, and reads are recorded so evidence citations are collected
deterministically rather than trusted from the model.
"""

from __future__ import annotations

import re

from langchain_core.tools import tool

from .model.ir import ProjectInventory

LIST_MAX = 80
SEARCH_MAX = 25
RAW_WINDOW_CHARS = 6_000


def build_project_tools(
    inv: ProjectInventory,
    recorder: list[str] | None = None,
    ir_max_chars: int = 12_000,
) -> list:
    """Create the tool set, closed over the inventory.

    ``recorder`` (when given) accumulates the workflow paths actually read, so
    callers can cite evidence deterministically.
    """

    def _record(path: str) -> None:
        if recorder is not None and path not in recorder:
            recorder.append(path)

    @tool
    def list_workflows() -> str:
        """List every workflow in the project with a one-line description."""
        lines = [ir.one_liner() for _, ir in sorted(inv.workflows.items())]
        if len(lines) > LIST_MAX:
            return "\n".join(lines[:LIST_MAX]) + f"\n(showing {LIST_MAX} of {len(lines)})"
        return "\n".join(lines) or "(no workflows)"

    @tool
    def read_workflow(path: str) -> str:
        """Read the parsed representation of one workflow: arguments, variables,
        invocations, log messages, selectors and the activity outline.
        ``path`` is the project-relative path shown by ``list_workflows``."""
        ir = inv.workflows.get(path) or inv.workflows.get(path.replace("\\", "/"))
        if ir is None:
            matches = [p for p in inv.workflows if p.lower().endswith("/" + path.lower()) or p.lower() == path.lower()]
            if len(matches) == 1:
                ir = inv.workflows[matches[0]]
            else:
                hint = f" Did you mean: {', '.join(matches[:5])}?" if matches else ""
                return f"ERROR: workflow not found: {path!r}.{hint}"
        _record(ir.path)
        return ir.to_context(max_chars=ir_max_chars)

    @tool
    def search_project(query: str, regex: bool = False) -> str:
        """Search all parsed workflow content and configuration (case-insensitive).
        Returns up to 25 matches as ``path: line``. Use to locate where something
        is implemented before reading it."""
        try:
            pat = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        except re.error as exc:
            return f"ERROR: invalid regex: {exc}"
        hits: list[str] = []
        for path, ir in sorted(inv.workflows.items()):
            for line in ir.to_context(max_chars=ir_max_chars).splitlines():
                if pat.search(line):
                    hits.append(f"{path}: {line.strip()[:160]}")
                    if len(hits) >= SEARCH_MAX:
                        break
            if len(hits) >= SEARCH_MAX:
                break
        if len(hits) < SEARCH_MAX:
            for e in inv.config_entries:
                text = f"[{e.sheet}] {e.name} = {e.value} {e.description}"
                if pat.search(text):
                    hits.append(f"{inv.config_file}: {text[:160]}")
                    if len(hits) >= SEARCH_MAX:
                        break
        if not hits:
            return f"(no matches for {query!r})"
        footer = f"\n(capped at {SEARCH_MAX} matches; narrow the query)" if len(hits) >= SEARCH_MAX else ""
        return "\n".join(hits) + footer

    @tool
    def read_config() -> str:
        """Read the project's Config workbook entries (Settings, Constants, Assets)."""
        return inv.config_context()

    @tool
    def read_logs() -> str:
        """Read the digest of the project's execution logs (levels, span, error samples)."""
        return inv.log_digest.to_context()

    @tool
    def read_xaml_source(path: str, offset: int = 0) -> str:
        """Read a window of the RAW XAML of a workflow, starting at character
        ``offset``. Only use when the parsed representation from ``read_workflow``
        is not detailed enough (e.g. to inspect an exact expression or selector)."""
        ir = inv.workflows.get(path.replace("\\", "/"))
        if ir is None:
            return f"ERROR: workflow not found: {path!r}"
        from pathlib import Path as _P

        try:
            raw = (_P(inv.root) / ir.path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: could not read {path!r}: {exc}"
        _record(ir.path)
        start = max(0, offset)
        window = raw[start : start + RAW_WINDOW_CHARS]
        footer = ""
        if start + RAW_WINDOW_CHARS < len(raw):
            footer = f"\n[more: chars {start}-{start + RAW_WINDOW_CHARS} of {len(raw)}; call again with offset={start + RAW_WINDOW_CHARS}]"
        return window + footer

    return [list_workflows, read_workflow, search_project, read_config, read_logs, read_xaml_source]
