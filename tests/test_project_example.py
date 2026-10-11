"""The example RTL project, examples/uart, through the steps CI can run."""

import shutil
from pathlib import Path

import pytest

from chipleap.rtl.project import load_project
from chipleap.rtl.sim import cocotb_python
from chipleap.rtl.synth import liberty_for
from chipleap.rtl.verify import verify_project
from chipleap.verify.tools import iverilog, verilator, yosys

UART = Path(__file__).resolve().parents[1] / "examples" / "uart"


def test_example_project_reads():
    p = load_project(UART)
    assert p.top == "uart" and p.parameters == {"CLKS_PER_BIT": 16}
    assert [p.rel(s) for s in p.sources] == ["rtl/uart_tx.sv", "rtl/uart_rx.sv", "rtl/uart.sv"]
    assert p.tests.modules == ("test_uart",) and p.golden is not None


@pytest.mark.skipif(
    cocotb_python() is None
    or not iverilog().found
    or not verilator().found
    or not yosys().found
    or liberty_for("ihp-sg13g2") is None,
    reason="needs cocotb, Icarus, Verilator, Yosys and the IHP liberty",
)
def test_example_lints_simulates_and_synthesizes(tmp_path):
    root = tmp_path / "uart"
    shutil.copytree(UART, root, ignore=shutil.ignore_patterns("build", "__pycache__"))
    results = {r.name: r for r in verify_project(root, steps="lint,sim,synth", sim_tool="icarus")}
    assert results["lint"].status == "pass", results["lint"].summary
    assert results["sim"].status == "pass", results["sim"].summary
    assert results["sim"].data["passed"] == 5
    synth = results["synth"]
    assert synth.status == "pass" and synth.data["flops"] > 40
    assert (root / "build/verify/report.html").is_file()
