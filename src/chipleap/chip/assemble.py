"""``chipleap verify PROJECT --steps chip``: a project's RTL, its macros and a pad ring
become one chip GDS, checked.

In order: the core's ports (Yosys), the macro packages (copied, checked, converted), the
nets, the floorplan, the layout (LibreLane), the finishing (bond pads, seal ring, PR
boundary, then IHP's fill), LVS on the chip without its IO cells, IHP's DRC on the whole
finished chip, the supply hookup from the DEF, the GDS precheck and the bond map. The
results go to ``build/chip``: ``<name>.gds``, ``<name>.bondmap.csv`` and ``.json``,
``chip.json``, and each tool's log beside its inputs.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.chip import checks, floorplan, flow, io, iotiming, merge, netlist, pdn
from chipleap.chip.checks import CheckError
from chipleap.chip.config import ChipConfig
from chipleap.chip.floorplan import Floorplan, FloorplanError
from chipleap.chip.io import IoError
from chipleap.chip.iotiming import IoTimingError
from chipleap.chip.merge import MergeError
from chipleap.chip.netlist import NetlistError
from chipleap.chip.pdn import PdnError
from chipleap.chip.run import ToolError, klayout, plain, private_dir, python_with_klayout, run
from chipleap.macro import layer
from chipleap.macro.layer import MacroImportError
from chipleap.macro.manifest import MANIFEST, MacroError, Manifest, parse_toml
from chipleap.rtl import builddir
from chipleap.rtl.openroad import OpenroadError
from chipleap.rtl.pnr import verilog_config
from chipleap.rtl.project import Project, runs_tools
from chipleap.rtl.synth import DEFAULT_PERIOD_NS
from chipleap.verify.pnr import PnrError, parse
from chipleap.verify.result import StepResult
from chipleap.verify.tools import librelane, openroad, yosys

SCRIPTS = Path(__file__).resolve().parent / "klayout"
BOND_SIZE = "70u"  # IHP's bondpad pcell, square
SEAL_EDGE_UM = 25  # the seal ring pcell's edge box on each side, outside its w and l
ERRORS = (
    IoTimingError,
    OpenroadError,
    MacroImportError,
    MergeError,
    NetlistError,
    FloorplanError,
    PdnError,
    IoError,
    CheckError,
    ToolError,
    PnrError,
    MacroError,
)


REQUIRED_METRICS = (
    "route__drc_errors",
    "timing__setup__ws",
    "timing__hold__ws",
    "antenna__violating__nets",
)


class ChipError(RuntimeError):
    pass


def check_metrics(metrics: dict[str, Any]) -> None:
    """Refuse LibreLane metrics without the numbers the verdict reads: one missing would
    read as zero, which is a pass."""
    missing = [
        k
        for k in REQUIRED_METRICS
        if isinstance(metrics.get(k), bool) or not isinstance(metrics.get(k), (int, float))
    ]
    if missing:
        raise PnrError(f"LibreLane's metrics have no {', '.join(missing)}")


def timeout() -> float:
    return float(os.environ.get("CHIPLEAP_CHIP_TIMEOUT", "10800"))


def _cfg(project: Project) -> ChipConfig:
    if project.chip is None:
        raise ChipError("the project has no chip section")
    return project.chip


# The floorplan alone


def manifest_sizes(project: Project) -> dict[str, tuple[str, float, float]]:
    """Each macro instance's cell and size from its package's manifest, read without
    checking the rest of the package (``plan_chip`` runs no tool)."""
    out: dict[str, tuple[str, float, float]] = {}
    for m in _cfg(project).macros:
        try:
            raw = project.read(m.package / MANIFEST).decode("utf-8")
            man = Manifest.from_dict(parse_toml(raw))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise MacroError(f"macro {m.instance}: cannot read {MANIFEST}: {exc}") from None
        out[m.instance] = (man.name, man.width_um, man.height_um)
    return out


def plan_chip(project: Project) -> dict[str, Any]:
    """The chip's floorplan from the project, its packages' manifests and the IO
    library's LEF; no tool runs, so a signal pad's cell (input or output) is not known
    yet."""
    chip = _cfg(project)
    pdk = io.pdk_dir()
    if pdk is None:
        raise ChipError(missing_pdk())
    cells = io.read_lef(io.lef_path(pdk).read_text())
    plan = floorplan.plan(chip, cells, manifest_sizes(project))
    return {"name": chip.name, **plan.to_dict()}


def missing_pdk() -> str:
    return (
        "IHP SG13G2's IO library (libs.ref/sg13g2_io) and KLayout technology are not "
        f"installed: set {io.PDK_ENV} to the ihp-sg13g2 directory of an IHP-Open-PDK checkout"
    )


# The chip


@dataclass
class Prepared:
    work: Path
    flow_dir: Path
    pdk: Path
    cells: dict[str, io.IoCell]
    imported: layer.Imported
    binding: netlist.Binding
    plan: Floorplan
    cfg: dict[str, Any]
    pdk_spec: str


def import_macros(project: Project, pdk: Path, spec: str, flow_dir: Path) -> layer.Imported:
    """The chip's macro packages, copied, checked against ``spec`` with the libraries of
    ``pdk`` and converted. Without the libraries the step stops (MacroImportError): a
    package's standard cells could not be shown to be the PDK's own. After LibreLane
    runs, ``chipleap.chip.merge`` compares them again with the standard cells it
    merged, under its own PDK_ROOT."""
    chip = _cfg(project)
    libs = layer.pdk_libs(pdk)
    return layer.import_packages(
        [(m.instance, m.package) for m in chip.macros],
        project.files_in,
        project.read,
        flow_dir,
        spec,
        layer.reserved_names(pdk, (chip.name, project.top)),
        libs=libs,
    )


def prepare(project: Project, pdk: Path) -> Prepared:
    """Everything before LibreLane runs: the ports, the macros, the nets, the floorplan
    and the files of the flow."""
    chip = _cfg(project)
    work = project.work_dir("chip", empty=True)
    cells = io.read_lef(io.lef_path(pdk).read_text())
    commit = io.pdk_commit(pdk)
    spec = f"ihp-sg13g2@{commit}" if commit else "ihp-sg13g2"
    flow_dir = builddir.work_dir(work, "flow")
    imported = import_macros(project, pdk, spec, flow_dir)
    ports_dir = builddir.work_dir(work, "ports")
    ports = netlist.core_ports(project, ports_dir)
    verilog = verilog_config(project, flow_dir)
    clocks = {c.port: c.period_ns for c in project.clocks}
    binding = netlist.bind(chip, ports, imported.views, imported.cells, clocks, project.top)
    sizes = {
        inst: (cell, imported.views[cell].width, imported.views[cell].height)
        for inst, cell in imported.cells.items()
    }
    plan = floorplan.plan(chip, cells, sizes, binding.directions)
    if any(not c.cell for c in plan.ring):
        raise NetlistError("a pad has no IO cell")
    merge.check_modules(
        netlist.core_modules(ports_dir),
        chip.name,
        sorted({c.cell for c in plan.ring}),
        imported.views,
    )
    flow.write_io(flow_dir, pdk, cells, sorted({c.cell for c in plan.ring}))
    pad = cells[io.PAD_CELLS["in"]]
    if f"dir::src/{chip.name}.v" in verilog.get("VERILOG_FILES", []):
        raise NetlistError(f"a source file is named {chip.name}.v, like the chip top; rename it")
    builddir.write_text(
        flow_dir,
        f"src/{chip.name}.v",
        netlist.chip_top(chip, binding, plan, project.top, project.parameters),
    )
    builddir.write_text(flow_dir, "chip.sdc", netlist.sdc(binding, clocks))
    builddir.write_text(
        flow_dir, "pins.def", netlist.pins_def(chip, binding, plan, (pad.width, pad.height))
    )
    builddir.write_text(flow_dir, "macro_placement.cfg", netlist.placement(plan))
    placed = {m.inst: (m, imported.views[m.cell]) for m in plan.macros}
    builddir.write_text(flow_dir, "pdn.tcl", pdn.script(placed))
    clk = project.fastest_clock
    cfg = flow.config(
        chip,
        plan,
        binding,
        imported,
        verilog,
        clk.period_ns if clk else DEFAULT_PERIOD_NS,
        flow_dir,
    )
    return Prepared(work, flow_dir, pdk, cells, imported, binding, plan, cfg, spec)


def run_script(script: str, plan_file: Path, cwd: Path, read: list[Path], log: str) -> None:
    """Run one of ChipLeap's KLayout scripts (klayout/) on the plan in ``plan_file``."""
    tool = klayout()
    if tool.path is None:
        raise ToolError(tool.missing("KLayout"))
    cmd = [tool.path, "-b", "-r", str(SCRIPTS / script), "-rd", f"plan={plan_file}"]
    code, tail = run(cmd, cwd, [SCRIPTS, *read], cwd, log, timeout())
    if code != 0:
        raise ToolError(f"KLayout's {script} failed (exit {code}): {_last(tail)}")


