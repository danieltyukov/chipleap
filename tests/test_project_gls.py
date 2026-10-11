"""Gate-level simulation: the project's cocotb tests on its netlist, with SDF delays."""

import re
import shutil
from pathlib import Path

import pytest

from chipleap.rtl import gls
from chipleap.rtl.pnr import seal_routed
from chipleap.rtl.project import load_project
from chipleap.rtl.sim import cocotb_python
from chipleap.rtl.synth import liberty_for
from chipleap.verify.tools import Tool, iverilog, yosys

ROUTED = Path(__file__).parent / "fixtures/uart_routed"
EXAMPLE = Path(__file__).parents[1] / "examples/uart"
CORNER = "nom_typ_1p20V_25C"


def icarus13() -> bool:
    return gls.icarus_major(iverilog()) >= 13


needs_gls = pytest.mark.skipif(
    not icarus13() or cocotb_python() is None or liberty_for("ihp-sg13g2") is None,
    reason="needs Icarus 13 (CHIPLEAP_IVERILOG), cocotb and the IHP SG13G2 PDK",
)

NETLIST = """\
module helper (input a, output y);
  assign y = a;
endmodule
module uart (clk_i, rst_ni);
 input clk_i;
 input rst_ni;
 sg13g2_buf_1 _1_ (.A(clk_i), .X());
endmodule
"""


def test_the_sdf_is_annotated_in_the_top_module():
    out = gls.annotated(NETLIST, "uart", Path("/w/uart.sdf"))
    top = out[out.index("module uart") :]
    assert 'initial $sdf_annotate("/w/uart.sdf");\nendmodule' in top
    assert out.count("$sdf_annotate") == 1  # not in helper
    with pytest.raises(ValueError):
        gls.annotated(NETLIST, "uart", Path('/w/"x".sdf'))
    with pytest.raises(ValueError, match="no module uart_top"):
        gls.annotated(NETLIST, "uart_top", Path("/w/uart.sdf"))


def test_icarus_12_is_not_enough(counter_project, monkeypatch):
    monkeypatch.setattr(gls, "iverilog", lambda: Tool("iverilog", "/usr/bin/iverilog", "12.0", ""))
    r = gls.gls(load_project(counter_project))
    assert r.status == "skipped" and "Icarus 13" in r.summary
    assert "install_iverilog13.sh" in r.summary


def test_a_routed_netlist_needs_a_routed_run(counter_project, monkeypatch):
    monkeypatch.setattr(gls, "iverilog", lambda: Tool("iverilog", "/x/iverilog", "13.0", ""))
    r = gls.gls(load_project(counter_project))
    assert r.status == "skipped" and "--steps pnr" in r.summary


def uart(tmp_path: Path, netlist: str = "routed", *, routed: bool = True) -> Path:
    root = tmp_path / "uart"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("build"))
    text = (root / "project.yaml").read_text()
    (root / "project.yaml").write_text(text + f"gate_sim: {{netlist: {netlist}}}\n")
    if routed:
        final = root / "build/pnr/runs/chipleap/final"
        (final / "nl").mkdir(parents=True)
        (final / "sdf" / CORNER).mkdir(parents=True)
        shutil.copy(ROUTED / "uart.nl.v", final / "nl/uart.nl.v")
        shutil.copy(ROUTED / f"uart__{CORNER}.sdf", final / "sdf" / CORNER)
        recorded(root)
    return root


def recorded(root: Path) -> None:
    """Record the routed run as place and route does: gate-level simulation reads only a
    run ChipLeap recorded (chipleap.rtl.provenance)."""
    seal_routed(root / "build/pnr")


@needs_gls
def test_the_uart_tests_pass_on_the_routed_netlist_with_sdf(tmp_path):
    root = uart(tmp_path)
    r = gls.gls(load_project(root))
    assert r.status == "pass", r.summary
    d = r.data
    assert d["netlist"] == "build/pnr/runs/chipleap/final/nl/uart.nl.v"
    assert d["sdf"] == f"build/pnr/runs/chipleap/final/sdf/{CORNER}/uart__{CORNER}.sdf"
    assert d["corner"] == CORNER and d["delays"] == "typ"
    assert d["passed"] == 5 and d["failed"] == 0
    assert "routed netlist with SDF" in r.summary
    copy = (root / "build/gls/uart.gls.v").read_text()
    assert re.search(r'initial \$sdf_annotate\(".*uart__nom_typ_1p20V_25C.sdf"\);', copy)
    # Icarus read the whole SDF; only wires to the top module's ports stay unannotated.
    assert d["sdf_messages"]["unreadable"] == []


@needs_gls
def test_a_changed_gate_in_the_routed_netlist_fails_the_tests(tmp_path):
    root = uart(tmp_path)
    nl = root / "build/pnr/runs/chipleap/final/nl/uart.nl.v"
    # The transmitter's data bits go through a mux; swap its inputs for one bit.
    text = nl.read_text()
    first = text.index("sg13g2_mux2_1 ")
    block = text[first : text.index(");", first)]
    swapped = block.replace(".A0(", ".TMP(").replace(".A1(", ".A0(").replace(".TMP(", ".A1(")
    nl.write_text(text.replace(block, swapped, 1))
    # Recorded again, as if place and route had made the changed netlist: what changes is
    # the logic, which the tests must catch.
    recorded(root)
    r = gls.gls(load_project(root))
    assert r.status == "fail" and r.data["failed"] > 0, r.summary


