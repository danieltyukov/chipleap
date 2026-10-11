"""Gate-level power from the activity of the project's own tests.

The project's cocotb tests run on the gate-level netlist (the routed one when there is a
routed result, else the synthesized one) with IHP's cell models on Icarus 13, and a
second top module dumps a VCD of the design. OpenROAD then reads the corner's liberty,
the netlist, the constraints and, when routed, the parasitics, and reports power twice:
vectorless, from a default input activity, and from the VCD. Both are reported per group
(sequential, combinational, clock, macro, pad), at each corner the project asks for.

Tests see ``GATES=yes`` (Tiny Tapeout's convention) and ``CHIPLEAP_GATES=1``, so a test
that reads an internal signal can skip what a netlist no longer has, and the project's
parameters as ``CHIPLEAP_PARAM_<name>``, since a netlist has none. Scan ports, when the
netlist has them, are forced to their functional values.

Activity is measured per clock period, so the clock in the VCD is checked against the
constraints' and a difference over 1 % is reported.

The tests run on a copy of the netlist: of a routed one, checked against the routed run's
manifest (``chipleap.rtl.provenance``). The VCD and what it was made from are recorded in
a manifest of their own, so the IR drop step reuses a VCD only when ChipLeap made it here
from the same netlist and tests.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.netlist import NetlistError, read_netlist
from chipleap.rtl.openroad import (
    Job,
    OpenroadError,
    OpenroadUnavailable,
    PdkViews,
    failure,
    pdk_views,
    read_limited,
    routed_run,
    routed_views,
)
from chipleap.rtl.project import CORNERS, Project
from chipleap.rtl.provenance import Sealed, SealError, copy_file, file_digest, open_sealed, seal
from chipleap.rtl.sim import RUNNER, TestResult, cocotb_python, manifest, parse_results
from chipleap.rtl.synth import SynthError, synthesize
from chipleap.verify import launch
from chipleap.verify.result import StepResult
from chipleap.verify.tools import gl_iverilog

GROUPS = ("Sequential", "Combinational", "Clock", "Macro", "Pad", "Total")
SCAN_PORT_RE = re.compile(r"^scan_(enable|in)_\d+$")
VCD_NAME = "gl.vcd"
ACTIVITY_MARKER = "activity.sha256"
GL_MANIFEST = "chipleap-gl.manifest.json"
# Rising edges of the clock read from a VCD to measure its period; the rest is skipped.
EDGES = 200
VCD_HEADER_LIMIT = 16 << 20


class PowerError(RuntimeError):
    pass


@dataclass
class GateRun:
    """The project's tests on a gate-level netlist."""

    netlist: Path
    routed: bool
    tests: list[TestResult]
    vcd: Path | None
    scope: str
    error: str = ""
    sealed: Sealed | None = None  # the manifest that records the VCD

    @property
    def failed(self) -> list[TestResult]:
        return [t for t in self.tests if t.status == "fail"]


@dataclass
class CornerPower:
    corner: str
    vectorless: dict[str, dict[str, float]]  # group -> internal, switching, leakage, total (W)
    vcd: dict[str, dict[str, float]] = field(default_factory=dict)
    annotated: int = 0
    unannotated: int = 0


def dump_module(top_path: str, vcd: Path, forced: list[str]) -> str:
    """A second top module: dump the design's signals to ``vcd`` and hold the scan ports
    at their functional values. ``top_path`` is the design's instance path with dots."""
    lines = [
        "`timescale 1ns/1ps",
        "module chipleap_gl_aux;",
        "  initial begin",
        f'    $dumpfile("{_verilog_string(vcd)}");',
        f"    $dumpvars(0, {top_path});",
        "  end",
    ]
    if forced:
        lines += [
            "  initial begin",
            *(f"    force {top_path}.{p} = 1'b0;" for p in forced),
            "  end",
        ]
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _verilog_string(path: Path) -> str:
    text = str(path)
    if any(c in text for c in '"\\\n') or not text.isprintable():
        raise PowerError(f"{text!r} cannot go into a Verilog string")
    return text


