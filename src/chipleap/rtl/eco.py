"""Engineering change orders on the routed design: small fixes and a new signoff, without
running synthesis, floorplanning, placement or clock-tree synthesis again.

The base is the routed run's state just before detailed routing: placed, with its clock
tree, globally routed and repaired. OpenROAD reads that DEF and applies the changes with
the database API: a ``resize`` swaps an instance's cell for one with the same pins, a
``buffer`` puts a new buffer after a driver's output or in front of a sink's input. The
edited DEF goes back to LibreLane's own OpenROAD through ``OpenROAD.DEFtoODB``, and
LibreLane, resumed from that state, legalizes the placement, routes globally (and, for
a ``repair`` change, repairs timing again with the margin asked for), then runs exactly
the steps the original run ran from detailed routing on: routing, fill, extraction,
timing at every corner, DRC, LVS and antenna checks.

Changes always apply to the original routed run, as a whole list, so an ECO result is a
function of the base and its changes, and the project's ``eco`` section reproduces it.
The changes reach OpenROAD as a data file it reads line by line; no name from a project
or an MCP client becomes script text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import quote as q
from chipleap.rtl.openroad import (
    ECO_MARKER,
    ECO_TAG,
    RUN_TAG,
    OpenroadError,
    OpenroadUnavailable,
    base_digest,
    failure,
    in_run,
    pdk_views,
    read_json,
    routed_views,
    run_dir,
    run_openroad,
    stage,
)
from chipleap.rtl.pnr import margins, pnr_data
from chipleap.rtl.project import CORNERS, EcoChange, Project
from chipleap.verify import launch
from chipleap.verify.pnr import PnrError, PnrResult, parse
from chipleap.verify.result import StepResult
from chipleap.verify.tools import librelane

RESUME_AT = "OpenROAD.DetailedRouting"
# Before routing again: the edited DEF into LibreLane's database, legal placement, and
# global routing for the nets that changed.
PREFIX = ("OpenROAD.DEFtoODB", "OpenROAD.DetailedPlacement", "OpenROAD.GlobalRouting")
REPAIR = "OpenROAD.ResizerTimingPostGRT"
PATHS_SHOWN = 20


class EcoError(RuntimeError):
    pass


@dataclass
class EcoResult:
    changes: list[dict[str, Any]]  # each change, with what it did
    before: PnrResult
    after: PnrResult
    run: Path
    log: str = ""


@dataclass
class Stage:
    pin: str
    cell: str
    instance: str
    edge: str  # ^ rising, v falling
    delay_ns: float
    time_ns: float


@dataclass
class TimingPath:
    startpoint: str
    endpoint: str
    group: str
    kind: str  # max (setup) | min (hold)
    slack_ns: float
    met: bool
    stages: list[Stage] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "startpoint": self.startpoint,
            "endpoint": self.endpoint,
            "group": self.group,
            "check": "setup" if self.kind == "max" else "hold",
            "slack_ns": self.slack_ns,
            "met": self.met,
            "stages": [
                {
                    "pin": s.pin,
                    "instance": s.instance,
                    "cell": s.cell,
                    "edge": "rise" if s.edge == "^" else "fall",
                    "delay_ns": s.delay_ns,
                    "time_ns": s.time_ns,
                }
                for s in self.stages
            ],
        }


def _steps(run: Path) -> list[tuple[str, Path]]:
    """The steps a LibreLane run ran, in order: each step directory's id."""
    out = []
    dirs = [d for d in run.iterdir() if d.is_dir() and re.match(r"\d+-", d.name)]
    for d in sorted(dirs, key=lambda d: int(d.name.split("-", 1)[0])):
        cfg = d / "config.json"
        if not cfg.is_file():
            continue
        meta = read_json(cfg).get("meta") or {}
        step = meta.get("step") if isinstance(meta, dict) else None
        if isinstance(step, str):
            out.append((step, d))
    return out


def _confined_state(state: Any, run: Path) -> dict[str, Any]:
    """A step's state with every file it names checked to be inside the run."""
    if not isinstance(state, dict):
        raise EcoError("the run's state is not a mapping")

    def check(value: Any, key: str) -> None:
        if isinstance(value, str):
            in_run(value, run, f"state {key}")
        elif isinstance(value, dict):
            for k, v in value.items():
                check(v, f"{key}.{k}")
        elif isinstance(value, list):
            for v in value:
                check(v, key)
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise EcoError(f"state {key} is not a path")

    for key, value in state.items():
        if key != "metrics":
            check(value, key)
    return state