def test_a_routed_netlist_changed_after_it_was_recorded_is_not_simulated(tmp_path, monkeypatch):
    monkeypatch.setattr(gls, "iverilog", lambda: Tool("iverilog", "/x/iverilog", "13.0", ""))
    monkeypatch.setattr(gls, "cell_models", lambda p: [])
    root = uart(tmp_path)
    nl = root / "build/pnr/runs/chipleap/final/nl/uart.nl.v"
    nl.write_text(nl.read_text() + "module planted; initial $finish; endmodule\n")
    r = gls.gls(load_project(root))
    assert r.status == "fail" and "changed after ChipLeap recorded it" in r.summary, r.summary
    (root / "build/pnr/chipleap-pnr.manifest.json").unlink()
    r = gls.gls(load_project(root))
    assert r.status == "fail" and "manifest.json is missing" in r.summary, r.summary


@needs_gls
@pytest.mark.skipif(not yosys().found, reason="needs Yosys")
def test_the_uart_tests_pass_on_the_synthesized_netlist(tmp_path):
    root = uart(tmp_path, "synth", routed=False)
    r = gls.gls(load_project(root))
    assert r.status == "pass", r.summary
    assert r.data["netlist"] == "build/synth/uart.synth.v" and r.data["sdf"] is None
    assert "synthesized netlist, zero delay" in r.summary


def test_sdf_conditions_are_rewritten_for_icarus():
    sdf = "    (COND A0 == 1'b1 & A1 == 1'b0\n     (IOPATH S X (1:1:1) (1:1:1)))\n"
    sdf += "    (COND (A1 == 1'b0 && A2 == 1'b1)\n     (IOPATH B1 Y (1:1:1) (1:1:1)))\n"
    out = gls.normalize_sdf(sdf)
    assert "(COND (A0 == 1'b1 && A1 == 1'b0)\n" in out
    assert "(COND (A1 == 1'b0 && A2 == 1'b1)\n" in out  # already in the form Icarus reads
    log = (
        "SDF ERROR: /w/x.sdf:1370: Invalid/malformed COND\n"
        "SDF ERROR: /w/x.sdf:649: Could not find intermodpath!\n"
    )
    assert gls.sdf_messages(log) == {
        "unreadable": ["/w/x.sdf:1370: Invalid/malformed COND"],
        "unplaced_interconnects": 1,
    }


@needs_gls
def test_the_delays_reach_the_simulation(tmp_path):
    # A hundred times slower cells cannot keep up with the tests' 10 ns clock.
    root = uart(tmp_path)
    sdf = root / f"build/pnr/runs/chipleap/final/sdf/{CORNER}/uart__{CORNER}.sdf"
    slow = re.sub(
        r"\((\d+\.\d+):(\d+\.\d+):(\d+\.\d+)\)",
        lambda m: "(" + ":".join(f"{float(x) * 100:.3f}" for x in m.groups()) + ")",
        sdf.read_text(),
    )
    sdf.write_text(slow)
    recorded(root)
    r = gls.gls(load_project(root))
    assert r.status == "fail" and r.data["failed"] > 0, r.summary
    assert r.data["sdf_messages"]["unreadable"] == []


def test_every_file_of_the_gate_level_run_is_written_through_builddir(
    counter_project, tmp_path, monkeypatch
):
    # The work directory was just emptied, but every file in it still goes through
    # chipleap.rtl.builddir, which never writes through a symlink.
    from chipleap.rtl import builddir

    pdk = tmp_path / "pdk/verilog"
    pdk.mkdir(parents=True)
    for name in ("cells.v", "udp.v"):
        (pdk / name).write_text(f"// {name}\n")
    netlist = counter_project / "build/pnr/counter.nl.v"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("module counter (clk_i);\n input clk_i;\nendmodule\n")
    sdf = netlist.with_suffix(".sdf")
    sdf.write_text("(DELAYFILE)\n")
    monkeypatch.setattr(gls, "cell_models", lambda p: sorted(pdk.glob("*.v")))
    monkeypatch.setattr(gls, "_netlist", lambda p: (netlist, sdf, "nom_typ_1p20V_25C"))
    monkeypatch.setattr(gls, "read_recorded", lambda p, path: path.read_bytes())
    written: list[Path] = []
    real = builddir.write_bytes

    def spy(root, rel, data):
        path = real(root, rel, data)
        written.append(path)
        return path

    monkeypatch.setattr(builddir, "write_bytes", spy)
    p = load_project(counter_project)
    work = p.work_dir("gls", empty=True)
    gate, _, _ = gls.prepare(p, work)
    files = sorted(f for f in work.rglob("*") if f.is_file())
    assert files == sorted(written) and len(files) == 4
    assert {f.name for f in gate.sources} == {"cells.v", "udp.v", "counter.gls.v"}
