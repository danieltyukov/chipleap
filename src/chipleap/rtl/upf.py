"""Basic power intent: UPF power domains read by OpenROAD for floorplanning and placement.

ChipLeap writes the UPF from the project's ``upf`` section, so every name in it has been
checked and is quoted: the top domain covers the design (``-elements {.}``), each other
domain names its hierarchical instances, and an area, given or sized here from the
domain's cells, becomes ``set_domain_area``. Synthesis keeps the module hierarchy so the
instances exist. OpenROAD links the hierarchical netlist, reads the UPF, floorplans
(each domain with an area becomes a placement region), places, and writes back the UPF
it understood. ChipLeap counts each domain's cells, checks they landed inside its area,
and lists the signals that cross between domains, flagging crossings between domains of
different voltages, which need level shifters IHP SG13G2's library does not have.

What the open flow cannot do: no open simulator is power aware, so a domain that is off
is never simulated as off; LibreLane routes a single supply and ignores the UPF;
isolation, retention and power switches need cells the library lacks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.netlist import Netlist, NetlistError, hierarchy, read_netlist
from chipleap.rtl.openroad import (
    Job,
    OpenroadError,
    OpenroadUnavailable,
    failure,
    pdk_views,
    read_limited,
)
from chipleap.rtl.project import PowerDomain, Project, UpfConfig
from chipleap.rtl.synth import SynthError, synthesize
from chipleap.verify.result import StepResult
from chipleap.verify.tools import excluding

SITE = "CoreSite"  # IHP SG13G2's standard-cell site
ROW_UM = 3.78
CORE_SPACE_UM = 5.0
# What OpenROAD keeps clear around a domain's region; measured on the UART, 10 to 12 um.
HALO_UM = 12.0
CROSSINGS_SHOWN = 10


class UpfError(RuntimeError):
    pass


@dataclass(frozen=True)
class CellLib:
    sizes: dict[str, tuple[float, float]]  # cell -> width, height (um)
    outputs: dict[str, set[str]]  # cell -> output pins


def read_lef(text: str) -> CellLib:
    """Cell sizes and output pins from a cell LEF."""
    sizes: dict[str, tuple[float, float]] = {}
    outputs: dict[str, set[str]] = {}
    for m in re.finditer(r"^\s*MACRO\s+(\S+)(.*?)^\s*END\s+\1\b", text, re.M | re.S):
        name, body = m.group(1), m.group(2)
        size = re.search(r"\bSIZE\s+([\d.]+)\s+BY\s+([\d.]+)", body)
        if size:
            sizes[name] = (float(size.group(1)), float(size.group(2)))
        outputs[name] = set(re.findall(r"^\s*PIN\s+(\S+)\s*\n\s*DIRECTION\s+OUTPUT\b", body, re.M))
    return CellLib(sizes, outputs)


def write_upf(upf: UpfConfig, areas: dict[str, tuple[float, float, float, float]]) -> str:
    """The UPF for the domains, with each domain's area (given or sized)."""
    lines = []
    for d in upf.domains:
        if d.elements:
            lines.append(f"create_power_domain {q.tcl(d.name)} -elements {q.tcl_list(d.elements)}")
        else:
            lines.append(f"create_power_domain {q.tcl(d.name)} -elements {{.}}")
    for d in upf.domains:
        if d.voltage is not None:
            lines.append(
                f"set_domain_voltage -domain {q.tcl(d.name)} -voltage {q.number(float(d.voltage))}"
            )
    for name, (x0, y0, x1, y1) in areas.items():
        box = " ".join(q.number(float(v)) for v in (x0, y0, x1, y1))
        lines.append(f"set_domain_area {q.tcl(name)} -area {{{box}}}")
    return "\n".join(lines) + "\n"


