from rpa_code_guardian.ingest.scanner import scan_project
from rpa_code_guardian.model.summaries import Finding, NarrativeSections, WorkflowSummary
from rpa_code_guardian.render.document import render_documentation
from rpa_code_guardian.render.lint import lint_markdown, md_anchor, normalize_prose


def test_lint_strips_emojis_and_extra_blanks():
    dirty = "# Title \U0001f680\n\n\n\nBody ✅ done \U0001f600\n"
    clean = lint_markdown(dirty)
    assert "\U0001f680" not in clean and "✅" not in clean and "\U0001f600" not in clean
    assert "\n\n\n" not in clean


def test_normalize_prose_demotes_stray_headings():
    out = normalize_prose("## Overview\nThe process does things.\n### Detail\nMore.")
    lines = out.split("\n")
    assert "#### Overview" in lines  # level-2 heading pushed below the document's ###
    assert "##### Detail" in lines  # relative depth preserved (was one level deeper)
    assert not any(line.startswith("## ") or line.startswith("### ") for line in lines)


def test_normalize_prose_separates_glued_list_and_heading():
    out = normalize_prose("Introduction paragraph:\n- first\n- second\nClosing paragraph.")
    lines = out.split("\n")
    assert "" in lines[lines.index("Introduction paragraph:") + 1 : lines.index("- first")] \
        or lines[lines.index("- first") - 1] == ""  # blank line before the list
    assert lines[lines.index("- second") + 1] == ""  # blank line after the list


def test_normalize_prose_unwraps_markdown_fence_and_odd_bullets():
    out = normalize_prose("```markdown\nSome text.\n\n• one\n• two\n```")
    assert "```" not in out
    assert "- one" in out and "- two" in out and "•" not in out


def test_normalize_prose_leaves_code_fences_untouched():
    src = "Here is code:\n```python\n# not a heading\nx = 1\n```\nDone."
    out = normalize_prose(src)
    assert "# not a heading" in out  # comment inside the fence not demoted
    assert "```python" in out and "x = 1" in out


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
