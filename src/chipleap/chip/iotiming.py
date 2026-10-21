"""The chip's timing at its ports: the pads' own delays in the signoff.

LibreLane times the core against its side of the pads (the IO cells' ``p2c`` and ``c2p``
pins), with an IO liberty that has no arcs, against the budget the pads leave it there:
the world outside less the pads' own delays at each signoff corner (``core_budget``,
written into the chip's constraints by ``chipleap.chip.netlist.sdc``). After routing, ChipLeap times the chip again with OpenSTA (in OpenROAD) at every corner
the core was signed off at: the routed netlist and its parasitics, the standard cells'
liberty of that corner, IHP's ``sg13g2_io`` liberty of the same process, core voltage and
temperature, which has the pads' arcs (``pad`` to ``p2c`` of an input pad, ``c2p`` to
``pad`` of an output pad), and the macros' liberty. The clocks are created on the chip's
own ports, so a clock's path through its input pad is part of its insertion delay, and
the data ports get the budget of the world outside (``chip.io``): an input delay, an
output delay, the load an output pad drives and the edge rate at an input pad.

The report gives each corner's worst setup and hold slack and, at the slow corner, the
worst pad-to-core, core-to-pad and pad-to-pad paths with the share of their delay inside
the pads. A pad whose IO cell has no arc in the liberty is reported as not timed, and
the chip's IO timing does not pass; its delay is never taken as zero.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from chipleap.chip.config import DEFAULT_IO_DELAY_PCT, IoBudget
from chipleap.chip.io import IO, PAD_CELLS
from chipleap.chip.netlist import Binding, CoreBudget, PadDelay, flat
from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.eco import TimingPath, parse_paths
from chipleap.rtl.openroad import Job, failure, read_limited
from chipleap.rtl.synth import DEFAULT_PERIOD_NS

# The arc each signal pad's cell must have: (from pin, to pin). The "to" pin is the
# cell's output, where a path's delay through the pad is reported.
PAD_ARCS = {PAD_CELLS["in"]: ("pad", "p2c"), PAD_CELLS["out"]: ("c2p", "pad")}
# The table both arcs' delays need, for a rising and a falling output.
DELAY_TABLES = ("cell_rise", "cell_fall")
PATHS_SHOWN = 5
LIBERTY_MAX = 64 << 20
UNCERTAINTY_MAX_NS = 100.0
# IHP's signoff corners in LibreLane (its STA_CORNERS): process, core supply, temperature.
SIGNOFF_CORNERS = (("typ", 1.2, 25.0), ("slow", 1.08, 125.0), ("fast", 1.32, -40.0))
# What the core-side budget takes the pads' arcs at, besides chip.io: the edge a core
# driver gives an output pad's c2p (the 0.14 to 0.38 ns LibreLane 3.0.0.dev44's
# resizer gave the UART chip's), and the load an input pad's p2c drives in the core
# (0.03 to 0.09 pF on that chip). Both are points of IHP's tables.
CORE_EDGE_NS = 0.33
CORE_LOAD_PF = 0.048
DERATE_MAX_PCT = 50.0
# LibreLane's signoff corners: parasitics (nom, min, max), process, core supply, temperature.
STA_CORNER_RE = re.compile(r"(nom|min|max)_(typ|slow|fast)_(\d+p\d+)V_(m?\d+)C")
# IHP's IO liberty: process, core supply, IO supply, temperature.
IO_LIB_RE = re.compile(rf"{IO}_(typ|slow|fast)_(\d+p\d+)V_(\d+p\d+)V_(m?\d+)C\.lib")


class IoTimingError(RuntimeError):
    pass


@dataclass(frozen=True)
class Corner:
    name: str  # LibreLane's, nom_slow_1p08V_125C
    process: str  # typ, slow or fast
    std_libs: tuple[Path, ...]
    io_lib: Path


@dataclass
class CornerTiming:
    corner: str  # typ, slow or fast
    setup_ns: float | None  # worst slack; None when nothing is constrained
    hold_ns: float | None


@dataclass
class IoTiming:
    corners: list[CornerTiming]
    paths: dict[str, list[dict[str, Any]]]  # category -> worst paths at the slow corner
    untimed: list[str] = field(default_factory=list)  # pads whose IO cell has no arc
    budget: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def met(self) -> bool:
        slacks = [s for c in self.corners for s in (c.setup_ns, c.hold_ns) if s is not None]
        return not self.untimed and all(s >= 0 for s in slacks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corners": [
                {"corner": c.corner, "setup_slack_ns": c.setup_ns, "hold_slack_ns": c.hold_ns}
                for c in self.corners
            ],
            "paths": self.paths,
            "untimed_pads": self.untimed,
            "budget": self.budget,
            "warnings": self.warnings,
            "met": self.met,
        }


def budget(io: IoBudget, period_ns: float) -> dict[str, float]:
    """The numbers the constraints use: ``chip.io``, its delays defaulting to
    ``DEFAULT_IO_DELAY_PCT`` of the reference clock's period."""
    share = period_ns * DEFAULT_IO_DELAY_PCT / 100
    return {
        "input_delay_ns": io.input_delay_ns if io.input_delay_ns is not None else share,
        "output_delay_ns": io.output_delay_ns if io.output_delay_ns is not None else share,
        "output_load_pf": io.output_load_pf,
        "input_transition_ns": io.input_transition_ns,
    }


