"""Shared fixtures: the sample REFramework project and a fake LLM gateway."""

from __future__ import annotations

from pathlib import Path

import pytest

from rpa_code_guardian.config import Settings
from rpa_code_guardian.llm import GuardianLLM
from rpa_code_guardian.model.summaries import (
    AnalysisPlan,
    ComplianceItem,
    NarrativeSections,
    Requirement,
    RequirementList,
    WorkflowSummary,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def sample_project() -> Path:
    return FIXTURES / "sample-project"


@pytest.fixture()
def sample_pdd_text() -> str:
    return (FIXTURES / "sample-pdd.md").read_text(encoding="utf-8")


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        _env_file=None,
        GUARDIAN_WORKER_MODEL="fake-worker",
        GUARDIAN_LEAD_MODEL="fake-lead",
        GUARDIAN_USE_CACHE=False,
        GUARDIAN_MAX_CONCURRENCY=2,
    )


class FakeGuardianLLM(GuardianLLM):
    """Deterministic stand-in: canned structured outputs, scripted tool use."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.structured_calls: list[str] = []
        self.narrative_round = 0

    def structured(self, schema, system, user, role="worker"):
        self.structured_calls.append(schema.__name__)
        if schema is AnalysisPlan:
            return AnalysisPlan(
                process_type="Invoice registration process",
                focus_areas=["Queue handling", "Invoice extraction"],
                boilerplate_workflows=["Framework/SetTransactionStatus.xaml"],
            )
        if schema is WorkflowSummary:
            path = ""
            for line in user.splitlines():
                if line.startswith("WORKFLOW: "):
                    path = line.removeprefix("WORKFLOW: ").strip()
                    break
            return WorkflowSummary(
                path=path,
                purpose=f"Handles the responsibility of {path or 'this workflow'} within the process.",
                key_logic=["Receive inputs", "Perform its step", "Log the outcome"],
                inputs_outputs="Receives the shared Config dictionary.",
                error_handling="none",
                external_systems=["chrome.exe"] if "ExtractInvoice" in path else [],
                is_boilerplate="SetTransactionStatus" in path,
                smells=["Selector relies on a fixed page title."] if "ExtractInvoice" in path else [],
                one_liner=f"Performs the {Path(path).stem} step.",
            )
        if schema is NarrativeSections:
            self.narrative_round += 1
            return NarrativeSections(
                executive_summary="The process registers supplier invoices automatically. \U0001f600",
                process_description="Invoices are read from the queue (Main.xaml) and typed into the invoicing system (Business/ExtractInvoice.xaml).",
                architecture="Standard REFramework state machine with four states.",
                exception_strategy="System exceptions are captured in Process Transaction and logged.",
                logging_observability="Each workflow logs its progress.",
                external_systems="The invoicing web application is accessed through Chrome.",
                open_questions=["Which queue name is actually used?"] if self.narrative_round == 1 else [],
            )
        if schema is RequirementList:
            return RequirementList(
                requirements=[
                    Requirement(id="R-01", text="Read pending invoices from the ACME_Invoices queue.", kind="functional"),
                    Requirement(id="R-02", text="Send a summary email to accounts payable at the end of the run.", kind="reporting"),
                ]
            )
        if schema is ComplianceItem:
            if "R-02" in user or "summary email" in user:
                return ComplianceItem(
                    requirement_id="",
                    verdict="Not verifiable" if "EVIDENCE FOUND" not in user else "Non-compliant",
                    justification="No email activity exists anywhere in the analyzed workflows.",
                    evidence=[],
                    gap="The end-of-run summary email is not implemented.",
                )
            return ComplianceItem(
                requirement_id="",
                verdict="Compliant",
                justification="The queue is read via GetTransactionData using the configured queue name.",
                evidence=["Framework/GetTransactionData.xaml"],
            )
        raise AssertionError(f"unexpected schema {schema.__name__}")

    def tool_loop(self, system, user, tools, role="lead", max_iterations=None):
        by_name = {t.name: t for t in tools}
        by_name["search_project"].invoke({"query": "OrchestratorQueueName"})
        by_name["read_workflow"].invoke({"path": "Framework/GetTransactionData.xaml"})
        return (
            "The queue name comes from the Config entry OrchestratorQueueName (ACME_Invoices).",
            [],
        )


@pytest.fixture()
def fake_llm(settings: Settings) -> FakeGuardianLLM:
    return FakeGuardianLLM(settings)
