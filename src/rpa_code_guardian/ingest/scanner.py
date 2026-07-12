"""Project directory scan: classify files, parse everything, build the call graph.

This is the single entry point of the ingestion layer:

    inventory = scan_project(Path("/path/to/uipath/project"))

It is fully deterministic (no LLM) and bounded, so it works on projects of any
size: XAML files above ``MAX_XAML_BYTES`` are skipped with a note instead of
exhausting memory, and every rendered context downstream is capped.
"""

from __future__ import annotations

from pathlib import Path

from ..model.ir import CallGraph, ProjectInventory
from .config_xlsx import parse_config_xlsx
from .logs import digest_logs
from .project_json import parse_project_json
from .xaml_parser import parse_xaml

SKIP_DIRS = {
    ".git", ".svn", ".local", ".settings", ".objects", ".tmh",
    ".screenshots", "node_modules", ".venv", "__pycache__",
}
MAX_XAML_BYTES = 8_000_000

# Workflows shipped with the REFramework template (used for fingerprinting).
REFRAMEWORK_FILES = {
    "InitAllSettings.xaml", "InitAllApplications.xaml", "KillAllProcesses.xaml",
    "GetTransactionData.xaml", "Process.xaml", "SetTransactionStatus.xaml",
    "TakeScreenshot.xaml", "CloseAllApplications.xaml", "RetryCurrentTransaction.xaml",
}
REFRAMEWORK_STATES = {"Initialization", "Get Transaction Data", "Process Transaction", "End Process"}


def scan_project(root: Path) -> ProjectInventory:
    """Walk the project directory and return the full parsed inventory."""
    root = root.resolve()
    inv = ProjectInventory(root=str(root))

    xaml_files: list[Path] = []
    log_files: list[Path] = []
    config_candidates: list[Path] = []

    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_dir():
            continue
        name = path.name
        suffix = path.suffix.lower()
        if name.startswith(".") or name.startswith("~$"):
            continue
        if suffix == ".xaml":
            xaml_files.append(path)
        elif name == "project.json" and path.parent == root:
            inv.meta = parse_project_json(path)
        elif suffix == ".xlsx" and "config" in name.lower():
            config_candidates.append(path)
        elif suffix == ".log" or (suffix == ".txt" and path.parent.name.lower() in ("log", "logs")):
            log_files.append(path)
        else:
            inv.other_files[suffix or name] = inv.other_files.get(suffix or name, 0) + 1

    for path in xaml_files:
        rel = str(path.relative_to(root)).replace("\\", "/")
        if path.stat().st_size > MAX_XAML_BYTES:
            inv.skipped_files.append(f"{rel} (too large: {path.stat().st_size} bytes)")
            continue
        ir = parse_xaml(path, rel)
        inv.workflows[rel] = ir
        inv.total_xaml_chars += ir.raw_chars

    if config_candidates:
        # Prefer the conventional Data/Config.xlsx over other matches.
        config_candidates.sort(key=lambda p: (p.parent.name.lower() != "data", len(str(p))))
        inv.config_file = str(config_candidates[0].relative_to(root)).replace("\\", "/")
        inv.config_entries = parse_config_xlsx(config_candidates[0])

    inv.log_digest = digest_logs(log_files, root)
    inv.call_graph = build_call_graph(inv)
    inv.is_reframework, inv.reframework_evidence = detect_reframework(inv)
    return inv


def build_call_graph(inv: ProjectInventory) -> CallGraph:
    """Resolve InvokeWorkflowFile targets into a project-relative edge list."""
    graph = CallGraph(entry=inv.meta.main if inv.meta.main in inv.workflows else _guess_entry(inv))
    known = set(inv.workflows)

    for caller, ir in inv.workflows.items():
        targets: list[str] = []
        for inv_call in ir.invocations:
            if inv_call.dynamic:
                if caller not in graph.dynamic_calls:
                    graph.dynamic_calls.append(caller)
                continue
            resolved = _resolve_target(inv_call.target, caller, known)
            inv_call.target = resolved
            if resolved not in targets:
                targets.append(resolved)
        if targets:
            graph.edges[caller] = targets

    reachable: set[str] = set()
    stack = [graph.entry] if graph.entry in known else []
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        stack.extend(t for t in graph.edges.get(node, []) if t in known)

    graph.orphans = sorted(
        p for p in known - reachable
        if not any(part.lower() in ("testcases", "tests", "test_framework") for part in Path(p).parts)
    )
    return graph


def _guess_entry(inv: ProjectInventory) -> str:
    for candidate in ("Main.xaml",):
        if candidate in inv.workflows:
            return candidate
    return next(iter(sorted(inv.workflows)), "Main.xaml")


def _resolve_target(target: str, caller: str, known: set[str]) -> str:
    """UiPath resolves invocation paths against the project root; some projects
    use caller-relative paths instead, so try both before giving up."""
    if target in known:
        return target
    caller_dir = Path(caller).parent
    candidate = str((caller_dir / target)).replace("\\", "/")
    # normalize ../ segments without touching the filesystem
    parts: list[str] = []
    for part in candidate.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    candidate = "/".join(parts)
    return candidate if candidate in known else target


def detect_reframework(inv: ProjectInventory) -> tuple[bool, list[str]]:
    """Score REFramework evidence; any strong signal is enough."""
    evidence: list[str] = []

    entry_ir = inv.workflows.get(inv.call_graph.entry)
    if entry_ir is not None and entry_ir.root_type == "StateMachine":
        matched = REFRAMEWORK_STATES & set(entry_ir.states)
        if len(matched) >= 3:
            evidence.append(f"{inv.call_graph.entry} is a StateMachine with REFramework states")
        else:
            evidence.append(f"{inv.call_graph.entry} is a StateMachine")

    framework_hits = {
        Path(p).name for p in inv.workflows if Path(p).name in REFRAMEWORK_FILES
    }
    if len(framework_hits) >= 3:
        evidence.append(f"{len(framework_hits)} standard Framework workflows present")

    sheets = {e.sheet for e in inv.config_entries}
    if {"Settings", "Constants"} <= sheets:
        evidence.append("Config workbook with Settings/Constants sheets")

    strong = any("REFramework states" in e or "Framework workflows" in e for e in evidence)
    return strong, evidence
