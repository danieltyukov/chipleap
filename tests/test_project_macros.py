"""A project's SRAM macros (``macros``): the section, and the steps that use them (lint,
synthesis, simulation, place and route) or say they cannot yet."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from chipleap.rtl import macros
from chipleap.rtl.project import ProjectError, RtlMacro, load_project, parse_project

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "sram"
CELL = "RM_IHPSG13_1P_256x32_c2_bm_bist"


@pytest.fixture
def sram_project(tmp_path: Path) -> Path:
    dest = tmp_path / "sram"
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns("build", "__pycache__"))
    return dest


@pytest.fixture
def stand_in_pdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An IHP PDK with the example's SRAM, as CHIPLEAP_IHP_PDK names it."""
    from chip_fakes import fake_pdk, sram_1p

    pdk = fake_pdk(tmp_path / "pdk")
    sram_1p(pdk, 256, 32, (416.64, 118.78))
    sram_1p(pdk, 64, 64, (784.48, 64.36))
    monkeypatch.setenv("CHIPLEAP_IHP_PDK", str(pdk))
    return pdk


def edit(project: Path, **changes: object) -> Path:
    f = project / "project.yaml"
    data = yaml.safe_load(f.read_text())
    data.update(changes)
    f.write_text(yaml.safe_dump(data))
    return project


# The section


def test_the_example_reads_its_macro(sram_project):
    p = load_project(sram_project)
    assert p.macros == (RtlMacro("u_mem", CELL),)
    assert p.to_dict()["macros"] == [{"instance": "u_mem", "cell": CELL}]
    deep = RtlMacro("u_core/u_mem", CELL, None, (40.0, 20.005), "FS")
    assert deep.flat == "u_core.u_mem"
    assert deep.to_dict(p.rel) == {
        "instance": "u_core/u_mem",
        "cell": CELL,
        "at_um": [40.0, 20.005],
        "orientation": "FS",
    }


@pytest.mark.parametrize(
    ("macro", "message"),
    [
        ({"instance": "u mem", "cell": CELL}, "must be the instance's path"),
        ({"instance": "u_mem.x", "cell": CELL}, "must be the instance's path"),
        ({"instance": "/".join(["u"] * 9), "cell": CELL}, "at most 8 levels"),
        ({"instance": "u_mem", "cell": "RM_IHPSG13_2P_64x32_c2"}, "single-port SRAMs"),
        ({"instance": "u_mem", "cell": "sg13g2_inv_1"}, "single-port SRAMs"),
        ({"instance": "u_mem", "cell": CELL, "at_um": [1.0012, 0]}, "on the 5 nm grid"),
        ({"instance": "u_mem", "cell": CELL, "at_um": [-1, 0]}, "must be from 0 to"),
        ({"instance": "u_mem", "cell": CELL, "at_um": [1]}, r"must be \[x, y\]"),
        ({"instance": "u_mem", "cell": CELL, "orientation": "R90"}, "orientation"),
        ({"instance": "u_mem", "cell": CELL, "size": 1}, "unknown keys size"),
        ({"instance": "u_mem"}, "needs a cell .* or a package"),
        ({"instance": "u_mem", "cell": CELL, "package": "macros/x.macro"}, "one of them"),
        ({"instance": "u_mem", "package": "macros/none.macro"}, "macros/none.macro"),
    ],
)
def test_the_section_refuses(sram_project, macro, message):
    edit(sram_project, macros=[macro])
    with pytest.raises(ProjectError, match=message):
        load_project(sram_project)


def test_the_section_refuses_twice_the_same_instance_and_too_many(sram_project):
    data = yaml.safe_load((sram_project / "project.yaml").read_text())
    data["macros"] = [{"instance": "u_mem", "cell": CELL}] * 2
    with pytest.raises(ProjectError, match="listed twice"):
        parse_project(data, sram_project)
    data["macros"] = [{"instance": f"u{i}", "cell": CELL} for i in range(33)]
    with pytest.raises(ProjectError, match="at most 32"):
        parse_project(data, sram_project)


# Lint and synthesis: black boxes from the name


def test_the_stub_needs_no_pdk(sram_project):
    p = load_project(sram_project)
    (stub,) = macros.stubs(p)
    assert stub == (sram_project / "build" / "macros" / f"{CELL}.v").resolve()
    text = stub.read_text()
    assert "(* blackbox *)" in text and "  input [7:0] A_ADDR;" in text
    assert "  output [31:0] A_DOUT;" in text and "  input [31:0] A_BIST_BM;" in text