def pad_ports(b: Binding) -> tuple[list[str], list[str]]:
    """The chip's ports of its input pads and of its output pads."""
    ins = sorted(p for p, inst in b.chip_ports.items() if "p2c" in b.io.get(inst, {}))
    outs = sorted(p for p, inst in b.chip_ports.items() if "c2p" in b.io.get(inst, {}))
    return ins, outs


def reference_period(b: Binding, clocks: Mapping[str, float]) -> float:
    """The period the IO delays refer to: the first clock's, or the virtual clock's."""
    first = next(iter(b.clocks), None)
    return clocks[first] if first is not None else DEFAULT_PERIOD_NS


def signoff_sdc(
    b: Binding,
    clocks: Mapping[str, float],
    numbers: Mapping[str, float],
    uncertainty: float,
    derate_pct: float = 0.0,
) -> str:
    """Constraints at the chip's ports: each clock on its pad's port, so its input pad is
    part of its insertion delay; the data ports with the outside world's budget, the
    same number for the earliest and the latest arrival (as LibreLane's IO constraint);
    the PDK's derate, as LibreLane timed the core with."""
    ins, outs = pad_ports(b)
    clock_ports = {f"pad_{flat(p)}" for p in b.clocks}
    lines = ["# Written by ChipLeap: the chip timed at its ports, through its pads."]
    if b.clocks:
        for port in b.clocks:
            name = flat(port)
            lines.append(
                f"create_clock -name {q.plain(name)} -period {q.number(clocks[port])} "
                f"[get_ports {q.plain('pad_' + name)}]"
            )
        ref = flat(next(iter(b.clocks)))
    else:
        ref = "__VIRTUAL_CLK__"
        lines.append(f"create_clock -name {ref} -period {q.number(DEFAULT_PERIOD_NS)}")
    data_in = [p for p in ins if p not in clock_ports]
    lines += [
        "set_propagated_clock [all_clocks]",
        f"set_clock_uncertainty {q.number(uncertainty)} [all_clocks]",
        f"set_timing_derate -early {q.number(1 - derate_pct / 100)}",
        f"set_timing_derate -late {q.number(1 + derate_pct / 100)}",
    ]
    if data_in:
        lines.append(
            f"set_input_delay {q.number(numbers['input_delay_ns'])} -clock {ref} "
            f"[get_ports {q.tcl_list(data_in)}]"
        )
    if ins:
        lines.append(
            f"set_input_transition {q.number(numbers['input_transition_ns'])} "
            f"[get_ports {q.tcl_list(ins)}]"
        )
    if outs:
        lines += [
            f"set_output_delay {q.number(numbers['output_delay_ns'])} -clock {ref} "
            f"[get_ports {q.tcl_list(outs)}]",
            f"set_load {q.number(numbers['output_load_pf'])} [get_ports {q.tcl_list(outs)}]",
        ]
    return "\n".join(lines) + "\n"


