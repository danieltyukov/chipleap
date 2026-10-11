"""Lint of RTL projects with Verilator and slang."""

import pytest

from chipleap.rtl import lint as lint_mod
from chipleap.rtl.lint import lint, parse_slang_json
from chipleap.rtl.project import load_project
from chipleap.verify.tools import Tool, slang, verilator

pytestmark = pytest.mark.skipif(not verilator().found, reason="Verilator not installed")

NO_SLANG = Tool("slang", None, "", "")


def test_clean_project_passes(counter_project, monkeypatch):
    monkeypatch.setattr(lint_mod, "slang", lambda: NO_SLANG)
    r = lint(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert r.summary == "Verilator -Wall reports no warnings; slang is not installed"
    assert r.data["diagnostics"] == [] and r.data["tools"]["slang"] == "not installed"


def test_warnings_fail_with_locations(counter_project, monkeypatch):
    monkeypatch.setattr(lint_mod, "slang", lambda: NO_SLANG)
    src = counter_project / "rtl/counter.sv"
    src.write_text(
        src.read_text()
        .replace("q_o + W'(1)", "q_o + 8'd1")
        .replace("endmodule", "  logic spare;\nendmodule")
    )
    r = lint(load_project(counter_project))
    assert r.status == "fail"
    codes = {d["code"]: d for d in r.data["diagnostics"]}
    assert {"WIDTHEXPAND", "UNUSEDSIGNAL"} <= set(codes)
    assert codes["WIDTHEXPAND"]["file"] == "rtl/counter.sv"
    assert codes["WIDTHEXPAND"]["line"] == 11
    assert r.summary.startswith("0 errors and ") and "rtl/counter.sv:" in r.summary


def test_waivers_and_errors(counter_project, monkeypatch):
    monkeypatch.setattr(lint_mod, "slang", lambda: NO_SLANG)
    src = counter_project / "rtl/counter.sv"
    src.write_text(src.read_text().replace("q_o + W'(1)", "q_o + 8'd1"))
    (counter_project / "rtl/waivers.vlt").write_text(
        "`verilator_config\n"
        'lint_off -rule WIDTHEXPAND -file "*counter.sv"\n'
        'lint_off -rule WIDTHTRUNC -file "*counter.sv"\n'
    )
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "lint: {waivers: [rtl/waivers.vlt]}\n")
    assert lint(load_project(counter_project)).status == "pass"
    src.write_text(src.read_text().replace("q_o <= '0;", "q_o <= '0"))
    r = lint(load_project(counter_project))
    assert r.status == "fail" and r.data["errors"] >= 1
    first = r.data["diagnostics"][0]
    assert first["severity"] == "error" and first["file"] == "rtl/counter.sv"


@pytest.mark.skipif(not slang().found, reason="slang not installed")
def test_slang_reports_what_verilator_does_not_see(counter_project):
    src = counter_project / "rtl/counter.sv"
    src.write_text(
        src.read_text().replace(
            "endmodule",
            "  logic [3:0] narrow;\n  assign narrow = q_o;\nendmodule",
        )
    )
    r = lint(load_project(counter_project))
    assert r.status == "fail"
    tools = {d["tool"] for d in r.data["diagnostics"]}
    assert "slang" in tools and r.data["tools"]["slang"]
    width = [d for d in r.data["diagnostics"] if d.get("code") == "width-trunc"]
    assert width and width[0]["file"] == "rtl/counter.sv" and width[0]["line"] == 14


def test_slang_json():
    text = """[{"severity": "warning", "message": "implicit conversion truncates",
    "optionName": "width-trunc", "location": "rtl/w.sv:3:14"},
    {"severity": "note", "message": "declared here", "location": "rtl/w.sv:1:8"},
    {"severity": "error", "message": "unknown module 'x'", "location": "rtl/t.sv:2:3"}]"""
    d = parse_slang_json(text)
    assert [(x.severity, x.code, x.file, x.line, x.column) for x in d] == [
        ("warning", "width-trunc", "rtl/w.sv", 3, 14),
        ("error", "", "rtl/t.sv", 2, 3),
    ]
