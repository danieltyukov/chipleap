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

A project with SRAM macros (``macros``) gets their views from the PDK, copied beside the
run, and a floorplan of ChipLeap's: the die sized from the macros and the cells' area at
the project's utilisation, the macros in rows above and below the logic
(``chipleap.macro.place``), or, with ``pnr.die_um``, where each macro's ``at_um`` puts it.
The power grid is LibreLane's with a macro grid over each SRAM (``chipleap.macro.grid``).
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from chipleap.chip import io
from chipleap.macro import grid, place, sram
from chipleap.rtl import builddir, macros
from chipleap.rtl import quote as q
from chipleap.rtl.openroad import ECO_MANIFEST, PNR_MANIFEST, RUN_TAG
from chipleap.rtl.project import Project, runs_tools
from chipleap.rtl.provenance import Sealed, SealError, seal, unseal
from chipleap.rtl.synth import DEFAULT_PERIOD_NS, SynthError, prepare_verilog
from chipleap.verify.pnr import (
    MACRO_PNR_SLEW_NS,
    MACRO_REPAIR,
    PnrError,
    PnrResult,
    macro_reach,
    parse_run,
    run_librelane,
    signoff,
    with_ihp_antenna,
)
from chipleap.verify.result import StepResult
from chipleap.verify.tools import librelane

SDC_NAME = "chipleap.sdc"
# With macros, placement and routing read this instead (``pnr_sdc``) and signoff the
# project's own constraints, as the accelerator's layout does (chipleap.verify.pnr).
PNR_SDC_NAME = "chipleap.pnr.sdc"


def pnr_sdc() -> str:
    """The constraints placement and routing read for a project with SRAM macros: the
    design-wide slew limit repair needs to keep the macros' inputs within theirs
    (``chipleap.verify.pnr.MACRO_PNR_SLEW_NS``), then the project's constraints from the
    file beside this one, so a limit they set themselves wins."""
    return (
        "# A slew target for repair under the SRAM macros' input limit (0.595 ns at the\n"
        "# slow corner), since repair holds nets to their drivers' limits, not their loads'.\n"
        f"set_max_transition {MACRO_PNR_SLEW_NS} [current_design]\n"
        "# The project's constraints, which signoff reads alone.\n"
        f"source [file join [file dirname [info script]] {SDC_NAME}]\n"
    )


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
    lines += io_lines(ref, period)
    return "\n".join(lines) + "\n"


def io_lines(ref: str, period: float) -> list[str]:
    """The rest of LibreLane's base SDC, after the clocks: inputs and outputs timed against
    clock ``ref`` of ``period`` ns, the PDK's driving cells (the clock's on
    ``$clock_inputs``, the rest on ``$data_inputs``), load, uncertainty, transition and
    derating, and ideal or propagated clocks."""
    return [
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
        # A real division: LibreLane 3.0.0.dev44 passes the PDK's 5 as an integer, and
        # Tcl's 5 / 100 is 0.
        "set_timing_derate -early [expr 1-[expr $::env(TIME_DERATING_CONSTRAINT) / 100.0]]",
        "set_timing_derate -late [expr 1+[expr $::env(TIME_DERATING_CONSTRAINT) / 100.0]]",
        # Ideal clocks before the clock tree exists, propagated after, as in the base SDC.
        "if { [info exists ::env(OPENLANE_SDC_IDEAL_CLOCKS)] && $::env(OPENLANE_SDC_IDEAL_CLOCKS) } {",
        "    unset_propagated_clock [all_clocks]",
        "} else {",
        "    set_propagated_clock [all_clocks]",
        "}",
    ]


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


def _scan_netlist(project: Project, workdir: Path, src: Path) -> dict[str, Any]:
    """The configuration's sources for a project with scan: OpenROAD's scan-inserted
    netlist. LibreLane's synthesis keeps its cells and buffers its assignments; Verilator
    cannot lint a netlist without the cell models, so lint is off."""
    from chipleap.rtl.dft import DftError, insert_scan, scan_sealed
    from chipleap.rtl.netlist import NetlistError
    from chipleap.rtl.openroad import OpenroadError

    try:
        scan = insert_scan(project, workdir / "scan")
        # Through the insertion's manifest: LibreLane routes the bytes that were checked.
        scan_sealed(scan).copy(scan.netlist, src / scan.netlist.name)
    except (DftError, OpenroadError, NetlistError, SealError) as exc:
        raise PnrError(f"scan insertion (dft.insert) failed: {exc}") from exc
    return {"VERILOG_FILES": [f"dir::src/{scan.netlist.name}"], "RUN_LINTER": False}


