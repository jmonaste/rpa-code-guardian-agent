"""Agentic verification passes: critic, smell verification, escalation, rescue."""

from __future__ import annotations

from rpa_code_guardian.graph.compliance import ComplianceNodes
from rpa_code_guardian.graph.nodes import PipelineNodes
from rpa_code_guardian.graph.state import MapPayload
from rpa_code_guardian.ingest.scanner import scan_project
from rpa_code_guardian.model.summaries import (
    ComplianceItem,
    NarrativeAudit,
    NarrativeSections,
    Requirement,
    WorkflowSummary,
)

from conftest import FakeGuardianLLM


def _narrative(**overrides) -> NarrativeSections:
    base = dict(
        executive_summary="Registers supplier invoices.",
        process_description="Reads the queue (Main.xaml) and posts invoices.",
        architecture="REFramework state machine.",
        exception_strategy="System exceptions are logged.",
    )
    base.update(overrides)
    return NarrativeSections(**base)


# ------------------------------------------------------------------- critic


def test_critic_warns_on_nonexistent_cited_workflow(sample_project, settings, fake_llm):
    nodes = PipelineNodes(settings, llm=fake_llm)
    state = {
        "inventory": scan_project(sample_project),
        "narrative": _narrative(
            process_description="Data is loaded by Framework/LoadStuff.xaml and Main.xaml."
        ),
        "summaries": {},
    }
    out = nodes.critic(state)
    warnings = out.get("warnings", [])
    assert any("Framework/LoadStuff.xaml" in w for w in warnings)
    assert not any("Main.xaml" in w for w in warnings)  # real path not flagged


def test_critic_turns_unsupported_claims_into_open_questions(sample_project, settings):
    class ClaimyLLM(FakeGuardianLLM):
        def structured(self, schema, system, user, role="worker"):
            if schema is NarrativeAudit:
                return NarrativeAudit(unsupported_claims=["The process sends a daily email."])
            return super().structured(schema, system, user, role)

    nodes = PipelineNodes(settings, llm=ClaimyLLM(settings))
    narrative = _narrative()
    state = {"inventory": scan_project(sample_project), "narrative": narrative, "summaries": {}}
    out = nodes.critic(state)
    questions = out["narrative"].open_questions
    assert any("daily email" in q for q in questions)
    assert any("unsupported claim" in w for w in out["warnings"])


# --------------------------------------------------------- smell verification


def _smelly_state(sample_project):
    return {
        "inventory": scan_project(sample_project),
        "summaries": {
            "Business/ExtractInvoice.xaml": WorkflowSummary(
                path="Business/ExtractInvoice.xaml",
                purpose="Extracts one invoice from the web application.",
                key_logic=["Open browser"],
                smells=["Selector relies on a fixed page title."],
            )
        },
    }


def test_refuted_smell_is_dropped(sample_project, settings):
    class SkepticLLM(FakeGuardianLLM):
        def tool_loop(self, system, user, tools, role="lead", max_iterations=None):
            return "REFUTED: the selector uses a wildcard title.", []

    nodes = PipelineNodes(settings, llm=SkepticLLM(settings))
    out = nodes.findings(_smelly_state(sample_project))
    assert not any("fixed page title" in f.description for f in out["findings"])
    assert any("dropped after adversarial verification" in w for w in out.get("warnings", []))


def test_confirmed_and_ambiguous_smells_are_kept(sample_project, settings, fake_llm):
    # The shared fake tool_loop answers with neither CONFIRMED nor REFUTED:
    # ambiguity must fail open (keep the finding).
    nodes = PipelineNodes(settings, llm=fake_llm)
    out = nodes.findings(_smelly_state(sample_project))
    assert any("fixed page title" in f.description for f in out["findings"])


