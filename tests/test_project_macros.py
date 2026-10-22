"""A project's SRAM macros (``macros``): the section, and the steps that use them (lint,
synthesis, simulation, place and route) or say they cannot yet."""

from __future__ import annotations

import json
import shutil
from fnmatch import fnmatch
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


@pytest.fixture
def module_check(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The module check place and route asks for, recorded instead of run: it needs Yosys
    and has tests of its own, and the configuration tests run without it."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        macros, "check_modules", lambda project, work, views: calls.append(sorted(views))
    )
    return calls


def test_place_and_route_sizes_the_die_around_the_macro(
    sram_project, stand_in_pdk, monkeypatch, module_check
):
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
    assert cfg["RUN_POST_GRT_DESIGN_REPAIR"] is True
    assert cfg["PL_MAX_DISPLACEMENT_Y"] >= 100
    script = (work / "pdn.tcl").read_text()
    assert "-inst_pattern {^u_mem$} -pin_pattern {^VDDARRAY!$} -power" in script
    x = entry["instances"]["u_mem"]["location"][0]
    assert x >= cfg["CORE_AREA"][0] + 40
    assert module_check == [[CELL]]
    # Placed and routed against a slew limit under the macro's own, then the project's
    # constraints; signed off against those alone.
    assert cfg["PNR_SDC_FILE"] == "dir::chipleap.pnr.sdc"
    assert cfg["SIGNOFF_SDC_FILE"] == "dir::chipleap.sdc"
    lines = (work / "chipleap.pnr.sdc").read_text().splitlines()
    assert "set_max_transition 0.3 [current_design]" in lines
    assert lines[-1] == "source [file join [file dirname [info script]] chipleap.sdc]"
    assert "set_max_transition 0.3" not in (work / "chipleap.sdc").read_text()


def test_a_die_of_the_projects_own_places_each_macro_where_it_says(
    sram_project, stand_in_pdk, monkeypatch, module_check
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
    # Rows between the macro's halo and the core's edge: the legaliser keeps its default.
    assert cfg["PL_MAX_DISPLACEMENT_Y"] == 100
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
    "step", ["formal", "cdc", "equiv", "gls", "eco", "dft", "power", "ir_drop", "upf", "chip"]
)
def test_the_steps_after_synthesis_model_the_macros(sram_project, step):
    assert macros.unsupported(load_project(sram_project), step) is None


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

    out = mcp_server.synthesize_rtl(str(sram_project), tool="genus")
    assert out["status"] == "skipped" and "does not model hard macros" in out["summary"]


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


def test_place_and_route_takes_the_package_as_a_chip_does(
    mixed_project, stand_in_pdk, monkeypatch, module_check
):
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