def gate_level_run(
    project: Project,
    netlist: Path,
    views: PdkViews,
    workdir: Path,
    routed: bool,
    sealed: Sealed | None = None,
) -> GateRun:
    """Run the project's cocotb tests on a copy of ``netlist`` (checked against ``sealed``
    when it comes from a routed run) with the cell models, dumping a VCD into
    ``workdir/gl``, which is emptied first."""
    if project.tests.dir is None:
        raise OpenroadUnavailable("the project has no tests to take activity from")
    tool = gl_iverilog()
    if not tool.found or tool.path is None:
        raise OpenroadUnavailable("Icarus 13 or newer is not installed (CHIPLEAP_GL_IVERILOG)")
    python = cocotb_python()
    if python is None:
        raise OpenroadUnavailable("cocotb is not installed: pip install 'chipleap[rtl]'")
    scope = project.power.scope or project.tests.toplevel
    if not project.power.scope and project.tests.toplevel != project.top:
        raise PowerError(
            f"the tests' top is {project.tests.toplevel}, not {project.top}: set power.scope "
            "to the design's instance in it, such as tb/u_dut"
        )
    work = builddir.work_dir(workdir, "gl", empty=True)
    local = work / f"{project.top}.gl.v"
    try:
        if sealed is not None:
            sealed.copy(netlist, local)
        else:
            copy_file(netlist.resolve(), local)
    except SealError as exc:
        raise PowerError(str(exc)) from None
    try:
        top = read_netlist([local]).top(project.top)
    except NetlistError as exc:
        raise PowerError(f"the gate-level netlist: {exc}") from None
    forced = [p for p in top.ports if SCAN_PORT_RE.match(p)]
    vcd = work / VCD_NAME
    aux = builddir.write_text(
        work, "chipleap_gl_aux.v", dump_module(scope.replace("/", "."), vcd, forced)
    )
    m = manifest(project, "icarus", work)
    m["sources"] = [
        str(local),
        *(str(p) for p in views.models),
        str(aux),
        *(str(s) for s in project.tests.sources),
    ]
    if project.tests.toplevel == project.top:
        m["parameters"] = {}  # a netlist has none
    m["build_args"] = [*m["build_args"], "-s", "chipleap_gl_aux"]
    # A netlist has no parameters, so the tests get the project's in the environment.
    params = {f"CHIPLEAP_PARAM_{k}": str(v) for k, v in project.parameters.items()}
    m["env"] = {**m["env"], **params, "GATES": "yes", "CHIPLEAP_GATES": "1"}
    m["vcd"] = True
    builddir.write_text(work, "manifest.json", json.dumps(m, indent=1))
    builddir.write_text(work, RUNNER, resources.files("chipleap.rtl").joinpath(RUNNER).read_text())
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    env["COCOTB_ANSI_OUTPUT"] = "0"
    env["PATH"] = str(Path(tool.path).parent) + os.pathsep + env.get("PATH", "")
    limit = float(os.environ.get("CHIPLEAP_SIM_TIMEOUT", "1800"))
    try:
        launch.run([python, RUNNER, "manifest.json"], work, work / "runner.log", limit, env=env)
    except launch.TimedOut as exc:
        raise PowerError(f"the gate-level tests timed out after {exc.seconds:g} s") from None
    except launch.Busy as exc:
        raise PowerError(str(exc)) from None
    outcome_file = work / "outcome.json"
    try:
        outcome = json.loads(read_limited(outcome_file, 1 << 20)) if outcome_file.is_file() else {}
    except json.JSONDecodeError:
        outcome = {}
    tests: list[TestResult] = []
    results = work / "results.xml"
    if results.is_file():
        try:
            tests = parse_results(read_limited(results), project)
        except ET.ParseError as exc:
            raise PowerError(f"the gate-level results are not valid XML: {exc}") from None
    stage_done = isinstance(outcome, dict) and outcome.get("stage") == "done"
    error = (
        ""
        if stage_done
        else str(outcome.get("error", "no outcome") if isinstance(outcome, dict) else "no outcome")
    )
    run = GateRun(
        netlist=netlist,
        routed=routed,
        tests=tests,
        vcd=vcd if vcd.is_file() and vcd.stat().st_size else None,
        scope=scope,
        error=error,
    )
    if run.vcd is not None:
        builddir.write_text(work, ACTIVITY_MARKER, _activity_key(project, file_digest(local)))
        run.sealed = seal(work, GL_MANIFEST, "gl", [VCD_NAME, ACTIVITY_MARKER])
    return run