def _last(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:300] if lines else "no output"


def finish(p: Prepared, gds: Path, name: str) -> tuple[Path, Path]:
    """Bond pads, seal ring and the PR boundary on the die (when LibreLane's layout has
    none), then IHP's fill: the chip's GDS before and after fill."""
    fin = builddir.work_dir(p.work, "finish", empty=True)
    s = p.plan.seal
    height, width = s[3] - s[1], s[2] - s[0]
    nofill = fin / f"{name}_nofill.gds"
    spec = {
        "input": str(gds),
        "output": str(nofill),
        "top": name,
        "bond_size": BOND_SIZE,
        "bond_centers": [list(x.bond_center) for x in p.plan.pads],
        # The pcell's w is the ring's height and l its width, without its edge boxes.
        "seal_w": f"{height - 2 * SEAL_EDGE_UM:g}u",
        "seal_l": f"{width - 2 * SEAL_EDGE_UM:g}u",
        "seal_origin": [s[0], s[1]],
        "boundary": [0.0, 0.0, *p.plan.die],
        "pdk_python": str(p.pdk / "libs.tech" / "klayout" / "python"),
    }
    plan_file = builddir.write_text(fin, "finish.json", checks.dump(spec))
    run_script("finish.py", plan_file, fin, [p.pdk, gds.parent], "finish.log")
    final = p.work / f"{name}.gds"
    tool = klayout()
    assert tool.path is not None  # _klayout found it
    filler = p.pdk / "libs.tech" / "klayout" / "tech" / "scripts" / "filler.py"
    cmd = [tool.path, "-n", "sg13g2", "-zz", "-r", str(filler), "-rd", f"output_file={final}"]
    env = {
        "PDK_ROOT": str(p.pdk.parent),
        "PDK": p.pdk.name,
        "KLAYOUT_PATH": str(p.pdk / "libs.tech" / "klayout"),
    }
    code, tail = run(
        [*cmd, str(nofill)], fin, [p.pdk, fin, p.work], fin, "fill.log", timeout(), env
    )
    if code != 0 or not final.is_file():
        raise ToolError(f"IHP's fill failed (exit {code}): {_last(tail)}")
    return nofill, final