def test_off_grid_pin_markers_in_the_pdks_sram_are_reported_apart_and_pass(
    sram_project, stand_in_pdk, monkeypatch, module_check, tmp_path
):
    """IHP's KLayout deck flags pin markers IHP's SRAMs draw off the grid, in a cell each
    SRAM is made of. Place and route reports them apart, with their cell and count, and
    passes; the same rule in a cell of the design, or another rule inside the SRAM, fails
    it."""
    from chip_fakes import _gds
    from chipleap.rtl import pnr

    bldrv = "RM_IHPSG13_1P_BLDRV"
    gds = stand_in_pdk / "libs.ref" / "sg13g2_sram" / "gds" / f"{CELL}.gds"
    gds.write_bytes(_gds(CELL, bldrv))
    tool = tmp_path / "librelane"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(tool))
    monkeypatch.setattr(pnr, "_cell_area", lambda project, workdir: 20000.0)
    monkeypatch.setattr(pnr, "check_merged", lambda *a: {})
    items: list[tuple[str, str]] = []

    def run(workdir, cfg, tag="chipleap", *, late_antenna_repair=False):
        assert late_antenna_repair  # every layout but the SoC's repairs antennas twice
        # LibreLane leaves the verdict to ChipLeap: its own would end the run on exit 2.
        assert cfg["ERROR_ON_KLAYOUT_DRC"] is False
        (workdir / "config.json").write_text("{}")
        reports = workdir / "runs" / tag / "66-klayout-drc" / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "drc.klayout.lyrdb").write_text(
            "<report-database><items>"
            + "".join(f"<item><category>'{r}'</category><cell>{c}</cell></item>" for r, c in items)
            + "</items></report-database>"
        )
        metrics = {
            "timing__setup__ws": 1.0,
            "timing__hold__ws": 0.1,
            "klayout__drc_error__count": len(items),
        }
        return metrics, None

    monkeypatch.setattr(pnr, "run_librelane", run)
    p = load_project(sram_project)
    markers = [("metal1_pin_Offgrid", f"{bldrv}:r0")] * 3 + [("metal1_pin_Offgrid", f"{bldrv}:m90")]
    items[:] = markers
    result = pnr.pnr(p)
    assert result.status == "pass", result.summary
    assert result.data["drc_errors"] == 0
    assert result.data["pin_markers_in_pdk_macros"] == [
        {"rule": "metal1_pin_Offgrid", "cell": bldrv, "macros": [CELL], "count": 4}
    ]
    assert result.summary.endswith(
        "DRC, LVS and antenna clean; 4 pin markers off the grid inside the PDK's own macros, "
        f"not counted: 4 metal1_pin_Offgrid in {bldrv}"
    )
    for other in (
        ("metal1_pin_Offgrid", p.top),  # the design's own cell
        ("metal1_pin_Offgrid", "sg13g2_dfrbp_1"),  # a standard cell: no macro of the PDK
        ("M1.b", f"{bldrv}:r0"),  # another rule, inside the SRAM
    ):
        items[:] = [*markers, other]
        result = pnr.pnr(p)
        assert result.status == "fail" and result.data["drc_errors"] == 1, other
        assert "1 DRC, 0 LVS and 0 antenna violations; 4 pin markers" in result.summary


BLDRV = "RM_IHPSG13_1P_BLDRV"
SLOW, TYP, FAST = "nom_slow_1p08V_125C", "nom_typ_1p20V_25C", "nom_fast_1p32V_m40C"


def _timing(setup: tuple[float, ...], hold: tuple[float, ...]) -> dict[str, float]:
    """LibreLane's worst slacks, per corner (slow, typical, fast) and overall."""
    out: dict[str, float] = {}
    for kind, values in (("setup", setup), ("hold", hold)):
        for corner, v in zip((SLOW, TYP, FAST), values, strict=True):
            out[f"timing__{kind}__ws__corner:{corner}"] = v
        out[f"timing__{kind}__ws"] = min(values)
    return out


MET = _timing((9.7, 12.1, 15.0), (0.5, 1.0, 0.3))
MARKERS = [("metal1_pin_Offgrid", f"{BLDRV}:r0")] * 8  # what the example's SRAM has


def _deferred(cfg: dict, metrics: dict, drc_count: int) -> list[str]:
    """The deferred errors of LibreLane 3.0.14's checkers on IHP-Open-PDK 22f2a25, as far as
    these tests need them: a KLayout DRC count unless ERROR_ON_KLAYOUT_DRC is off, and a
    negative worst slack at a corner SETUP_VIOLATION_CORNERS (else IHP's
    TIMING_VIOLATION_CORNERS, "*") or HOLD_VIOLATION_CORNERS (LibreLane's default, "*")
    matches; "" matches none."""
    errors = []
    if drc_count and cfg.get("ERROR_ON_KLAYOUT_DRC", True):
        errors.append(f"{drc_count} KLayout DRC errors found.")
    for kind in ("setup", "hold"):
        corners = [c for c in (cfg.get(f"{kind.upper()}_VIOLATION_CORNERS") or ["*"]) if c]
        for key, value in metrics.items():
            corner = key.partition(f"timing__{kind}__ws__corner:")[2]
            if corner and value < 0 and any(fnmatch(corner, c) for c in corners):
                errors.append(f"{kind} violations at {corner}")
    return errors


