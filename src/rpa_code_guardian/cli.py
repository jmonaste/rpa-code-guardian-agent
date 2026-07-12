"""Command-line entry point.

    rpa-guardian analyze /path/to/project [--pdd PDD.md] [-o out/]
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

from . import __version__
from .config import load_settings

app = typer.Typer(add_completion=False, help="Document and audit UiPath projects with a local LLM.")
console = Console()


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
) -> None:
    """Analyze a UiPath project and write Obsidian-ready documentation."""
    from .graph.builder import run_pipeline

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

    done = {"summaries": 0, "verified": 0}

    def on_event(node: str, payload: object) -> None:
        if node == "ingest" and isinstance(payload, dict):
            inv = payload.get("inventory")
            if inv is not None:
                console.print(
                    f"[cyan]ingest[/cyan]: {len(inv.workflows)} workflows, "
                    f"REFramework={inv.is_reframework}"
                )
            return
        if node == "summarize":
            done["summaries"] += 1
            console.print(f"[cyan]map[/cyan]: {done['summaries']} workflows analyzed", end="\r")
            return
        if node == "verify_requirement":
            done["verified"] += 1
            console.print(f"[cyan]verify[/cyan]: {done['verified']} requirements checked", end="\r")
            return
        if verbose:
            console.print(f"[dim]{node}[/dim]")
        elif node in ("plan", "reduce", "gapfill", "compose", "extract_requirements", "compose_compliance"):
            console.print(f"[cyan]{node}[/cyan] done")

    try:
        state = run_pipeline(
            project_path.resolve(),
            settings,
            pdd_text=pdd_text,
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

    for warning in state.get("warnings", []) or []:
        console.print(f"[yellow]warning:[/yellow] {warning}")

    if not doc_md:
        raise typer.Exit(code=1)


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name.strip()).strip("-")


if __name__ == "__main__":
    app()