def verilog_config(project: Project, workdir: Path) -> dict[str, Any]:
    """LibreLane's Verilog settings for ``project``; copies its RTL into ``workdir/src``."""
    src = builddir.work_dir(workdir, "src", empty=True)
    try:
        files, raw = prepare_verilog(project, src)
    except SynthError as exc:
        raise PnrError(str(exc)) from exc
    cfg: dict[str, Any] = {}
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
    return cfg


def config(project: Project, workdir: Path) -> dict[str, Any]:
    """The LibreLane configuration for ``project``; copies its RTL and constraints into
    ``workdir``. With ``dft.insert`` the scan-inserted netlist takes the RTL's place."""
    cfg: dict[str, Any] = {"DESIGN_NAME": project.top}
    if project.dft is not None and project.dft.insert:
        src = builddir.work_dir(workdir, "src", empty=True)
        cfg.update(_scan_netlist(project, workdir, src))
        # The scan netlist instantiates the hard macros as the RTL does.
        if project.macros:
            cfg.update(macro_config(project, workdir))
        return _finish(project, workdir, cfg)
    cfg.update(verilog_config(project, workdir))
    if project.parameters:
        cfg["SYNTH_PARAMETERS"] = [f"{k}={v}" for k, v in project.parameters.items()]
    if project.macros:
        cfg.update(macro_config(project, workdir))
    return _finish(project, workdir, cfg)


def _cell_area(project: Project, workdir: Path) -> float:
    """The standard cells' area, from a synthesis of the project of its own."""
    from chipleap.rtl.synth import SynthUnavailable, synthesize

    try:
        return synthesize(project, workdir / "estimate").area_um2
    except (SynthError, SynthUnavailable) as exc:
        raise PnrError(f"sizing the die around the macros needs a synthesis: {exc}") from exc


def macro_config(project: Project, workdir: Path) -> dict[str, Any]:
    """LibreLane's settings for the project's hard macros: their views, the floorplan
    around them and their power, written into ``workdir``. An SRAM gets the macro grid
    over it; a package whose supply pins reach an edge gets them strapped to the rails,
    as in a chip (``chipleap.chip.pdn``)."""
    from chipleap.chip import merge, pdn
    from chipleap.chip.floorplan import Placed
    from chipleap.macro.layer import MacroImportError

    try:
        views, by_sram, cell = macros.views(project, workdir)
        macros.check_modules(project, builddir.work_dir(workdir, "modules"), sorted(views))
        merge.check_macros(views.values())
    except (macros.MacroStepError, MacroImportError, merge.MergeError) as exc:
        raise PnrError(f"macros: {exc}") from exc
    sizes = {
        m.flat: place.Macro(
            m.flat,
            views[cell[m.flat]].width,
            views[cell[m.flat]].height,
            gridded=not views[cell[m.flat]].supply_hookup,
        )
        for m in project.macros
    }
    try:
        if project.pnr.die_um:
            missing = [m.instance for m in project.macros if m.at_um is None]
            if missing:
                raise PnrError(
                    f"with pnr.die_um every macro needs its at_um ({', '.join(missing)}); or "
                    "leave die_um out and ChipLeap sizes the die and places them"
                )
            fp = place.given(
                project.pnr.die_um,
                [(sizes[m.flat], *m.at_um, m.orientation) for m in project.macros if m.at_um],
            )
        else:
            placed = [m.instance for m in project.macros if m.at_um is not None]
            if placed:
                raise PnrError(
                    f"at_um ({', '.join(placed)}) needs pnr.die_um: a die of your own size; "
                    "without it ChipLeap sizes the die and places every macro"
                )
            fp = place.rows(
                list(sizes.values()),
                _cell_area(project, workdir),
                project.pnr.utilisation / 100,
            )
        gridded = [
            grid.Placement(p.instance, views[cell[p.instance]], p.x, p.y, p.orient)
            for p in fp.macros
            if sizes[p.instance].gridded
        ]
        script = grid.core_script(gridded, fp.core[1])
        hooked = {
            p.instance: (
                Placed(
                    p.instance,
                    cell[p.instance],
                    p.x,
                    p.y,
                    p.orient,
                    sizes[p.instance].width,
                    sizes[p.instance].height,
                ),
                views[cell[p.instance]],
            )
            for p in fp.macros
            if not sizes[p.instance].gridded
        }
        if hooked:
            connects, hooks = pdn.hookup_lines(hooked, "$::env(VDD_NET)", "$::env(GND_NET)")
            script += "\n".join([*connects, "global_connect", pdn.HOOKUP_PROC.strip("\n"), *hooks])
            script += "\n"
    except (place.PlaceError, grid.GridError, pdn.PdnError) as exc:
        raise PnrError(f"macros: {exc}") from exc
    builddir.write_text(workdir, "pdn.tcl", script)
    entries, _ = sram.librelane_macros(
        by_sram,
        [(p.instance, cell[p.instance], p.x, p.y, p.orient) for p in fp.macros],
        workdir,
        views,
    )
    return {
        **MACRO_REPAIR,
        **macro_reach(fp, list(sizes.values())),
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, *fp.die],
        "CORE_AREA": list(fp.core),
        "MACROS": entries,
        "PDN_CFG": "dir::pdn.tcl",
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
    }