def base(project: Project) -> tuple[Path, dict[str, Any], list[str]]:
    """The routed run, its state before detailed routing, and the steps it ran from there."""
    run = run_dir(project, RUN_TAG)
    if not (run / "final" / "metrics.json").is_file():
        raise OpenroadUnavailable("no routed design: run the pnr step first")
    steps = _steps(run)
    ids = [s for s, _ in steps]
    if RESUME_AT not in ids:
        raise EcoError(f"the routed run has no {RESUME_AT} step to resume from")
    i = ids.index(RESUME_AT)
    state_file = steps[i][1] / "state_in.json"
    if not state_file.is_file():
        raise EcoError(f"{steps[i][1].name} has no state_in.json")
    state = _confined_state(read_json(state_file), run)
    if not isinstance(state.get("def"), str):
        raise EcoError("the state before detailed routing has no DEF")
    return run, state, ids[i:]


def changes_file(changes: list[EcoChange]) -> str:
    """The edits OpenROAD applies, one per line, fields separated by tabs. Repairs are
    LibreLane steps, not edits."""
    lines = []
    for c in changes:
        if c.kind == "resize":
            lines.append(f"resize\t{c.instance}\t{c.cell}")
        elif c.kind == "buffer":
            lines.append(f"buffer\t{c.instance}\t{c.pin}\t{c.cell}")
    return "\n".join(lines) + ("\n" if lines else "")


EDIT_SCRIPT = r"""
set block [ord::get_db_block]
set db [ord::get_db]
set f [open "changes.txt" r]
set n 0
while {[gets $f line] >= 0} {
    set w [split $line "\t"]
    set kind [lindex $w 0]
    set inst [$block findInst [lindex $w 1]]
    if {$inst eq "NULL"} { puts "CHIPLEAP-ECO error $n no-instance"; exit 2 }
    if {$kind eq "resize"} {
        set master [$db findMaster [lindex $w 2]]
        if {$master eq "NULL"} { puts "CHIPLEAP-ECO error $n no-cell"; exit 2 }
        set from [[$inst getMaster] getName]
        if {![$inst swapMaster $master]} {
            puts "CHIPLEAP-ECO error $n pins-differ $from"
            exit 2
        }
        puts "CHIPLEAP-ECO done $n $from"
    } elseif {$kind eq "buffer"} {
        set master [$db findMaster [lindex $w 3]]
        if {$master eq "NULL"} { puts "CHIPLEAP-ECO error $n no-cell"; exit 2 }
        set iterm [$inst findITerm [lindex $w 2]]
        if {$iterm eq "NULL"} { puts "CHIPLEAP-ECO error $n no-pin"; exit 2 }
        set net [$iterm getNet]
        if {$net eq "NULL"} { puts "CHIPLEAP-ECO error $n no-net"; exit 2 }
        set ins {}
        set outs {}
        foreach mt [$master getMTerms] {
            if {[$mt getSigType] ne "SIGNAL"} { continue }
            if {[$mt getIoType] eq "INPUT"} { lappend ins [$mt getName] }
            if {[$mt getIoType] eq "OUTPUT"} { lappend outs [$mt getName] }
        }
        if {[llength $ins] != 1 || [llength $outs] != 1} {
            puts "CHIPLEAP-ECO error $n not-a-buffer"
            exit 2
        }
        set k 0
        while {[$block findInst "eco_buf_$k"] ne "NULL" || [$block findNet "eco_net_$k"] ne "NULL"} {
            incr k
        }
        set buf [odb::dbInst_create $block $master "eco_buf_$k"]
        set fresh [odb::dbNet_create $block "eco_net_$k"]
        set dir [$iterm getIoType]
        $iterm disconnect
        if {$dir eq "INPUT"} {
            # In front of a sink: the net drives the buffer, the buffer drives the sink.
            [$buf findITerm [lindex $ins 0]] connect $net
            [$buf findITerm [lindex $outs 0]] connect $fresh
            $iterm connect $fresh
        } elseif {$dir eq "OUTPUT"} {
            # After a driver: the driver drives the buffer, the buffer drives every load.
            $iterm connect $fresh
            [$buf findITerm [lindex $ins 0]] connect $fresh
            [$buf findITerm [lindex $outs 0]] connect $net
        } else {
            puts "CHIPLEAP-ECO error $n inout"
            exit 2
        }
        set loc [$inst getLocation]
        $buf setLocation [lindex $loc 0] [lindex $loc 1]
        $buf setPlacementStatus PLACED
        puts "CHIPLEAP-ECO done $n eco_buf_$k"
    }
    incr n
}
close $f
write_def "eco.def"
puts "CHIPLEAP-ECO end"
"""