@dataclass
class Flat:
    """A hierarchical netlist flattened to leaf cells: each with its path, cell, domain and
    the nets (merged across the hierarchy) on its pins."""

    cells: list[tuple[str, str, str, dict[str, list[str]]]] = field(default_factory=list)
    parent: dict[str, str] = field(default_factory=dict)
    ports: dict[str, str] = field(default_factory=dict)  # top port bit -> direction

    def find(self, bit: str) -> str:
        root = bit
        while self.parent.get(root, root) != root:
            root = self.parent[root]
        while self.parent.get(bit, bit) != root:
            self.parent[bit], bit = root, self.parent[bit]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def flatten(nl: Netlist, top: str, domains: tuple[PowerDomain, ...]) -> Flat:
    """Leaf cells with their domain: the domain whose element is the longest prefix of the
    cell's path, else the top domain. The hierarchy is walked with ``netlist.hierarchy``,
    which refuses loops and bounds its depth and size."""
    owner = {e: d.name for d in domains for e in d.elements}
    top_domain = next(d.name for d in domains if not d.elements)
    flat = Flat()
    mod = nl.top(top)
    for p in mod.ports:
        for b in mod.bits(p):
            flat.ports[b] = mod.directions.get(p, "")
    found: set[str] = set()
    # Each module's domain, set when its parent is visited, which is always first.
    domain_of: dict[str, str] = {"": top_domain}
    for path, m in hierarchy(nl, top):
        domain = domain_of.pop(path)
        prefix = f"{path}/" if path else ""
        for lhs, rhs in m.assigns:
            for a, b in zip(reversed(lhs), reversed(rhs), strict=False):
                flat.union(prefix + a, prefix + b)
        for inst in m.instances:
            ipath = prefix + inst.name
            dom = owner.get(ipath, domain)
            if ipath in owner:
                found.add(ipath)
            child = nl.modules.get(inst.cell)
            if child is None:
                pins = {pin: [prefix + b for b in bits] for pin, bits in inst.pins.items()}
                flat.cells.append((ipath, inst.cell, dom, pins))
                continue
            for pin, bits in inst.pins.items():
                for a, b in zip(reversed(child.bits(pin)), reversed(bits), strict=False):
                    flat.union(f"{ipath}/{a}", prefix + b)
            domain_of[ipath] = dom
    missing = sorted(set(owner) - found)
    if missing:
        raise UpfError(f"power domain element {missing[0]} is not an instance in the design")
    return flat


def crossings(flat: Flat, lib: CellLib, domains: tuple[PowerDomain, ...]) -> list[dict[str, Any]]:
    """Nets driven in one domain and read in another, grouped by the pair of domains. Top
    ports belong to the top domain."""
    top_domain = next(d.name for d in domains if not d.elements)
    drivers: dict[str, str] = {}
    readers: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for _, cell, dom, pins in flat.cells:
        outs = lib.outputs.get(cell, set())
        for pin, bits in pins.items():
            for b in bits:
                net = flat.find(b)
                names.setdefault(net, b)
                if pin in outs:
                    drivers[net] = dom
                else:
                    readers.setdefault(net, set()).add(dom)
    for bit, direction in flat.ports.items():
        net = flat.find(bit)
        names[net] = bit
        if direction == "input":
            drivers[net] = top_domain
        elif direction == "output":
            readers.setdefault(net, set()).add(top_domain)
    pairs: dict[tuple[str, str], list[str]] = {}
    for net, src in drivers.items():
        for dst in sorted(readers.get(net, set()) - {src}):
            pairs.setdefault((src, dst), []).append(names[net])
    voltage = {d.name: d.voltage for d in domains}
    return [
        {
            "from": src,
            "to": dst,
            "nets": len(nets),
            "examples": sorted(nets)[:CROSSINGS_SHOWN],
            "needs_level_shifters": voltage.get(src) is not None
            and voltage.get(dst) is not None
            and voltage[src] != voltage[dst],
        }
        for (src, dst), nets in sorted(pairs.items())
    ]


