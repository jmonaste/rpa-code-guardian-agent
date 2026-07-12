from rpa_code_guardian.ingest.scanner import scan_project
from rpa_code_guardian.model.summaries import Finding, NarrativeSections, WorkflowSummary
from rpa_code_guardian.render.document import render_documentation
from rpa_code_guardian.render.lint import lint_markdown, md_anchor


def test_lint_strips_emojis_and_extra_blanks():
    dirty = "# Title \U0001f680\n\n\n\nBody ✅ done \U0001f600\n"
    clean = lint_markdown(dirty)
    assert "\U0001f680" not in clean and "✅" not in clean and "\U0001f600" not in clean
    assert "\n\n\n" not in clean


def test_render_documentation_structure(sample_project):
    inv = scan_project(sample_project)
    narrative = NarrativeSections(
        executive_summary="Registers supplier invoices. \U0001f600",
        process_description="Reads the queue and posts invoices.",
        architecture="REFramework state machine.",
        exception_strategy="System exceptions are logged.",
    )
    summaries = {
        "Business/ExtractInvoice.xaml": WorkflowSummary(
            path="Business/ExtractInvoice.xaml",
            purpose="Extracts one invoice.",
            key_logic=["Open browser", "Type number"],
            one_liner="Extracts one invoice.",
        ),
        "Framework/SetTransactionStatus.xaml": WorkflowSummary(
            path="Framework/SetTransactionStatus.xaml",
            purpose="Standard status handling.",
            key_logic=[],
            is_boilerplate=True,
            one_liner="Standard REFramework status handling.",
        ),
    }
    findings = [Finding(severity="High", category="Reliability", location="X.xaml",
                        description="Empty catch.", recommendation="Log and rethrow.")]
    md = render_documentation({
        "inventory": inv, "narrative": narrative, "summaries": summaries,
        "findings": findings, "gap_answers": [], "warnings": ["one warning"],
    })

    assert md.startswith("# ACME-InvoiceProcessing — Technical Documentation")
    for heading in ("Executive summary", "Project overview", "Architecture",
                    "Configuration", "Workflow reference", "Improvement suggestions"):
        assert f"## {heading}" in md
        assert f"](#{md_anchor(heading)})" in md  # ToC entry
    assert "```mermaid" in md
    assert "| in_InvoiceId | in | String |" in md  # argument table straight from IR
    assert "Standard framework workflows" in md  # boilerplate grouped, not expanded
    assert "\U0001f600" not in md  # emoji stripped even if the model emitted one
    assert "SuperSecret123" not in md
    assert "High priority" in md