# The IO liberty


def _plain_liberty(text: str) -> str:
    """Liberty text without comments and line continuations."""
    if len(text) > LIBERTY_MAX:
        raise IoTimingError("the IO liberty is larger than expected")
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S).replace("\\\n", " ")


def _groups(text: str, kind: str, name: str | None = None) -> Iterator[tuple[str, str]]:
    """Each ``kind (name) { ... }`` group of liberty text at any depth, as (its name, its
    body), braces matched; only those named ``name`` when given."""
    head = re.compile(rf"\b{kind}\s*\(\s*\"?([^\")]*?)\"?\s*\)\s*\{{")
    for m in head.finditer(text):
        if name is not None and m.group(1) != name:
            continue
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        yield m.group(1), text[m.end() : i - 1]


def _first(text: str, kind: str, name: str) -> str | None:
    return next((body for _, body in _groups(text, kind, name)), None)


def _arc(liberty: str, cell: str) -> tuple[str | None, str]:
    """The timing group of ``cell``'s pad arc with both delay tables, or None and what
    is missing."""
    arc = PAD_ARCS.get(cell)
    if arc is None:
        return None, f"{cell} has no pad arc ChipLeap knows"
    src, dst = arc
    body = _first(liberty, "cell", cell)
    if body is None:
        return None, f"{cell} is not in the IO liberty"
    pin = _first(body, "pin", dst)
    if pin is None:
        return None, f"{cell} has no pin {dst}"
    lacking = ""
    for _, timing in _groups(pin, "timing"):
        related = re.search(r"\brelated_pin\s*:\s*\"?(\w+)\"?\s*;", timing)
        if related is None or related.group(1) != src:
            continue
        absent = [t for t in DELAY_TABLES if not re.search(rf"\b{t}\s*\(", timing)]
        if not absent:
            return timing, ""
        lacking = f"{cell}'s {src} to {dst} arc has no {' or '.join(absent)} table"
    return None, lacking or f"{cell} has no {src} to {dst} arc"


def missing_arcs(liberty: str, cells: Iterable[str]) -> dict[str, str]:
    """What the liberty lacks, by cell, among ``cells``: a pad arc with a delay table for
    both edges. An IO cell whose arc ChipLeap does not know is reported too."""
    text = _plain_liberty(liberty)
    out = {}
    for cell in sorted(set(cells)):
        timing, problem = _arc(text, cell)
        if timing is None:
            out[cell] = problem
    return out


def _index(timing: str, table: str, n: int) -> list[float]:
    """The ``index_n`` axis of the first ``table`` group in a timing group."""
    body = next((b for _, b in _groups(timing, table)), "")
    m = re.search(rf"\bindex_{n}\s*\(\s*\"([^\"]*)\"", body)
    try:
        return [float(v) for v in m.group(1).split(",")] if m else []
    except ValueError:
        return []


def _table(timing: str, table: str) -> tuple[list[float], list[float], list[list[float]]]:
    """A two-dimensional delay table of a timing group: its axes and rows."""
    body = next((b for _, b in _groups(timing, table)), "")
    x1, x2 = _index(timing, table, 1), _index(timing, table, 2)
    m = re.search(r"\bvalues\s*\((.*?)\)\s*;", body, re.S)
    rows: list[list[float]] = []
    if m is not None:
        try:
            rows = [
                [float(v) for v in r.split(",")] for r in re.findall(r"\"([^\"]*)\"", m.group(1))
            ]
        except ValueError:
            rows = []
    if len(x1) < 2 or len(x2) < 2 or len(rows) != len(x1) or any(len(r) != len(x2) for r in rows):
        raise IoTimingError(f"a {table} table of IHP's IO liberty is not a table ChipLeap reads")
    return x1, x2, rows


def _between(axis: list[float], x: float) -> tuple[int, float]:
    """The pair of axis points around ``x`` (the first or last pair beyond the axis) and
    where ``x`` lies along it, as OpenSTA interpolates and extrapolates."""
    i = max(
        0, min(len(axis) - 2, next((n - 1 for n, v in enumerate(axis) if v > x), len(axis) - 2))
    )
    return i, (x - axis[i]) / (axis[i + 1] - axis[i])