def domain_areas(
    flat: Flat, lib: CellLib, project: Project
) -> tuple[dict[str, float], dict[str, tuple[float, float, float, float]], float]:
    """Each domain's cell area, the areas the UPF gives, and the side of the square core.
    A domain without an area gets a strip of the core's full height, as wide as its share
    of the cells, so every domain is placed at the project's utilisation. OpenROAD keeps
    about ``HALO_UM`` clear around a region, so the core grows by that much per domain."""
    upf = project.upf
    assert upf is not None
    cell_area: dict[str, float] = {d.name: 0.0 for d in upf.domains}
    for _, cell, dom, _ in flat.cells:
        w, h = lib.sizes.get(cell, (0.0, 0.0))
        cell_area[dom] += w * h
    total = sum(cell_area.values())
    side = math.sqrt(total / (project.pnr.utilisation / 100)) if total else 10.0
    others = [d for d in upf.domains if d.elements]
    core = side + HALO_UM * len(others) + 2
    areas: dict[str, tuple[float, float, float, float]] = {}
    x = CORE_SPACE_UM
    y0, y1 = CORE_SPACE_UM + ROW_UM, CORE_SPACE_UM + core - ROW_UM
    for d in others:
        if d.area_um is not None:
            areas[d.name] = d.area_um
            continue
        width = side * cell_area[d.name] / total if total else 1.0
        areas[d.name] = (round(x, 2), round(y0, 2), round(x + width, 2), round(y1, 2))
        x += width + HALO_UM
    return cell_area, areas, round(core, 2)


def upf_script(
    project: Project, files: dict[str, Any], netlist: str, upf_file: str, core: float
) -> str:
    density = project.pnr.density / 100
    cs = CORE_SPACE_UM
    die = round(core + 2 * cs, 2)
    lines = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        f"read_liberty {q.tcl(files['liberty']['typ'])}",
        f"read_verilog {q.tcl(netlist)}",
        f"link_design -hier {q.tcl(project.top)}",
        f"read_upf -file {q.tcl(upf_file)}",
        f"initialize_floorplan -die_area {{0 0 {q.number(die)} {q.number(die)}}} "
        f"-core_area {{{q.number(cs)} {q.number(cs)} {q.number(cs + core)} {q.number(cs + core)}}} "
        f"-site {q.tcl(SITE)}",
        "make_tracks",
        "place_pins -hor_layers Metal3 -ver_layers Metal2",
        f"global_placement -skip_io -density {q.number(density)}",
        "detailed_placement",
        # A cell detailed placement could not legalize is a finding, not a crash.
        "if {[catch {check_placement} err]} {",
        '    puts "CHIPLEAP-UPF check_placement $err"',
        "}",
        "set block [ord::get_db_block]",
        "foreach group [$block getGroups] {",
        "    set region [$block findRegion [$group getName]]",
        "    set inside 0",
        "    set outside 0",
        "    foreach inst [$group getInsts] {",
        "        set bb [$inst getBBox]",
        "        set in 0",
        '        if {$region ne "NULL"} {',
        "            foreach box [$region getBoundaries] {",
        "                if {[$bb xMin] >= [$box xMin] && [$bb xMax] <= [$box xMax] &&",
        "                    [$bb yMin] >= [$box yMin] && [$bb yMax] <= [$box yMax]} { set in 1 }",
        "            }",
        "        }",
        "        if {$in} { incr inside } else { incr outside }",
        "    }",
        '    puts "CHIPLEAP-UPF placed $inside $outside [$group getName]"',
        "}",
        'write_upf "openroad.upf"',
        'puts "CHIPLEAP-UPF end"',
    ]
    return "\n".join(lines) + "\n"


def parse_placed(log: str) -> dict[str, tuple[int, int]]:
    out = {}
    for m in re.finditer(r"CHIPLEAP-UPF placed (\d+) (\d+) (\S+)", log):
        out[m.group(3)] = (int(m.group(1)), int(m.group(2)))
    return out


@dataclass
class UpfResult:
    domains: list[dict[str, Any]]
    crossings: list[dict[str, Any]]
    upf: Path
    roundtrip: str
    placement_check: str = ""  # OpenROAD's complaint about the legalized placement, if any


def placement_check(log: str) -> str:
    """What ``check_placement`` found wrong, from OpenROAD's warnings, or ""."""
    if "CHIPLEAP-UPF check_placement" not in log:
        return ""
    found = re.findall(r"\[WARNING DPL-\d+\] (.+)", log)
    return "; ".join(dict.fromkeys(f.strip() for f in found)) or "the placement check failed"