def lvs(p: Prepared, nofill: Path, outs: dict[str, Path], name: str) -> dict[str, Any]:
    """IHP's LVS deck on the chip without its IO cells."""
    lvs_dir = builddir.work_dir(p.work, "lvs", empty=True)
    io_cdl = (p.flow_dir / "io" / f"{io.IO}.cdl").read_text()
    extra = [flow.std_cell_cdl(outs["resolved"]).read_text()]
    extra += [v.spice.read_text() for v in p.imported.views.values()]
    inputs = checks.lvs_inputs(outs["cdl"].read_text(), io_cdl, p.plan, p.cells, extra)
    netlist_file = builddir.write_text(lvs_dir, f"{name}_lvs.cdl", inputs.netlist)
    view = lvs_dir / f"{name}_lvs.gds"
    plan_file = builddir.write_text(
        lvs_dir, "lvs_view.json", checks.dump(checks.lvs_plan(p.plan, name, inputs, nofill, view))
    )
    run_script("lvs_view.py", plan_file, lvs_dir, [nofill.parent], "lvs_view.log")
    log = run_lvs(p.pdk, lvs_dir, view, netlist_file, name)
    outcome = checks.lvs_outcome(log)
    return {
        "result": outcome,
        "ports": len(inputs.ports),
        "instances": inputs.instances,
        "view": _rel(p, view),
        "netlist": _rel(p, netlist_file),
    }


def _rel(p: Prepared, path: Path) -> str:
    try:
        return path.resolve().relative_to(p.work.resolve()).as_posix()
    except ValueError:
        return path.name


