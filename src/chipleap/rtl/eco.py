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

Everything an ECO takes from the routed run (the steps it ran, the state before detailed
routing and every file that state names, LibreLane's configuration, the constraints and
sources beside it) must be a file ChipLeap recorded in the run's manifest, with the hash
it recorded (``chipleap.rtl.provenance``). The configuration passes on only the LibreLane
variables ChipLeap writes and the project's own numeric settings, each a plain value: no
variable that names a script, hook or plugin, and no substitution. A configuration or
script found in ``build/`` that ChipLeap did not record is never run.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.openroad import (
    ECO_MANIFEST,
    ECO_TAG,
    RUN_TAG,
    Job,
    OpenroadError,
    OpenroadUnavailable,
    failure,
    pdk_views,
    pnr_sealed,
    read_json,
    read_limited,
    routed_views,
    run_dir,
)
from chipleap.rtl.pnr import SDC_NAME, margins, pnr_data
from chipleap.rtl.project import CORNERS, EcoChange, Project
from chipleap.rtl.provenance import Sealed, SealError, seal, unseal
from chipleap.verify import launch
from chipleap.verify.pnr import PnrError, PnrResult, parse
from chipleap.verify.result import StepResult
from chipleap.verify.tools import excluding, librelane

RESUME_AT = "OpenROAD.DetailedRouting"
# Before routing again: the edited DEF into LibreLane's database, legal placement, and
# global routing for the nets that changed.
PREFIX = ("OpenROAD.DEFtoODB", "OpenROAD.DetailedPlacement", "OpenROAD.GlobalRouting")
REPAIR = "OpenROAD.ResizerTimingPostGRT"
STEP_ID_RE = re.compile(r"[A-Z][A-Za-z]*\.[A-Z][A-Za-z0-9]*")
# The LibreLane variables ChipLeap's place and route writes (chipleap.rtl.pnr.config and
# chipleap.verify.pnr.signoff), by the kind of value each takes. An ECO resumes LibreLane
# with these, and with the project's own pnr.librelane settings that are numbers or
# booleans; anything else in the configuration fails the ECO.
TEMPLATE_KEYS = {
    "DESIGN_NAME": "text",
    "VERILOG_FILES": "file",
    "VERILOG_INCLUDE_DIRS": "dir",
    "VERILOG_DEFINES": "text",
    "SYNTH_PARAMETERS": "text",
    "RUN_LINTER": "bool",
    "CLOCK_PORT": "text",
    "CLOCK_PERIOD": "number",
    "PNR_SDC_FILE": "file",
    "SIGNOFF_SDC_FILE": "file",
    "FP_CORE_UTIL": "number",
    "PL_TARGET_DENSITY_PCT": "number",
    "FP_SIZING": "text",
    "DIE_AREA": "number",
    "RUN_MAGIC_DRC": "bool",
    "DRT_ANTENNA_REPAIR_ITERS": "number",
    "DEFAULT_CORNER": "text",
    "RUN_POST_GRT_RESIZER_TIMING": "bool",
    "PL_RESIZER_SETUP_SLACK_MARGIN": "number",
    "GRT_RESIZER_SETUP_SLACK_MARGIN": "number",
    "GRT_RESIZER_HOLD_SLACK_MARGIN": "number",
    "KLAYOUT_DRC_THREADS": "number",
}
# Names of variables that run or locate code, whatever the project says.
SCRIPT_KEY_RE = re.compile(
    r"SCRIPT|HOOK|PLUGIN|TCL|CFG|CONFIG|FILE|DIR|PATH|CMD|COMMAND|EXTRA|META"
)
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


def _steps(run: Path, sealed: Sealed) -> list[tuple[str, Path]]:
    """The steps a LibreLane run ran, in order: each recorded step directory's id."""
    prefix = run.relative_to(sealed.root).as_posix() + "/"
    out = []
    configs = []
    for rel in sealed.files:
        m = re.fullmatch(re.escape(prefix) + r"(\d+)-[^/]+/config\.json", rel)
        if m:
            configs.append((int(m.group(1)), sealed.root / rel))
    for _, cfg in sorted(configs):
        meta = _read(sealed, cfg).get("meta") or {}
        step = meta.get("step") if isinstance(meta, dict) else None
        if isinstance(step, str):
            if not STEP_ID_RE.fullmatch(step):
                raise EcoError(f"{cfg.parent.name} names a step ChipLeap does not know: {step!r}")
            out.append((step, cfg.parent))
    return out


def _read(sealed: Sealed, path: Path) -> dict[str, Any]:
    try:
        data = sealed.read_json(path)
    except SealError as exc:
        raise EcoError(str(exc)) from None
    if not isinstance(data, dict):
        raise EcoError(f"{path.name} is not a mapping")
    return data


def _check(sealed: Sealed, path: str | Path, what: str) -> Path:
    try:
        return sealed.check(path)
    except SealError as exc:
        raise EcoError(f"{what}: {exc}") from None