def _finish(project: Project, workdir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """The clocks, constraints and signoff settings every configuration shares."""
    clk = project.fastest_clock
    if project.clocks:
        ports = [c.port for c in project.clocks]
        cfg["CLOCK_PORT"] = ports[0] if len(ports) == 1 else ports
    cfg["CLOCK_PERIOD"] = clk.period_ns if clk else DEFAULT_PERIOD_NS
    if project.sdc is not None:
        project.stage(project.sdc, workdir / SDC_NAME)
    else:
        (workdir / SDC_NAME).write_text(sdc(project))
    cfg["PNR_SDC_FILE"] = f"dir::{SDC_NAME}"
    cfg["SIGNOFF_SDC_FILE"] = f"dir::{SDC_NAME}"
    if project.macros:
        builddir.write_text(workdir, PNR_SDC_NAME, pnr_sdc())
        cfg["PNR_SDC_FILE"] = f"dir::{PNR_SDC_NAME}"
    cfg["PL_TARGET_DENSITY_PCT"] = project.pnr.density
    if not project.macros:
        cfg["FP_CORE_UTIL"] = project.pnr.utilisation
    if project.pnr.die_um and not project.macros:
        w, h = project.pnr.die_um
        cfg["FP_SIZING"] = "absolute"
        cfg["DIE_AREA"] = [0, 0, w, h]
    cfg.update(signoff(*margins(project)))
    cfg.update(project.pnr.librelane)
    # LibreLane hands these values to Tcl; refuse any a Tcl word could not hold as text.
    # The macros' entries are ChipLeap's own: checked names and paths it wrote.
    for value in (v for k, v in cfg.items() if k != "MACROS"):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                try:
                    q.plain(item)
                except q.QuoteError as exc:
                    raise PnrError(f"LibreLane configuration: {exc}") from exc
    return cfg


@runs_tools
def place_route(project: Project, workdir: Path | None = None) -> PnrResult:
    if project.tech != "ihp-sg13g2":
        raise PnrError(f"LibreLane runs on IHP SG13G2, not {project.tech}; use --pnr-tool innovus")
    if not (ll := librelane()).found:
        raise PnrError(ll.missing("LibreLane"))
    work = project.work_dir(workdir or "pnr")
    # The manifests go first, so a run that stops halfway leaves none (and an ECO of the
    # previous run is no longer current); the new one is written once LibreLane has
    # finished: later steps reuse only what it records.
    unseal(work, PNR_MANIFEST)
    unseal(work, ECO_MANIFEST)
    if (why := macros.unsupported(project, "dft")) and project.dft and project.dft.insert:
        raise PnrError(f"scan insertion (dft.insert): {why}")
    cfg = config(project, work)
    metrics, gds = run_librelane(work, cfg, RUN_TAG, late_antenna_repair=True)
    if project.macros:
        check_merged(work / "runs" / RUN_TAG, work, project.top)
    seal_routed(work, bool(project.macros))
    clk = project.fastest_clock
    try:
        held = macros.pdk_cells(project)
    except macros.MacroStepError as exc:
        raise PnrError(f"macros: {exc}") from exc
    mhz = clk.mhz if clk else 1000.0 / DEFAULT_PERIOD_NS
    r = parse_run(metrics, mhz, gds, work / "runs" / RUN_TAG, held, io.pdk_dir())
    r = with_ihp_antenna(r, io.pdk_dir(), project.top, work)
    if not project.macros:
        return r
    area = macros.area_um2(project) or 0.0
    names = {m.flat: m.cell or (m.package.name if m.package else "") for m in project.macros}
    return replace(r, macros=tuple(names.items()), macro_um2=area)


def check_merged(run_dir: Path, work: Path, top: str) -> dict[str, int]:
    """The names LibreLane merged into the layout and the LVS netlist, from its resolved
    configuration: no GDS cell or subcircuit defined twice (``chipleap.chip.merge``), so
    no macro can stand in for a standard cell or another macro."""
    from chipleap.chip.merge import MergeError, check_merges

    resolved = run_dir / "resolved.json"
    def_file = run_dir / "final" / "def" / f"{top}.def"
    if not resolved.is_file() or not def_file.is_file():
        raise PnrError("LibreLane's run has no resolved configuration or DEF to check")
    try:
        return check_merges(resolved, work, top, def_file.read_text(errors="replace"))
    except MergeError as exc:
        raise PnrError(f"merged names: {exc}") from exc


def seal_routed(work: Path, with_macros: bool = False) -> Sealed:
    """Record the routed run in ``work`` (``build/pnr``): the configuration, the
    constraints and sources ChipLeap copied beside it (and the macros' views and their
    power grid), and LibreLane's run directory."""
    extra = ["macros", "pdn.tcl", PNR_SDC_NAME] if with_macros else []
    return seal(
        work, PNR_MANIFEST, "pnr", ["config.json", SDC_NAME, "src", *extra, f"runs/{RUN_TAG}"]
    )


def pnr_data(project: Project, r: PnrResult) -> dict[str, Any]:
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
        **r.ihp_antenna_data(),
        **({"pin_markers_in_pdk_macros": r.pin_markers} if r.pin_markers else {}),
        "max_slew_violations": r.max_slew_violations,
        "max_cap_violations": r.max_cap_violations,
        "max_fanout_violations": r.max_fanout_violations,
        "power_mw": round(r.power_mw, 4),
        "gds": project.rel(r.gds) if r.gds else None,
        **(
            {
                "macros": [{"instance": i, "cell": c} for i, c in r.macros],
                "macro_um2": round(r.macro_um2),
            }
            if r.macros
            else {}
        ),
    }


