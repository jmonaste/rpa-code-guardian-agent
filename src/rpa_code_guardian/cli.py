"""Command-line entry point.

    rpa-guardian analyze /path/to/project [--pdd PDD.md] [-o out/]
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from . import __version__
from .config import load_settings

app = typer.Typer(add_completion=False, help="Document and audit UiPath projects with a local LLM.")
console = Console()

_LOG_LEVELS = ("debug", "info", "warning", "error")


def _setup_logging(level: str, log_file: Optional[Path]) -> None:
    """Route the agent's loggers to the console and optionally to a file.

    The LLM gateway logs every retry, backoff wait and structured-output method
    degradation. 'warning' (the default) keeps endpoint trouble visible without
    noise; 'debug' shows every model call with its timing. The file (when
    given) always receives full detail with timestamps, regardless of the
    console level, so a long run can be reviewed afterwards.
    """
    from rich.logging import RichHandler

    logger = logging.getLogger("rpa_code_guardian")
    logger.handlers.clear()
    console_level = getattr(logging, level.upper(), logging.WARNING)
    logger.setLevel(logging.DEBUG if log_file is not None else console_level)
    handler = RichHandler(console=console, show_path=False, log_time_format="[%X]")
    handler.setLevel(console_level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    if log_file is not None:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(file_handler)


@app.callback()
def _root() -> None:
    """rpa-code-guardian: UiPath project documentation and PDD compliance."""


@app.command()
def analyze(
    project_path: Annotated[Path, typer.Argument(help="Root directory of the UiPath project (contains project.json).")],
    pdd: Annotated[Optional[Path], typer.Option("--pdd", help="PDD as .md/.txt; when given, a compliance report is also generated.")] = None,
    output: Annotated[Path, typer.Option("--output", "-o", help="Directory where the Markdown files are written.")] = Path("out"),
    base_url: Annotated[Optional[str], typer.Option(help="OpenAI-compatible endpoint base URL (overrides .env).")] = None,
    worker_model: Annotated[Optional[str], typer.Option(help="Model for per-workflow analysis (overrides .env).")] = None,
    lead_model: Annotated[Optional[str], typer.Option(help="Model for narrative and compliance (overrides .env).")] = None,
    max_concurrency: Annotated[Optional[int], typer.Option(help="Parallel LLM calls in the map phase.")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="Resume the previous interrupted run of this project.")] = False,
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Ignore cached per-workflow summaries.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show every pipeline event.")] = False,
    log_level: Annotated[str, typer.Option("--log-level", help="Console log level: debug, info, warning or error. 'debug' shows every LLM call with timing.")] = "warning",
    log_file: Annotated[Optional[Path], typer.Option("--log-file", help="Also write full debug logs (with timestamps) to this file.")] = None,
) -> None:
    """Analyze a UiPath project and write Obsidian-ready documentation."""
    from .graph.builder import run_pipeline
    from .llm import GuardianLLM, LLMCallStats

    if log_level.lower() not in _LOG_LEVELS:
        console.print(f"[red]Invalid log level:[/red] {log_level} (use one of: {', '.join(_LOG_LEVELS)})")
        raise typer.Exit(code=2)
    _setup_logging(log_level, log_file)

    if not project_path.is_dir():
        console.print(f"[red]Not a directory:[/red] {project_path}")
        raise typer.Exit(code=2)

    pdd_text = ""
    if pdd is not None:
        try:
            pdd_text = pdd.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            console.print(f"[red]Could not read PDD:[/red] {exc}")
            raise typer.Exit(code=2)

    settings = load_settings(
        openai_base_url=base_url,
        GUARDIAN_WORKER_MODEL=worker_model,
        GUARDIAN_LEAD_MODEL=lead_model,
        GUARDIAN_MAX_CONCURRENCY=max_concurrency,
        GUARDIAN_USE_CACHE=False if no_cache else None,
    )

    console.print(
        f"[bold]rpa-code-guardian v{__version__}[/bold] — "
        f"worker: {settings.resolved_worker_model()}, lead: {settings.resolved_lead_model()}"
    )

    started = time.monotonic()
    done = {"summaries": 0, "verified": 0}
    totals = {"workflows": 0, "requirements": 0}
    progress = {"active": False}  # a \r progress line is on screen
    phase = {"text": ""}  # current in-place phase text (map/verify progress)
    llm_counts = {"ok": 0, "retried": 0, "failed": 0}
    print_lock = threading.Lock()  # stats callbacks fire from map worker threads

    def _llm_suffix() -> str:
        parts = [f"[green]{llm_counts['ok']} ok[/green]"]
        if llm_counts["retried"]:
            parts.append(f"[yellow]{llm_counts['retried']} retried[/yellow]")
        if llm_counts["failed"]:
            parts.append(f"[red]{llm_counts['failed']} failed[/red]")
        return "llm: " + ", ".join(parts)

    def _progress_line() -> None:
        """Redraw the in-place status line: current phase + live LLM counters."""
        text = f"{phase['text']} · {_llm_suffix()}" if phase["text"] else _llm_suffix()
        console.print(text, end="\r")
        progress["active"] = True

    def _line(text: str) -> None:
        """Print a normal line, first closing any in-place progress line."""
        with print_lock:
            if progress["active"]:
                console.print("")
                progress["active"] = False
            phase["text"] = ""  # the stage that owned the progress line is over
            console.print(text)

    def on_stats(snapshot: dict) -> None:
        with print_lock:
            llm_counts.update(snapshot)
            _progress_line()

    llm = GuardianLLM(settings, stats=LLMCallStats(on_change=on_stats))

    def on_event(node: str, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        if node == "ingest":
            inv = data.get("inventory")
            waves = data.get("waves") or []
            totals["workflows"] = sum(len(w) for w in waves)
            if inv is not None:
                _line(
                    f"[cyan]ingest[/cyan]: {len(inv.workflows)} workflows "
                    f"({totals['workflows']} to analyze in {len(waves)} waves), "
                    f"REFramework={inv.is_reframework}"
                )
            return
        if node == "summarize":
            with print_lock:
                done["summaries"] += 1
                total = f"/{totals['workflows']}" if totals["workflows"] else ""
                phase["text"] = f"[cyan]map[/cyan]: {done['summaries']}{total} workflows analyzed"
                _progress_line()
            return
        if node == "critic":
            flagged = [w for w in data.get("warnings") or []]
            if flagged:
                _line(f"[cyan]critic[/cyan]: {len(flagged)} grounding issue(s) flagged")
            else:
                _line("[cyan]critic[/cyan]: narrative grounded")
            return
        if node == "findings":
            found = data.get("findings") or []
            by: dict[str, int] = {}
            for f in found:
                by[f.severity] = by.get(f.severity, 0) + 1
            detail = ", ".join(f"{by[s]} {s.lower()}" for s in ("High", "Medium", "Low") if by.get(s))
            _line(f"[cyan]findings[/cyan]: {len(found)} issue(s)" + (f" ({detail})" if detail else ""))
            return
        if node == "gapfill":
            answers = data.get("gap_answers") or []
            if answers:
                _line(f"[cyan]gapfill[/cyan]: {len(answers)} open question(s) answered with project evidence")
            else:
                _line("[cyan]gapfill[/cyan]: nothing to verify")
            return
        if node == "extract_requirements":
            totals["requirements"] = len(data.get("requirements") or [])
            _line(f"[cyan]requirements[/cyan]: {totals['requirements']} extracted from the PDD")
            return
        if node == "verify_requirement":
            with print_lock:
                done["verified"] += 1
                total = f"/{totals['requirements']}" if totals["requirements"] else ""
                phase["text"] = f"[cyan]verify[/cyan]: {done['verified']}{total} requirements checked"
                _progress_line()
            return
        if node == "evidence_rescue":
            revised = data.get("compliance") or {}
            if revised:
                _line(f"[cyan]evidence rescue[/cyan]: {len(revised)} verdict(s) re-checked against the project")
            else:
                _line("[cyan]evidence rescue[/cyan]: all verdicts already evidenced")
            return
        if verbose:
            _line(f"[dim]{node}[/dim]")
        elif node in ("plan", "reduce", "compose", "compose_compliance"):
            _line(f"[cyan]{node}[/cyan] done")

    try:
        state = run_pipeline(
            project_path.resolve(),
            settings,
            pdd_text=pdd_text,
            llm=llm,
            on_event=on_event,
            resume=resume,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    output.mkdir(parents=True, exist_ok=True)
    inv = state.get("inventory")
    stem = _safe_name(inv.meta.name if inv else "") or _safe_name(project_path.name) or "Project"

    doc_md = state.get("documentation_md", "")
    if doc_md:
        doc_path = output / f"{stem}-Documentation.md"
        doc_path.write_text(doc_md, encoding="utf-8")
        console.print(f"\n[green]Documentation written:[/green] {doc_path}")
    else:
        console.print("[red]No documentation was produced.[/red]")

    if pdd_text:
        comp_md = state.get("compliance_md", "")
        if comp_md:
            comp_path = output / f"{stem}-Compliance.md"
            comp_path.write_text(comp_md, encoding="utf-8")
            console.print(f"[green]Compliance report written:[/green] {comp_path}")
        else:
            console.print("[red]No compliance report was produced.[/red]")

    warnings = state.get("warnings", []) or []
    for warning in warnings:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    analyzed = len(state.get("summaries") or {})
    calls = llm.stats.snapshot()
    call_detail = f"{calls['ok']} llm calls ok"
    if calls["retried"]:
        call_detail += f", {calls['retried']} retried"
    if calls["failed"]:
        call_detail += f", {calls['failed']} failed"
    console.print(
        f"[green]Done in {_fmt_duration(time.monotonic() - started)}[/green] — "
        f"{analyzed} workflows analyzed, {call_detail}, {len(warnings)} warning(s)"
    )

    if not doc_md:
        raise typer.Exit(code=1)


def _fmt_duration(seconds: float) -> str:
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{seconds:.0f}s"


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name.strip()).strip("-")


if __name__ == "__main__":
    app()
