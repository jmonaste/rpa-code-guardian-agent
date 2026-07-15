"""CLI: logging configuration and console progress reporting."""

from __future__ import annotations

import logging

from typer.testing import CliRunner

import rpa_code_guardian.graph.builder as builder_mod
from rpa_code_guardian.cli import _setup_logging, app
from rpa_code_guardian.ingest.scanner import scan_project
from rpa_code_guardian.model.summaries import Finding

runner = CliRunner()


def test_analyze_reports_progress_and_writes_output(tmp_path, monkeypatch, sample_project):
    inv = scan_project(sample_project)

    def fake_run_pipeline(project_root, settings, pdd_text="", llm=None, on_event=None, resume=False):
        on_event("ingest", {"inventory": inv, "waves": [["A.xaml"], ["B.xaml", "C.xaml"]], "warnings": []})
        on_event("plan", {"plan": None})
        llm.stats.record("ok")  # live endpoint counter ticks from the gateway
        llm.stats.record("ok")
        llm.stats.record("retried")
        on_event("summarize", {"summaries": {}})
        on_event("summarize", {"summaries": {}})
        on_event("reduce", {})
        on_event("critic", {"warnings": ["narrative cites nonexistent workflow: X.xaml"]})
        on_event("findings", {"findings": [Finding(severity="High", category="Reliability", description="Empty catch.", recommendation="Fix.")]})
        on_event("gapfill", {"gap_answers": []})
        on_event("compose", {})
        return {
            "inventory": inv,
            "summaries": {"A.xaml": None, "B.xaml": None, "C.xaml": None},
            "documentation_md": "# Doc\n",
            "warnings": ["one warning"],
        }

    monkeypatch.setattr(builder_mod, "run_pipeline", fake_run_pipeline)
    result = runner.invoke(app, ["analyze", str(sample_project), "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "3 to analyze in 2 waves" in result.output
    assert "2/3 workflows analyzed" in result.output
    assert "2 ok" in result.output and "1 retried" in result.output  # live LLM counter
    assert "2 llm calls ok, 1 retried" in result.output  # final summary
    assert "1 grounding issue(s) flagged" in result.output
    assert "1 issue(s) (1 high)" in result.output
    assert "nothing to verify" in result.output
    assert "one warning" in result.output
    assert "3 workflows analyzed, 2 llm calls ok, 1 retried, 1 warning(s)" in result.output
    doc = tmp_path / "ACME-InvoiceProcessing-Documentation.md"
    assert doc.read_text(encoding="utf-8") == "# Doc\n"


def test_invalid_log_level_is_rejected(sample_project):
    result = runner.invoke(app, ["analyze", str(sample_project), "--log-level", "loud"])
    assert result.exit_code == 2
    assert "Invalid log level" in result.output


def test_setup_logging_routes_agent_records_to_file(tmp_path):
    log_path = tmp_path / "run.log"
    _setup_logging("error", log_path)  # console quiet; file still gets full detail
    logging.getLogger("rpa_code_guardian.llm").warning("transient endpoint error, retrying in 2s")
    for handler in logging.getLogger("rpa_code_guardian").handlers:
        handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert "retrying in 2s" in content
    assert "WARNING" in content
    logging.getLogger("rpa_code_guardian").handlers.clear()


def test_setup_logging_console_level_applies(capsys):
    _setup_logging("warning", None)
    logger = logging.getLogger("rpa_code_guardian.llm")
    assert not logger.isEnabledFor(logging.DEBUG)
    assert logger.isEnabledFor(logging.WARNING)
    logging.getLogger("rpa_code_guardian").handlers.clear()