def lookup(timing: str, table: str, x1: float, x2: float) -> float:
    """A delay table's value at ``x1`` (the input edge, ns) and ``x2`` (the load, pF)."""
    a1, a2, rows = _table(timing, table)
    i, t = _between(a1, x1)
    j, u = _between(a2, x2)
    return (
        (1 - t) * (1 - u) * rows[i][j]
        + t * (1 - u) * rows[i + 1][j]
        + (1 - t) * u * rows[i][j + 1]
        + t * u * rows[i + 1][j + 1]
    )


def pad_delay(
    liberty: str, cell: str, edge_ns: float, load_pf: float
) -> tuple[float, float, float] | None:
    """A pad cell's arc at an input edge and a load: its rising, slower and faster edge's
    delay in ns. None when the liberty has no arc."""
    timing, _ = _arc(liberty, cell)
    if timing is None:
        return None
    rise = lookup(timing, "cell_rise", edge_ns, load_pf)
    fall = lookup(timing, "cell_fall", edge_ns, load_pf)
    return rise, max(rise, fall), min(rise, fall)


def core_budget(io_dir: Path, numbers: Mapping[str, float]) -> CoreBudget:
    """The budget the pads leave the core at their core-side pins, for LibreLane
    (``chipleap.chip.netlist.sdc``): ``chip.io``'s delays and, at each of IHP's signoff
    corners (``SIGNOFF_CORNERS``), an input pad's arc at ``chip.io``'s input edge rate
    and the load ``CORE_LOAD_PF`` (a clock pad's too), and an output pad's at the edge
    ``CORE_EDGE_NS`` and ``chip.io``'s output load. A corner whose IO liberty lacks an arc
    gives that pad no delay; the timing at the ports finds the pad untimed, as before."""
    pads = []
    for process, volts, temp in SIGNOFF_CORNERS:
        lib = io_library(io_dir, process, volts, temp)
        text = _plain_liberty(lib.read_text(errors="replace"))
        d_in = pad_delay(text, PAD_CELLS["in"], numbers["input_transition_ns"], CORE_LOAD_PF)
        d_out = pad_delay(text, PAD_CELLS["out"], CORE_EDGE_NS, numbers["output_load_pf"])
        pads.append(
            PadDelay(
                process,
                d_in[1] if d_in else None,
                d_in[2] if d_in else None,
                d_in[0] if d_in else None,
                d_out[1] if d_out else None,
                d_out[2] if d_out else None,
            )
        )
    return CoreBudget(numbers["input_delay_ns"], numbers["output_delay_ns"], tuple(pads))


def characterised(liberty: str, numbers: Mapping[str, float], cells: Iterable[str]) -> list[str]:
    """The budget's numbers outside the range IHP characterised the pads for, where
    OpenSTA extrapolates: the input edge rate on an input pad's table, the load on an
    output pad's."""
    text = _plain_liberty(liberty)
    out = []
    checks = {
        PAD_CELLS["in"]: ("input_transition_ns", 1, "ns"),
        PAD_CELLS["out"]: ("output_load_pf", 2, "pF"),
    }
    for cell in sorted(set(cells)):
        if cell not in checks:
            continue
        timing, _ = _arc(text, cell)
        key, n, unit = checks[cell]
        axis = _index(timing, "cell_rise", n) if timing is not None else []
        if axis and not min(axis) <= numbers[key] <= max(axis):
            out.append(
                f"chip.io.{key} {numbers[key]:g} {unit} is outside the {min(axis):g} to "
                f"{max(axis):g} {unit} {cell} is characterised for; its delay is extrapolated"
            )
    return out


# LibreLane's signoff


def _volts(text: str) -> float:
    return float(text.replace("p", "."))


def _celsius(text: str) -> float:
    return -float(text[1:]) if text.startswith("m") else float(text)


