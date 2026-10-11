"""Place and route of a project with LibreLane on IHP SG13G2.

The configuration comes from the project and the signoff settings come from the
accelerator's flow (``chipleap.verify.pnr.signoff``): optimisation at the slow corner,
repair after global routing, antenna repair and KLayout's IHP rule deck. The slack
margins scale with the clock (8 % and 16 % of the period, at most the accelerator's 0.8
and 1.6 ns), so a slow design is not asked for margins it cannot use and a fast one is not
given more than the accelerator's.

Constraints: the project's own SDC when it names one, else one written here. It follows
LibreLane's base SDC (the same input and output delays, driving cell, load, uncertainty,
transition and derating, read from the flow's variables), extended to every clock the
project lists; inputs and outputs are timed against the first clock.

The LibreLane wrapper mounts only the run directory into its container, so the RTL is
copied into it: one sv2v output with includes and defines resolved when sv2v is
installed, else the sources and include directories as they are.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from chipleap.rtl import quote as q
from chipleap.rtl.project import Project
from chipleap.rtl.synth import DEFAULT_PERIOD_NS, SynthError, prepare_verilog
from chipleap.verify.pnr import PnrError, PnrResult, parse, run_librelane, signoff
from chipleap.verify.result import StepResult
from chipleap.verify.tools import librelane

SDC_NAME = "chipleap.sdc"


def sdc(project: Project) -> str:
    """Timing constraints for every clock of the project, in LibreLane's base SDC style;
    the expressions are the base SDC's own, so a one-clock design is timed the same."""
    clocks = project.clocks
    lines = [
        f"# ChipLeap: constraints for {project.top}, written from its project file.",
        "# Inputs and outputs are timed against the first clock, as LibreLane's base SDC does.",
    ]
    if clocks:
        lines += [
            f"create_clock [get_ports {q.plain(c.port)}] -name {q.plain(c.port)} "
            f"-period {float(c.period_ns):g}"
            for c in clocks
        ]
        ref, period = q.plain(clocks[0].port), float(clocks[0].period_ns)
        lines += [
            "set clock_inputs {}",
            "set data_inputs [all_inputs]",
            f"foreach name {q.tcl_list(c.port for c in clocks)} {{",
            "    set port [get_ports $name]",
            "    lappend clock_inputs $port",
            "    set i [lsearch $data_inputs $port]",
            "    if { $i >= 0 } { set data_inputs [lreplace $data_inputs $i $i] }",
            "}",
        ]
    else:
        ref, period = "__VIRTUAL_CLK__", DEFAULT_PERIOD_NS
        lines += [
            f"create_clock -name {ref} -period {period:g}",
            "set clock_inputs {}",
            "set data_inputs [all_inputs]",
        ]
    lines += [
        f"set io_delay [expr {period:g} * $::env(IO_DELAY_CONSTRAINT) / 100]",
        f"set_input_delay $io_delay -clock [get_clocks {ref}] $data_inputs",
        f"set_output_delay $io_delay -clock [get_clocks {ref}] [all_outputs]",
        "set_max_fanout $::env(MAX_FANOUT_CONSTRAINT) [current_design]",
        "if { [info exists ::env(MAX_TRANSITION_CONSTRAINT)] } {",
        "    set_max_transition $::env(MAX_TRANSITION_CONSTRAINT) [current_design]",
        "}",
        "if { [info exists ::env(MAX_CAPACITANCE_CONSTRAINT)] } {",
        "    set_max_capacitance $::env(MAX_CAPACITANCE_CONSTRAINT) [current_design]",
        "}",
        "if { ![info exists ::env(SYNTH_CLK_DRIVING_CELL)] } {",
        "    set ::env(SYNTH_CLK_DRIVING_CELL) $::env(SYNTH_DRIVING_CELL)",
        "}",
        "set_driving_cell \\",
        '    -lib_cell [lindex [split $::env(SYNTH_DRIVING_CELL) "/"] 0] \\',
        '    -pin [lindex [split $::env(SYNTH_DRIVING_CELL) "/"] 1] \\',
        "    $data_inputs",
        "foreach port $clock_inputs {",
        "    set_driving_cell \\",
        '        -lib_cell [lindex [split $::env(SYNTH_CLK_DRIVING_CELL) "/"] 0] \\',
        '        -pin [lindex [split $::env(SYNTH_CLK_DRIVING_CELL) "/"] 1] \\',
        "        $port",
        "}",
        "set_load [expr $::env(OUTPUT_CAP_LOAD) / 1000.0] [all_outputs]",
        "set_clock_uncertainty $::env(CLOCK_UNCERTAINTY_CONSTRAINT) [all_clocks]",
        "set_clock_transition $::env(CLOCK_TRANSITION_CONSTRAINT) [all_clocks]",
        "set_timing_derate -early [expr 1-[expr $::env(TIME_DERATING_CONSTRAINT) / 100]]",
        "set_timing_derate -late [expr 1+[expr $::env(TIME_DERATING_CONSTRAINT) / 100]]",
        # Ideal clocks before the clock tree exists, propagated after, as in the base SDC.
        "if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {",
        "    unset_propagated_clock [all_clocks]",
        "} else {",
        "    set_propagated_clock [all_clocks]",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _copy_into(project: Project, path: Path, src: Path) -> str:
    rel = project.rel(path)
    if Path(rel).is_absolute():
        raise PnrError(
            f"{path} is outside the project directory; install sv2v, or move it in, so the "
            "flow can copy it into its run directory"
        )
    project.stage(path, src / rel)
    return f"dir::src/{rel}"


def margins(project: Project) -> tuple[float, float]:
    """The setup slack the repairs after placement and after global routing aim for."""
    clk = project.fastest_clock
    period = clk.period_ns if clk else DEFAULT_PERIOD_NS
    return round(min(0.8, 0.08 * period), 3), round(min(1.6, 0.16 * period), 3)


def config(project: Project, workdir: Path) -> dict[str, Any]:
    """The LibreLane configuration for ``project``; copies its RTL and constraints into
    ``workdir``."""
    src = workdir / "src"
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    try:
        files, raw = prepare_verilog(project, src)
    except SynthError as exc:
        raise PnrError(str(exc)) from exc
    cfg: dict[str, Any] = {"DESIGN_NAME": project.top}
    if raw:
        cfg["VERILOG_FILES"] = [_copy_into(project, f, src) for f in files]
        if project.include_dirs:
            cfg["VERILOG_INCLUDE_DIRS"] = [
                _copy_into(project, d, src) for d in project.include_dirs
            ]
        if project.defines:
            cfg["VERILOG_DEFINES"] = project.define_args()
    else:
        cfg["VERILOG_FILES"] = [f"dir::src/{f.name}" for f in files]
    if project.parameters:
        cfg["SYNTH_PARAMETERS"] = [f"{k}={v}" for k, v in project.parameters.items()]
    clk = project.fastest_clock
    if project.clocks:
        ports = [c.port for c in project.clocks]
        cfg["CLOCK_PORT"] = ports[0] if len(ports) == 1 else ports
    cfg["CLOCK_PERIOD"] = clk.period_ns if clk else DEFAULT_PERIOD_NS
    if project.sdc is not None:
        shutil.copy2(project.sdc, workdir / SDC_NAME)
    else:
        (workdir / SDC_NAME).write_text(sdc(project))
    cfg["PNR_SDC_FILE"] = f"dir::{SDC_NAME}"
    cfg["SIGNOFF_SDC_FILE"] = f"dir::{SDC_NAME}"
    cfg["FP_CORE_UTIL"] = project.pnr.utilisation
    cfg["PL_TARGET_DENSITY_PCT"] = project.pnr.density
    if project.pnr.die_um:
        w, h = project.pnr.die_um
        cfg["FP_SIZING"] = "absolute"
        cfg["DIE_AREA"] = [0, 0, w, h]
    cfg.update(signoff(*margins(project)))
    cfg.update(project.pnr.librelane)
    return cfg


def place_route(project: Project, workdir: Path | None = None) -> PnrResult:
    if project.tech != "ihp-sg13g2":
        raise PnrError(f"LibreLane runs on IHP SG13G2, not {project.tech}; use --pnr-tool innovus")
    if not librelane().found:
        raise PnrError("LibreLane is not installed")
    work = (workdir or project.build_dir / "pnr").resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg = config(project, work)
    metrics, gds = run_librelane(work, cfg)
    clk = project.fastest_clock
    return parse(metrics, clk.mhz if clk else 1000.0 / DEFAULT_PERIOD_NS, gds)


def pnr_data(project: Project, r: PnrResult) -> dict[str, Any]:
    m = r.metrics

    def count(key: str) -> int:
        v = m.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    return {
        "tool": "LibreLane",
        "technology": project.tech,
        "clock_mhz": round(r.clock_mhz, 3),
        "die_um2": round(r.die_um2),
        "core_um2": round(r.core_um2),
        "cell_um2": round(r.cell_um2),
        "utilisation": round(r.utilisation, 3),
        "cells": r.cells,
        "setup_slack_typ_ns": round(r.setup_slack_typ_ns, 3),
        "setup_slack_ns": round(r.setup_slack_ns, 3),
        "hold_slack_ns": round(r.hold_slack_ns, 3),
        "fmax_mhz": round(r.fmax_mhz, 1),
        "fmax_worst_mhz": round(r.fmax_worst_mhz, 1),
        "drc_errors": r.drc_errors,
        "lvs_errors": r.lvs_errors,
        "antenna_nets": r.antenna_nets,
        "max_slew_violations": count("design__max_slew_violation__count"),
        "max_cap_violations": count("design__max_cap_violation__count"),
        "power_mw": round(r.power_mw, 4),
        "gds": project.rel(r.gds) if r.gds else None,
    }


def pnr(project: Project) -> StepResult:
    """The place-and-route step: passes when setup is met at the slow corner, hold at every
    corner, and DRC, LVS and antenna checks are clean."""
    try:
        r = place_route(project)
    except PnrError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not installed" in msg or "runs on IHP" in msg else "fail"
        return StepResult("pnr", status, msg, {"error": str(exc)})
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    clean = r.clean and r.antenna_nets == 0
    if clean:
        checks = "DRC, LVS and antenna clean"
    else:
        checks = f"{r.drc_errors} DRC, {r.lvs_errors} LVS and {r.antenna_nets} antenna violations"
    clk = project.fastest_clock
    clock = f"{r.clock_mhz:g} MHz on {clk.port}" if clk else f"a {r.clock_mhz:g} MHz virtual clock"
    msg = (
        f"placed and routed on IHP SG13G2: {r.core_um2 / 1e6:.4f} mm2 core at "
        f"{r.utilisation * 100:.0f}% utilisation, {r.cells} cells; {clock} "
        f"{'met' if met else 'missed'} at the slow corner (about {r.fmax_worst_mhz:.0f} MHz, "
        f"and about {r.fmax_mhz:.0f} MHz at the typical one); {checks}"
    )
    return StepResult("pnr", "pass" if met and clean else "fail", msg, pnr_data(project, r))
