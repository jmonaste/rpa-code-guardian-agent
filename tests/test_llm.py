"""Retry/backoff behavior of the LLM gateway and JSON-extraction robustness."""

from __future__ import annotations

import logging

import pytest

import rpa_code_guardian.llm as llm_mod
from rpa_code_guardian.config import Settings
from rpa_code_guardian.llm import (
    GuardianLLM,
    GuardianLLMUnavailable,
    LLMCallStats,
    _error_summary,
    _is_retryable,
)
from rpa_code_guardian.model.summaries import AnalysisPlan, WorkflowSummary


class _Runner:
    """Stub runner: raises ``exc`` for the first ``failures`` invokes, then returns ``result``."""

    def __init__(self, failures: int, exc: Exception, result="ok") -> None:
        self.failures = failures
        self.exc = exc
        self.result = result
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return self.result


@pytest.fixture
def fast_llm(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)  # no real waiting
    settings = Settings(
        _env_file=None, GUARDIAN_LLM_RETRIES=3, GUARDIAN_LLM_RETRY_BASE_DELAY=0.01
    )
    return GuardianLLM(settings)


def test_is_retryable_matches_gateway_and_rate_limit_errors():
    assert _is_retryable(RuntimeError("Error code: 429 - {'detail': 'Rate limit exceeded'}"))
    assert _is_retryable(RuntimeError("<html><body><h1>504 Gateway Time-out</h1></body></html>"))
    assert _is_retryable(RuntimeError("The read operation timed out"))
    assert not _is_retryable(ValueError("1 validation error for WorkflowSummary"))


def test_is_retryable_walks_cause_chain():
    inner = RuntimeError("504 Gateway Time-out")
    outer = RuntimeError("call failed")
    outer.__cause__ = inner
    assert _is_retryable(outer)


def test_error_summary_strips_html_pages():
    text = _error_summary(RuntimeError("<html><body><h1>504 Gateway Time-out</h1>\nslow</body></html>"))
    assert "<html>" not in text
    assert "504" in text


def test_invoke_retries_then_succeeds(fast_llm):
    runner = _Runner(2, RuntimeError("429 rate limit"))
    assert fast_llm._invoke_with_retry(runner, []) == "ok"
    assert runner.calls == 3


def test_invoke_raises_unavailable_after_exhaustion(fast_llm):
    runner = _Runner(99, RuntimeError("504 Gateway Time-out"))
    with pytest.raises(GuardianLLMUnavailable):
        fast_llm._invoke_with_retry(runner, [])
    assert runner.calls == 4  # 1 attempt + 3 retries


def test_non_retryable_errors_propagate_immediately(fast_llm):
    runner = _Runner(99, ValueError("validation error"))
    with pytest.raises(ValueError):
        fast_llm._invoke_with_retry(runner, [])
    assert runner.calls == 1


def test_retries_emit_reviewable_log_records(fast_llm):
    """Each backoff wait is logged with attempt count and delay."""
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    lg = logging.getLogger("rpa_code_guardian.llm")
    old_level = lg.level
    handler = _Capture(level=logging.DEBUG)
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        runner = _Runner(1, RuntimeError("429 rate limit"))
        fast_llm._invoke_with_retry(runner, [])
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)
    assert any("retrying in" in m for m in records)
    assert any("LLM call ok" in m for m in records)


def test_stats_count_ok_retries_and_failures(monkeypatch):
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)
    snapshots: list[dict] = []
    settings = Settings(_env_file=None, GUARDIAN_LLM_RETRIES=1, GUARDIAN_LLM_RETRY_BASE_DELAY=0.01)
    llm = GuardianLLM(settings, stats=LLMCallStats(on_change=snapshots.append))

    llm._invoke_with_retry(_Runner(1, RuntimeError("429 rate limit")), [])  # retry then ok
    with pytest.raises(GuardianLLMUnavailable):
        llm._invoke_with_retry(_Runner(99, RuntimeError("504 Gateway Time-out")), [])

    assert llm.stats.snapshot() == {"ok": 1, "retried": 2, "failed": 1}
    assert snapshots[-1] == {"ok": 1, "retried": 2, "failed": 1}  # observer saw every update
    assert len(snapshots) == 4


def test_stats_ignore_non_transport_errors(fast_llm):
    with pytest.raises(ValueError):
        fast_llm._invoke_with_retry(_Runner(99, ValueError("validation error")), [])
    # The endpoint answered (unusable content is not a transport failure).
    assert fast_llm.stats.snapshot() == {"ok": 0, "retried": 0, "failed": 0}


def test_stats_callback_errors_never_break_a_call(fast_llm):
    def broken(_snapshot: dict) -> None:
        raise RuntimeError("display crashed")

    fast_llm.stats.on_change = broken
    assert fast_llm._invoke_with_retry(_Runner(0, RuntimeError("unused")), []) == "ok"


class _StructStub:
    """Chat-model stub whose every structured runner fails with the given error."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.methods: list[str] = []

    def with_structured_output(self, schema, method):
        self.methods.append(method)
        return _Runner(999, self.exc)

    def invoke(self, messages):  # the plain-JSON fallback path
        raise self.exc


def test_structured_aborts_method_ladder_when_endpoint_is_down(fast_llm, monkeypatch):
    stub = _StructStub(RuntimeError("504 Gateway Time-out"))
    monkeypatch.setattr(fast_llm, "_chat_model", lambda role: stub)
    with pytest.raises(GuardianLLMUnavailable):
        fast_llm.structured(AnalysisPlan, "sys", "user")
    # A dead transport must NOT fall through to function_calling / JSON fallback.
    assert stub.methods == ["json_schema"]


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content


class _PlainModel:
    """Chat-model stub for the plain-JSON fallback: returns canned text."""

    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, messages):
        return _Reply(self.content)


GOOD = '{"path":"X.xaml","purpose":"does X","key_logic":["a","b"],"one_liner":"Does X."}'


@pytest.mark.parametrize(
    "text",
    [
        GOOD,  # plain
        '{"WorkflowSummary": %s}' % GOOD,  # wrapped under schema name
        '<think>let me {reason} about it</think>\n```json\n%s\n```' % GOOD,  # reasoning + fence
        '{"properties":{"path":1}} the answer: %s' % GOOD,  # schema echo then real object
    ],
    ids=["plain", "wrapped", "reasoning", "schema-echo"],
)
def test_structured_via_json_recovers_messy_outputs(fast_llm, monkeypatch, text):
    monkeypatch.setattr(fast_llm, "_chat_model", lambda role: _PlainModel(text))
    result = fast_llm._structured_via_json(WorkflowSummary, "sys", "user", "worker")
    assert result.purpose == "does X"
    assert result.key_logic == ["a", "b"]