def _confined_state(state: dict[str, Any], sealed: Sealed) -> dict[str, Any]:
    """A step's state with every file it names checked to be a file the run recorded,
    unchanged. A loop over an explicit stack."""
    todo: list[tuple[str, Any]] = [(k, v) for k, v in state.items() if k != "metrics"]
    while todo:
        key, value = todo.pop()
        if isinstance(value, str):
            _check(sealed, value, f"state {key}")
        elif isinstance(value, dict):
            todo += [(f"{key}.{k}", v) for k, v in value.items()]
        elif isinstance(value, list):
            todo += [(key, v) for v in value]
        elif value is not None and not isinstance(value, (int, float, bool)):
            raise EcoError(f"state {key} is not a path")
    return state


def base(project: Project) -> tuple[Path, Sealed, dict[str, Any], list[str]]:
    """The routed run, its manifest, its state before detailed routing, and the steps it
    ran from there."""
    run = run_dir(project, RUN_TAG)
    final = builddir.existing_dir(project.root, "build", "pnr", "runs", RUN_TAG, "final")
    if final is None or not (final / "metrics.json").is_file():
        raise OpenroadUnavailable("no routed design: run the pnr step first")
    sealed = pnr_sealed(project)
    steps = _steps(run, sealed)
    ids = [s for s, _ in steps]
    if RESUME_AT not in ids:
        raise EcoError(f"the routed run has no {RESUME_AT} step to resume from")
    i = ids.index(RESUME_AT)
    state = _confined_state(_read(sealed, steps[i][1] / "state_in.json"), sealed)
    if not isinstance(state.get("def"), str):
        raise EcoError("the state before detailed routing has no DEF")
    return run, sealed, state, ids[i:]


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
set f [open $changes_file r]
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


def edit_script(files: dict[str, Any], def_: str, changes: str) -> str:
    head = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        f"read_def {q.tcl(def_)}",
        f"set changes_file {q.tcl(changes)}",
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


def _plain_text(key: str, value: Any) -> str:
    """A text value LibreLane takes as it is: no Tcl syntax and no substitution
    (``expr::``, ``refg::``, ``pdk_dir::``)."""
    if not isinstance(value, str):
        raise EcoError(f"the routed run's configuration sets {key} to {value!r}, not text")
    try:
        q.plain(value)
    except q.QuoteError as exc:
        raise EcoError(f"the routed run's configuration sets {key}: {exc}") from None
    if "::" in value:
        raise EcoError(f"the routed run's configuration sets {key} to a substitution: {value!r}")
    return value


def _settings(project: Project, cfg: dict[str, Any], sealed: Sealed) -> dict[str, Any]:
    """The routed run's configuration as an ECO passes it on: only ``TEMPLATE_KEYS`` and
    the project's own numeric or boolean ``pnr.librelane`` settings, each with a plain
    value, and every file it names one the run recorded beside it, unchanged."""
    for key, value in cfg.items():
        kind = TEMPLATE_KEYS.get(key)
        if kind is None:
            own = project.pnr.librelane
            if key not in own or own[key] != value or SCRIPT_KEY_RE.search(key):
                raise EcoError(
                    f"the routed run's configuration sets {key}, which an ECO does not pass "
                    "on to LibreLane; run the pnr step again"
                )
            kind = "setting"
        for item in value if isinstance(value, list) else [value]:
            if kind in ("file", "dir"):
                _recorded(key, item, kind, sealed)
            elif kind == "text":
                _plain_text(key, item)
            elif kind == "bool" and not isinstance(item, bool):
                raise EcoError(f"the routed run's configuration sets {key} to {item!r}")
            elif kind in ("number", "setting") and not (
                isinstance(item, (int, float)) and (kind == "setting" or not isinstance(item, bool))
            ):
                raise EcoError(
                    f"the routed run's configuration sets {key} to {item!r}, not a number"
                )
    return dict(cfg)


def _recorded(key: str, item: Any, kind: str, sealed: Sealed) -> None:
    """A ``dir::`` path to a source or the constraints ChipLeap copied beside the
    configuration, which the run recorded."""
    text = _plain_text(key, item.removeprefix("dir::") if isinstance(item, str) else item)
    rel = Path(text)
    if (
        not item.startswith("dir::")
        or rel.is_absolute()
        or ".." in rel.parts
        or not (rel.parts[:1] == ("src",) or text == SDC_NAME)
    ):
        raise EcoError(
            f"the routed run's configuration sets {key} to {item!r}, not a file ChipLeap "
            "wrote beside it"
        )
    if kind == "file":
        _check(sealed, sealed.root / rel, key)
    elif not any(f.startswith(rel.as_posix() + "/") for f in sealed.files):
        raise EcoError(f"{key}: {text} is not a directory the run recorded")