ERRORS = {
    "no-instance": "no instance of that name in the routed design",
    "no-cell": "no cell of that name in the library",
    "pins-differ": "the new cell's pins differ from the old cell's",
    "no-pin": "the instance has no pin of that name",
    "no-net": "nothing is connected to that pin",
    "not-a-buffer": "the cell is not a buffer (one input, one output)",
    "inout": "an inout pin cannot take a buffer",
}


def edit_script(files: dict[str, Any], def_: str) -> str:
    head = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        f"read_def {q.tcl(def_)}",
    ]
    return "\n".join(head) + EDIT_SCRIPT


def parse_edits(log: str, edits: list[EcoChange]) -> list[str]:
    """What each edit did (the old cell, or the new buffer's name); raises EcoError with the
    change that failed."""
    done: list[str] = []
    for line in log.splitlines():
        m = re.match(r"\s*CHIPLEAP-ECO (done|error) (\d+) (\S+)(?: (\S+))?", line)
        if not m:
            continue
        n = int(m.group(2))
        if m.group(1) == "error":
            c = edits[n] if n < len(edits) else None
            what = f"{c.kind} {c.target}" + (f" to {c.cell}" if c and c.cell else "") if c else "?"
            why = ERRORS.get(m.group(3), m.group(3))
            if m.group(3) == "pins-differ" and m.group(4):
                why += f" ({m.group(4)})"
            raise EcoError(f"{what}: {why}")
        done.append(m.group(3))
    if len(done) != len(edits):
        raise EcoError("OpenROAD did not finish the edits")
    return done


def eco_config(project: Project, changes: list[EcoChange], steps: list[str]) -> dict[str, Any]:
    """The configuration the ECO resumes LibreLane with: the routed run's, with the step
    list and, for a repair, the margins it asks for."""
    cfg = read_json(project.build_dir / "pnr" / "config.json")
    if not isinstance(cfg, dict):
        raise EcoError("the routed run's config.json is not a mapping")
    flow = list(PREFIX)
    repairs = [c for c in changes if c.kind == "repair"]
    if repairs:
        flow.append(REPAIR)
        _, route = margins(project)
        for c in repairs:
            if c.target == "setup":
                cfg["GRT_RESIZER_SETUP_SLACK_MARGIN"] = (
                    c.margin_ns if c.margin_ns is not None else route
                )
            else:
                cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] = (
                    c.margin_ns if c.margin_ns is not None else 0.1
                )
    flow += steps
    cfg["meta"] = {"version": 2, "flow": flow}
    return cfg


def _run_librelane(
    work: Path, cfg: dict[str, Any], state: Path
) -> tuple[dict[str, Any], Path | None]:
    tool = librelane()
    if not tool.found or tool.path is None:
        raise OpenroadUnavailable("LibreLane is not installed")
    (work / "config.eco.json").write_text(json.dumps(cfg, indent=2))
    cmd = [
        tool.path,
        "--pdk",
        "ihp-sg13g2",
        "--run-tag",
        ECO_TAG,
        "--overwrite",
        "--with-initial-state",
        str(state),
        "config.eco.json",
    ]
    log = work / "librelane-eco.log"
    limit = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "10800"))
    try:
        code = launch.run(cmd, work, log, limit)
    except launch.Busy as exc:
        raise EcoError(f"LibreLane did not start: {exc}") from None
    except launch.TimedOut as exc:
        raise EcoError(f"LibreLane timed out after {exc.seconds:g} s") from None
    final = run_dir_of(work) / "final"
    if not (final / "metrics.json").is_file():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-15:])
        raise EcoError(f"LibreLane did not finish the ECO (exit {code}):\n{tail}")
    gds = next(iter(sorted((final / "gds").glob("*.gds"))), None)
    return read_json(final / "metrics.json"), gds


