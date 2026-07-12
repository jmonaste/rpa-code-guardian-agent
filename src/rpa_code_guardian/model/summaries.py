"""Structured outputs produced by the LLM at each stage of the pipeline.

Every schema here is used with tool-calling structured output, so field
descriptions double as instructions to the model. Keep them precise.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AnalysisPlan(BaseModel):
    """Output of the plan node: how to approach this specific project."""

    process_type: str = Field(description="One line naming what kind of business process this appears to be.")
    focus_areas: list[str] = Field(description="3-6 specific things the analysis should pay attention to for THIS project.")
    boilerplate_workflows: list[str] = Field(
        default_factory=list,
        description="Paths of workflows that look like unmodified REFramework template boilerplate.",
    )
    notes: str = Field(default="", description="Any other observation useful for the downstream analysis.")


class WorkflowSummary(BaseModel):
    """Per-workflow analysis produced in the map phase."""

    path: str = Field(default="", description="Project-relative path of the workflow. Copy it exactly from the input.")
    purpose: str = Field(description="One paragraph: what this workflow does and why it exists in the process.")
    key_logic: list[str] = Field(description="The main logical steps, in order, as short plain statements.")
    inputs_outputs: str = Field(default="", description="One or two sentences on what flows in and out (arguments, side effects).")
    error_handling: str = Field(default="", description="How errors are handled here (retries, try/catch, throws), or 'none'.")
    external_systems: list[str] = Field(default_factory=list, description="Applications, URLs, files, queues or services this workflow touches.")
    is_boilerplate: bool = Field(default=False, description="True if this is standard REFramework template code with no meaningful customization.")
    smells: list[str] = Field(default_factory=list, description="Concrete code-quality issues observed, each one line. Empty if none.")
    one_liner: str = Field(default="", description="A single sentence (max 20 words) describing the workflow, used as digest for its callers.")


class NarrativeSections(BaseModel):
    """Output of the reduce node: the document's prose sections."""

    executive_summary: str = Field(description="3-6 sentences: what the process automates, for whom, and its overall shape.")
    process_description: str = Field(description="The business process end to end, as flowing prose with paragraph breaks. Reference workflows by path in parentheses.")
    architecture: str = Field(description="How the solution is structured technically (states, layers, orchestration), in prose.")
    exception_strategy: str = Field(description="System and business exception handling across the project, in prose.")
    logging_observability: str = Field(default="", description="Logging approach and how operable the process is, in prose.")
    external_systems: str = Field(default="", description="Prose paragraph on every external application/system involved and how it is accessed.")
    open_questions: list[str] = Field(
        default_factory=list,
        description="Up to 5 specific questions that could NOT be answered from the summaries alone and need evidence from the project files. Empty if none.",
    )


class GapAnswer(BaseModel):
    """One resolved open question from the gap-fill agent."""

    question: str
    answer: str
    evidence: list[str] = Field(default_factory=list)  # workflow/config paths consulted


class Finding(BaseModel):
    """A single improvement suggestion (deterministic check or LLM observation)."""

    severity: str = Field(description="High, Medium or Low.")
    category: str = Field(description="Short category, e.g. 'Reliability', 'Maintainability', 'Security', 'Performance'.")
    location: str = Field(default="", description="Workflow path or project area the finding applies to.")
    description: str = Field(description="What is wrong, one or two sentences.")
    recommendation: str = Field(description="What to do about it, one or two sentences.")


class Requirement(BaseModel):
    """One requirement extracted from the PDD."""

    id: str = Field(description="Stable identifier like R-01, R-02, in document order.")
    text: str = Field(description="The requirement, condensed to one or two sentences but preserving specifics (values, systems, rules).")
    kind: str = Field(description="functional | exception-handling | reporting | non-functional | other")
    section: str = Field(default="", description="PDD section or heading it came from, if identifiable.")


class RequirementList(BaseModel):
    """Structured wrapper so requirement extraction is a single tool call."""

    requirements: list[Requirement] = Field(description="Every testable requirement found, in document order.")


class ComplianceItem(BaseModel):
    """Verdict for one requirement against the implemented code."""

    requirement_id: str = Field(default="", description="The id of the requirement being assessed. Copy it exactly.")
    verdict: str = Field(description="Compliant | Partially compliant | Non-compliant | Not verifiable")
    justification: str = Field(description="Two to four sentences explaining the verdict with concrete references to the implementation.")
    evidence: list[str] = Field(default_factory=list, description="Workflow or config paths that support the verdict.")
    gap: str = Field(default="", description="If not fully compliant: what exactly is missing or deviates. Empty otherwise.")
