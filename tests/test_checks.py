from rpa_code_guardian.checks import run_checks
from rpa_code_guardian.ingest.scanner import scan_project


def test_deterministic_findings(sample_project):
    inv = scan_project(sample_project)
    findings = run_checks(inv)
    text = {(f.location, f.category, f.severity): f.description for f in findings}

    assert any(loc == "Framework/SetTransactionStatus.xaml" and sev == "High"
               for loc, _, sev in text), "empty catch must be a High finding"
    assert any("Delay" in d for d in text.values())
    assert any(loc == "Business/Unused.xaml" for loc, _, _ in text), "orphan workflow flagged"
    assert any("InvoiceFolder" in d for d in text.values()), "missing Config key flagged"
    assert any("ReportPath" in d for d in text.values()), "unused Config key flagged"
    # findings are sorted by severity
    severities = [f.severity for f in findings]
    assert severities == sorted(severities, key={"High": 0, "Medium": 1, "Low": 2}.get)