def test_smell_verification_can_be_disabled(sample_project, settings):
    class ExplodingLLM(FakeGuardianLLM):
        def tool_loop(self, *a, **k):  # must never be called
            raise AssertionError("tool_loop called with verify_smells disabled")

    settings = settings.model_copy(update={"verify_smells": False})
    nodes = PipelineNodes(settings, llm=ExplodingLLM(settings))
    out = nodes.findings(_smelly_state(sample_project))
    assert any("fixed page title" in f.description for f in out["findings"])


# ------------------------------------------------------------- escalation


def test_weak_worker_summary_escalates_to_lead(settings):
    class WeakWorkerLLM(FakeGuardianLLM):
        def structured(self, schema, system, user, role="worker"):
            self.structured_calls.append(f"{schema.__name__}:{role}")
            if role == "worker":
                return WorkflowSummary(path="X.xaml", purpose="Short.", key_logic=[])
            return WorkflowSummary(
                path="X.xaml",
                purpose="Extracts invoice data from the queue item and posts it to the ERP.",
                key_logic=["Read item", "Post to ERP"],
            )

    llm = WeakWorkerLLM(settings)
    nodes = PipelineNodes(settings, llm=llm)
    payload = MapPayload(
        path="X.xaml", ir_context="WORKFLOW: X.xaml", callee_digests="", plan_notes="",
        content_hash="h1",
    )
    out = nodes.summarize(payload)
    summary = out["summaries"]["X.xaml"]
    assert summary.key_logic  # the lead's stronger answer won
    assert llm.structured_calls == ["WorkflowSummary:worker", "WorkflowSummary:lead"]


def test_boilerplate_summary_never_escalates(settings):
    class CountingLLM(FakeGuardianLLM):
        def structured(self, schema, system, user, role="worker"):
            self.structured_calls.append(role)
            return WorkflowSummary(
                path="X.xaml", purpose="Std.", key_logic=[], is_boilerplate=True
            )

    llm = CountingLLM(settings)
    nodes = PipelineNodes(settings, llm=llm)
    payload = MapPayload(
        path="X.xaml", ir_context="WORKFLOW: X.xaml", callee_digests="", plan_notes="",
        content_hash="h2",
    )
    nodes.summarize(payload)
    assert llm.structured_calls == ["worker"]


# --------------------------------------------------- compliance evidence rescue


def test_compliant_verdict_without_valid_evidence_is_rescued(sample_project, settings, fake_llm):
    pipeline = PipelineNodes(settings, llm=fake_llm)
    compliance = ComplianceNodes(pipeline)
    state = {
        "inventory": scan_project(sample_project),
        "requirements": [
            Requirement(id="R-01", text="Read pending invoices from the ACME_Invoices queue.", kind="functional")
        ],
        "compliance": {
            "R-01": ComplianceItem(
                requirement_id="R-01",
                verdict="Compliant",
                justification="Looks fine.",
                evidence=["Framework/DoesNotExist.xaml"],  # hallucinated
            )
        },
    }
    out = compliance.evidence_rescue(state)
    revised = out["compliance"]["R-01"]
    assert revised.evidence == ["Framework/GetTransactionData.xaml"]  # from actual tool reads


def test_compliant_verdict_with_real_evidence_is_left_alone(sample_project, settings):
    class ExplodingLLM(FakeGuardianLLM):
        def tool_loop(self, *a, **k):
            raise AssertionError("rescue ran for a well-evidenced verdict")

    pipeline = PipelineNodes(settings, llm=ExplodingLLM(settings))
    compliance = ComplianceNodes(pipeline)
    state = {
        "inventory": scan_project(sample_project),
        "requirements": [Requirement(id="R-01", text="Read the queue.", kind="functional")],
        "compliance": {
            "R-01": ComplianceItem(
                requirement_id="R-01",
                verdict="Compliant",
                justification="Queue read via GetTransactionData.",
                evidence=["Framework/GetTransactionData.xaml"],
            )
        },
    }
    assert compliance.evidence_rescue(state) == {}
