"""The project commands and `chipleap verify` on an RTL project."""

import json
from pathlib import Path

import pytest

from chipleap.cli import main
from chipleap.rtl.sim import cocotb_python
from chipleap.rtl.verify import DEFAULT_STEPS, PROJECT_STEPS, steps_from
from chipleap.verify.tools import iverilog, sby, verilator, yosys, z3

needs_tools = pytest.mark.skipif(
    cocotb_python() is None or not iverilog().found or not verilator().found,
    reason="needs cocotb, Icarus and Verilator",
)


def test_project_init_and_check(counter_project, capsys):
    (counter_project / "project.yaml").unlink()
    assert main(["project", "init", str(counter_project), "--clock", "clk_i:20"]) == 0
    out = capsys.readouterr().out
    assert "Wrote" in out and "top: counter" in out and "tests: cocotb tests in test/" in out
    assert main(["project", "check", str(counter_project), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["top"] == "counter" and d["clocks"] == [{"port": "clk_i", "period_ns": 20.0}]
    assert d["resets"][0]["port"] == "rst_ni" and d["resets"][0]["active"] == "low"
    assert main(["project", "init", str(counter_project)]) == 2  # exists
    assert "exists; use --force" in capsys.readouterr().err
    assert main(["project", "check", str(counter_project / "missing")]) == 2


def test_steps():
    assert steps_from("") == DEFAULT_STEPS and steps_from("all") == PROJECT_STEPS
    assert steps_from("sim, lint") == ("sim", "lint")
    with pytest.raises(ValueError, match="unknown step 'xheep' for an RTL project"):
        steps_from("lint,xheep")


@needs_tools
def test_verify_project(counter_project, capsys):
    code = main(["verify", str(counter_project), "--steps", "lint,sim", "--sim-tool", "icarus"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.splitlines()[0].startswith("lint     PASS")
    assert "sim      PASS     Icarus" in out
    v = counter_project / "build/verify"
    data = json.loads((v / "verify.json").read_text())
    assert [s["step"] for s in data["steps"]] == ["lint", "sim"]
    assert data["project"]["top"] == "counter"
    html = (v / "report.html").read_text()
    assert "counts_when_enabled" in html and "<h2>Simulation</h2>" in html
    assert "test_counter.holds_when_disabled" in (v / "VERIFY.md").read_text()


@needs_tools
def test_verify_reports_the_failing_test(counter_project, capsys):
    t = counter_project / "test/test_counter.py"
    t.write_text(
        t.read_text().replace("== counter_model.after(5,", "== 1 + counter_model.after(5,")
    )
    code = main(["verify", str(counter_project / "project.yaml"), "--steps", "sim"])
    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out and "test_counter.counts_when_enabled" in out
    md = (counter_project / "build/verify/VERIFY.md").read_text()
    assert "| test_counter.counts_when_enabled | fail |" in md


FIFO = Path(__file__).parent / "fixtures/formal/fifo.sv"


@pytest.mark.skipif(
    not (sby().found and z3().found and yosys().found), reason="needs SymbiYosys, z3 and Yosys"
)
def test_verify_reports_proofs_and_crossings(tmp_path, capsys):
    root = tmp_path / "fifo"
    (root / "rtl").mkdir(parents=True)
    # The read pointer moves on a pop of an empty FIFO: pointers_agree fails.
    text = FIFO.read_text().replace("if (do_pop) rptr", "if (pop_i) rptr")
    (root / "rtl/fifo.sv").write_text(text)
    (root / "project.yaml").write_text(
        "top: fifo\nsources: [rtl/fifo.sv]\nclocks: [{port: clk_i, period_ns: 10}]\n"
        "resets: [{port: rst_ni}]\nformal: {checks: [{name: prove, depth: 8}]}\n"
        "cdc: {async_inputs: [push_i]}\n"
    )
    code = main(["verify", str(root), "--steps", "formal,cdc"])
    out = capsys.readouterr().out
    assert code == 1, out
    assert out.splitlines()[0].startswith("formal   FAIL     prove (prove depth 8): fifo.")
    assert "cdc      FAIL" in out
    md = (root / "build/verify/VERIFY.md").read_text()
    assert "## Formal properties" in md and "| prove | prove | 8 | fail |" in md
    assert "| prove | fifo.pointers_agree | rtl/fifo.sv:" in md
    assert "## Clock and reset domain crossings" in md
    assert "| unsynchronized | error | push_i (async:push_i) | count (clk_i) |" in md
    html = (root / "build/verify/report.html").read_text()
    assert "fifo.pointers_agree" in html and "unsynchronized" in html


def test_verify_errors(counter_project, capsys, tmp_path):
    assert main(["verify", str(counter_project), "--steps", "consistency"]) == 2
    assert "unknown step 'consistency'" in capsys.readouterr().err
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "design.yaml").write_text("design: {name: x}\n")
    assert main(["verify", str(bundle), "--sim-tool", "icarus"]) == 2
    assert "accelerator's testbench runs on verilator" in capsys.readouterr().err