def _pdk_file(value: Any, pdk_root: Path) -> Path:
    """A file LibreLane names in its resolved configuration, which must be in the PDK it
    installed for itself."""
    if not isinstance(value, str) or not value:
        raise IoTimingError("LibreLane's resolved configuration names a file that is not a path")
    path = Path(value)
    if not path.resolve().is_relative_to(pdk_root.resolve()) or not path.is_file():
        raise IoTimingError(f"LibreLane read {path.name} from outside its PDK")
    return path


def pdk_root(resolved: Mapping[str, Any]) -> Path:
    root = Path(str(resolved.get("PDK_ROOT", "")))
    if not root.is_absolute() or not root.is_dir():
        raise IoTimingError("LibreLane's resolved configuration names no PDK")
    return root


def io_library(io_dir: Path, process: str, core_v: float, temp: float) -> Path:
    """IHP's IO liberty of a process at a core supply and temperature: exactly one."""
    found = []
    for f in sorted(io_dir.glob(f"{IO}_*.lib")):
        m = IO_LIB_RE.fullmatch(f.name)
        if m is None or m.group(1) != process:
            continue
        if math.isclose(_volts(m.group(2)), core_v) and _celsius(m.group(4)) == temp:
            found.append(f)
    if len(found) != 1:
        what = "no" if not found else f"{len(found)}"
        raise IoTimingError(
            f"IHP's IO library has {what} {process} liberty at {core_v:g} V and {temp:g} C"
        )
    return found[0]


def corners(resolved: Mapping[str, Any], io_dir: Path) -> list[Corner]:
    """The corners LibreLane signed the core off at, each with its standard cells'
    liberty and IHP's IO liberty of the same process, core supply and temperature. The
    parasitics are the nominal extraction, so a ``min_`` or ``max_`` corner is timed as
    its ``nom_`` one; a corner ChipLeap cannot match is an error."""
    names = resolved.get("STA_CORNERS")
    libs = resolved.get("LIB")
    if not isinstance(names, list) or not isinstance(libs, dict):
        raise IoTimingError("LibreLane's resolved configuration has no signoff corners")
    root = pdk_root(resolved)
    out: dict[str, Corner] = {}
    for name in names:
        m = STA_CORNER_RE.fullmatch(str(name))
        if m is None:
            raise IoTimingError(
                f"LibreLane's corner {str(name)[:60]} has no IO liberty ChipLeap knows"
            )
        nominal = f"nom_{str(name).split('_', 1)[1]}"
        if nominal in out:
            continue
        files = next((v for k, v in libs.items() if fnmatchcase(nominal, str(k))), None)
        if not isinstance(files, list) or not files:
            raise IoTimingError(f"LibreLane's resolved configuration has no liberty for {nominal}")
        process, volts, temp = m.group(2), _volts(m.group(3)), _celsius(m.group(4))
        out[nominal] = Corner(
            nominal,
            process,
            tuple(_pdk_file(f, root) for f in files),
            io_library(io_dir, process, volts, temp),
        )
    if not any(c.process == "slow" for c in out.values()):
        raise IoTimingError("LibreLane's resolved configuration has no slow corner")
    return sorted(out.values(), key=lambda c: ("typ", "slow", "fast").index(c.process))


def uncertainty(resolved: Mapping[str, Any]) -> float:
    """The clock uncertainty LibreLane timed the core with."""
    v = resolved.get("CLOCK_UNCERTAINTY_CONSTRAINT")
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= UNCERTAINTY_MAX_NS:
        raise IoTimingError("LibreLane's resolved configuration has no clock uncertainty")
    return float(v)


def derate(resolved: Mapping[str, Any]) -> float:
    """The timing derate LibreLane timed the core with, in percent (0 without one)."""
    v = resolved.get("TIME_DERATING_CONSTRAINT", 0)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= DERATE_MAX_PCT:
        raise IoTimingError("LibreLane's resolved configuration has no usable timing derate")
    return float(v)


# OpenSTA

SCRIPT_CATEGORIES = {
    # name: report_checks options (ChipLeap's own text; the port lists are quoted)
    "pad_to_core": "-from $data_in -to [all_registers -data_pins]",
    "core_to_pad": "-from [all_registers -clock_pins] -to $data_out",
    "pad_to_pad": "-from $data_in -to $data_out",
}


