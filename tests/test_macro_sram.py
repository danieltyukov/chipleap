"""IHP's single-port SRAM macros: the PDK's list, the bank built from them, and the views a
flow needs beside the macro layer's, on a stand-in PDK."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from chip_fakes import sram_1p
from chipleap.macro import sram
from chipleap.macro.layer import MacroPin
from chipleap.macro.sram import Sram, SramError

C256x64 = "RM_IHPSG13_1P_256x64_c2_bm_bist"


@pytest.fixture
def pdk(tmp_path: Path) -> Path:
    root = tmp_path / "ihp-sg13g2"
    sram_1p(root, 64, 64, (784.48, 64.36))
    sram_1p(root, 256, 64, (784.48, 118.78))
    sram_1p(root, 512, 64, (784.48, 191.34))
    sram_1p(root, 256, 32, (416.64, 118.78))
    sram_1p(root, 512, 32, (416.64, 191.34))
    sram_1p(root, 8192, 32, (1520.16, 618.3), bm=False, mux=4)
    return root


def test_the_catalog_is_every_1p_macro_with_all_its_views(pdk):
    found = {s.cell: s for s in sram.catalog(pdk)}
    assert len(found) == 6
    s = found[C256x64]
    assert (s.words, s.bits, s.width_um, s.height_um, s.bit_mask) == (
        256,
        64,
        784.48,
        118.78,
        True,
    )
    assert s.addr_bits == 8
    assert not found["RM_IHPSG13_1P_8192x32_c4"].bit_mask
    # Without the slow corner's liberty, a macro cannot be signed off: left out.
    ref = pdk / "libs.ref" / "sg13g2_sram"
    (ref / "lib" / f"{C256x64}_slow_1p08V_125C.lib").unlink()
    # Two ports, or a name ChipLeap does not read: left out.
    shutil.copy(ref / "lef" / f"{C256x64}.lef", ref / "lef" / "RM_IHPSG13_2P_64x32_c2.lef")
    shutil.copy(ref / "lef" / f"{C256x64}.lef", ref / "lef" / "RM_IHPSG13_1P_100x8_c2.lef")
    assert C256x64 not in {s.cell for s in sram.catalog(pdk)}
    assert len(sram.catalog(pdk)) == 5
    assert sram.catalog(pdk.parent / "none") == []


def test_a_bank_is_the_narrowest_fit_then_fewest_stacked_then_area(pdk):
    srams = sram.catalog(pdk)
    # 128 words of 64 bits: one 256-word macro over two of 64 words.
    b = sram.choose(srams, 128, 64)
    assert (b.macro.cell, b.across, b.deep, b.count) == (C256x64, 1, 1, 1)
    # 128 bits: two 64-bit macros side by side.
    b = sram.choose(srams, 256, 128)
    assert (b.macro.bits, b.across, b.deep) == (64, 2, 1)
    # Deeper than the deepest macro of the width: stacked.
    b = sram.choose(srams, 1024, 32)
    assert (b.macro.cell, b.across, b.deep) == ("RM_IHPSG13_1P_512x32_c2_bm_bist", 1, 2)
    assert b.area_um2 == pytest.approx(2 * 416.64 * 191.34)
    # Without the bit mask, the 8192-word macro holds it all.
    b = sram.choose(srams, 8192, 32, bit_mask=False)
    assert (b.macro.cell, b.deep) == ("RM_IHPSG13_1P_8192x32_c4", 1)
    # 16 bits: no 16-bit macro here, so a 32-bit one, half used.
    assert sram.choose(srams, 64, 16).macro.bits == 32
    with pytest.raises(SramError, match="holds nothing"):
        sram.choose(srams, 0, 32)
    with pytest.raises(SramError, match="no single-port macro"):
        sram.choose([s for s in srams if not s.bit_mask], 64, 32)
    assert sram.find(srams, C256x64).words == 256
    with pytest.raises(SramError, match="no single-port macro"):
        sram.find(srams, "RM_IHPSG13_1P_7x7")


def test_the_views_add_every_corner_and_the_black_box(pdk, tmp_path):
    work = tmp_path / "flow"
    work.mkdir()
    v = sram.views(pdk, C256x64, work)
    assert v.view.cell == C256x64 and not v.view.supply_hookup
    assert set(v.libs) == {"*_typ_*", "*_slow_*", "*_fast_*"}
    assert v.libs["*_slow_*"].name == f"{C256x64}_slow_1p08V_125C.lib"
    assert all(p.parent == v.view.lef.parent and p.is_file() for p in v.libs.values())
    stub = v.stub.read_text()
    assert stub.startswith("// Written by ChipLeap") and "(* blackbox *)" in stub
    assert f"module {C256x64} (A_CLK, A_MEN, A_WEN, A_REN, A_ADDR," in stub
    assert "  input [7:0] A_ADDR;" in stub and "  output [63:0] A_DOUT;" in stub
    assert "  input A_DLY;" in stub and "VDD" not in stub
    macros, cdls = sram.librelane_macros({C256x64: v}, [("abuf_s0_m0", C256x64, 20, 30, "N")], work)
    entry = macros[C256x64]
    assert entry["lib"]["*_slow_*"] == [f"dir::macros/{C256x64}/{C256x64}_slow_1p08V_125C.lib"]
    assert entry["vh"] == [f"dir::macros/{C256x64}/{C256x64}_bb.v"]
    assert entry["instances"] == {"abuf_s0_m0": {"location": [20, 30], "orientation": "N"}}
    assert cdls == [f"dir::macros/{C256x64}/{C256x64}.cdl"]
    with pytest.raises(SramError, match="not a single-port SRAM"):
        sram.views(pdk, "RM_IHPSG13_2P_64x32_c2", work)


def test_the_black_box_takes_only_whole_buses(pdk, tmp_path):
    work = tmp_path / "flow"
    work.mkdir()
    view = sram.views(pdk, C256x64, work).view
    gap = tuple(p for p in view.pins if p.name != "A_DIN[3]")
    with pytest.raises(SramError, match="not 0 to"):
        sram.blackbox(replace(view, pins=gap))
    odd = (*view.pins, MacroPin("A_X;", "input", "signal", ()))
    with pytest.raises(SramError, match="not a Verilog port"):
        sram.blackbox(replace(view, pins=odd))
    mixed = (*view.pins, MacroPin("A_DIN", "input", "signal", ()))
    with pytest.raises(SramError, match="one port"):
        sram.blackbox(replace(view, pins=mixed))


def test_the_ports_are_the_pdk_models():
    bm = [n for n, _, _ in sram.ports(Sram("x", 256, 32, 1.0, 1.0, True))]
    assert bm[:8] == ["A_CLK", "A_MEN", "A_WEN", "A_REN", "A_ADDR", "A_DIN", "A_DLY", "A_DOUT"]
    assert "A_BM" in bm and "A_BIST_BM" in bm
    assert len(sram.ports(Sram("x", 8192, 32, 1.0, 1.0, False))) == 8


BENCH = """\
module bench;
  reg clk = 0, men = 0, wen = 0, ren = 0;
  reg [5:0] addr = 0;
  reg [15:0] din = 0, bm = 0;
  wire [15:0] dout;
  integer cycle = 0;
  RM_IHPSG13_1P_64x16_c2_bm_bist ram (
    .A_CLK(clk), .A_MEN(men), .A_WEN(wen), .A_REN(ren), .A_ADDR(addr), .A_DIN(din),
    .A_DLY(1'b1), .A_DOUT(dout), .A_BM(bm), .A_BIST_CLK(1'b0), .A_BIST_EN(1'b0),
    .A_BIST_MEN(1'b0), .A_BIST_WEN(1'b0), .A_BIST_REN(1'b0), .A_BIST_ADDR(6'd0),
    .A_BIST_DIN(16'd0), .A_BIST_BM(16'd0));
  always #5 clk = ~clk;
  always @(negedge clk) begin
    cycle <= cycle + 1;
    men <= 1; wen <= 0; ren <= 0;
    case (cycle)
      0: begin wen <= 1; addr <= 5; din <= 16'hABCD; bm <= 16'hFFFF; end
      1: begin wen <= 1; addr <= 5; din <= 16'h1234; bm <= 16'h00FF; end
      2: begin ren <= 1; addr <= 5; end
      3: begin men <= 0; if (dout !== 16'hAB34) begin $display("FAIL read %h", dout); $stop; end end
      4: begin if (dout !== 16'hAB34) begin $display("FAIL hold %h", dout); $stop; end end
      5: begin wen <= 1; ren <= `BOTH; addr <= 6; din <= 16'h5555; bm <= 16'hFFFF; end
      6: begin $display("PASS"); $finish; end
      default: ;
    endcase
  end
endmodule
"""


def _simulate(tmp_path: Path, both: int) -> str:
    model = sram.model(Sram("RM_IHPSG13_1P_64x16_c2_bm_bist", 64, 16, 1.0, 1.0, True))
    (tmp_path / "model.v").write_text(model)
    (tmp_path / "bench.v").write_text(BENCH.replace("`BOTH", str(both)))
    exe = tmp_path / "sim"
    subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-o",
            str(exe),
            str(tmp_path / "bench.v"),
            str(tmp_path / "model.v"),
        ],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(["vvp", "-n", str(exe)], capture_output=True, text=True, timeout=60)
    return out.stdout


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="Icarus Verilog not installed")
def test_the_model_reads_a_cycle_late_masks_and_holds(tmp_path):
    assert "PASS" in _simulate(tmp_path, 0)
    out = _simulate(tmp_path, 1)
    assert "a read and a write in one cycle" in out and "PASS" not in out


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator not installed")
def test_the_model_lints_clean(tmp_path):
    for s in (
        Sram("RM_IHPSG13_1P_256x64_c2_bm_bist", 256, 64, 1, 1, True),
        Sram("RM_IHPSG13_1P_8192x32_c4", 8192, 32, 1, 1, False),
    ):
        (tmp_path / f"{s.cell}.v").write_text(sram.model(s))
        out = subprocess.run(
            [
                "verilator",
                "--lint-only",
                "-Wall",
                "--top-module",
                s.cell,
                str(tmp_path / f"{s.cell}.v"),
            ],
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0, out.stderr


def test_the_models_catalog_is_the_pdks():
    """The model prices buffers from IHP_1P without the PDK; it must be what the PDK has."""
    from chipleap.chip import io
    from chipleap.macro.sram import IHP_1P, catalog

    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("no IHP PDK")
    assert catalog(pdk) == list(IHP_1P)


def test_the_models_read_times_are_the_pdks():
    """The model caps a macro-backed build's clock with IHP_1P_READ_NS; each is the slow
    liberty's clock to output."""
    from chipleap.chip import io

    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("no IHP PDK")
    ref = pdk / "libs.ref" / sram.LIBRARY / "lib"
    assert set(sram.IHP_1P_READ_NS) == {s.cell for s in sram.IHP_1P}
    for cell, ns in sram.IHP_1P_READ_NS.items():
        assert sram.read_ns((ref / f"{cell}_slow_1p08V_125C.lib").read_text()) == ns, cell


def test_a_read_time_is_the_later_edge_of_the_clock_to_output_arc():
    lib = """
    bus(A_ADDR) { timing() { related_pin : "A_CLK"; cell_rise(t) { values ("9.9"); } } }
    bus(A_DOUT) {
      pin(A_DOUT[7:0]) { internal_power() { rise_power("scalar") { values (0); } } }
      timing() {
        related_pin : "A_CLK";
        cell_rise(SIG2SRAM_delay_template) { index_1("0.008"); values ("5.15,5.2", "5.3,5.4"); }
        cell_fall(SIG2SRAM_delay_template) { index_1("0.008"); values ("5.04,5.1"); }
      }
    }
    """
    assert sram.read_ns(lib) == 5.15
    with pytest.raises(SramError, match="A_DOUT bus"):
        sram.read_ns(lib.replace("A_DOUT", "Q"))
    with pytest.raises(SramError, match="A_CLK to A_DOUT"):
        sram.read_ns(lib.replace('related_pin : "A_CLK";\n        cell_rise', "cell_rise"))