def test_synthesis_reads_the_black_box_first(sram_project):
    from chipleap.rtl.synth import read_commands

    p = load_project(sram_project)
    cmds = read_commands(p, list(p.sources), True)
    assert cmds[0].startswith("read_verilog -sv ") and cmds[0].endswith(f'{CELL}.v"')
    assert cmds[-1] == "hierarchy -check -top scratchpad"


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator not installed")
def test_lint_is_clean_with_the_black_box(sram_project):
    from chipleap.rtl.lint import lint

    r = lint(load_project(sram_project))
    assert r.status == "pass", r.summary
    assert "no warnings" in r.summary


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not installed")
def test_synthesis_keeps_the_macro(sram_project):
    from chipleap.rtl.synth import SynthUnavailable, synthesize

    try:
        r = synthesize(load_project(sram_project))
    except SynthUnavailable as exc:
        pytest.skip(str(exc))
    assert f"{CELL} u_mem" in r.netlist.read_text()


# Simulation: the PDK's models


def test_simulation_uses_the_pdks_models(sram_project, stand_in_pdk, tmp_path):
    from chipleap.rtl.sim import manifest

    p = load_project(sram_project)
    work = p.work_dir("sim-icarus", empty=True)
    m = manifest(p, "icarus", work)
    assert m["sources"][:2] == [
        "macros/RM_IHPSG13_1P_core_behavioral_bm_bist.v",
        f"macros/{CELL}.v",
    ]
    assert (work / "macros" / f"{CELL}.v").is_file()
    assert m["defines"]["FUNCTIONAL"] == 1


def test_simulation_without_the_pdk_says_so(sram_project, monkeypatch):
    from chipleap.rtl.sim import SimError, manifest

    monkeypatch.setattr("chipleap.chip.io.pdk_dir", lambda: None)
    p = load_project(sram_project)
    with pytest.raises(SimError, match="need the IHP PDK"):
        manifest(p, "icarus", p.work_dir("sim-icarus", empty=True))


def test_a_cell_the_pdk_lacks_is_refused(sram_project, stand_in_pdk):
    edit(sram_project, macros=[{"instance": "u_mem", "cell": "RM_IHPSG13_1P_1024x8_c2_bm_bist"}])
    p = load_project(sram_project)
    with pytest.raises(macros.MacroStepError, match="no single-port SRAM RM_IHPSG13_1P_1024x8"):
        macros.models(p, p.work_dir("x"))


# Place and route


def test_place_and_route_sizes_the_die_around_the_macro(sram_project, stand_in_pdk, monkeypatch):
    from chipleap.rtl import pnr

    monkeypatch.setattr(pnr, "_cell_area", lambda project, workdir: 20000.0)
    p = load_project(sram_project)
    work = p.work_dir("pnr")
    cfg = pnr.config(p, work)
    assert cfg["FP_SIZING"] == "absolute" and "FP_CORE_UTIL" not in cfg
    entry = cfg["MACROS"][CELL]
    assert set(entry["instances"]) == {"u_mem"}
    assert entry["instances"]["u_mem"]["orientation"] == "N"
    assert set(entry["lib"]) == {"*_typ_*", "*_slow_*", "*_fast_*"}
    assert cfg["PDN_CFG"] == "dir::pdn.tcl"
    script = (work / "pdn.tcl").read_text()
    assert "-inst_pattern {^u_mem$} -pin_pattern {^VDDARRAY!$} -power" in script
    x = entry["instances"]["u_mem"]["location"][0]
    assert x >= cfg["CORE_AREA"][0] + 40