def script(
    files: Mapping[str, Any],
    top: str,
    sdc: str,
    ins: list[str],
    outs: list[str],
    with_paths: bool,
) -> str:
    """OpenSTA in OpenROAD: the routed chip at one corner, its worst slacks and, with
    ``with_paths``, its worst paths by category, input pins shown so a pad's own delay
    is apart from the wire's into it."""
    lines = [
        *(f"read_lef {q.tcl(f)}" for f in files["lefs"]),
        *(f"read_liberty {q.tcl(f)}" for f in files["libs"]),
        f"read_verilog {q.tcl(files['netlist'])}",
        f"link_design {q.tcl(top)}",
        f"read_sdc {q.tcl(sdc)}",
        f"read_spef {q.tcl(files['spef'])}",
        'puts "CHIPLEAP-WORST setup [sta::format_time [sta::worst_slack_cmd max] 4]"',
        'puts "CHIPLEAP-WORST hold [sta::format_time [sta::worst_slack_cmd min] 4]"',
    ]
    if with_paths:
        lines += [
            f"set data_in [get_ports -quiet {q.tcl_list(ins)}]",
            f"set data_out [get_ports -quiet {q.tcl_list(outs)}]",
        ]
        for name, opts in SCRIPT_CATEGORIES.items():
            needs = [v for v in ("$data_in", "$data_out") if v in opts]
            guard = " && ".join(f"[llength {v}] > 0" for v in needs)
            for kind in ("max", "min"):
                lines += [
                    f'puts "CHIPLEAP-PATHS begin {name} {kind}"',
                    f"if {{{guard}}} {{",
                    f"  report_checks {opts} -path_delay {kind} "
                    f"-group_path_count {PATHS_SHOWN} -fields {{input_pin}} -digits 4",
                    "}",
                    f'puts "CHIPLEAP-PATHS end {name} {kind}"',
                ]
    return "\n".join(lines) + "\n"


def parse_worst(log: str) -> tuple[float | None, float | None]:
    out: dict[str, float | None] = {"setup": None, "hold": None}
    for kind, value in re.findall(r"^CHIPLEAP-WORST (setup|hold) (\S+)", log, re.M):
        try:
            v = float(value)
        except ValueError:
            continue
        # OpenSTA reports a huge slack when nothing is constrained.
        out[kind] = v if abs(v) < 1e6 else None
    return out["setup"], out["hold"]


