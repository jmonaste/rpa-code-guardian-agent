"""Deterministic, Workflow-Analyzer-style quality checks over the parsed IR.

These run without any LLM and are merged with the LLM's own observations into
the "Improvement suggestions" section. Every check is cheap, explainable and
sourced directly from the IR, so a finding here is always trustworthy.
"""

from __future__ import annotations

from .model.ir import ProjectInventory
from .model.summaries import Finding

BIG_WORKFLOW_ACTIVITIES = 150
DEEP_NESTING = 9


def run_checks(inv: ProjectInventory) -> list[Finding]:
    findings: list[Finding] = []
    config_keys = {e.name for e in inv.config_entries}
    used_keys: set[str] = set()

    for path, ir in sorted(inv.workflows.items()):
        used_keys.update(ir.config_keys_used)

        if ir.parse_error:
            findings.append(Finding(
                severity="Medium", category="Maintainability", location=path,
                description=f"The workflow could not be fully parsed ({ir.parse_error}); it may be corrupted or use an unsupported format.",
                recommendation="Open the file in Studio and re-save it; verify it loads without errors.",
            ))
            continue

        if not ir.annotation and ir.activity_count > 5:
            findings.append(Finding(
                severity="Low", category="Maintainability", location=path,
                description="The workflow has no top-level annotation describing its purpose.",
                recommendation="Add an annotation in Studio stating what the workflow does, its inputs and outputs.",
            ))

        if ir.empty_catches:
            findings.append(Finding(
                severity="High", category="Reliability", location=path,
                description=f"{ir.empty_catches} Catch block(s) swallow exceptions without any handling or logging.",
                recommendation="Log the exception and rethrow it, or handle it explicitly; silent catches hide production failures.",
            ))

        if ir.hardcoded_delays:
            findings.append(Finding(
                severity="Medium", category="Reliability", location=path,
                description=f"Hardcoded Delay activities found ({', '.join(ir.hardcoded_delays[:5])}).",
                recommendation="Replace static delays with activity timeouts or element-based synchronization; move any unavoidable waits to Config.",
            ))

        if ir.hardcoded_paths:
            findings.append(Finding(
                severity="Medium", category="Maintainability", location=path,
                description=f"Hardcoded absolute paths found ({ir.hardcoded_paths[0]}{', ...' if len(ir.hardcoded_paths) > 1 else ''}).",
                recommendation="Move file locations to the Config workbook or Orchestrator assets so environments can differ.",
            ))

        if ir.disabled_activities:
            findings.append(Finding(
                severity="Low", category="Maintainability", location=path,
                description=f"{ir.disabled_activities} disabled (commented-out) activity block(s) left in the workflow.",
                recommendation="Delete dead code; version control preserves history.",
            ))

        if ir.activity_count > BIG_WORKFLOW_ACTIVITIES:
            findings.append(Finding(
                severity="Medium", category="Maintainability", location=path,
                description=f"Very large workflow ({ir.activity_count} activities).",
                recommendation="Split it into smaller invoked workflows with clear responsibilities.",
            ))

        if ir.max_depth > DEEP_NESTING:
            findings.append(Finding(
                severity="Low", category="Maintainability", location=path,
                description=f"Deeply nested logic (depth {ir.max_depth}).",
                recommendation="Extract inner branches into invoked workflows or flatten condition chains.",
            ))

        if ir.activity_count > 15 and not ir.log_messages and ir.root_type != "StateMachine":
            findings.append(Finding(
                severity="Low", category="Observability", location=path,
                description="No Log Message activities in a non-trivial workflow.",
                recommendation="Log the start, outcome and key decisions so production runs can be diagnosed.",
            ))

        missing = [k for k in ir.config_keys_used if config_keys and k not in config_keys]
        if missing:
            findings.append(Finding(
                severity="High", category="Reliability", location=path,
                description=f"Config keys referenced but not present in {inv.config_file or 'the Config workbook'}: {', '.join(sorted(missing)[:8])}.",
                recommendation="Add the missing keys (or the Orchestrator assets backing them) before running the process.",
            ))

    for orphan in inv.call_graph.orphans:
        findings.append(Finding(
            severity="Low", category="Maintainability", location=orphan,
            description="Workflow is not reachable from the entry point (dead code, unless invoked dynamically).",
            recommendation="Remove it or document why it is kept; verify it is not meant to be invoked.",
        ))

    unused = sorted(config_keys - used_keys)
    if config_keys and unused and not inv.call_graph.dynamic_calls:
        findings.append(Finding(
            severity="Low", category="Maintainability", location=inv.config_file,
            description=f"Config entries never referenced by any workflow: {', '.join(unused[:10])}{'...' if len(unused) > 10 else ''}.",
            recommendation="Prune unused configuration to keep the workbook trustworthy.",
        ))

    order = {"High": 0, "Medium": 1, "Low": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.location))
    return findings