@pytest.fixture
def librelane_3014(sram_project, stand_in_pdk, monkeypatch, module_check, tmp_path):
    """Place and route of the SRAM example through ``run_librelane`` with a LibreLane that
    behaves like 3.0.14 at the end of a run: it writes final/ and exits 2 on a deferred
    error (``_deferred``). Set ``items`` (KLayout DRC report) and ``metrics`` first; each
    run records the errors under ChipLeap's configuration and under LibreLane's own."""
    from chip_fakes import _gds
    from chipleap.rtl import pnr
    from chipleap.verify import pnr as accel_pnr

    gds = stand_in_pdk / "libs.ref" / "sg13g2_sram" / "gds" / f"{CELL}.gds"
    gds.write_bytes(_gds(CELL, BLDRV))
    tool = tmp_path / "librelane"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(tool))
    monkeypatch.setattr(pnr, "_cell_area", lambda project, workdir: 20000.0)
    monkeypatch.setattr(pnr, "check_merged", lambda *a: {})
    monkeypatch.setattr(
        accel_pnr, "patch_min_area", lambda *a, **k: (accel_pnr.AFTER_ROUTING, None)
    )
    state: dict = {"items": [], "metrics": MET, "runs": []}

    def fake(cmd, workdir, log, timeout, limit, env):
        run = workdir / "runs" / "chipleap"
        log.write_text("")
        if "--to" in cmd:
            routed = run / "45-openroad-detailedrouting"
            routed.mkdir(parents=True)
            (routed / "state_out.json").write_text("{}")
            return 0
        reports = run / "66-klayout-drc" / "reports"
        reports.mkdir(parents=True)
        (reports / "drc.klayout.lyrdb").write_text(
            "<report-database><items>"
            + "".join(
                f"<item><category>'{r}'</category><cell>{c}</cell></item>"
                for r, c in state["items"]
            )
            + "</items></report-database>"
        )
        metrics = {**state["metrics"], "klayout__drc_error__count": len(state["items"])}
        (run / "final").mkdir()
        (run / "final" / "metrics.json").write_text(json.dumps(metrics))
        cfg = json.loads((workdir / cmd[-1]).read_text())
        ours, theirs = (_deferred(c, metrics, len(state["items"])) for c in (cfg, {}))
        state["runs"].append({"chipleap": ours, "librelane": theirs})
        return 2 if ours else 0

    monkeypatch.setattr(accel_pnr, "_librelane", fake)
    state["project"] = load_project(sram_project)
    return state


def test_the_srams_markers_alone_pass_where_librelanes_gate_would_end_the_run(librelane_3014):
    from chipleap.rtl import pnr

    librelane_3014["items"] = MARKERS
    result = pnr.pnr(librelane_3014["project"])
    assert result.status == "pass", result.summary
    assert result.data["drc_errors"] == 0
    assert result.data["pin_markers_in_pdk_macros"][0]["count"] == 8
    assert librelane_3014["runs"] == [
        {"chipleap": [], "librelane": ["8 KLayout DRC errors found."]}
    ]


@pytest.mark.parametrize(
    "extra",
    [
        ("M1.b", f"{BLDRV}:r0"),  # another rule, inside the SRAM
        ("metal1_pin_Offgrid", "scratchpad"),  # the marker rule in the design's own cell
        ("metal1_pin_Offgrid", "sg13g2_dfrbp_1"),  # and in a standard cell, no PDK macro
    ],
)
def test_without_librelanes_drc_gate_one_error_beyond_the_markers_fails(librelane_3014, extra):
    from chipleap.rtl import pnr

    librelane_3014["items"] = [*MARKERS, extra]
    result = pnr.pnr(librelane_3014["project"])
    assert result.status == "fail" and result.data["drc_errors"] == 1, result.summary
    assert "1 DRC, 0 LVS and 0 antenna violations; 8 pin markers" in result.summary
    assert librelane_3014["runs"][0]["chipleap"] == []  # judged by ChipLeap, not LibreLane