def run_lvs(pdk: Path, out_dir: Path, view: Path, netlist_file: Path, name: str) -> str:
    """IHP's run_lvs.py on ``view`` against ``netlist_file``; its log, which says whether
    the netlists match (``checks.lvs_outcome``). A run that compared nothing, or whose
    exit code disagrees with a match, is an error, never a result."""
    code, log = _ihp_runner(
        pdk,
        "lvs",
        out_dir,
        {"layout.gds": view, "netlist.cdl": netlist_file},
        lambda d: [
            "--layout=layout.gds",
            "--netlist=netlist.cdl",
            f"--topcell={name}",
            f"--run_dir={d}",
            "--run_mode=deep",
        ],
        ("layout.lvsdb", "layout_extracted.cir", "layout.log"),
    )
    outcome = checks.lvs_outcome(log)
    if outcome == "unknown" or (outcome == "pass" and code != 0):
        raise ToolError(f"IHP's LVS did not compare the netlists (exit {code}): {_last(log)}")
    return log


def _ihp_runner(
    pdk: Path,
    which: str,
    out_dir: Path,
    inputs: dict[str, Path],
    args: Any,
    keep: tuple[str, ...],
) -> tuple[int, str]:
    """Run IHP's run_drc.py or run_lvs.py in a private directory on copies of
    ``inputs``; copy back the files named in ``keep``, every report (.lyrdb) and every
    log. Returns the exit code and the runner's output with the logs named in ``keep``.
    A report too large to read is an error: it would be the one with the most in it."""
    found = python_with_klayout()
    if found is None:
        raise ToolError(
            "IHP's DRC and LVS runners need a Python with KLayout's module (pip install "
            "klayout); set CHIPLEAP_KLAYOUT_PYTHON to one"
        )
    python, module_dir = found
    runner = pdk / "libs.tech" / "klayout" / "tech" / which / f"run_{which}.py"
    plain(runner, "IHP's KLayout decks")
    with private_dir(f"chipleap-{which}-") as tmp:
        for dest, src in inputs.items():
            shutil.copyfile(src, tmp / dest)
        run_dir = tmp / "run"
        # The runners import klayout.db; with HOME private, Python would not look in
        # the user's site directory, where pip puts it.
        # run_drc.py runs its tables in parallel with Python's multiprocessing, whose
        # semaphores are files in /dev/shm.
        shm = [Path("/dev/shm")] if which == "drc" and Path("/dev/shm").is_dir() else []
        code, tail = run(
            [python, str(runner), *args(run_dir)],
            tmp,
            [pdk, Path(module_dir), *shm],
            out_dir,
            f"{which}.log",
            timeout(),
            {"PYTHONPATH": module_dir},
        )
        log = tail
        if run_dir.is_dir():
            for f in sorted(run_dir.iterdir()):
                wanted = f.name in keep or f.suffix in (".lyrdb", ".log")
                if not f.is_file() or not wanted:
                    continue
                if f.stat().st_size > checks.DB_MAX:
                    raise ToolError(
                        f"IHP's {which.upper()} wrote {f.name}, larger than ChipLeap reads"
                    )
                builddir.write_bytes(out_dir, f.name, f.read_bytes())
                if f.name in keep and f.suffix == ".log":
                    log += "\n" + f.read_text(errors="replace")
    return code, log


# The DRC tables a run must have reported: the main deck's tables, merged into one
# database, and the decks run beside them unless switched off.
DRC_LOGS = ("density", "sg13g2_maximal")