@runs_tools
def pnr(project: Project) -> StepResult:
    """The place-and-route step: passes when setup is met at the slow corner, hold at every
    corner, DRC, LVS and antenna checks are clean, and no pin or net is over its slew or
    capacitance limit at any corner."""
    try:
        r = place_route(project)
    except PnrError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not installed" in msg or "runs on IHP" in msg else "fail"
        return StepResult("pnr", status, msg, {"error": str(exc)})
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    clean = r.checks_clean
    checks = r.checks()
    hold = f"; hold missed by {-r.hold_slack_ns:.3f} ns" if r.hold_slack_ns < 0 else ""
    clk = project.fastest_clock
    clock = f"{r.clock_mhz:g} MHz on {clk.port}" if clk else f"a {r.clock_mhz:g} MHz virtual clock"
    srams = ""
    if r.macros:
        srams = f" and {len(r.macros)} SRAM macro{'s' if len(r.macros) != 1 else ''}"
    msg = (
        f"placed and routed on IHP SG13G2: {r.core_um2 / 1e6:.4f} mm2 core at "
        f"{r.utilisation * 100:.0f}% utilisation, {r.cells} cells{srams}; {clock} "
        f"{'met' if r.setup_slack_ns >= 0 else 'missed'} at the slow corner (about "
        f"{r.fmax_worst_mhz:.0f} MHz, and about {r.fmax_mhz:.0f} MHz at the typical one){hold}; "
        f"{checks}"
    )
    return StepResult("pnr", "pass" if met and clean else "fail", msg, pnr_data(project, r))