def parse_categories(log: str, pad_pins: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Each category's worst paths, with the delay at the pads' output pins (``pad_pins``,
    ``instance/pin``): the arcs through the pads, without the wires into them."""
    out: dict[str, list[dict[str, Any]]] = {}
    for name, kind in re.findall(r"^CHIPLEAP-PATHS begin (\w+) (max|min)", log, re.M):
        start = log.find(f"CHIPLEAP-PATHS begin {name} {kind}")
        end = log.find(f"CHIPLEAP-PATHS end {name} {kind}", start)
        section = log[start:end] if end > start else ""
        found = parse_paths(f"CHIPLEAP-PATHS begin\n{section}\nCHIPLEAP-PATHS end", kind)
        key = f"{name}_{'setup' if kind == 'max' else 'hold'}"
        out[key] = [_path(p, pad_pins) for p in sorted(found, key=lambda p: p.slack_ns)]
    return out


def _path(p: TimingPath, pad_pins: set[str]) -> dict[str, Any]:
    d = p.to_dict()
    d["pad_delay_ns"] = round(sum(s.delay_ns for s in p.stages if s.pin in pad_pins), 4)
    return d


def _inside(path: Path, root: Path, what: str) -> Path:
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise IoTimingError(f"LibreLane's run has no {what}")
    return path


def run(
    work: Path,
    run_dir: Path,
    pdk: Path,
    name: str,
    b: Binding,
    pad_cells: Mapping[str, str],
    clocks: Mapping[str, float],
    io: IoBudget,
    macro_files: Iterable[tuple[Path, Path]],
    io_lef: Path,
) -> IoTiming:
    """Time the routed chip in ``run_dir`` at its ports at every corner. ``pad_cells``
    is each signal pad instance's IO cell, ``macro_files`` each macro's LEF and liberty,
    ``io_lef`` the IO library's LEF the flow used."""
    resolved = json.loads(read_limited(run_dir / "resolved.json"))
    root = pdk_root(resolved)
    signoff = corners(resolved, pdk / "libs.ref" / IO / "lib")
    netlist = _inside(run_dir / "final" / "nl" / f"{name}.nl.v", run_dir, f"{name}.nl.v")
    spef = _inside(
        run_dir / "final" / "spef" / "nom" / f"{name}.nom.spef", run_dir, f"{name}.nom.spef"
    )
    # The nominal corner's tech LEF: LibreLane 3.0.0.dev44 names one per corner
    # ("nom_*"), LibreLane 3.0 one for all of them ("*").
    tech = resolved.get("TECH_LEFS")
    techs = list(tech.items()) if isinstance(tech, dict) else []
    tech_lef = next((v for k, v in techs if str(k).startswith("nom")), None) or next(
        (v for k, v in techs if k == "*"), None
    )
    cell_lefs = resolved.get("CELL_LEFS")
    if tech_lef is None or not isinstance(cell_lefs, list) or not cell_lefs:
        raise IoTimingError("LibreLane's resolved configuration names no LEF")
    lefs = [_pdk_file(tech_lef, root), *(_pdk_file(f, root) for f in cell_lefs)]
    numbers = budget(io, reference_period(b, clocks))
    sdc_text = signoff_sdc(b, clocks, numbers, uncertainty(resolved), derate(resolved))
    ins, outs = pad_ports(b)
    clock_ports = {f"pad_{flat(p)}" for p in b.clocks}
    data_in = [p for p in ins if p not in clock_ports]
    ports_of: dict[str, list[str]] = {}
    for port, inst in b.chip_ports.items():
        if inst in pad_cells:
            ports_of.setdefault(pad_cells[inst], []).append(port)
    pad_pins = {
        f"{inst}/{PAD_ARCS[cell][1]}" for inst, cell in pad_cells.items() if cell in PAD_ARCS
    }
    untimed: list[str] = []
    warnings: list[str] = []
    results: list[CornerTiming] = []
    paths: dict[str, list[dict[str, Any]]] = {}
    macro_list = list(macro_files)
    for corner in signoff:
        text = read_limited(corner.io_lib, LIBERTY_MAX)
        for cell, problem in missing_arcs(text, pad_cells.values()).items():
            who = ", ".join(sorted(ports_of.get(cell, [])))
            entry = f"{problem} ({corner.process} corner): {who} not timed"
            if entry not in untimed:
                untimed.append(entry)
        for w in characterised(text, numbers, pad_cells.values()):
            if w not in warnings:
                warnings.append(w)
        job = Job(work, f"io_timing_{corner.process}")
        files = {
            "lefs": [
                job.stage(lefs[0], "tech.lef"),
                *(job.stage(f, f"cells_{i}.lef") for i, f in enumerate(lefs[1:])),
                job.stage(io_lef, "io.lef"),
                *(job.stage(lef, f"macro_{i}.lef") for i, (lef, _) in enumerate(macro_list)),
            ],
            "libs": [
                *(job.stage(f, f"cells_{i}.lib") for i, f in enumerate(corner.std_libs)),
                job.stage(corner.io_lib, "io.lib"),
                *(job.stage(lib, f"macro_{i}.lib") for i, (_, lib) in enumerate(macro_list)),
            ],
            "netlist": job.stage(netlist, "chip.nl.v"),
            "spef": job.stage(spef, "chip.spef"),
        }
        sdc = builddir.write_text(job.dir, "chip_io.sdc", sdc_text)
        slow = corner.process == "slow"
        log, code = job.run(script(files, name, sdc.name, data_in, outs, slow))
        if code != 0 or "CHIPLEAP-WORST hold" not in log:
            raise failure("OpenSTA at the chip's ports", log, code)
        setup, hold = parse_worst(log)
        results.append(CornerTiming(corner.process, setup, hold))
        if slow:
            paths = parse_categories(log, pad_pins)
    return IoTiming(results, paths, untimed, numbers, warnings)