def drc(p: Prepared, final: Path, name: str) -> checks.DrcResult:
    """IHP's DRC deck on the finished chip: every table, density and the extra rules
    included, recommended rules left out (bond pads against their IO cells break
    Pad.d1R and Pad.fR_* by design, as on IHP's own reference chips)."""
    drc_dir = builddir.work_dir(p.work, "drc", empty=True)
    workers = max(1, min(4, (os.cpu_count() or 2) // 4))
    code, log = _ihp_runner(
        p.pdk,
        "drc",
        drc_dir,
        {"chip.gds": final},
        lambda d: [
            "--path=chip.gds",
            f"--topcell={name}",
            f"--run_dir={d}",
            f"--mp={workers}",
            "--run_mode=deep",
            "--no_recommended",
        ],
        (),
    )
    return drc_result(drc_dir, name, code, log)


def drc_result(drc_dir: Path, name: str, code: int, log: str) -> checks.DrcResult:
    """The violations of a DRC run, after checking it ran: run_drc.py exits 0 when clean
    and 1 with violations, and merges the tables into one report; every expected table's
    log must be there and the code must agree with the report."""
    report = drc_dir / f"chip_{name}_full.lyrdb"
    missing = [t for t in DRC_LOGS if not (drc_dir / f"chip_{name}_{t}.log").is_file()]
    if code not in (0, 1) or not report.is_file() or missing:
        what = f"no {', '.join(missing)} table" if missing else f"exit {code}"
        raise ToolError(f"IHP's DRC did not finish ({what}): {_last(log)}")
    result = checks.DrcResult()
    checks.read_lyrdb(report, result)
    if (code == 1) != (result.total > 0):
        raise ToolError(
            f"IHP's DRC exited {code} with {result.total} violations in its report; one of "
            "them is wrong"
        )
    return result


def precheck(p: Prepared, final: Path, name: str) -> list[dict[str, Any]]:
    pre = builddir.work_dir(p.work, "precheck", empty=True)
    out = pre / "facts.json"
    plan_file = builddir.write_text(
        pre, "precheck.json", checks.dump({"input": str(final), "output": str(out)})
    )
    run_script("precheck.py", plan_file, pre, [final.parent], "precheck.log")
    facts = json.loads(out.read_text())
    lyp = (p.pdk / "libs.tech" / "klayout" / "tech" / "sg13g2.lyp").read_text()
    die = (0.0, 0.0, *p.plan.die)
    return checks.precheck(facts, name, checks.lyp_layers(lyp), die, p.plan.seal)


def assemble(project: Project, pdk: Path) -> dict[str, Any]:
    """Build and check the chip; its results as data."""
    chip = _cfg(project)
    name = chip.name
    p = prepare(project, pdk)
    metrics, run_dir = flow.run(p.flow_dir, p.cfg)
    outs = flow.outputs(run_dir, name)
    # What LibreLane merged, before anything it made is used.
    merged = merge.check_merges(outs["resolved"], p.flow_dir, name, outs["def"].read_text())
    clk = project.fastest_clock
    check_metrics(metrics)
    r = parse(metrics, clk.mhz if clk else 1000.0 / DEFAULT_PERIOD_NS, outs["gds"])
    io_timing = iotiming.run(
        p.work,
        run_dir,
        p.pdk,
        name,
        p.binding,
        {s.cell.inst: s.cell.cell for s in p.plan.pads if s.pad.kind == "signal"},
        {c.port: c.period_ns for c in project.clocks},
        chip.io,
        [(v.lef, v.lib) for v in p.imported.views.values()],
        p.flow_dir / "io" / f"{io.IO}_chip.lef",
    )
    nofill, final = finish(p, outs["gds"], name)
    lvs_data = lvs(p, nofill, outs, name)
    drc_result = drc(p, final, name)
    hookup = checks.supply_hookup(outs["def"].read_text(), p.plan)
    pre = precheck(p, final, name)
    csv_text, rows = checks.bond_map(p.plan, float(BOND_SIZE.rstrip("u")))
    builddir.write_text(p.work, f"{name}.bondmap.csv", csv_text)
    builddir.write_text(p.work, f"{name}.bondmap.json", checks.dump(rows))
    data: dict[str, Any] = {
        "tool": "LibreLane, OpenSTA, KLayout and IHP's DRC and LVS decks",
        "name": name,
        "pdk": p.pdk_spec,
        "die_um": list(p.plan.die),
        "outline_um": list(p.plan.seal),
        "core_um": list(p.plan.core),
        "pads": len(p.plan.pads),
        "macros": [
            {
                "instance": m.inst,
                "cell": m.cell,
                "at_um": [m.x, m.y],
                "orientation": m.orient,
                "warnings": p.imported.views[m.cell].warnings,
            }
            for m in p.plan.macros
        ],
        "warnings": p.binding.warnings,
        "merged_names": merged,
        "route_drc_errors": r.drc_errors,
        "antenna_nets": r.antenna_nets,
        # LibreLane's, at the core's side of the pads
        "setup_slack_ns": round(r.setup_slack_ns, 3),
        "hold_slack_ns": round(r.hold_slack_ns, 3),
        # ChipLeap's, at the chip's ports through the pads
        "io_timing": io_timing.to_dict(),
        "cells": r.cells,
        "lvs": lvs_data,
        "drc": {
            "violations": drc_result.total,
            "rules": dict(sorted(drc_result.violations.items())),
            "recommended_rules": "not checked",
        },
        "supply_hookup": hookup,
        "precheck": pre,
        "gds": f"build/chip/{name}.gds",
        "bond_map": f"build/chip/{name}.bondmap.csv",
    }
    builddir.write_text(p.work, "chip.json", checks.dump(data))
    return data


def verdict(data: dict[str, Any]) -> tuple[bool, str]:
    """Whether the chip passes, and a one-line summary."""
    problems = []
    if data["route_drc_errors"]:
        problems.append(f"{data['route_drc_errors']} routing DRC errors")
    if data["antenna_nets"]:
        problems.append(f"{data['antenna_nets']} nets with antenna violations")
    if data["lvs"]["result"] != "pass":
        problems.append(f"LVS {data['lvs']['result']}")
    if data["drc"]["violations"]:
        rules = ", ".join(list(data["drc"]["rules"])[:3])
        problems.append(f"{data['drc']['violations']} DRC violations ({rules})")
    for net in ("VDD", "VSS"):
        if not sum(data["supply_hookup"].get(net, {}).values()):
            problems.append(f"{net} does not reach the pad ring")
    failed = [c["check"] for c in data["precheck"] if not c["pass"]]
    if failed:
        problems.append(f"precheck failed: {', '.join(failed)}")
    if data["setup_slack_ns"] < 0 or data["hold_slack_ns"] < 0:
        problems.append(
            f"timing missed (setup {data['setup_slack_ns']:g} ns, hold {data['hold_slack_ns']:g} ns)"
        )
    io_t = data.get("io_timing")
    if io_t is not None:
        for c in io_t["corners"]:
            for check in ("setup", "hold"):
                v = c[f"{check}_slack_ns"]
                if v is not None and v < 0:
                    problems.append(
                        f"{check} missed at the ports at the {c['corner']} corner ({v:g} ns)"
                    )
        problems += [f"not timed: {u}" for u in io_t["untimed_pads"]]
    w, h = (
        data["outline_um"][2] - data["outline_um"][0],
        data["outline_um"][3] - data["outline_um"][1],
    )
    head = (
        f"{data['name']}: {w / 1000:.2f} x {h / 1000:.2f} mm, {data['pads']} pads, "
        f"{len(data['macros'])} macro{'' if len(data['macros']) == 1 else 's'}"
    )
    if problems:
        return False, f"{head}; " + "; ".join(problems)
    timing = ""
    if io_t is not None:
        slow = next((c for c in io_t["corners"] if c["corner"] == "slow"), None)
        if slow is not None and slow["setup_slack_ns"] is not None:
            timing = f", timing met through the pads ({slow['setup_slack_ns']:+.3f} ns at the slow corner)"
    return True, (
        f"{head}; LVS clean on IHP's deck, DRC clean on IHP's deck (density included, "
        f"recommended rules not checked), supplies reach the ring, precheck passed{timing}"
    )


@runs_tools
def chip(project: Project) -> StepResult:
    """The chip step: skipped without a chip section or its tools, failed on any error."""
    if project.chip is None:
        return StepResult("chip", "skipped", "the project has no chip section")
    if project.tech != "ihp-sg13g2":
        return StepResult("chip", "skipped", f"chips are built on IHP SG13G2, not {project.tech}")
    pdk = io.pdk_dir()
    if pdk is None:
        return StepResult("chip", "skipped", missing_pdk())
    for label, tool in (
        ("Yosys", yosys()),
        ("LibreLane", librelane()),
        ("KLayout", klayout()),
        ("OpenROAD", openroad()),
    ):
        if not tool.found:
            return StepResult("chip", "skipped", tool.missing(label))
    if python_with_klayout() is None:
        return StepResult(
            "chip",
            "skipped",
            "IHP's DRC and LVS runners need a Python with KLayout's module (pip install klayout)",
        )
    try:
        data = assemble(project, pdk)
    except ERRORS as exc:
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return StepResult("chip", "fail", msg, {"error": str(exc)})
    ok, summary = verdict(data)
    return StepResult("chip", "pass" if ok else "fail", summary, data)