def _activity_key(project: Project, netlist_sha256: str) -> str:
    """What a VCD depends on: the netlist, the project's files (its tests) and the scope."""
    h = hashlib.sha256(netlist_sha256.encode())
    h.update(project.inputs_digest().encode())
    h.update(project.power.scope.encode())
    return h.hexdigest()


def activity(
    project: Project, netlist: Path, views: PdkViews, routed: bool, sealed: Sealed | None = None
) -> tuple[Path, str, Sealed] | None:
    """A VCD of the tests on ``netlist``, its scope and the manifest that records it: the
    power step's when ChipLeap made it from this netlist and these tests, else a new
    gate-level run; None when the tests cannot run here."""
    gl = project.build_dir / "power" / "gl"
    scope = project.power.scope or project.tests.toplevel
    try:
        digest = sealed.locate(netlist)[1] if sealed is not None else file_digest(netlist)
        made = open_sealed(gl, GL_MANIFEST, "gl")
        if made.read_text(gl / ACTIVITY_MARKER, 1 << 10) == _activity_key(project, digest):
            return gl / VCD_NAME, scope, made
    except (SealError, OSError):
        pass
    try:
        run = gate_level_run(project, netlist, views, project.work_dir("power"), routed, sealed)
    except OpenroadUnavailable:
        return None
    if run.vcd is None or run.sealed is None:
        return None
    return run.vcd, run.scope, run.sealed


