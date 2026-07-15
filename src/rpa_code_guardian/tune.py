"""Endpoint probing and parameter tuning (the ``rpa-guardian tune`` command).

Answers, against the *configured* endpoint and models, the questions that
otherwise surface mid-run as warnings: is the endpoint reachable, which
structured-output method actually works per model, does the configured
``max_tokens`` truncate a reasoning model's output, and how many parallel
calls the endpoint sustains before it starts returning transient errors.

Every probe goes through the same ``GuardianLLM`` gateway the pipeline uses,
so what works here works in a real run.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .config import Settings
from .llm import GuardianLLM, _error_summary

PROBE_SYSTEM = "You answer exactly as instructed, nothing else."
PROBE_USER = "Reply with message set to 'pong' and number set to 7."
TRUNCATION_USER = (
    "List the numbers 1 to 30, one per line, then write the word END on its own line."
)
STRUCTURED_METHODS = ("json_schema", "function_calling")


class ProbeAnswer(BaseModel):
    """Tiny schema used to probe structured-output support."""

    message: str = Field(description="The word 'pong'.")
    number: int = Field(description="The number 7.")


@dataclass
class MethodResult:
    method: str
    ok: bool
    seconds: float
    error: str = ""


@dataclass
class SweepResult:
    level: int
    calls: int
    errors: int
    retried: int
    wall_seconds: float
    avg_seconds: float
    throughput: float  # successful calls per second at this level


@dataclass
class TruncationResult:
    truncated: bool
    finish_reason: str
    error: str = ""


@dataclass
class TuneReport:
    reachable: bool
    connectivity_detail: str
    available_models: list[str] = field(default_factory=list)
    methods: dict[str, list[MethodResult]] = field(default_factory=dict)  # role -> results
    truncation: dict[str, TruncationResult] = field(default_factory=dict)  # role -> result
    sweep: list[SweepResult] = field(default_factory=list)


# ----------------------------------------------------------------- probes


def probe_connectivity(settings: Settings) -> tuple[bool, str, list[str]]:
    """GET /models on the endpoint: reachability plus the served model ids."""
    import httpx

    url = settings.openai_base_url.rstrip("/") + "/models"
    try:
        response = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            verify=settings.verify_ssl,
            timeout=min(settings.request_timeout, 15.0),
        )
        response.raise_for_status()
        data = response.json()
        ids = [str(m.get("id", "")) for m in data.get("data", []) if m.get("id")]
        return True, f"HTTP {response.status_code}", ids
    except Exception as exc:  # noqa: BLE001 - report, don't crash the tool
        return False, _error_summary(exc), []


def probe_methods(llm: GuardianLLM, role: str) -> list[MethodResult]:
    """Try each structured-output method once, plus the plain-JSON fallback."""
    model = llm._chat_model(role)
    results: list[MethodResult] = []
    for method in STRUCTURED_METHODS:
        started = time.perf_counter()
        try:
            runner = model.with_structured_output(ProbeAnswer, method=method)
            out = runner.invoke(
                [SystemMessage(content=PROBE_SYSTEM), HumanMessage(content=PROBE_USER)]
            )
            ok = isinstance(out, ProbeAnswer)
            error = "" if ok else "returned no valid object"
        except Exception as exc:  # noqa: BLE001
            ok, error = False, _error_summary(exc)
        results.append(MethodResult(method, ok, time.perf_counter() - started, error))

    started = time.perf_counter()
    try:
        llm._structured_via_json(ProbeAnswer, PROBE_SYSTEM, PROBE_USER, role)
        results.append(MethodResult("plain-json", True, time.perf_counter() - started))
    except Exception as exc:  # noqa: BLE001
        results.append(
            MethodResult("plain-json", False, time.perf_counter() - started, _error_summary(exc))
        )
    return results


def probe_truncation(llm: GuardianLLM, role: str) -> TruncationResult:
    """Detect whether the configured max_tokens cuts a completion short.

    Reasoning models spend budget thinking before answering; if the sentinel
    word never arrives or the endpoint reports finish_reason='length', the
    configured GUARDIAN_MAX_TOKENS is too small for real workloads.
    """
    model = llm._chat_model(role)
    try:
        reply = model.invoke(
            [SystemMessage(content=PROBE_SYSTEM), HumanMessage(content=TRUNCATION_USER)]
        )
    except Exception as exc:  # noqa: BLE001
        return TruncationResult(truncated=False, finish_reason="", error=_error_summary(exc))
    finish = str((getattr(reply, "response_metadata", None) or {}).get("finish_reason", ""))
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    truncated = finish == "length" or "END" not in text
    return TruncationResult(truncated=truncated, finish_reason=finish or "?")


def sweep_concurrency(
    llm: GuardianLLM, role: str, levels: list[int], calls_per_level: int
) -> list[SweepResult]:
    """Fire the probe call at increasing parallelism and measure the endpoint.

    Retries are read from the gateway's own stats, so a level that only
    survives thanks to backoff is visibly worse than one that answers clean.
    """
    results: list[SweepResult] = []
    for level in levels:
        base = llm.stats.snapshot()
        latencies: list[float] = []
        errors = 0
        lock = threading.Lock()

        def one_call(_: int) -> None:
            nonlocal errors
            started = time.perf_counter()
            try:
                llm.structured(ProbeAnswer, PROBE_SYSTEM, PROBE_USER, role=role)
                with lock:
                    latencies.append(time.perf_counter() - started)
            except Exception:  # noqa: BLE001
                with lock:
                    errors += 1

        wall_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=level) as pool:
            list(pool.map(one_call, range(calls_per_level)))
        wall = time.perf_counter() - wall_started

        retried = llm.stats.snapshot()["retried"] - base["retried"]
        results.append(
            SweepResult(
                level=level,
                calls=calls_per_level,
                errors=errors,
                retried=retried,
                wall_seconds=wall,
                avg_seconds=sum(latencies) / len(latencies) if latencies else 0.0,
                throughput=len(latencies) / wall if wall > 0 else 0.0,
            )
        )
    return results


def recommend_concurrency(sweep: list[SweepResult], min_gain: float = 1.15) -> int:
    """Highest level that stays clean AND still improves throughput.

    Walk the levels in order; stop at the first level that errors, needs
    retries, or gains less than ``min_gain`` over the best so far — beyond
    that point extra parallelism only queues requests or trips rate limits.
    """
    if not sweep:
        return 1
    best = sweep[0]
    if best.errors or best.retried:
        return best.level  # even sequential calls struggle; nothing to gain
    for result in sweep[1:]:
        if result.errors or result.retried:
            break
        if result.throughput < best.throughput * min_gain:
            break
        best = result
    return best.level


# ------------------------------------------------------------------ runner


def run_tune(
    settings: Settings,
    llm: GuardianLLM | None = None,
    levels: list[int] | None = None,
    calls_per_level: int = 6,
    skip_sweep: bool = False,
) -> TuneReport:
    """Run every probe and return the full report (rendering is the CLI's job)."""
    llm = llm or GuardianLLM(settings)
    reachable, detail, models = probe_connectivity(settings)
    report = TuneReport(reachable=reachable, connectivity_detail=detail, available_models=models)
    if not reachable:
        return report

    roles = {"worker": settings.resolved_worker_model(), "lead": settings.resolved_lead_model()}
    probed: dict[str, str] = {}
    for role, model_name in roles.items():
        if model_name in probed:
            report.methods[role] = report.methods[probed[model_name]]
            report.truncation[role] = report.truncation[probed[model_name]]
            continue
        report.methods[role] = probe_methods(llm, role)
        report.truncation[role] = probe_truncation(llm, role)
        probed[model_name] = role

    if not skip_sweep:
        report.sweep = sweep_concurrency(
            llm, "worker", levels or [1, 2, 4, 8], calls_per_level
        )
    return report
