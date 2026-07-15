"""Endpoint tuning utility: probes, concurrency sweep and recommendation."""

from __future__ import annotations

import threading
import time

from typer.testing import CliRunner

import rpa_code_guardian.tune as tune_mod
from rpa_code_guardian.cli import app
from rpa_code_guardian.config import Settings
from rpa_code_guardian.llm import LLMCallStats
from rpa_code_guardian.tune import (
    MethodResult,
    ProbeAnswer,
    SweepResult,
    TruncationResult,
    TuneReport,
    probe_methods,
    recommend_concurrency,
    run_tune,
    sweep_concurrency,
)

runner = CliRunner()


def _sweep(level, throughput, errors=0, retried=0):
    return SweepResult(
        level=level, calls=6, errors=errors, retried=retried,
        wall_seconds=1.0, avg_seconds=0.5, throughput=throughput,
    )


# ------------------------------------------------------------ recommendation


def test_recommend_picks_highest_clean_improving_level():
    sweep = [_sweep(1, 1.0), _sweep(2, 1.9), _sweep(4, 3.4), _sweep(8, 3.5)]
    # 8 adds only ~3% over 4: not worth the extra pressure.
    assert recommend_concurrency(sweep) == 4


def test_recommend_stops_at_first_level_with_errors_or_retries():
    sweep = [_sweep(1, 1.0), _sweep(2, 1.9), _sweep(4, 3.0, retried=2), _sweep(8, 4.0)]
    assert recommend_concurrency(sweep) == 2


def test_recommend_handles_struggling_endpoint_and_empty_sweep():
    assert recommend_concurrency([_sweep(1, 0.5, errors=3)]) == 1
    assert recommend_concurrency([]) == 1


# ------------------------------------------------------------------- sweep


class _SweepStubLLM:
    """Gateway stub: fixed small latency, optional failures every Nth call."""

    def __init__(self, error_every: int = 0) -> None:
        self.stats = LLMCallStats()
        self.calls = 0
        self.error_every = error_every
        self._lock = threading.Lock()

    def structured(self, schema, system, user, role="worker"):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.error_every and n % self.error_every == 0:
            raise RuntimeError("boom")
        time.sleep(0.002)
        return ProbeAnswer(message="pong", number=7)


def test_sweep_measures_each_level_and_counts_errors():
    llm = _SweepStubLLM(error_every=4)  # every 4th call fails
    results = sweep_concurrency(llm, "worker", levels=[1, 2], calls_per_level=4)
    assert [r.level for r in results] == [1, 2]
    assert all(r.calls == 4 for r in results)
    assert sum(r.errors for r in results) == 2  # calls 4 and 8
    assert all(r.throughput > 0 for r in results)


# ------------------------------------------------------------------ methods


class _MethodRunner:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def invoke(self, messages):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _MethodsStubLLM:
    """json_schema unsupported (400), function_calling works, fallback works."""

    def __init__(self) -> None:
        self.stats = LLMCallStats()

    def _chat_model(self, role):
        outer = self

        class _Model:
            def with_structured_output(self, schema, method):
                if method == "json_schema":
                    return _MethodRunner(RuntimeError("400 response_format not supported"))
                return _MethodRunner(ProbeAnswer(message="pong", number=7))

        return _Model()

    def _structured_via_json(self, schema, system, user, role):
        return ProbeAnswer(message="pong", number=7)


def test_probe_methods_reports_per_method_support():
    results = probe_methods(_MethodsStubLLM(), "worker")
    by_method = {r.method: r for r in results}
    assert not by_method["json_schema"].ok
    assert "400" in by_method["json_schema"].error
    assert by_method["function_calling"].ok
    assert by_method["plain-json"].ok


# ----------------------------------------------------------------- run_tune


def test_run_tune_probes_shared_model_once(monkeypatch):
    settings = Settings(
        _env_file=None, GUARDIAN_WORKER_MODEL="gpt-oss", GUARDIAN_LEAD_MODEL="gpt-oss"
    )
    calls = {"methods": 0, "truncation": 0}
    monkeypatch.setattr(tune_mod, "probe_connectivity", lambda s: (True, "HTTP 200", ["gpt-oss"]))

    def fake_methods(llm, role):
        calls["methods"] += 1
        return [MethodResult("json_schema", True, 0.5)]

    def fake_truncation(llm, role):
        calls["truncation"] += 1
        return TruncationResult(truncated=False, finish_reason="stop")

    monkeypatch.setattr(tune_mod, "probe_methods", fake_methods)
    monkeypatch.setattr(tune_mod, "probe_truncation", fake_truncation)

    report = run_tune(settings, llm=_SweepStubLLM(), skip_sweep=True)
    assert calls == {"methods": 1, "truncation": 1}  # same model: probed once
    assert report.methods["worker"] is report.methods["lead"]


def test_run_tune_stops_when_unreachable(monkeypatch):
    monkeypatch.setattr(tune_mod, "probe_connectivity", lambda s: (False, "connection error", []))
    report = run_tune(Settings(_env_file=None), llm=_SweepStubLLM())
    assert not report.reachable
    assert not report.methods and not report.sweep


# --------------------------------------------------------------------- CLI


def _full_report() -> TuneReport:
    return TuneReport(
        reachable=True,
        connectivity_detail="HTTP 200",
        available_models=["gpt-oss"],
        methods={
            "worker": [
                MethodResult("json_schema", True, 1.2),
                MethodResult("function_calling", True, 1.5),
                MethodResult("plain-json", True, 1.1),
            ]
        },
        truncation={"worker": TruncationResult(truncated=True, finish_reason="length")},
        sweep=[_sweep(1, 1.0), _sweep(2, 1.9), _sweep(4, 3.4), _sweep(8, 3.4, retried=1)],
    )


def test_tune_command_renders_report_and_recommendations(monkeypatch):
    monkeypatch.setattr(tune_mod, "run_tune", lambda *a, **k: _full_report())
    result = runner.invoke(app, ["tune"])
    assert result.exit_code == 0, result.output
    assert "Endpoint reachable" in result.output
    assert "Structured output support" in result.output
    assert "Concurrency sweep" in result.output
    assert "GUARDIAN_MAX_CONCURRENCY=4" in result.output
    assert "GUARDIAN_MAX_TOKENS" in result.output  # truncation was detected


def test_tune_command_fails_cleanly_when_unreachable(monkeypatch):
    monkeypatch.setattr(
        tune_mod, "run_tune",
        lambda *a, **k: TuneReport(reachable=False, connectivity_detail="connection error"),
    )
    result = runner.invoke(app, ["tune"])
    assert result.exit_code == 1
    assert "Endpoint unreachable" in result.output


def test_tune_command_rejects_invalid_levels():
    result = runner.invoke(app, ["tune", "--levels", "1,two,4"])
    assert result.exit_code == 2
    assert "Invalid levels" in result.output
