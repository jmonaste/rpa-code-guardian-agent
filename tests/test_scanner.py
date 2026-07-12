from rpa_code_guardian.ingest.scanner import scan_project


def test_inventory_census(sample_project):
    inv = scan_project(sample_project)
    assert inv.meta.name == "ACME-InvoiceProcessing"
    assert len(inv.workflows) == 7
    assert inv.is_reframework
    assert inv.config_file == "Data/Config.xlsx"
    assert inv.meta.dependencies["UiPath.System.Activities"] == "25.10.2"


def test_config_parsed_and_secrets_redacted(sample_project):
    inv = scan_project(sample_project)
    by_name = {e.name: e for e in inv.config_entries}
    assert by_name["OrchestratorQueueName"].value == "ACME_Invoices"
    assert by_name["ApplicationPassword"].value == "(redacted)"
    assert "SuperSecret123" not in inv.config_context()
    assert by_name["InvoiceSystemCredential"].sheet == "Assets"


def test_call_graph_edges_and_orphans(sample_project):
    inv = scan_project(sample_project)
    g = inv.call_graph
    assert g.entry == "Main.xaml"
    assert "Process.xaml" in g.edges["Main.xaml"]
    assert g.edges["Process.xaml"] == ["Business/ExtractInvoice.xaml"]
    assert g.orphans == ["Business/Unused.xaml"]


def test_bottom_up_order_puts_callees_first(sample_project):
    inv = scan_project(sample_project)
    waves = inv.call_graph.bottom_up_order(list(inv.workflows))
    position = {p: n for n, wave in enumerate(waves) for p in wave}
    for caller, callees in inv.call_graph.edges.items():
        for callee in callees:
            assert position[callee] < position[caller]


def test_log_digest(sample_project):
    inv = scan_project(sample_project)
    d = inv.log_digest
    assert d.files == ["logs/execution.log"]
    assert d.level_counts["ERROR"] == 1
    assert d.level_counts["INFO"] == 7
    assert any("Selector not found" in s for s in d.error_samples)
    assert d.first_timestamp.startswith("2026-06-30T08:00")
