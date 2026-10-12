"""The chip's timing at its ports: the pads' own delays in the signoff.

LibreLane times the core against its side of the pads (the IO cells' ``p2c`` and ``c2p``
pins), with an IO liberty that has no arcs, so the pads' delays are not in its numbers.
After routing, ChipLeap times the chip again with OpenSTA (in OpenROAD) at every corner
the core was signed off at: the routed netlist and its parasitics, the standard cells'
liberty of that corner, IHP's ``sg13g2_io`` liberty of the matching corner, which has
the pads' arcs (``pad`` to ``p2c`` of an input pad, ``c2p`` to ``pad`` of an output pad)
and the macros' liberty. The clocks are created on the chip's own ports, so a clock's
path through its input pad is part of its insertion delay, and the data ports get the
budget of the world outside (``chip.io``): an input delay, an output delay, the load an
output pad drives and the edge rate at an input pad.

The report gives each corner's worst setup and hold slack and, at the slow corner, the
worst pad-to-core, core-to-pad and pad-to-pad paths with the share of their delay inside
the pads. A pad whose IO cell has no arc in the liberty is reported as not timed, and
the chip's IO timing does not pass; its delay is never taken as zero.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.chip.config import DEFAULT_IO_DELAY_PCT, IoBudget
from chipleap.chip.io import IO, PAD_CELLS
from chipleap.chip.netlist import Binding, flat
from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.eco import TimingPath, parse_paths
from chipleap.rtl.openroad import Job, failure, read_limited
from chipleap.rtl.synth import DEFAULT_PERIOD_NS

# IHP's IO liberty for each corner of the standard cells (core supply, IO supply).
IO_CORNERS = {
    "typ": "typ_1p2V_3p3V_25C",
    "slow": "slow_1p08V_3p0V_125C",
    "fast": "fast_1p32V_3p6V_m40C",
}
# The arc each signal pad's cell must have: (from pin, to pin).
PAD_ARCS = {PAD_CELLS["in"]: ("pad", "p2c"), PAD_CELLS["out"]: ("c2p", "pad")}
PATHS_SHOWN = 5
LIBERTY_MAX = 64 << 20
CORNER_RE = re.compile(r"nom_(typ|slow|fast)_[A-Za-z0-9_]+")


class IoTimingError(RuntimeError):
    pass


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
            "met": self.met,
        }


def budget(io: IoBudget, period_ns: float) -> dict[str, float]:
    """The numbers the constraints use: ``chip.io``, its delays defaulting to
    ``DEFAULT_IO_DELAY_PCT`` of the first clock's period."""
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


def signoff_sdc(b: Binding, clocks: Mapping[str, float], numbers: Mapping[str, float]) -> str:
    """Constraints at the chip's ports: each clock on its pad's port, so its input pad is
    part of its insertion delay; the data ports with the outside world's budget."""
    ins, outs = pad_ports(b)
    clock_ports = {f"pad_{flat(p)}" for p in b.clocks}
    lines = ["# Written by ChipLeap: the chip timed at its ports, through its pads."]
    if b.clocks:
        for port in b.clocks:
            name = flat(port)
            lines.append(
                f"create_clock -name {q.plain(name)} -period {clocks[port]:g} "
                f"[get_ports {q.plain('pad_' + name)}]"
            )
        ref = flat(next(iter(b.clocks)))
    else:
        ref = "__VIRTUAL_CLK__"
        lines.append(f"create_clock -name {ref} -period {DEFAULT_PERIOD_NS:g}")
    data_in = [p for p in ins if p not in clock_ports]
    lines += ["set_propagated_clock [all_clocks]", "set_clock_uncertainty 0.25 [all_clocks]"]
    if data_in:
        lines.append(
            f"set_input_delay {numbers['input_delay_ns']:g} -clock {ref} "
            f"[get_ports {q.tcl_list(data_in)}]"
        )
    if ins:
        lines.append(
            f"set_input_transition {numbers['input_transition_ns']:g} [get_ports {q.tcl_list(ins)}]"
        )
    if outs:
        lines += [
            f"set_output_delay {numbers['output_delay_ns']:g} -clock {ref} "
            f"[get_ports {q.tcl_list(outs)}]",
            f"set_load {numbers['output_load_pf']:g} [get_ports {q.tcl_list(outs)}]",
        ]
    return "\n".join(lines) + "\n"


