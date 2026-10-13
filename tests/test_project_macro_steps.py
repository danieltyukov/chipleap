"""The steps after synthesis on a project with hard macros: each treats a macro as a box
with its ports, and a check that would need what the macro does says so or fails."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from chipleap.rtl import formal
from chipleap.rtl.project import load_project
from chipleap.rtl.synth import liberty_for
from chipleap.verify.tools import sby, yosys, z3

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "sram"
CELL = "RM_IHPSG13_1P_256x32_c2_bm_bist"

needs_yosys = pytest.mark.skipif(not yosys().found, reason="needs Yosys")
needs_sby = pytest.mark.skipif(
    not sby().found or not z3().found, reason="needs SymbiYosys (CHIPLEAP_SBY) and z3"
)


def sram_project(root: Path, rtl_extra: str = "", **changes: object) -> Path:
    """The SRAM example in ``root``, with ``rtl_extra`` before its ``endmodule``."""
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("build", "__pycache__"))
    rtl = root / "rtl" / "scratchpad.sv"
    text = rtl.read_text()
    head, _, tail = text.rpartition("endmodule")
    rtl.write_text(head + rtl_extra + "endmodule" + tail)
    f = root / "project.yaml"
    data = yaml.safe_load(f.read_text())
    data.update(changes)
    f.write_text(yaml.safe_dump(data))
    return root


# Formal: each macro is a cut point.

PROPERTIES = """
`ifdef FORMAL
  logic past_valid = 1'b0;
  always_ff @(posedge clk_i) past_valid <= 1'b1;
  always_comb if (!past_valid) assume (!rst_ni);
  // The logic around the macro: rvalid_o follows a read request.
  always_ff @(posedge clk_i)
    if (past_valid && $past(rst_ni) && rst_ni) assert (rvalid_o == $past(req_i && !we_i));
  // What the macro does: its output holds between reads. True of IHP's SRAM, unknown of
  // a box.
  always_ff @(posedge clk_i)
    if (past_valid && !$past(req_i)) assert ($stable(rdata_o));
`endif
"""


@needs_sby
def test_formal_proves_the_logic_around_a_macro_and_leaves_the_macro_free(tmp_path):
    root = sram_project(
        tmp_path / "sram",
        PROPERTIES,
        formal={"checks": [{"name": "bmc", "mode": "bmc", "depth": 6}]},
    )
    p = load_project(root)
    boxes, cuts = formal.macro_cuts(p)
    assert [b.name for b in boxes] == [f"{CELL}.v"] and cuts == [f"cutpoint t:{CELL}"]
    r = formal.formal(p)
    assert r.status == "fail", r.summary
    (check,) = r.data["checks"]
    failures = check["failures"]
    # Only the property about the macro's output fails: the macro is a cut point, and
    # rdata_o changes without a read in the counterexample.
    assert len(failures) == 1 and failures[0]["line"] == text_line(root, "$stable(rdata_o)")
    values = failures[0]["values"]["signals"]
    assert "rdata_o" in values


def text_line(root: Path, needle: str) -> int:
    lines = (root / "rtl" / "scratchpad.sv").read_text().splitlines()
    return next(i for i, line in enumerate(lines, 1) if needle in line)


# CDC: a macro is a state element of the clock its port samples on.

TWO_CLOCKS = [{"port": "clk_a", "period_ns": 10}, {"port": "clk_b", "period_ns": 7}]
CROSSING = f"""
module top (
    input  logic       clk_a,
    input  logic       clk_b,
    input  logic       rst_ni,
    input  logic [7:0] addr_i,
    output logic [31:0] q_o,
    output logic       bit_o
);
  logic [7:0] addr_q;
  logic [31:0] dout;
  logic s1, s2;
  always_ff @(posedge clk_a or negedge rst_ni)
    if (!rst_ni) addr_q <= '0; else addr_q <= addr_i;
  {CELL} u_mem (
      .A_CLK(clk_b), .A_MEN(1'b1), .A_WEN(1'b0), .A_REN(1'b1), .A_ADDR(addr_q),
      .A_DIN(32'd0), .A_DLY(1'b1), .A_DOUT(dout), .A_BM(32'd0), .A_BIST_CLK(1'b0),
      .A_BIST_EN(1'b0), .A_BIST_MEN(1'b0), .A_BIST_WEN(1'b0), .A_BIST_REN(1'b0),
      .A_BIST_ADDR(8'd0), .A_BIST_DIN(32'd0), .A_BIST_BM(32'd0)
  );
  // The word straight into clk_a, and one bit of it through two flip-flops.
  always_ff @(posedge clk_a) q_o <= dout;
  always_ff @(posedge clk_a) begin
    s1 <= dout[0];
    s2 <= s1;
  end
  assign bit_o = s2;
endmodule
"""


def crossing_project(root: Path) -> Path:
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "top.sv").write_text(CROSSING)
    data = {
        "top": "top",
        "sources": ["rtl/top.sv"],
        "clocks": TWO_CLOCKS,
        "resets": [{"port": "rst_ni"}],
        "macros": [{"instance": "u_mem", "cell": CELL}],
    }
    (root / "project.yaml").write_text(yaml.safe_dump(data))
    return root


def test_an_sram_samples_its_port_on_its_clock_and_bist_on_its_own():
    from chipleap.macro.sram import Sram
    from chipleap.rtl import cdc, macros

    p = load_project(EXAMPLE)
    (box,) = cdc.macro_boxes(p).values()
    assert box.outputs == ("A_DOUT",) and box.outputs_on == "A_CLK"
    assert "A_ADDR" in box.sampled["A_CLK"] and "A_BIST_ADDR" not in box.sampled["A_CLK"]
    assert box.sampled["A_BIST_CLK"][0] == "A_BIST_EN"
    assert macros.cells(p) == [Sram(CELL, 256, 32, 0.0, 0.0, True)]


@needs_yosys
def test_cdc_takes_the_sram_into_its_clock_and_finds_crossings_through_it(tmp_path):
    from chipleap.rtl import cdc

    r = cdc.cdc(load_project(crossing_project(tmp_path / "x")))
    assert r.status == "fail"
    assert r.data["macros"] == {"u_mem": "clk_b"}
    errors = {
        (f["rule"], f["from"], f["to"]) for f in r.data["findings"] if f["severity"] == "error"
    }
    # Into the macro: clk_a's address sampled on clk_b. Out of it: the word into clk_a.
    assert errors == {("unsynchronized", "addr_q", "u_mem"), ("unsynchronized", "u_mem", "q_o")}
    # One bit of the word through two clk_a flip-flops is a synchronizer.
    assert any(a["rule"] == "synchronizer" and a["from"] == "u_mem" for a in r.data["accepted"])


@needs_yosys
def test_the_sram_example_has_one_domain_with_its_macro(tmp_path):
    from chipleap.rtl import cdc

    root = sram_project(tmp_path / "sram")
    r = cdc.cdc(load_project(root))
    assert r.status == "pass", r.summary
    assert r.data["macros"] == {"u_mem": "clk_i"}


COMPARATOR = """\
module top (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic inp_i,
    input  logic inm_i,
    output logic outp_o,
    output logic outn_o
);
  logic outp;
  strongarm u_cmp (.clk(clk_i), .inp(inp_i), .inm(inm_i), .outp(outp), .outn(outn_o));
  always_ff @(posedge clk_i) outp_o <= outp;
endmodule
"""
STRONGARM = ROOT / "examples" / "uart" / "macros" / "strongarm.macro"


def comparator_project(root: Path, clocked: bool) -> Path:
    shutil.copytree(STRONGARM, root / "macros" / "strongarm.macro")
    if not clocked:  # the same block, its clock pin a plain signal
        toml = root / "macros" / "strongarm.macro" / "macro.toml"
        text = toml.read_text()
        toml.write_text(text.replace('kind = "clock"', 'kind = "signal"', 1))
    (root / "rtl").mkdir()
    (root / "rtl" / "top.sv").write_text(COMPARATOR)
    data = {
        "top": "top",
        "sources": ["rtl/top.sv"],
        "clocks": [{"port": "clk_i", "period_ns": 10}],
        "resets": [{"port": "rst_ni"}],
        "macros": [{"instance": "u_cmp", "package": "macros/strongarm.macro"}],
    }
    (root / "project.yaml").write_text(yaml.safe_dump(data))
    return root


@needs_yosys
def test_a_clocked_package_is_in_its_clocks_domain_and_an_unclocked_one_is_asynchronous(
    tmp_path,
):
    from chipleap.rtl import cdc

    p = load_project(comparator_project(tmp_path / "clocked", clocked=True))
    (box,) = cdc.macro_boxes(p).values()
    assert box.outputs_on == "clk" and box.sampled == {"clk": ()}  # its inputs are analog
    r = cdc.cdc(p)
    assert r.status == "pass", r.summary
    assert r.data["macros"] == {"u_cmp": "clk_i"}
    # Without a clock its outputs change on their own: sampling one once is an error.
    r = cdc.cdc(load_project(comparator_project(tmp_path / "free", clocked=False)))
    assert r.status == "fail"
    assert r.data["macros"] == {"u_cmp": "async:u_cmp"}
    errors = [
        (f["rule"], f["from"], f["to"]) for f in r.data["findings"] if f["severity"] == "error"
    ]
    assert errors == [("unsynchronized", "u_cmp", "outp_o")]


# Equivalence: a macro's pins are ports on both sides.

needs_liberty = pytest.mark.skipif(
    not yosys().found or liberty_for("ihp-sg13g2") is None,
    reason="needs Yosys and IHP SG13G2's liberty",
)


@needs_liberty
@pytest.mark.parametrize("engine", ["yosys", "eqy"])
def test_equivalence_compares_what_goes_into_a_macro_and_shares_what_comes_out(tmp_path, engine):
    from chipleap.rtl import equiv
    from chipleap.rtl.synth import synthesize
    from chipleap.verify.tools import eqy

    if engine == "eqy" and not eqy().found:
        pytest.skip("needs EQY (CHIPLEAP_EQY)")
    section = {"engine": engine, "checks": ["rtl-synth"]}
    p = load_project(sram_project(tmp_path / "sram", equivalence=section))
    r = equiv.check_pair(p, "rtl-synth")
    assert r["status"] == "proved", r
    # The macro's pins are matched points: its write enable, its address and its word.
    work = p.root / "build" / "equiv" / "rtl-synth"
    script = (work / ("rtl-synth.eqy" if engine == "eqy" else "equiv.ys")).read_text()
    assert f"expose -evert -sep __ t:{CELL}" in script
    # A netlist of an RTL that writes when it should read: the write enable into the
    # macro differs. With the macro left as a box on both sides this proved equal.
    bad = sram_project(tmp_path / "bad")
    rtl = bad / "rtl" / "scratchpad.sv"
    rtl.write_text(rtl.read_text().replace(".A_WEN(req_i & we_i)", ".A_WEN(req_i & ~we_i)"))
    netlist = synthesize(load_project(bad)).netlist
    gate = p.root / "gate.v"
    gate.write_bytes(netlist.read_bytes())
    r = equiv.check_pair(p, "rtl-synth", gate_netlist=gate)
    assert r["status"] == "differs", r
    assert any("u_mem__A_WEN" in n for n in r["unproven"]), r["unproven"]


# Gate-level simulation: the SRAM on the PDK's model.


def _gls_ready() -> bool:
    from chipleap.rtl.gls import icarus_major
    from chipleap.rtl.sim import cocotb_python
    from chipleap.verify.tools import iverilog

    return (
        icarus_major(iverilog()) >= 13
        and cocotb_python() is not None
        and liberty_for("ihp-sg13g2") is not None
    )


@pytest.mark.skipif(not _gls_ready(), reason="needs Icarus 13 (CHIPLEAP_IVERILOG), cocotb, the PDK")
def test_the_tests_pass_on_the_netlist_with_the_srams_functional_model(tmp_path):
    from chipleap.rtl.gls import gls

    root = sram_project(tmp_path / "sram", gate_sim={"netlist": "synth"})
    r = gls(load_project(root))
    assert r.status == "pass", r.summary
    assert r.data["macro_models"] == "functional"
    m = (root / "build" / "gls" / "sim" / "manifest.json").read_text()
    assert '"FUNCTIONAL": 1' in m and f"macros/{CELL}.v" in m
