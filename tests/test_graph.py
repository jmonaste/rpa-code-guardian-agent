"""End-to-end pipeline test with the fake LLM over the fixture project."""

from rpa_code_guardian.graph.builder import build_graph


def _run(sample_project, settings, fake_llm, pdd_text=""):
    graph = build_graph(settings, llm=fake_llm)
    return graph.invoke(
        {"project_root": str(sample_project), "pdd_text": pdd_text},
        config={"recursion_limit": 200, "max_concurrency": settings.max_concurrency},
    )


def test_full_pipeline_without_pdd(sample_project, settings, fake_llm):
    state = _run(sample_project, settings, fake_llm)

    # Every non-test workflow was summarized, bottom-up.
    assert set(state["summaries"]) == set(state["inventory"].workflows)
    # The gap-fill agent ran (the fake narrative asks one open question)...
    assert len(state["gap_answers"]) == 1
    answer = state["gap_answers"][0]
    # ...and its evidence was recorded deterministically from actual tool reads.
    assert answer.evidence == ["Framework/GetTransactionData.xaml"]

    md = state["documentation_md"]
    assert md.startswith("# ACME-InvoiceProcessing — Technical Documentation")
    assert "## Improvement suggestions" in md
    assert "Selector relies on a fixed page title." in md  # LLM smell merged into findings
    assert "\U0001f600" not in md  # emoji from the fake narrative was linted out
    assert "compliance_md" not in state or not state.get("compliance_md")


def test_full_pipeline_with_pdd(sample_project, settings, fake_llm, sample_pdd_text):
    state = _run(sample_project, settings, fake_llm, pdd_text=sample_pdd_text)

    assert [r.id for r in state["requirements"]] == ["R-01", "R-02"]
    compliance = state["compliance"]
    assert compliance["R-01"].verdict == "Compliant"
    # R-02 was 'Not verifiable', went through the evidence rescue pass and
    # came back as a justified Non-compliant with recorded evidence.
    assert compliance["R-02"].verdict == "Non-compliant"
    assert compliance["R-02"].evidence == ["Framework/GetTransactionData.xaml"]

    md = state["compliance_md"]
    assert md.startswith("# ACME-InvoiceProcessing — PDD Compliance Report")
    assert "Requirements traceability matrix" in md
    assert "| R-01 |" in md and "| R-02 |" in md
    assert "## Gaps and deviations" in md
    assert "summary email is not implemented" in md
    assert "1 of 2 extracted requirements are fully compliant" in md


def test_summary_cache_reused_across_runs(sample_project, settings, fake_llm, tmp_path):
    import shutil

    project = tmp_path / "proj"
    shutil.copytree(sample_project, project)
    settings = settings.model_copy(update={"use_cache": True})

    _run(project, settings, fake_llm)
    first_calls = fake_llm.structured_calls.count("WorkflowSummary")
    assert first_calls == 7

    _run(project, settings, fake_llm)
    second_calls = fake_llm.structured_calls.count("WorkflowSummary")
    assert second_calls == first_calls  # all summaries came from the disk cache
    assert (project / ".guardian_cache" / "summaries.json").exists()
