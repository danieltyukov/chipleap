"""Place and route of RTL projects: LibreLane configuration, constraints, a real run."""

import os

import pytest

from chipleap.rtl import synth as synth_mod
from chipleap.rtl.pnr import config, margins, pnr, sdc
from chipleap.rtl.project import load_project
from chipleap.verify import pnr as accel_pnr
from chipleap.verify.tools import Tool, librelane, sv2v


def test_config_from_the_project(counter_project, tmp_path):
    proj = counter_project / "project.yaml"
    proj.write_text(
        proj.read_text()
        + "pnr: {utilisation: 30, die_um: [150, 120], librelane: {FP_PDN_VPITCH: 25}}\n"
    )
    p = load_project(counter_project)
    cfg = config(p, tmp_path)
    assert cfg["DESIGN_NAME"] == "counter" and cfg["SYNTH_PARAMETERS"] == ["W=6"]
    assert cfg["CLOCK_PORT"] == "clk_i" and cfg["CLOCK_PERIOD"] == 10.0
    assert cfg["PNR_SDC_FILE"] == cfg["SIGNOFF_SDC_FILE"] == "dir::chipleap.sdc"
    assert (
        "create_clock [get_ports clk_i] -name clk_i -period 10"
        in (tmp_path / "chipleap.sdc").read_text()
    )
    assert cfg["FP_CORE_UTIL"] == 30 and cfg["PL_TARGET_DENSITY_PCT"] == 55
    assert cfg["FP_SIZING"] == "absolute" and cfg["DIE_AREA"] == [0, 0, 150, 120]
    assert cfg["FP_PDN_VPITCH"] == 25  # the project's own LibreLane keys come last
    # The accelerator's signoff settings, with margins for a 10 ns clock.
    assert {k: cfg[k] for k in accel_pnr.signoff()} == accel_pnr.signoff(0.8, 1.6)
    for f in cfg["VERILOG_FILES"]:
        assert f.startswith("dir::src/") and (tmp_path / f[5:]).is_file()


def test_margins_scale_with_the_clock(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text().replace("period_ns: 10", "period_ns: 4"))
    assert margins(load_project(counter_project)) == (0.32, 0.64)
    proj.write_text(proj.read_text().replace("period_ns: 4", "period_ns: 40"))
    assert margins(load_project(counter_project)) == (0.8, 1.6)


def test_sources_copied_without_sv2v(counter_project, tmp_path, monkeypatch):
    monkeypatch.setattr(synth_mod, "sv2v", lambda: Tool("sv2v", None, "", ""))
    (counter_project / "rtl/inc").mkdir()
    (counter_project / "rtl/inc/defs.svh").write_text("`define ONE 1\n")
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "include_dirs: [rtl/inc]\ndefines: {FAST: null}\n")
    cfg = config(load_project(counter_project), tmp_path)
    assert cfg["VERILOG_FILES"] == ["dir::src/rtl/counter.sv"]
    assert cfg["VERILOG_INCLUDE_DIRS"] == ["dir::src/rtl/inc"]
    assert cfg["VERILOG_DEFINES"] == ["FAST"]
    assert (tmp_path / "src/rtl/inc/defs.svh").is_file()


def test_own_sdc_and_several_clocks(counter_project, tmp_path):
    proj = counter_project / "project.yaml"
    text = proj.read_text().replace(
        "clocks: [{port: clk_i, period_ns: 10}]",
        "clocks: [{port: clk_i, period_ns: 10}, {port: clk2_i, period_ns: 5}]",
    )
    proj.write_text(text)
    p = load_project(counter_project)
    text = sdc(p)
    assert "create_clock [get_ports clk2_i] -name clk2_i -period 5" in text
    assert "-clock [get_clocks clk_i]" in text and "foreach name {{clk_i} {clk2_i}}" in text
    cfg = config(p, tmp_path)
    assert cfg["CLOCK_PORT"] == ["clk_i", "clk2_i"] and cfg["CLOCK_PERIOD"] == 5.0
    (counter_project / "own.sdc").write_text("create_clock [get_ports clk_i] -period 7\n")
    proj.write_text(proj.read_text() + "constraints: {sdc: own.sdc}\n")
    config(load_project(counter_project), tmp_path)
    assert (tmp_path / "chipleap.sdc").read_text().endswith("-period 7\n")


def derates(sdc_text: str, value: str) -> list[str]:
    """The derates an SDC's ``set_timing_derate`` lines set, evaluated by Tcl with
    TIME_DERATING_CONSTRAINT as LibreLane passes it (``5`` from 3.0.0.dev44)."""
    tkinter = pytest.importorskip("tkinter")
    tcl = tkinter.Tcl()
    tcl.eval("set seen {}; proc set_timing_derate {args} { lappend ::seen $args }")
    tcl.eval(f"set ::env(TIME_DERATING_CONSTRAINT) {value}")
    for line in sdc_text.splitlines():
        if line.startswith("set_timing_derate"):
            tcl.eval(line)
    return [str(x) for x in tcl.splitlist(tcl.eval("set seen"))]


def test_the_derate_applies_whatever_form_librelane_passes_it_in(counter_project):
    # dev44 passes the PDK's 5 as "5", 3.0.14 as "5.0"; 5 / 100 is 0 in Tcl.
    text = sdc(load_project(counter_project))
    for value in ("5", "5.0"):
        assert derates(text, value) == ["-early 0.95", "-late 1.05"]


def test_no_clock_uses_a_virtual_one(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text().replace("clocks: [{port: clk_i, period_ns: 10}]\n", ""))
    text = sdc(load_project(counter_project))
    assert "create_clock -name __VIRTUAL_CLK__ -period 10" in text


def test_other_technologies_skip(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "tech: generic-28nm\n")
    r = pnr(load_project(counter_project))
    assert r.status == "skipped" and "LibreLane runs on IHP SG13G2" in r.summary


@pytest.mark.pnr
@pytest.mark.skipif(not librelane().found, reason="LibreLane not installed")
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_PNR"), reason="set CHIPLEAP_RUN_PNR=1")
@pytest.mark.skipif(not sv2v().found, reason="sv2v not installed")
def test_counter_places_and_routes(counter_project):
    r = pnr(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert r.data["drc_errors"] == r.data["lvs_errors"] == r.data["antenna_nets"] == 0
    assert r.data["gds"] == "build/pnr/runs/chipleap/final/gds/counter.gds"