def vcd_clock_period_ns(vcd: Path, scope: str, clock: str) -> float | None:
    """The clock period in a VCD, from the first rising edges of ``clock`` in ``scope``
    (slash-separated). Reads the header and then only as far as it needs to."""
    want = [*scope.split("/"), clock] if scope else [clock]
    stack: list[str] = []
    code = ""
    scale_ns = 1.0
    edges: list[float] = []
    now = 0.0
    timescale = False
    read = 0
    with vcd.open("r", errors="replace") as fh:
        for line in fh:
            read += len(line)
            words = line.split()
            if not words:
                continue
            w = words[0]
            if not code:
                if read > VCD_HEADER_LIMIT:
                    return None
                if w == "$timescale":
                    timescale = True
                    words = words[1:]
                if timescale:
                    text = " ".join(x for x in words if x != "$end")
                    m = re.match(r"(\d+)\s*(s|ms|us|ns|ps|fs)", text)
                    if m:
                        unit = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}
                        scale_ns = int(m.group(1)) * unit[m.group(2)]
                    if "$end" in line:
                        timescale = False
                    continue
                if w == "$scope" and len(words) >= 3:
                    stack.append(words[2])
                elif w == "$upscope":
                    stack = stack[:-1]
                elif w == "$var" and len(words) >= 5 and [*stack, words[4]] == want:
                    code = words[3]
                elif w == "$enddefinitions":
                    return None
                continue
            if w.startswith("#"):
                try:
                    now = float(w[1:]) * scale_ns
                except ValueError:
                    continue
            elif w == "1" + code or (len(w) > 1 and w[0] == "1" and w[1:] == code):
                edges.append(now)
                if len(edges) > EDGES:
                    break
    if len(edges) < 3:
        return None
    gaps = sorted(b - a for a, b in itertools.pairwise(edges))
    return gaps[len(gaps) // 2]


def power_script(
    project: Project,
    files: dict[str, Any],
    corner: str,
    netlist: str,
    sdc: str,
    spef: str | None,
    vcd: str | None,
    scope: str,
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
        lines += [f"read_spef {q.tcl(spef)}", "set_propagated_clock [all_clocks]"]
    lines += [
        f"set_power_activity -input -activity {q.number(float(project.power.input_activity))}",
        'puts "CHIPLEAP-POWER vectorless"',
        "report_power -digits 9",
    ]
    if vcd:
        lines += [
            f"read_vcd -scope {q.tcl(scope)} {q.tcl(vcd)}",
            'puts "CHIPLEAP-POWER vcd"',
            "if {[llength [info commands report_activity_annotation]]} {",
            "    report_activity_annotation",
            "}",
            "report_power -digits 9",
        ]
    lines.append('puts "CHIPLEAP-POWER end"')
    return "\n".join(lines) + "\n"


_ROW_RE = re.compile(
    r"^\s*(" + "|".join(GROUPS) + r")\s+([-+.\deE]+)\s+([-+.\deE]+)\s+([-+.\deE]+)\s+([-+.\deE]+)"
)


def parse_power(log: str) -> dict[str, Any]:
    """The power tables in the log of ``power_script``: per section (vectorless, vcd), per
    group, internal, switching, leakage and total in W; and the annotation counts."""
    out: dict[str, Any] = {"vectorless": {}, "vcd": {}, "annotated": 0, "unannotated": 0}
    section = ""
    for line in log.splitlines():
        m = re.match(r"\s*CHIPLEAP-POWER (\w+)", line)
        if m:
            section = m.group(1) if m.group(1) in ("vectorless", "vcd") else ""
            continue
        if not section:
            continue
        m = _ROW_RE.match(line)
        if m:
            try:
                values = [float(m.group(i)) for i in range(2, 6)]
            except ValueError:
                continue
            out[section][m.group(1)] = dict(
                zip(("internal", "switching", "leakage", "total"), values, strict=True)
            )
            continue
        m = re.match(r"\s*vcd\s+(\d+)\s*$", line)
        if m and section == "vcd":
            out["annotated"] = int(m.group(1))
        m = re.match(r"\s*unannotated\s+(\d+)\s*$", line)
        if m and section == "vcd":
            out["unannotated"] = int(m.group(1))
    return out


def simple_sdc(project: Project) -> str:
    """Clocks for the synthesized netlist: power needs their periods, not the I/O timing."""
    lines = [
        f"create_clock -name {q.tcl(c.port)} -period {q.number(float(c.period_ns))} "
        f"[get_ports {q.tcl(c.port)}]"
        for c in project.clocks
    ]
    return "\n".join(lines) + "\n"


@dataclass
class PowerResult:
    run: GateRun | None
    corners: list[CornerPower]
    routed: bool
    clock_check: str
    librelane_mw: float | None
    notes: list[str] = field(default_factory=list)


def analyze(project: Project) -> PowerResult:
    cfg = project.power
    work = project.work_dir("power")
    run = routed_run(project) if cfg.netlist in ("auto", "routed") else None
    if cfg.netlist == "routed" and run is None:
        raise OpenroadUnavailable("power.netlist is routed but there is no routed design yet")
    notes: list[str] = []
    librelane_mw = None
    sealed: Sealed | None = None
    if run is not None:
        views = routed_views(project, run)
        pdk = pdk_views(project, run)
        sealed = views.sealed
        netlist, sdc_src, spef = views.netlist, views.sdc, views.spef
        total = views.metrics.get("power__total")
        librelane_mw = float(total) * 1000 if isinstance(total, (int, float)) else None
    else:
        pdk = pdk_views(project)
        try:
            netlist = synthesize(project, work / "synth").netlist
        except SynthError as exc:
            raise PowerError(f"synthesis failed: {exc}") from exc
        sdc_src = builddir.write_text(work, "clocks.sdc", simple_sdc(project))
        spef = None
        notes.append("synthesized netlist: no parasitics")
    gl: GateRun | None = None
    try:
        gl = gate_level_run(project, netlist, pdk, work, run is not None, sealed)
    except OpenroadUnavailable as exc:
        notes.append(f"vectorless only: {exc}")
    if gl is not None and gl.vcd is None:
        notes.append(f"the gate-level tests wrote no VCD: {gl.error or 'no dump'}")
    corners = []
    for corner in cfg.corners:
        job = Job(work, f"power_{corner}")
        files = pdk.stage_all(job, (corner,))
        script = power_script(
            project,
            files,
            corner,
            job.stage(netlist, sealed=sealed),
            job.stage(sdc_src, sealed=sealed if run is not None else None),
            job.stage(spef, sealed=sealed) if spef else None,
            job.stage(gl.vcd, sealed=gl.sealed) if gl and gl.vcd else None,
            gl.scope if gl else "",
        )
        log, code = job.run(script)
        if code != 0 or "CHIPLEAP-POWER end" not in log:
            raise failure(f"OpenROAD power at {corner}", log, code)
        parsed = parse_power(log)
        if "Total" not in parsed["vectorless"]:
            raise PowerError(f"OpenROAD reported no power at {corner}")
        corners.append(
            CornerPower(
                corner,
                parsed["vectorless"],
                parsed["vcd"],
                parsed["annotated"],
                parsed["unannotated"],
            )
        )
    check = ""
    clk = project.fastest_clock
    if gl is not None and gl.vcd is not None and clk is not None:
        period = vcd_clock_period_ns(gl.vcd, gl.scope, clk.port)
        if period is None:
            check = f"no {clk.port} edges found in the VCD"
        elif abs(period - clk.period_ns) / clk.period_ns > 0.01:
            check = (
                f"the tests run {clk.port} at {period:g} ns, the constraints at "
                f"{clk.period_ns:g} ns; activity per cycle follows the tests"
            )
            notes.append(check)
        else:
            check = f"{clk.port} at {period:g} ns in the VCD, as constrained"
    return PowerResult(gl, corners, run is not None, check, librelane_mw, notes)


def _mw(groups: dict[str, dict[str, float]], group: str = "Total") -> float | None:
    g = groups.get(group)
    return round(g["total"] * 1000, 6) if g else None


def power_data(project: Project, r: PowerResult) -> dict[str, Any]:
    gl = r.run
    return {
        "tool": "OpenROAD",
        "netlist": "routed" if r.routed else "synthesized",
        "gate_level_tests": None
        if gl is None
        else {
            "simulator": f"Icarus {gl_iverilog().version}",
            "passed": sum(t.status == "pass" for t in gl.tests),
            "failed": len(gl.failed),
            "tests": [t.to_dict() for t in gl.tests],
            "vcd": project.rel(gl.vcd) if gl.vcd else None,
            "error": gl.error,
        },
        "corners": [
            {
                "corner": c.corner,
                "liberty": CORNERS[c.corner],
                "vectorless_mw": _mw(c.vectorless),
                "vcd_mw": _mw(c.vcd),
                "annotated_pins": c.annotated,
                "unannotated_pins": c.unannotated,
                "groups": {
                    g: {
                        "vectorless_mw": _mw(c.vectorless, g),
                        "vcd_mw": _mw(c.vcd, g),
                    }
                    for g in GROUPS[:-1]
                    if g in c.vectorless
                },
            }
            for c in r.corners
        ],
        "input_activity": project.power.input_activity,
        "librelane_mw": None if r.librelane_mw is None else round(r.librelane_mw, 6),
        "clock_check": r.clock_check,
        "notes": r.notes,
    }


def power(project: Project) -> StepResult:
    """The power step: gate-level power from the tests' activity at each corner, beside
    the vectorless estimate. Fails when a test fails on the netlist."""
    try:
        r = analyze(project)
    except OpenroadUnavailable as exc:
        return StepResult("power", "skipped", str(exc))
    except (PowerError, OpenroadError, ValueError) as exc:
        return StepResult("power", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    data = power_data(project, r)
    where = "routed" if r.routed else "synthesized"
    per = []
    for c in data["corners"]:
        if c["vcd_mw"] is not None:
            per.append(
                f"{c['vcd_mw']:.3g} mW at {c['corner']} (vectorless {c['vectorless_mw']:.3g})"
            )
        else:
            per.append(f"{c['vectorless_mw']:.3g} mW vectorless at {c['corner']}")
    gl = r.run
    status = "pass"
    head = f"power of the {where} netlist: " + ", ".join(per)
    parts = [head]
    if gl is not None:
        n, failed = len(gl.tests), len(gl.failed)
        parts.append(
            f"activity from {n - failed} of {n} cocotb tests passing on the gate-level netlist"
        )
        if failed or not gl.tests or gl.vcd is None:
            status = "fail"
            if failed:
                parts[-1] += f" ({', '.join(t.full_name for t in gl.failed[:3])} fail)"
        c0 = r.corners[0]
        if c0.vcd and c0.annotated + c0.unannotated:
            parts.append(f"{c0.annotated} of {c0.annotated + c0.unannotated} pins annotated")
    parts += r.notes
    return StepResult("power", status, "; ".join(parts), data)