def run_dir_of(work: Path) -> Path:
    return work / "runs" / ECO_TAG


def apply_eco(project: Project, changes: list[EcoChange]) -> EcoResult:
    """Apply ``changes`` to the routed run and sign the result off again."""
    if not changes:
        raise EcoError("no changes to apply")
    run, state, steps = base(project)
    work = (project.build_dir / "pnr").resolve()
    edit_dir = work / "eco"
    if edit_dir.exists():
        shutil.rmtree(edit_dir)
    edit_dir.mkdir(parents=True)
    edits = [c for c in changes if c.kind != "repair"]
    done: list[str] = []
    def_path = Path(state["def"])
    if edits:
        pdk = pdk_views(project, run)
        files = pdk.stage_all(edit_dir)
        (edit_dir / "changes.txt").write_text(changes_file(edits))
        log, code = run_openroad(edit_script(files, stage(def_path, edit_dir)), edit_dir, "edit")
        done = parse_edits(log, edits)
        if code != 0 or "CHIPLEAP-ECO end" not in log:
            raise failure("the ECO edits", log, code)
        def_path = edit_dir / "eco.def"
    state = {**state, "def": str(def_path)}
    state_file = edit_dir / "state_in.json"
    state_file.write_text(json.dumps(state, indent=1))
    eco_run = run_dir(project, ECO_TAG)
    if eco_run.exists():
        shutil.rmtree(eco_run)  # an earlier ECO of the same base, which this one replaces
    try:
        metrics, gds = _run_librelane(work, eco_config(project, changes, steps), state_file)
    except PnrError as exc:
        raise EcoError(str(exc)) from exc
    clk = project.fastest_clock
    mhz = clk.mhz if clk else 100.0
    before = parse(read_json(run / "final" / "metrics.json"), mhz, None)
    after = parse(metrics, mhz, gds)
    record = []
    k = 0
    for c in changes:
        item = c.to_dict()
        if c.kind != "repair":
            item["result"] = f"was {done[k]}" if c.kind == "resize" else f"new buffer {done[k]}"
            k += 1
        record.append(item)
    (eco_run / ECO_MARKER).write_text(
        json.dumps({"base_metrics_sha256": base_digest(project), "changes": record}, indent=1)
    )
    return EcoResult(record, before, after, eco_run)


def eco_data(project: Project, r: EcoResult) -> dict[str, Any]:
    return {
        "tool": "OpenROAD and LibreLane",
        "changes": r.changes,
        "before": pnr_data(project, r.before),
        "after": pnr_data(project, r.after),
        "run": project.rel(r.run),
    }


def summarize(project: Project, r: EcoResult) -> tuple[str, str]:
    a, b = r.after, r.before
    met = a.setup_slack_ns >= 0 and a.hold_slack_ns >= 0
    clean = a.clean and a.antenna_nets == 0
    n = len(r.changes)
    checks = (
        "DRC, LVS and antenna clean"
        if clean
        else f"{a.drc_errors} DRC, {a.lvs_errors} LVS and {a.antenna_nets} antenna violations"
    )
    msg = (
        f"{n} change{'s' if n != 1 else ''} applied and signed off again: setup slack at the "
        f"slow corner {b.setup_slack_ns:+.3f} to {a.setup_slack_ns:+.3f} ns, hold "
        f"{b.hold_slack_ns:+.3f} to {a.hold_slack_ns:+.3f} ns, {b.cells} to {a.cells} cells; "
        f"{checks}"
    )
    return ("pass" if met and clean else "fail"), msg


