"""Synthesis and place and route of a project with Cadence and Synopsys tools.

The same backends as the generated accelerator's (``chipleap.verify.csynth`` and
``chipleap.verify.cpnr``), given the project's top, sources, include directories,
defines, parameters and clocks instead of the accelerator's. Simulation on Xcelium, VCS
or Questa needs nothing here: the cocotb runner drives them (``chipleap.rtl.sim``).

The project's RTL is copied into the work directory, which ``chipleap.verify.remote``
copies to a design server when ``--remote`` is given; a liberty or LEF on this machine
is copied too, and a path that exists only on the server is used where it is.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from chipleap.hw.tech import get_tech
from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.project import Project, runs_tools
from chipleap.verify import cpnr, csynth, innovus
from chipleap.verify.backends import synth_liberty
from chipleap.verify.pnr import PnrError, PnrResult
from chipleap.verify.remote import Remote, RemoteError, Step, run_steps
from chipleap.verify.result import StepResult
from chipleap.verify.synth import SynthError
from chipleap.verify.tools import which

DEFAULT_PERIOD_NS = 10.0


def stage_rtl(project: Project, workdir: Path) -> tuple[list[str], list[str]]:
    """Copy the sources and include directories under ``workdir/src``; returns their
    names relative to the work directory."""
    src = builddir.work_dir(workdir, "src", empty=True)

    def put(p: Path) -> str:
        rel = project.rel(p)
        if Path(rel).is_absolute():
            raise SynthError(f"{p} is outside the project directory and cannot be staged")
        project.stage(p, src / rel)
        return f"src/{rel}"

    return [put(s) for s in project.sources], [put(d) for d in project.include_dirs]


def _constraints(project: Project, period: float) -> str:
    clocks = project.clocks
    if not clocks:
        return (
            f"create_clock -name vclk -period {float(period):g}\n"
            "set_input_delay 0 -clock vclk [all_inputs]\n"
            "set_output_delay 0 -clock vclk [all_outputs]\n"
        )
    lines = [
        f"create_clock -name {q.tcl(c.port)} -period {float(c.period_ns):g} "
        f"[get_ports {q.tcl(c.port)}]"
        for c in clocks
    ]
    ports = q.tcl_list(c.port for c in clocks)
    ref = q.tcl(clocks[0].port)
    lines += [
        f"set_input_delay 0 -clock {ref} [remove_from_collection [all_inputs] [get_ports {ports}]]",
        f"set_output_delay 0 -clock {ref} [all_outputs]",
    ]
    return "\n".join(lines) + "\n"


def genus_script(
    project: Project,
    files: list[str],
    includes: list[str],
    liberty: str,
    tag: str,
    lefs: tuple[str, ...] = (),
) -> str:
    """Genus on the project: the accelerator's script with the project's design. Every
    name, path and value from the project is one brace-quoted Tcl word."""
    clk = project.fastest_clock
    period = clk.period_ns if clk else DEFAULT_PERIOD_NS
    physical = (
        f"read_physical -lef {q.tcl_list(lefs)}\nset_db interconnect_mode ple\n" if lefs else ""
    )
    search = f"set_db init_hdl_search_path {q.tcl_list(includes)}\n" if includes else ""
    defines = project.define_args()
    define_opt = f" -define {q.tcl_list(defines)}" if defines else ""
    params = " ".join("{" + f"{q.plain(k)} {int(v)}" + "}" for k, v in project.parameters.items())
    param_opt = f" -parameters {{{params}}}" if params else ""
    top = q.plain(project.top)
    tag = q.plain(tag)
    # Elaborating with parameters names the design after them (uart_CLKS_PER_BIT16); the
    # netlist and Innovus expect the top's own name.
    rename = (
        f"set chipleap_design [get_db designs]\n"
        f'if {{[get_db $chipleap_design .name] ne "{top}"}} {{ rename_obj $chipleap_design {top} }}\n'
        if params
        else ""
    )
    return f"""# ChipLeap: Genus synthesis of {top}, run {tag}.
set_db information_level 1
# Registers that reset to 1 need an async-preset flop; libraries with only async-reset
# flops (IHP SG13G2) get one by inverting around a reset flop, as Yosys does.
set_db lbr_seq_in_out_phase_opto true
read_libs {q.tcl(liberty)}
{physical}{search}read_hdl -sv{define_opt} {q.tcl_list(files)}
elaborate {top}{param_opt}
{rename}{_constraints(project, period)}syn_generic
syn_map
syn_opt
report_area > area_{tag}.rpt
report_timing > timing_{tag}.rpt
write_hdl > {top}_{tag}.v
write_sdc > {top}_{tag}.sdc
puts "CHIPLEAP_DONE {tag}"
exit
"""


def dc_script(project: Project, files: list[str], includes: list[str], db: str, tag: str) -> str:
    """Design Compiler on the project, quoted as the Genus script is."""
    clk = project.fastest_clock
    period = clk.period_ns if clk else DEFAULT_PERIOD_NS
    top = q.plain(project.top)
    tag = q.plain(tag)
    search = (
        f"set_app_var search_path [concat $search_path {q.tcl_list(includes)}]\n"
        if includes
        else ""
    )
    defines = project.define_args()
    define_opt = f" -define {q.tcl_list(defines)}" if defines else ""
    params = ",".join(f"{q.plain(k)}={int(v)}" for k, v in project.parameters.items())
    param_opt = f" -parameters {q.tcl(params)}" if params else ""
    # As in Genus: the elaborated design is named after its parameters until renamed.
    rename = f"rename_design [current_design] {top}\n" if params else ""
    return f"""# ChipLeap: Design Compiler synthesis of {top}, run {tag}.