def eco_config(
    project: Project, changes: list[EcoChange], steps: list[str], sealed: Sealed
) -> dict[str, Any]:
    """The configuration the ECO resumes LibreLane with: the routed run's recorded one,
    through ``_settings``, with the step list and, for a repair, the margins it asks for."""
    cfg = _settings(project, _read(sealed, sealed.root / "config.json"), sealed)
    flow = list(PREFIX)
    repairs = [c for c in changes if c.kind == "repair"]
    if repairs:
        flow.append(REPAIR)
        # The routed run may have had the repair turned off (pnr.librelane); this is the
        # step that turns it on.
        cfg["RUN_POST_GRT_RESIZER_TIMING"] = True
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
    for step in steps:
        if not STEP_ID_RE.fullmatch(step):
            raise EcoError(f"the routed run names a step ChipLeap does not know: {step!r}")
    flow += steps
    cfg["meta"] = {"version": 2, "flow": flow}
    return cfg


def _run_librelane(
    work: Path, cfg: dict[str, Any], state: Path
) -> tuple[dict[str, Any], Path | None]:
    tool = librelane()
    if not tool.found or tool.path is None:
        raise OpenroadUnavailable("LibreLane is not installed")
    builddir.write_text(work, "config.eco.json", json.dumps(cfg, indent=2))
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
        tail = "\n".join(read_limited(log).splitlines()[-15:])
        raise EcoError(f"LibreLane did not finish the ECO (exit {code}):\n{tail}")
    gds = next(iter(sorted((final / "gds").glob("*.gds"))), None)
    metrics = read_json(final / "metrics.json")
    if not isinstance(metrics, dict):
        raise EcoError("the ECO's metrics.json is not a mapping")
    return metrics, gds


def run_dir_of(work: Path) -> Path:
    return work / "runs" / ECO_TAG


def apply_eco(project: Project, changes: list[EcoChange]) -> EcoResult:
    """Apply ``changes`` to the routed run and sign the result off again."""
    if not changes:
        raise EcoError("no changes to apply")
    # First, so a symlinked build/ or build/pnr is refused before anything is read.
    work = project.work_dir("pnr")
    run, sealed, state, steps = base(project)
    cfg = eco_config(project, changes, steps, sealed)
    # The ECO's own files; an earlier ECO of the same base is replaced as a whole.
    unseal(work, ECO_MANIFEST)
    builddir.work_dir(work, "runs", ECO_TAG, empty=True)
    edit_dir = builddir.work_dir(work, "eco", empty=True)
    edits = [c for c in changes if c.kind != "repair"]
    done: list[str] = []
    def_path = _check(sealed, state["def"], "state def")
    if edits:
        job = Job(edit_dir, "edit")
        files = pdk_views(project, run).stage_all(job)
        changes_txt = builddir.write_text(edit_dir, "changes.txt", changes_file(edits))
        script = edit_script(files, job.stage(def_path, sealed=sealed), job.stage(changes_txt))
        log, code = job.run(script)
        done = parse_edits(log, edits)
        if code != 0 or "CHIPLEAP-ECO end" not in log:
            raise failure("the ECO edits", log, code)
        def_path = job.output("eco.def")
        if not def_path.is_file():
            raise EcoError("OpenROAD wrote no edited DEF")
    state_file = builddir.write_text(
        edit_dir, "state_in.json", json.dumps({**state, "def": str(def_path)}, indent=1)
    )
    try:
        metrics, gds = _run_librelane(work, cfg, state_file)
    except PnrError as exc:
        raise EcoError(str(exc)) from exc
    clk = project.fastest_clock
    mhz = clk.mhz if clk else 100.0
    before = parse(_read(sealed, run / "final" / "metrics.json"), mhz, None)
    after = parse(metrics, mhz, gds)
    record = []
    k = 0
    for c in changes:
        item = c.to_dict()
        if c.kind != "repair":
            item["result"] = f"was {done[k]}" if c.kind == "resize" else f"new buffer {done[k]}"
            k += 1
        record.append(item)
    seal(
        work,
        ECO_MANIFEST,
        "eco",
        [f"runs/{ECO_TAG}", "eco"],
        {"base": sealed.mac, "changes": record},
    )
    return EcoResult(record, before, after, run_dir_of(work))


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
        with excluding(project.root):
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
        f"-group_path_count {q.number(int(count))} -digits 4",
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
    with excluding(project.root):
        work = project.work_dir("paths")
        views = routed_views(project)
        pdk = pdk_views(project, views.run)
        job = Job(work, f"paths_{kind}_{corner}")
        files = pdk.stage_all(job, (corner,))
        script = paths_script(
            project,
            files,
            corner,
            job.stage(views.netlist, sealed=views.sealed),
            job.stage(views.sdc, sealed=views.sealed),
            job.stage(views.spef, sealed=views.sealed) if views.spef else None,
            "max" if kind == "setup" else "min",
            max(1, min(count, PATHS_SHOWN)),
        )
        log, code = job.run(script)
        if code != 0 or "CHIPLEAP-PATHS end" not in log:
            raise failure("OpenSTA's report_checks", log, code)
    found = parse_paths(log, "max" if kind == "setup" else "min")
    # Worst first across path groups; OpenSTA lists them group by group.
    return sorted(found, key=lambda p: p.slack_ns), project.rel(views.run)