def eco(project: Project, changes: list[EcoChange] | None = None) -> StepResult:
    """The ECO step: the project's ``eco`` changes, or ``changes``, on the routed design."""
    todo = list(project.eco if changes is None else changes)
    if not todo:
        return StepResult("eco", "skipped", "no eco changes in the project")
    try:
        r = apply_eco(project, todo)
    except OpenroadUnavailable as exc:
        return StepResult("eco", "skipped", str(exc))
    except (EcoError, OpenroadError, ValueError) as exc:
        return StepResult("eco", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    status, msg = summarize(project, r)
    return StepResult("eco", status, msg, eco_data(project, r))


# Timing paths, to choose changes from.


def paths_script(
    project: Project,
    files: dict[str, Any],
    corner: str,
    netlist: str,
    sdc: str,
    spef: str | None,
    kind: str,
    count: int,
) -> str:
    lines = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        f"read_liberty {q.tcl(files['liberty'][corner])}",
        f"read_verilog {q.tcl(netlist)}",
        f"link_design {q.tcl(project.top)}",
        f"read_sdc {q.tcl(sdc)}",
    ]
    if spef:
        lines.append(f"read_spef {q.tcl(spef)}")
    lines += [
        "set_propagated_clock [all_clocks]",
        'puts "CHIPLEAP-PATHS begin"',
        f"report_checks -path_delay {'max' if kind == 'max' else 'min'} "
        f"-group_path_count {int(count)} -digits 4",
        'puts "CHIPLEAP-PATHS end"',
    ]
    return "\n".join(lines) + "\n"


_STAGE_RE = re.compile(r"^\s*(-?[\d.]+)\s+(-?[\d.]+)\s+([v^])\s+(\S+)\s+\((\S+)\)\s*$")


def parse_paths(log: str, kind: str) -> list[TimingPath]:
    """OpenSTA's report_checks paths: start, end, group, slack, and the data path's stages."""
    start = log.find("CHIPLEAP-PATHS begin")
    end = log.find("CHIPLEAP-PATHS end", start)
    text = log[start : end if end > 0 else len(log)] if start >= 0 else ""
    paths: list[TimingPath] = []
    cur: TimingPath | None = None
    in_data = False
    seen_clock_edge = 0
    for line in text.splitlines():
        m = re.match(r"\s*Startpoint:\s*(\S+)", line)
        if m:
            cur = TimingPath(m.group(1), "", "", kind, 0.0, True)
            paths.append(cur)
            in_data, seen_clock_edge = False, 0
            continue
        if cur is None:
            continue
        m = re.match(r"\s*Endpoint:\s*(\S+)", line)
        if m:
            cur.endpoint = m.group(1)
            continue
        m = re.match(r"\s*Path Group:\s*(\S+)", line)
        if m:
            cur.group = m.group(1)
            continue
        if re.search(r"\bclock \S+ \((rise|fall) edge\)", line):
            seen_clock_edge += 1
            in_data = seen_clock_edge == 1
            continue
        if "data arrival time" in line:
            in_data = False
            continue
        m = re.match(r"\s*(-?[\d.]+)\s+slack \((MET|VIOLATED)\)", line)
        if m:
            cur.slack_ns = float(m.group(1))
            cur.met = m.group(2) == "MET"
            continue
        m = _STAGE_RE.match(line)
        if m and in_data:
            pin = m.group(4)
            inst = pin.rpartition("/")[0] if "/" in pin else ""
            cur.stages.append(
                Stage(pin, m.group(5), inst, m.group(3), float(m.group(1)), float(m.group(2)))
            )
    return [p for p in paths if p.endpoint]


def timing_paths(
    project: Project, corner: str = "slow", kind: str = "setup", count: int = 5
) -> tuple[list[TimingPath], str]:
    """The worst ``count`` setup or hold paths of the routed design (after an ECO, of the
    ECO's result) at ``corner``, and which run they come from."""
    if corner not in CORNERS:
        raise ValueError(f"corner must be one of {', '.join(CORNERS)}")
    if kind not in ("setup", "hold"):
        raise ValueError("kind must be setup or hold")
    views = routed_views(project)
    pdk = pdk_views(project, views.run)
    work = (project.build_dir / "paths").resolve()
    work.mkdir(parents=True, exist_ok=True)
    files = pdk.stage_all(work, (corner,))
    script = paths_script(
        project,
        files,
        corner,
        stage(views.netlist, work),
        stage(views.sdc, work),
        stage(views.spef, work) if views.spef else None,
        "max" if kind == "setup" else "min",
        max(1, min(count, PATHS_SHOWN)),
    )
    log, code = run_openroad(script, work, f"paths_{kind}_{corner}")
    if code != 0 or "CHIPLEAP-PATHS end" not in log:
        raise failure("OpenSTA's report_checks", log, code)
    found = parse_paths(log, "max" if kind == "setup" else "min")
    # Worst first across path groups; OpenSTA lists them group by group.
    return sorted(found, key=lambda p: p.slack_ns), project.rel(views.run)
