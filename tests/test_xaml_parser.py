from pathlib import Path

from rpa_code_guardian.ingest.xaml_parser import parse_xaml

FIXTURE = Path(__file__).parent / "fixtures" / "sample-project"


def test_main_is_state_machine_with_reframework_states():
    ir = parse_xaml(FIXTURE / "Main.xaml", "Main.xaml")
    assert ir.root_type == "StateMachine"
    assert {"Initialization", "Get Transaction Data", "Process Transaction", "End Process"} <= set(ir.states)
    assert ir.annotation.startswith("REFramework")
    assert not ir.parse_error


def test_main_invocations_and_arguments():
    ir = parse_xaml(FIXTURE / "Main.xaml", "Main.xaml")
    targets = {i.target for i in ir.invocations}
    assert "Framework/InitAllSettings.xaml" in targets
    assert "Framework/GetTransactionData.xaml" in targets
    assert "Process.xaml" in targets
    init = next(i for i in ir.invocations if i.target.endswith("InitAllSettings.xaml"))
    assert init.arguments.get("out_Config") == "[Config]"
    assert [a.name for a in ir.arguments] == ["in_OrchestratorQueueName"]
    assert ir.arguments[0].direction == "in"
    assert ir.arguments[0].type == "String"


def test_extract_invoice_signal_extraction():
    ir = parse_xaml(FIXTURE / "Business" / "ExtractInvoice.xaml", "Business/ExtractInvoice.xaml")
    assert {a.name: a.direction for a in ir.arguments} == {
        "in_Config": "in", "in_InvoiceId": "in", "out_InvoiceNumber": "out",
    }
    assert any(v.name == "InvoicePath" for v in ir.variables)
    assert "InvoiceSystemUrl" in ir.config_keys_used
    assert "InvoiceFolder" in ir.config_keys_used
    assert any("chrome.exe" in app for app in ir.selector_apps)
    assert "00:00:05" in ir.hardcoded_delays
    assert any(p.startswith("C:\\Temp\\invoices") for p in ir.hardcoded_paths)
    assert any("extracted" in m.message for m in ir.log_messages)
    assert ir.raw_chars > len(ir.to_context())  # compression actually happened


def test_empty_catch_detected():
    ir = parse_xaml(FIXTURE / "Framework" / "SetTransactionStatus.xaml", "Framework/SetTransactionStatus.xaml")
    assert ir.try_catch_count == 1
    assert ir.empty_catches == 1
    assert "00:00:02" in ir.hardcoded_delays


def test_populated_catch_not_flagged():
    ir = parse_xaml(FIXTURE / "Main.xaml", "Main.xaml")
    assert ir.try_catch_count == 1
    assert ir.empty_catches == 0


def test_outline_skips_designer_noise():
    ir = parse_xaml(FIXTURE / "Main.xaml", "Main.xaml")
    assert "TextExpression" not in ir.outline
    assert "InArgument" not in ir.outline
    assert 'StateMachine "General Business Process"' in ir.outline


def test_parse_error_is_recorded_not_raised(tmp_path):
    bad = tmp_path / "Broken.xaml"
    bad.write_text("<Activity><unclosed>", encoding="utf-8")
    ir = parse_xaml(bad, "Broken.xaml")
    assert ir.parse_error