def test_a_die_of_the_projects_own_places_each_macro_where_it_says(
    sram_project, stand_in_pdk, monkeypatch
):
    from chipleap.rtl import pnr
    from chipleap.verify.pnr import PnrError

    edit(
        sram_project,
        pnr={"die_um": [1000, 400]},
        macros=[{"instance": "u_mem", "cell": CELL, "at_um": [60, 20], "orientation": "FS"}],
    )
    p = load_project(sram_project)
    cfg = pnr.config(p, p.work_dir("pnr"))
    assert cfg["DIE_AREA"] == [0, 0, 1000, 400]
    loc = cfg["MACROS"][CELL]["instances"]["u_mem"]
    assert loc["orientation"] == "FS"
    assert loc["location"] == [cfg["CORE_AREA"][0] + 60, cfg["CORE_AREA"][1] + 20]
    # Under no TopMetal2 supply pair: refused, with where it could go.
    edit(
        sram_project,
        macros=[{"instance": "u_mem", "cell": "RM_IHPSG13_1P_64x64_c2_bm_bist", "at_um": [60, 20]}],
    )
    with pytest.raises(PnrError, match=r"place it at 42\.14 um"):
        pnr.config(load_project(sram_project), p.work_dir("pnr"))
    # At the core's left edge: refused, for the column of rows.
    edit(sram_project, macros=[{"instance": "u_mem", "cell": CELL, "at_um": [10, 20]}])
    with pytest.raises(PnrError, match="from the core's left edge"):
        pnr.config(load_project(sram_project), p.work_dir("pnr"))
    # Half the information is refused either way.
    edit(sram_project, macros=[{"instance": "u_mem", "cell": CELL}])
    with pytest.raises(PnrError, match="needs its at_um"):
        pnr.config(load_project(sram_project), p.work_dir("pnr"))
    data = yaml.safe_load((sram_project / "project.yaml").read_text())
    del data["pnr"]
    data["macros"] = [{"instance": "u_mem", "cell": CELL, "at_um": [60, 20]}]
    (sram_project / "project.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(PnrError, match=r"needs pnr\.die_um"):
        pnr.config(load_project(sram_project), p.work_dir("pnr"))


# The steps that do not model a macro yet


@pytest.mark.parametrize(
    "step", ["formal", "cdc", "equiv", "gls", "dft", "power", "ir_drop", "chip"]
)
def test_steps_without_a_macro_model_say_so(sram_project, step):
    from chipleap.rtl.verify import run_step

    r = run_step(load_project(sram_project), step)
    assert r.status == "skipped"
    assert r.summary.startswith(f"{step} does not model hard macros yet") and "u_mem" in r.summary


def test_commercial_tools_say_so_too(sram_project):
    from chipleap.rtl.verify import run_step

    r = run_step(load_project(sram_project), "synth", synth_tool="genus")
    assert r.status == "skipped" and r.summary.startswith("synth with genus does not model")


@pytest.mark.skipif(shutil.which("iverilog") is None, reason="Icarus Verilog not installed")
def test_the_example_simulates_with_the_pdks_model(sram_project):
    """The real PDK's FUNCTIONAL model, when it is installed here."""
    from chipleap.chip import io
    from chipleap.rtl.sim import simulate

    if io.pdk_dir() is None:
        pytest.skip("no IHP PDK")
    r = simulate(load_project(sram_project), "icarus")
    assert r.status == "pass", r.summary


def test_the_assistants_tools_say_so_too(sram_project):
    from chipleap import mcp_server
    from chipleap.mcp_server import ToolError

    out = mcp_server.prove_rtl(str(sram_project))
    assert out["status"] == "skipped" and "does not model hard macros" in out["summary"]
    assert mcp_server.cdc_rtl(str(sram_project))["status"] == "skipped"
    with pytest.raises(ToolError, match="does not model hard macros"):
        mcp_server.eco_rtl(str(sram_project), ["repair:setup"])


# A macro/v1 package beside an SRAM, named as a chip's macros are

STRONGARM = ROOT / "examples" / "uart" / "macros" / "strongarm.macro"
MIXED_SV = """\
module mixed (
    input  logic        clk_i,
    input  logic        rst_ni,
    input  logic        req_i,
    input  logic        we_i,
    input  logic [ 7:0] addr_i,
    input  logic [31:0] wdata_i,
    input  logic [ 3:0] be_i,
    output logic [31:0] rdata_o,
    output logic        rvalid_o,
    input  logic        inp_i,
    input  logic        inm_i,
    output logic        outp_o,
    output logic        outn_o
);
  scratchpad u_pad (.*);
  strongarm u_cmp (.clk(clk_i), .inp(inp_i), .inm(inm_i), .outp(outp_o), .outn(outn_o));
endmodule
"""


@pytest.fixture
def mixed_project(sram_project: Path) -> Path:
    shutil.copytree(STRONGARM, sram_project / "macros" / "strongarm.macro")
    (sram_project / "rtl" / "mixed.sv").write_text(MIXED_SV)
    return edit(
        sram_project,
        top="mixed",
        sources=["rtl/scratchpad.sv", "rtl/mixed.sv"],
        tests={"dir": "test", "toplevel": "scratchpad"},
        macros=[
            {"instance": "u_pad/u_mem", "cell": CELL},
            {"instance": "u_cmp", "package": "macros/strongarm.macro"},
        ],
    )


def test_a_package_is_read_like_a_chips(mixed_project):
    p = load_project(mixed_project)
    assert [m.flat for m in p.macros] == ["u_pad.u_mem", "u_cmp"]
    assert p.to_dict()["macros"][1] == {
        "instance": "u_cmp",
        "package": "macros/strongarm.macro",
    }
    stubs = {s.name: s.read_text() for s in macros.stubs(p)}
    assert set(stubs) == {f"{CELL}.v", "strongarm.v"}
    assert "module strongarm (clk, inm, inp, outn, outp);" in stubs["strongarm.v"]
    assert "vdd" not in stubs["strongarm.v"]


def test_a_package_named_into_the_pdks_names_is_refused(mixed_project):
    toml = mixed_project / "macros" / "strongarm.macro" / "macro.toml"
    toml.write_text(toml.read_text().replace('name = "strongarm"', 'name = "sg13g2_cmp"', 1))
    with pytest.raises(macros.MacroStepError, match="a name the PDK or ChipLeap uses"):
        macros.stubs(load_project(mixed_project))
    toml.write_text(toml.read_text().replace('name = "sg13g2_cmp"', 'name = "mixed_cmp"', 1))
    with pytest.raises(macros.MacroStepError, match="name space of mixed"):
        macros.stubs(load_project(mixed_project))


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator not installed")
def test_lint_reads_both_black_boxes(mixed_project):
    from chipleap.rtl.lint import lint

    r = lint(load_project(mixed_project))
    assert r.status == "pass", r.summary


def test_simulation_adds_the_packages_model(mixed_project, stand_in_pdk):
    from chipleap.rtl.sim import manifest

    p = load_project(mixed_project)
    m = manifest(p, "icarus", p.work_dir("sim-icarus", empty=True))
    assert "macros/strongarm.v" in m["sources"] and f"macros/{CELL}.v" in m["sources"]


def test_place_and_route_takes_the_package_as_a_chip_does(mixed_project, stand_in_pdk, monkeypatch):
    from chipleap.rtl import pnr

    monkeypatch.setattr(pnr, "_cell_area", lambda project, workdir: 20000.0)
    p = load_project(mixed_project)
    work = p.work_dir("pnr")
    cfg = pnr.config(p, work)
    assert set(cfg["MACROS"]) == {CELL, "strongarm"}
    assert set(cfg["MACROS"][CELL]["instances"]) == {"u_pad.u_mem"}
    assert set(cfg["MACROS"]["strongarm"]["instances"]) == {"u_cmp"}
    # The package's views come through the macro layer, under its instance.
    assert cfg["MACROS"]["strongarm"]["lef"] == ["dir::macros/u_cmp/views/strongarm.lef"]
    script = (work / "pdn.tcl").read_text()
    # The SRAM under its macro grid, the package's supply pins strapped to the rails.
    assert f"define_pdn_grid -macro -cells {{{CELL}}}" in script
    assert r"-inst_pattern {^u_pad\.u_mem$} -pin_pattern {^VSS!$} -ground" in script
    assert (
        "add_global_connection -net $::env(VDD_NET) -inst_pattern {^u_cmp$} -pin_pattern {^vdd$} -power"
        in script
    )
    assert "proc chipleap_hookup" in script
    assert "\nchipleap_hookup $::env(GND_NET) Metal4 " in script


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not installed")
def test_an_rtl_module_named_like_a_macro_is_refused(sram_project):
    from chipleap.macro.sram import Sram, stub

    # A model of the macro among the sources: synthesis would take it for the macro.
    model = stub(Sram(CELL, 256, 32, 0.0, 0.0, True)).replace("(* blackbox *)\n", "")
    (sram_project / "rtl" / "model.v").write_text(model)
    p = load_project(edit(sram_project, sources=["rtl/scratchpad.sv", "rtl/model.v"]))
    with pytest.raises(macros.MacroStepError, match=f"{CELL}"):
        macros.check_modules(p, p.work_dir("pnr/modules"), [CELL])
    p = load_project(edit(sram_project, sources=["rtl/scratchpad.sv"]))
    macros.check_modules(p, p.work_dir("pnr/modules"), [CELL])