def _group(text: str, kind: str, name: str) -> str:
    """The body of ``kind (name) { ... }`` in liberty text, braces matched; "" if absent."""
    m = re.search(rf"\b{kind}\s*\(\s*\"?{re.escape(name)}\"?\s*\)\s*\{{", text)
    if m is None:
        return ""
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i]


def missing_arcs(liberty: str, cells: Iterable[str]) -> list[str]:
    """The cells among ``cells`` whose pad arc (``PAD_ARCS``) the liberty lacks."""
    if len(liberty) > LIBERTY_MAX:
        raise IoTimingError("the IO liberty is larger than expected")
    out = []
    for cell in sorted(set(cells)):
        arc = PAD_ARCS.get(cell)
        if arc is None:
            continue
        pin = _group(_group(liberty, "cell", cell), "pin", arc[1])
        if not re.search(rf"related_pin\s*:\s*\"?{arc[0]}\"?\s*;", pin):
            out.append(f"{cell} has no {arc[0]} to {arc[1]} arc")
    return out


def corners(resolved: Mapping[str, Any]) -> dict[str, list[Path]]:
    """The signoff corners LibreLane used (typ, slow, fast) with their standard-cell
    liberty files, from its resolved configuration."""
    libs = resolved.get("LIB") or {}
    out: dict[str, list[Path]] = {}
    for name in resolved.get("STA_CORNERS") or []:
        m = CORNER_RE.fullmatch(str(name))
        files = libs.get(name) if isinstance(libs, dict) else None
        if m is None or not isinstance(files, list):
            continue
        out[m.group(1)] = [Path(f) for f in files if isinstance(f, str)]
    if "slow" not in out:
        raise IoTimingError("LibreLane's resolved configuration has no slow corner")
    return out


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
    ``with_paths``, its worst paths by category."""
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
            for kind in ("max", "min"):
                guard = "$data_in" if "data_in" in opts else "$data_out"
                if "data_in" in opts and "data_out" in opts:
                    guard = "[concat $data_in $data_out]"
                lines += [
                    f'puts "CHIPLEAP-PATHS begin {name} {kind}"',
                    f"if {{[llength {guard}] > 0}} {{",
                    f"  report_checks {opts} -path_delay {kind} "
                    f"-group_path_count {PATHS_SHOWN} -digits 4",
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


def parse_categories(log: str, pads: set[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, kind in re.findall(r"^CHIPLEAP-PATHS begin (\w+) (max|min)", log, re.M):
        start = log.find(f"CHIPLEAP-PATHS begin {name} {kind}")
        end = log.find(f"CHIPLEAP-PATHS end {name} {kind}", start)
        section = log[start:end] if end > start else ""
        found = parse_paths(f"CHIPLEAP-PATHS begin\n{section}\nCHIPLEAP-PATHS end", kind)
        key = f"{name}_{'setup' if kind == 'max' else 'hold'}"
        out[key] = [_path(p, pads) for p in sorted(found, key=lambda p: p.slack_ns)]
    return out


def _path(p: TimingPath, pads: set[str]) -> dict[str, Any]:
    d = p.to_dict()
    d["pad_delay_ns"] = round(sum(s.delay_ns for s in p.stages if s.instance in pads), 4)
    return d


def run(
    work: Path,
    run_dir: Path,
    pdk: Path,
    name: str,
    b: Binding,
    clocks: Mapping[str, float],
    io: IoBudget,
    macro_files: Iterable[tuple[Path, Path]],
    io_lef: Path,
) -> IoTiming:
    """Time the routed chip in ``run_dir`` at its ports at every corner. ``macro_files``
    are each macro's LEF and liberty, ``io_lef`` the IO library's LEF the flow used."""
    resolved = json.loads(read_limited(run_dir / "resolved.json"))
    std = corners(resolved)
    netlist = run_dir / "final" / "nl" / f"{name}.nl.v"
    spef = run_dir / "final" / "spef" / "nom" / f"{name}.nom.spef"
    for f in (netlist, spef):
        if not f.is_file():
            raise IoTimingError(f"LibreLane's run has no {f.name}")
    tech = resolved.get("TECH_LEFS") or {}
    tech_lef = next((Path(v) for k, v in tech.items() if str(k).startswith("nom")), None)
    cell_lefs = [Path(v) for v in resolved.get("CELL_LEFS") or [] if isinstance(v, str)]
    if tech_lef is None or not cell_lefs:
        raise IoTimingError("LibreLane's resolved configuration names no LEF")
    period = next(iter(clocks.values()), DEFAULT_PERIOD_NS)
    numbers = budget(io, period)
    ins, outs = pad_ports(b)
    clock_ports = {f"pad_{flat(p)}" for p in b.clocks}
    data_in = [p for p in ins if p not in clock_ports]
    used = _io_cells(b)
    untimed: list[str] = []
    results: list[CornerTiming] = []
    paths: dict[str, list[dict[str, Any]]] = {}
    pads = set(b.io)
    macro_list = list(macro_files)
    for corner in ("typ", "slow", "fast"):
        if corner not in std:
            continue
        io_lib = pdk / "libs.ref" / IO / "lib" / f"{IO}_{IO_CORNERS[corner]}.lib"
        if not io_lib.is_file():
            raise IoTimingError(f"IHP's IO library has no {corner} corner ({io_lib.name})")
        text = read_limited(io_lib, LIBERTY_MAX)
        for problem in missing_arcs(text, used.values()):
            entry = f"{problem} ({corner} corner): its pads are not timed"
            if entry not in untimed:
                untimed.append(entry)
        job = Job(work, f"io_timing_{corner}")
        files = {
            "lefs": [
                job.stage(tech_lef, "tech.lef"),
                *(job.stage(f, f"cells_{i}.lef") for i, f in enumerate(cell_lefs)),
                job.stage(io_lef, "io.lef"),
                *(job.stage(lef, f"macro_{i}.lef") for i, (lef, _) in enumerate(macro_list)),
            ],
            "libs": [
                *(job.stage(f, f"cells_{i}.lib") for i, f in enumerate(std[corner])),
                job.stage(io_lib, "io.lib"),
                *(job.stage(lib, f"macro_{i}.lib") for i, (_, lib) in enumerate(macro_list)),
            ],
            "netlist": job.stage(netlist, "chip.nl.v"),
            "spef": job.stage(spef, "chip.spef"),
        }
        sdc = builddir.write_text(job.dir, "chip_io.sdc", signoff_sdc(b, clocks, numbers))
        log, code = job.run(script(files, name, sdc.name, data_in, outs, corner == "slow"))
        if code != 0 or "CHIPLEAP-WORST hold" not in log:
            raise failure("OpenSTA at the chip's ports", log, code)
        setup, hold = parse_worst(log)
        results.append(CornerTiming(corner, setup, hold))
        if corner == "slow":
            paths = parse_categories(log, pads)
    return IoTiming(results, paths, untimed, numbers)


def _io_cells(b: Binding) -> dict[str, str]:
    """Each signal pad instance's IO cell, from the nets it carries."""
    out = {}
    for inst, conns in b.io.items():
        if "p2c" in conns:
            out[inst] = PAD_CELLS["in"]
        elif "c2p" in conns:
            out[inst] = PAD_CELLS["out"]
    return out