def test_without_librelanes_timing_gate_a_slow_corner_miss_is_reported_as_missed(
    librelane_3014,
):
    """Setup met at the typical corner and missed by 0.4 ns at the slow one: IHP's
    configuration at 22f2a25 checks every corner, so LibreLane would end the run; ChipLeap
    reads it and reports the clock it does meet."""
    from chipleap.rtl import pnr

    librelane_3014["metrics"] = _timing((-0.4, 1.2, 3.0), (0.5, 1.0, 0.3))
    result = pnr.pnr(librelane_3014["project"])
    assert result.status == "fail", result.summary
    assert "50 MHz on clk_i missed at the slow corner (about 49 MHz" in result.summary
    assert result.data["setup_slack_ns"] == -0.4
    assert librelane_3014["runs"] == [
        {"chipleap": [], "librelane": [f"setup violations at {SLOW}"]}
    ]


def test_without_librelanes_timing_gate_a_fast_corner_hold_miss_fails(librelane_3014):
    from chipleap.rtl import pnr

    librelane_3014["metrics"] = _timing((9.7, 12.1, 15.0), (0.5, 1.0, -0.05))
    result = pnr.pnr(librelane_3014["project"])
    assert result.status == "fail", result.summary
    assert "50 MHz on clk_i met at the slow corner" in result.summary
    assert "; hold missed by 0.050 ns; DRC, LVS and antenna clean" in result.summary
    assert result.data["hold_slack_ns"] == -0.05
    assert librelane_3014["runs"] == [{"chipleap": [], "librelane": [f"hold violations at {FAST}"]}]


def test_a_gate_the_project_turns_back_on_ends_the_run(librelane_3014, sram_project):
    """A project may hand LibreLane its own verdict again (pnr.librelane); the run then
    ends on exit 2 and is not read."""
    from chipleap.rtl import pnr

    data = yaml.safe_load((sram_project / "project.yaml").read_text())
    data.setdefault("pnr", {}).setdefault("librelane", {})["ERROR_ON_KLAYOUT_DRC"] = True
    (sram_project / "project.yaml").write_text(yaml.safe_dump(data))
    librelane_3014["items"] = MARKERS
    result = pnr.pnr(load_project(sram_project))
    assert result.status == "fail" and "did not finish (exit 2)" in result.summary


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


@pytest.mark.skipif(shutil.which("yosys") is None, reason="Yosys not installed")
@pytest.mark.parametrize(
    "model",
    [
        "module strongarm(input a); endmodule\n",  # empty: read_verilog makes it a black box
        "(* blackbox *) module strongarm(input a); endmodule\n",
        "module strongarm(input a, output y); assign y = a; endmodule\n",  # not instantiated
    ],
)
def test_every_module_of_the_rtl_is_checked_boxes_and_unused_ones_too(sram_project, model):
    import subprocess

    # Verilog sources reach LibreLane as they are (SystemVerilog goes through sv2v, which
    # keeps only what the top reaches, and LibreLane reads its output).
    top = f"module scratchpad(input clk_i, output [31:0] q);\n  {CELL} u_mem (.A_CLK(clk_i), "
    top += ".A_DOUT(q));\nendmodule\n"
    (sram_project / "rtl" / "top.v").write_text(top)
    (sram_project / "rtl" / "extra.v").write_text(model)
    sources = ["rtl/top.v", "rtl/extra.v"]
    p = load_project(edit(sram_project, sources=sources, resets=[], tests=None))
    with pytest.raises(macros.MacroStepError, match="module strongarm is defined by macro"):
        macros.check_modules(p, p.work_dir("pnr/modules"), [CELL, "strongarm"])
    macros.check_modules(p, p.work_dir("pnr/modules"), [CELL, "comparator"])
    # Listed after hierarchy, as a chip's check once listed them, none of these is there:
    # the top does not instantiate it, and a black box is not listed. LibreLane reads the
    # same sources beside the macro's black box, which a module of the RTL replaces.
    from chipleap.macro.sram import Sram, stub

    (sram_project / "box.v").write_text(stub(Sram(CELL, 256, 32, 0.0, 0.0, True)))
    reads = "; ".join(f"read_verilog {f}" for f in ("box.v", *sources))
    script = f"{reads}; hierarchy -top scratchpad; tee -q -o ls.txt ls"
    subprocess.run(["yosys", "-q", "-p", script], cwd=sram_project, check=True)
    assert "strongarm" not in (sram_project / "ls.txt").read_text()