set_app_var target_library {q.tcl(db)}
set_app_var link_library [list * {q.tcl(db)}]
{search}analyze -format sverilog{define_opt} {q.tcl_list(files)}
elaborate {top}{param_opt}
{rename}current_design {top}
link
{_constraints(project, period)}compile
report_area > area_{tag}.rpt
report_timing > timing_{tag}.rpt
write -format verilog -hierarchy -output {top}_{tag}.v
write_sdc {top}_{tag}.sdc
puts "CHIPLEAP_DONE {tag}"
exit
"""


def _lefs(tech_key: str, lib: str) -> tuple[str, ...]:
    tech = get_tech(tech_key)
    return tuple(tech.lef) or (innovus.ihp_lefs(Path(lib)) if tech_key == "ihp-sg13g2" else ())


@runs_tools
def synthesize_with(
    project: Project, tool: str, workdir: Path, remote: Remote | None = None
) -> dict[str, Any]:
    """Genus or Design Compiler on the project at its fastest clock, in ``workdir``, a
    directory in the project's ``build/`` (``Project.work_dir``)."""
    st = csynth.get_synth_tool(tool)
    if remote is None and which(st.binary) is None:
        raise SynthError(
            f"{st.vendor} {st.name} ({st.binary}) is not on the path; run it on a server "
            "with --remote HOST"
        )
    tech = get_tech(project.tech)
    lib, host = synth_liberty(project.tech)
    if host and (remote is None or remote.host != host):
        raise SynthError(f"the {project.tech} liberty is on {host}; run with --remote {host}")
    workdir = project.work_dir(workdir)
    for old in [*workdir.glob("area_*.rpt"), *workdir.glob("timing_*.rpt")]:
        old.unlink()
    files, includes = stage_rtl(project, workdir)
    lib_ref = csynth.stage(lib, workdir, remote)
    tag = "syn"
    steps: list[Step] = []
    if tool == "genus":
        lefs = tuple(csynth.stage(p, workdir, remote) for p in _lefs(project.tech, lib))
        (workdir / f"run_{tag}.tcl").write_text(
            genus_script(project, files, includes, lib_ref, tag, lefs)
        )
        steps.append(
            Step(
                ("genus", "-batch", "-no_gui", "-files", f"run_{tag}.tcl", "-log", f"genus_{tag}"),
                f"genus_{tag}.out",
            )
        )
    else:
        db = csynth.stage(tech.db, workdir, remote) if tech.db else ""
        if not db:
            db = Path(lib_ref).with_suffix(".db").name
            (workdir / "compile_lib.tcl").write_text(csynth.lc_script(lib_ref, db))
            steps.append(Step(("lc_shell", "-f", "compile_lib.tcl"), "lc.log"))
        (workdir / f"run_{tag}.tcl").write_text(dc_script(project, files, includes, db, tag))
        steps.append(Step(("dc_shell", "-f", f"run_{tag}.tcl"), f"dc_{tag}.out"))
    timeout = float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "7200"))
    try:
        out = run_steps(steps, workdir, remote, timeout)
    except RemoteError as exc:
        raise SynthError(str(exc)) from exc
    area_rpt = workdir / f"area_{tag}.rpt"
    if not area_rpt.is_file():
        logs = sorted(workdir.glob(f"*{tag}.out"))
        tail = logs[0].read_text(errors="replace")[-1500:] if logs else out.stdout[-1500:]
        raise SynthError(f"{st.name} produced no area report:\n{tail}")
    area, cells = csynth.parse_area(tool, area_rpt.read_text(errors="replace"), project.top)
    slack = csynth.parse_slack(tool, (workdir / f"timing_{tag}.rpt").read_text(errors="replace"))
    clk = project.fastest_clock
    period = clk.period_ns if clk else DEFAULT_PERIOD_NS
    delay = period - slack
    return {
        "tool": st.name,
        "technology": project.tech,
        "clock_mhz": round(1000 / period, 3),
        "area_um2": round(area, 2),
        "cells": cells,
        "slack_ns": round(slack, 3),
        "critical_path_ns": round(delay, 3),
        "fmax_mhz_estimate": round(1000 / delay, 1) if delay > 0 else 0.0,
        "netlist": project.rel(workdir / f"{project.top}_{tag}.v"),
    }