def analyze(project: Project) -> UpfResult:
    upf = project.upf
    if upf is None:
        raise OpenroadUnavailable("no upf section in the project")
    work = project.work_dir("upf")
    views = pdk_views(project)
    try:
        synth = synthesize(project, work / "synth", flatten=False)
    except SynthError as exc:
        raise UpfError(f"synthesis failed: {exc}") from exc
    nl = read_netlist([synth.netlist])
    lib = read_lef("\n".join(read_limited(p) for p in views.cell_lefs))
    flat = flatten(nl, project.top, upf.domains)
    cell_area, areas, core = domain_areas(flat, lib, project)
    upf_file = builddir.write_text(work, f"{project.top}.upf", write_upf(upf, areas))
    job = Job(work, "upf")
    files = views.stage_all(job)
    script = upf_script(project, files, job.stage(synth.netlist), job.stage(upf_file), core)
    log, code = job.run(script)
    if code != 0 or "CHIPLEAP-UPF end" not in log:
        raise failure("OpenROAD with the UPF", log, code)
    placed = parse_placed(log)
    counts: dict[str, int] = {d.name: 0 for d in upf.domains}
    for _, _, dom, _ in flat.cells:
        counts[dom] += 1
    domains = []
    for d in upf.domains:
        inside, outside = placed.get(d.name, (0, 0))
        domains.append(
            {
                "name": d.name,
                "elements": list(d.elements) or ["."],
                "cells": counts[d.name],
                "cell_um2": round(cell_area[d.name], 2),
                "voltage": d.voltage,
                "area_um": list(areas[d.name]) if d.name in areas else None,
                "area_sized": d.name in areas and d.area_um is None,
                "placed_inside": inside if d.elements else None,
                "placed_outside": outside if d.elements else None,
            }
        )
    roundtrip = job.output("openroad.upf")
    return UpfResult(
        domains=domains,
        crossings=crossings(flat, lib, upf.domains),
        upf=upf_file,
        roundtrip=read_limited(roundtrip, 1 << 20) if roundtrip.is_file() else "",
        placement_check=placement_check(log),
    )


def upf_data(project: Project, r: UpfResult) -> dict[str, Any]:
    return {
        "tool": "OpenROAD",
        "domains": r.domains,
        "crossings": r.crossings,
        "upf": project.rel(r.upf),
        "openroad_upf": r.roundtrip,
        "placement_check": r.placement_check or "pass",
        "limits": "no power-aware simulation; LibreLane routes one supply; no level "
        "shifter, isolation or power switch cells in IHP SG13G2's library",
    }


def upf(project: Project) -> StepResult:
    """The UPF step: OpenROAD reads the power domains, places each domain's cells in its
    area, and the signals between domains are listed."""
    if project.upf is None:
        return StepResult("upf", "skipped", "no upf section in the project")
    try:
        with excluding(project.root):
            r = analyze(project)
    except OpenroadUnavailable as exc:
        return StepResult("upf", "skipped", str(exc))
    except (UpfError, OpenroadError, NetlistError, ValueError) as exc:
        return StepResult("upf", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    data = upf_data(project, r)
    problems = []
    for d in r.domains:
        if d["placed_outside"]:
            problems.append(f"{d['placed_outside']} cells of {d['name']} placed outside its area")
        if d["placed_inside"] == 0 and d["cells"]:
            problems.append(f"OpenROAD placed none of {d['name']}'s cells in its region")
    if r.placement_check:
        problems.append(f"OpenROAD's placement check fails: {r.placement_check}")
    shifters = [c for c in r.crossings if c["needs_level_shifters"]]
    if shifters:
        c = shifters[0]
        problems.append(
            f"{sum(x['nets'] for x in shifters)} signals cross between domains at different "
            f"voltages ({c['from']} to {c['to']}, such as {c['examples'][0]}) and need level "
            "shifters, which IHP SG13G2's standard cells do not have"
        )
    doms = ", ".join(
        f"{d['name']} {d['cells']} cells"
        + (f" inside its {'sized ' if d['area_sized'] else ''}area" if d["placed_inside"] else "")
        for d in r.domains
    )
    nets = sum(c["nets"] for c in r.crossings)
    msg = (
        f"OpenROAD read {len(r.domains)} power domains and placed them: {doms}; "
        f"{nets} signals cross between domains"
    )
    if problems:
        return StepResult("upf", "fail", f"{msg}; " + "; ".join(problems), data)
    return StepResult("upf", "pass", msg, data)
