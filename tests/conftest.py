import os
import tempfile
import textwrap
from pathlib import Path

import pytest

# Technologies added with `chipleap tech add` live in the user's config directory and
# would change exploration results; the tests run on the built-in ones only.
os.environ["CHIPLEAP_TECH_DIR"] = ""
# The key that authenticates ChipLeap's manifests of build results: a fresh one for the
# test run, never the user's.
os.environ["CHIPLEAP_BUILD_KEY"] = str(Path(tempfile.mkdtemp(prefix="chipleap-key-")) / "build.key")


# A small RTL project that lints clean on Verilator -Wall and slang -Wextra, with cocotb
# tests that pass. Tests copy it into a temporary directory and break it as they need.
COUNTER_SV = """\
module counter #(
  parameter int unsigned W = 4
) (
  input  logic         clk_i,
  input  logic         rst_ni,
  input  logic         en_i,
  output logic [W-1:0] q_o
);
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) q_o <= '0;
    else if (en_i) q_o <= q_o + W'(1);
  end
endmodule
"""

COUNTER_TEST = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

import counter_model


@cocotb.test()
async def counts_when_enabled(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())
    dut.rst_ni.value = 0
    dut.en_i.value = 0
    await ClockCycles(dut.clk_i, 2)
    dut.rst_ni.value = 1
    dut.en_i.value = 1
    await ClockCycles(dut.clk_i, 5)
    await FallingEdge(dut.clk_i)
    assert int(dut.q_o.value) == counter_model.after(5, int(dut.W.value))


@cocotb.test()
async def holds_when_disabled(dut):
    cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())
    dut.rst_ni.value = 0
    dut.en_i.value = 0
    await ClockCycles(dut.clk_i, 2)
    dut.rst_ni.value = 1
    await ClockCycles(dut.clk_i, 3)
    await FallingEdge(dut.clk_i)
    assert int(dut.q_o.value) == 0
"""

COUNTER_MODEL = """\
def after(cycles, width):
    return cycles % (1 << width)
"""

COUNTER_PROJECT = """\
schema: project/v1
name: counter
top: counter
sources: [rtl/counter.sv]
parameters: {W: 6}
clocks: [{port: clk_i, period_ns: 10}]
resets: [{port: rst_ni, active: low}]
tests: {dir: test, seed: 3}
golden: model/counter_model.py
"""


@pytest.fixture
def counter_project(tmp_path: Path) -> Path:
    root = tmp_path / "counter"
    for rel, text in {
        "rtl/counter.sv": COUNTER_SV,
        "test/test_counter.py": COUNTER_TEST,
        "model/counter_model.py": COUNTER_MODEL,
        "project.yaml": COUNTER_PROJECT,
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    return root


IHP_VIEWS = (
    "lef/sg13g2_tech.lef",
    "lef/sg13g2_stdcell.lef",
    "lib/sg13g2_stdcell_typ_1p20V_25C.lib",
    "lib/sg13g2_stdcell_slow_1p08V_125C.lib",
    "verilog/sg13g2_stdcell.v",
    "verilog/sg13g2_udp.v",
)


@pytest.fixture
def ihp_pdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in IHP SG13G2 standard-cell library under PDK_ROOT, as the implementation
    steps find it; returns its libs.ref/sg13g2_stdcell directory."""
    from chipleap.rtl import openroad

    root = tmp_path / "pdk"
    sc = root / "ihp-sg13g2" / "libs.ref" / "sg13g2_stdcell"
    for rel in IHP_VIEWS:
        (sc / rel).parent.mkdir(parents=True, exist_ok=True)
        (sc / rel).write_text("x\n")
    monkeypatch.setenv("PDK_ROOT", str(root))
    monkeypatch.delenv("CHIPLEAP_LIBERTY", raising=False)
    lib = sc / "lib/sg13g2_stdcell_typ_1p20V_25C.lib"
    monkeypatch.setattr(openroad, "ihp_liberty", lambda: lib)
    return sc