@runs_tools
def synth_step(project: Project, tool: str, remote: Remote | None = None) -> StepResult:
    try:
        d = synthesize_with(project, tool, project.build_dir / tool, remote)
    except SynthError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not on the path" in msg else "fail"
        return StepResult("synth", status, msg, {"error": str(exc)})
    met = "met" if d["slack_ns"] >= 0 else "missed"
    msg = (
        f"{d['tool']}: {d['area_um2'] / 1e6:.4f} mm2 of standard cells on {project.tech} "
        f"({d['cells']} cells); critical path {d['critical_path_ns']:.2f} ns, "
        f"{d['clock_mhz']:g} MHz {met}"
    )
    return StepResult("synth", "pass", msg, d)


@runs_tools
def place_route_with(project: Project, workdir: Path, remote: Remote | None = None) -> PnrResult:
    """Genus then Innovus on the project, here or on ``remote``, with the project's
    utilisation, placement density and die size, as LibreLane gets them. ``workdir`` is a
    directory in the project's ``build/`` (``Project.work_dir``)."""
    if remote is None and (which("genus") is None or which("innovus") is None):
        raise PnrError(
            "Cadence Genus and Innovus are not on the path; run them on a server with --remote HOST"
        )
    tech = get_tech(project.tech)
    try:
        lib, host = synth_liberty(project.tech)
    except SynthError as exc:
        raise PnrError(str(exc)) from exc
    if host and (remote is None or remote.host != host):
        raise PnrError(f"the {project.tech} files are on {host}; run with --remote {host}")
    lefs = _lefs(project.tech, lib)
    if not lefs:
        raise PnrError(
            f"technology {project.tech!r} names no LEF files; add them with tech add --lef"
        )
    workdir = project.work_dir(workdir)
    for old in ("chipleap_pnr.txt", "innovus.out", "genus.out"):
        (workdir / old).unlink(missing_ok=True)
    try:
        files, includes = stage_rtl(project, workdir)
        typ = csynth.stage(lib, workdir, remote)
        slow_src = cpnr.slow_liberty(tech, lib)
        slow = csynth.stage(slow_src, workdir, remote) if slow_src else ""
        phys = tuple(csynth.stage(p, workdir, remote) for p in lefs)
    except Exception as exc:
        raise PnrError(str(exc)) from exc
    tag = "pnr"
    top = q.plain(project.top)
    (workdir / "synth.tcl").write_text(genus_script(project, files, includes, typ, tag, phys))
    (workdir / "mmmc.tcl").write_text(cpnr.mmmc(typ, slow, f"{top}_{tag}.sdc"))
    (workdir / "pnr.tcl").write_text(
        cpnr.script(
            f"{top}_{tag}.v",
            phys,
            node_nm=tech.node_nm,
            utilisation=project.pnr.utilisation / 100,
            slow=bool(slow),
            top=top,
            density=project.pnr.density / 100,
            die_um=project.pnr.die_um,
        )
    )
    steps = [
        Step(("genus", "-batch", "-no_gui", "-files", "synth.tcl", "-log", "genus"), "genus.out"),
        Step(
            ("innovus", "-batch", "-no_gui", "-files", "pnr.tcl", "-log", "innovus"), "innovus.out"
        ),
    ]
    timeout = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "14400"))
    try:
        run_steps(steps, workdir, remote, timeout)
    except RemoteError as exc:
        raise PnrError(str(exc)) from exc
    clk = project.fastest_clock
    return cpnr.result(workdir, clk.mhz if clk else 1000 / DEFAULT_PERIOD_NS, bool(slow))


@runs_tools
def pnr_step(project: Project, remote: Remote | None = None) -> StepResult:
    try:
        r = place_route_with(project, project.build_dir / "innovus", remote)
    except PnrError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not on the path" in msg else "fail"
        return StepResult("pnr", status, f"Innovus: {msg}", {"error": str(exc)})
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    clean = r.clean and r.antenna_nets == 0
    checks = (
        "DRC, connectivity and antenna clean"
        if clean
        else f"{r.drc_errors} DRC, {r.lvs_errors} connectivity and {r.antenna_nets} antenna violations"
    )
    data = {
        "tool": "Innovus",
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
        "connectivity_errors": r.lvs_errors,  # Innovus checks connectivity, not LVS
        "antenna_nets": r.antenna_nets,
    }
    corner = "slow" if r.setup_slack_ns != r.setup_slack_typ_ns else "typical"
    msg = (
        f"Innovus: placed and routed on {project.tech}: {r.core_um2 / 1e6:.4f} mm2 core at "
        f"{r.utilisation * 100:.0f}% utilisation, {r.cells} cells; {r.clock_mhz:g} MHz "
        f"{'met' if met else 'missed'} at the {corner} corner (about {r.fmax_worst_mhz:.0f} MHz); "
        f"{checks}"
    )
    return StepResult("pnr", "pass" if met and clean else "fail", msg, data)
