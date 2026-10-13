"""The chip's checks: IHP's DRC and LVS decks, the supply hookup, a GDS precheck, and the
bond map.

LVS: IHP's deck compares transistors, and cannot compare the IO cells (their ESD
devices and the pad itself have no counterpart it extracts). So LVS runs on a view of
the chip without them (``lvs_plan`` and the KLayout script ``klayout/lvs_view.py``):
the IO cells, bond pads and seal ring are taken out, and each IO cell's core-side pins
stay as metal labelled with the net OpenROAD connected them to. The netlist is
OpenROAD's, without the IO cells, whose core-side nets become its ports, with the
standard cells' CDL and each macro's subcircuits. A pad on the wrong net, a macro pin
left open or two nets shorted all fail it. That the IO cells reach the supplies is
checked from the DEF instead (``supply_hookup``).
"""

from __future__ import annotations

import csv
import io as textio
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.chip.floorplan import BOND_UM, Floorplan, Placed
from chipleap.chip.io import IoCell
from chipleap.chip.netlist import transform

GDS_LAYERS = {
    "Metal1": 8,
    "Metal2": 10,
    "Metal3": 30,
    "Metal4": 50,
    "Metal5": 67,
    "TopMetal1": 126,
    "TopMetal2": 134,
}
NET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A flattened net as OpenROAD's CDL writes it: identifiers joined by dots, a bus bit's
# brackets escaped (u_core.rdata_o\[3\]).
FLAT_NET_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){1,8}(\\\[[0-9]{1,6}\\\])?"
)
DROP_PREFIXES = ("sg13g2_IOPad", "sg13g2_Corner", "sg13g2_Filler", "bondpad", "sealring")
NETLIST_MAX = 1 << 30
DB_MAX = 256 << 20
# The IO ring's own supply rails on TopMetal1, from its outer (bond-side) edge, in um.
RAILS = {"VDD": (140.0, 158.0), "VSS": (160.0, 178.0)}
GRID_NM = 5
VIA_AT = re.compile(r"\(\s*(-?\d+)\s+(-?\d+)(?:\s+-?\d+)?\s*\)\s+([A-Za-z_][A-Za-z0-9_]*)")


class CheckError(RuntimeError):
    pass


# LVS


def _cdl_lines(text: str) -> list[str]:
    """CDL lines with their ``+`` continuations joined."""
    pieces: list[list[str]] = []
    for raw in text.splitlines():
        if raw.startswith("+") and pieces:
            pieces[-1].append(raw[1:].strip())
        else:
            pieces.append([raw.strip()])
    return [" ".join(p) for p in pieces]


def subckt_ports(text: str) -> dict[str, list[str]]:
    """Each ``.SUBCKT``'s ports, by name."""
    out: dict[str, list[str]] = {}
    for line in _cdl_lines(text):
        words = line.split()
        if len(words) >= 2 and words[0].upper() == ".SUBCKT":
            out.setdefault(words[1], words[2:])
    return out


@dataclass
class LvsInputs:
    netlist: str
    pins: list[dict[str, Any]]  # for the view: net and (GDS layer, rect) shapes
    ports: list[str]
    instances: int


@dataclass(frozen=True)
class Abstract:
    """A macro the chip's LVS compares as a box, as it does the IO cells: where it is
    placed, its pins and their shapes (``MacroView.pins``, in its own coordinates), its
    size, and its subcircuit's ports in order (``<i>`` for a bus bit, as in its CDL)."""

    placed: Placed
    pins: dict[str, list[tuple[str, tuple[float, float, float, float]]]]
    size: tuple[float, float]
    ports: list[str]


def lvs_inputs(
    cdl: str,
    io_cdl: str,
    plan: Floorplan,
    io_cells: Mapping[str, IoCell],
    extra: Iterable[str],
    abstract: Mapping[str, Abstract] | None = None,
) -> LvsInputs:
    """The LVS netlist and the pins of the LVS view, from OpenROAD's CDL of the chip
    (``cdl``), the IO library's CDL, the floorplan and the IO cells' LEF; ``extra`` are
    the subcircuit libraries to append (the standard cells', each macro's). The macro
    instances of ``abstract`` leave the netlist as the IO cells do, and each of their
    pins becomes metal labelled with its net: what reaches the macro is still checked,
    not what is inside it."""
    if len(cdl) > NETLIST_MAX:
        raise CheckError("OpenROAD's CDL is larger than expected")
    orders = subckt_ports(io_cdl)
    placed = {c.inst: c for c in plan.ring}
    lines = _cdl_lines(cdl)
    head = next((i for i, ln in enumerate(lines) if ln.upper().startswith(".SUBCKT ")), None)
    if head is None:
        raise CheckError("OpenROAD's CDL has no .SUBCKT")
    top = lines[head].split()[1]
    body: list[str] = []
    boxes: list[tuple[str, list[str], str]] = []
    pins: list[dict[str, Any]] = []
    ports = ["VDD", "VSS"]
    for line in lines[head + 1 :]:
        words = line.split()
        if not words:
            continue
        if words[0].upper().startswith(".ENDS"):
            break
        if not words[0].upper().startswith("X"):
            continue
        inst, nets, master = words[0][1:], words[1:-1], words[-1]
        if abstract and inst in abstract:
            boxes.append((inst, nets, master))
            continue
        if master not in orders:
            body.append(line)
            continue
        cell = io_cells.get(master)
        where = placed.get(inst)
        if cell is None or where is None:
            raise CheckError(f"IO cell {inst} ({master}) is not in the floorplan")
        if len(nets) != len(orders[master]):
            raise CheckError(f"IO cell {inst} has {len(nets)} nets for {len(orders[master])} pins")
        for pin, net in zip(orders[master], nets, strict=True):
            if cell.uses.get(pin) in ("POWER", "GROUND"):
                continue
            shapes = [s for port in cell.core_ports(pin) for s in port]
            if not shapes:
                continue
            if not NET_RE.fullmatch(net) and not FLAT_NET_RE.fullmatch(net):
                raise CheckError(f"net {net[:40]!r} on {inst}/{pin} is not a plain name")
            rects = []
            for layer, rect in shapes:
                if layer in GDS_LAYERS:
                    r = transform(where, rect, (cell.width, cell.height))
                    rects.append([GDS_LAYERS[layer], [round(v, 4) for v in r]])
            pins.append({"net": net, "rects": rects})
            if net not in ports:
                ports.append(net)
    used = {w for line in body for w in line.split()[1:-1]} | set(ports)
    for inst, nets, master in boxes:
        box = (abstract or {})[inst]
        if master != box.placed.cell or len(nets) != len(box.ports):
            raise CheckError(f"macro {inst} ({master}) is not the cell or the pins planned")
        for port, net in zip(box.ports, nets, strict=True):
            # A net only this pin has (an output nothing reads) has nothing to compare.
            if net not in used:
                continue
            if not NET_RE.fullmatch(net) and not FLAT_NET_RE.fullmatch(net):
                raise CheckError(f"net {net[:40]!r} on {inst}/{port} is not a plain name")
            on_pin = box.pins.get(port.replace("<", "[").replace(">", "]"))
            if not on_pin:
                raise CheckError(f"macro {inst} has no shape for its pin {port}")
            rects = [
                [GDS_LAYERS[layer], [round(v, 4) for v in transform(box.placed, rect, box.size)]]
                for layer, rect in on_pin
                if layer in GDS_LAYERS
            ]
            pins.append({"net": net, "rects": rects})
            if net not in ports:
                ports.append(net)
    # A pad's net the core drives straight from a cell (an SRAM's output, say) keeps the
    # core's flattened name (u_core.rdata_o\\[3\\]); it becomes a plain one here, in
    # the netlist and on the pin's label alike.
    alias = _plain_aliases(ports, body)
    if alias:
        body = [" ".join(alias.get(w, w) for w in line.split()) for line in body]
        ports = [alias.get(n, n) for n in ports]
        for pin_entry in pins:
            pin_entry["net"] = alias.get(pin_entry["net"], pin_entry["net"])
    netlist = [
        "* LVS netlist of the chip without its IO cells, written by ChipLeap",
        f".SUBCKT {top} {' '.join(ports)}",
        *body,
        f".ENDS {top}",
        *extra,
    ]
    return LvsInputs("\n".join(netlist) + "\n", pins, ports, len(body))


def _plain_aliases(nets: list[str], body: list[str]) -> dict[str, str]:
    """A plain name for each net in ``nets`` that is a flattened one, unused anywhere in
    ``body`` and by any other net."""
    taken = {w for line in body for w in line.split()} | set(nets)
    out: dict[str, str] = {}
    for net in nets:
        if NET_RE.fullmatch(net):
            continue
        base = re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", net)).strip("_")
        name, n = base, 0
        while name in taken:
            n += 1
            name = f"{base}_{n}"
        taken.add(name)
        out[net] = name
    return out


def lvs_plan(
    plan: Floorplan,
    name: str,
    inputs: LvsInputs,
    src: Path,
    out: Path,
    abstract_cells: Iterable[str] = (),
) -> dict[str, Any]:
    """The plan of the LVS view: what to drop (the IO ring, and the cells of the macros
    compared as boxes), what to keep, which pins to label."""
    inner = plan.ring_inner
    return {
        "input": str(src),
        "output": str(out),
        "top": name,
        "drop_prefixes": [*DROP_PREFIXES, *sorted(set(abstract_cells))],
        "keep_box": [inner[0] - 1, inner[1] - 1, inner[2] + 1, inner[3] + 1],
        "pins": inputs.pins,
    }


LVS_PASS = re.compile(
    r"^[0-9: +-]+: Memory Usage \(\d+K\) : INFO : Congratulations! Netlists match\.$"
)


def lvs_outcome(log: str) -> str:
    """``pass``, ``fail`` or ``unknown`` from the LVS deck's log. A failure anywhere wins,
    and a pass is only the deck's own whole line, so no name in the layout can print one."""
    lines = [ln.rstrip() for ln in log.splitlines()]
    if any("Netlists don't match" in ln for ln in lines):
        return "fail"
    if any(LVS_PASS.match(ln) for ln in lines):
        return "pass"
    return "unknown"


# DRC


@dataclass
class DrcResult:
    violations: dict[str, int] = field(default_factory=dict)  # rule -> count
    reports: int = 0
    # Pin markers off the grid inside the PDK's own macros (IHP's SRAMs draw Metal1 pin
    # shapes off the 5 nm grid): reported, not counted. Pin purposes are no mask layer.
    in_pdk_macros: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.violations.values())


PIN_OFFGRID_RE = re.compile(r"[A-Za-z0-9]+_pin_Offgrid")


def read_lyrdb(path: Path, into: DrcResult, pdk_cells: frozenset[str] = frozenset()) -> None:
    """Count a KLayout report database's items by rule; an off-grid pin marker inside one
    of ``pdk_cells`` (the PDK's own macros) is counted apart, every other item as it is."""
    if path.stat().st_size > DB_MAX:
        raise CheckError(f"{path.name} is larger than expected")
    into.reports += 1
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "item":
            rule = (el.findtext("category") or "?").strip().strip("'")
            cell = (el.findtext("cell") or "").strip()
            if cell in pdk_cells and PIN_OFFGRID_RE.fullmatch(rule):
                into.in_pdk_macros[rule] = into.in_pdk_macros.get(rule, 0) + 1
            else:
                into.violations[rule] = into.violations.get(rule, 0) + 1
            el.clear()


# Supply hookup


def _def_section(text: str, name: str) -> str:
    start = re.search(rf"^{name}\s+\d+\s*;", text, re.M)
    end = re.search(rf"^END {name}\b", text, re.M)
    if start is None or end is None or end.start() < start.end():
        return ""
    return text[start.end() : end.start()]


def supply_hookup(def_text: str, plan: Floorplan) -> dict[str, dict[str, int]]:
    """For VDD and VSS, how many TopVia2 cuts the PDN put on the IO ring's rail of that
    net on each side: the joins between the core's grid and the pad ring."""
    top_vias = set()
    for line in _def_section(def_text, "VIAS").split(";"):
        words = line.split()
        if len(words) >= 2 and words[0] == "-" and "TopVia2" in words:
            top_vias.add(words[1])
    w, h = plan.die
    b = float(BOND_UM)
    out: dict[str, dict[str, int]] = {"VDD": {}, "VSS": {}}
    special = _def_section(def_text, "SPECIALNETS")
    for net_text in re.split(r"^\s*- ", special, flags=re.M)[1:]:
        words = net_text.split()
        if not words or words[0] not in out:
            continue
        net = words[0]
        lo, hi = RAILS[net]
        bands = {
            "south": (None, (b + lo, b + hi)),
            "north": (None, (h - b - hi, h - b - lo)),
            "west": ((b + lo, b + hi), None),
            "east": ((w - b - hi, w - b - lo), None),
        }
        # pdngen writes each via as "( x y ) name", after the segment it joins.
        for x, y, via in VIA_AT.findall(net_text):
            if via not in top_vias:
                continue
            px, py = int(x) / 1000, int(y) / 1000
            for side, (xb, yb) in bands.items():
                if (xb is None or xb[0] <= px <= xb[1]) and (yb is None or yb[0] <= py <= yb[1]):
                    out[net][side] = out[net].get(side, 0) + 1
    return out


# Precheck


def lyp_layers(text: str) -> set[tuple[int, int]]:
    """The (layer, datatype) pairs a KLayout layer properties file names."""
    return {(int(a), int(b)) for a, b in re.findall(r"<source>(\d+)/(\d+)", text)}


def precheck(
    report: Mapping[str, Any],
    name: str,
    allowed: set[tuple[int, int]],
    die: tuple[float, float, float, float],
    outline: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """The precheck's verdicts from the facts ``klayout/precheck.py`` reported: ``die`` is
    the die's box, which the PR boundary marks, and ``outline`` the seal ring's outer
    edge, which bounds everything."""
    checks: list[dict[str, Any]] = []

    def add(check: str, ok: bool, detail: str) -> None:
        checks.append({"check": check, "pass": ok, "detail": detail})

    def same(box: Any, ref: tuple[float, float, float, float], scale: float = 1.0) -> bool:
        return len(box) == 4 and all(
            abs(float(a) * scale - b) < 0.001 for a, b in zip(box, ref, strict=True)
        )

    tops = list(report.get("top_cells", []))
    add("top cell", tops == [name], f"top cells: {', '.join(map(str, tops)) or 'none'}")
    used = {(int(a), int(b)) for a, b in report.get("layers", [])}
    extra = sorted(used - allowed)
    add(
        "layers",
        not extra,
        f"{len(used)} layers, all in IHP's layer map"
        if not extra
        else "not in IHP's layer map: " + ", ".join(f"{a}/{b}" for a, b in extra[:10]),
    )
    bad = list(report.get("bad_cell_names", []))
    add("cell names", not bad, "no # or / in a cell name" if not bad else ", ".join(bad[:5]))
    zero = int(report.get("zero_area_shapes", 0))
    add("zero-area shapes", zero == 0, f"{zero} shapes without area")
    fits = any(same(box, die) for box in report.get("pr_boundary", []))
    add("PR boundary", fits, "on the die" if fits else "missing, or not on the die")
    dbu = float(report.get("dbu_um", 0.001))
    bbox = report.get("bbox_dbu", [])
    grid = round(GRID_NM / 1000 / dbu) if dbu > 0 else 1
    on_grid = bool(bbox) and all(int(v) % grid == 0 for v in bbox)
    ok = on_grid and same(bbox, outline, dbu)
    add(
        "outline",
        ok,
        f"the seal ring's outer edge, on the {GRID_NM} nm grid"
        if ok
        else f"the layout's extent {bbox} is not the seal ring's outline on the grid",
    )
    return checks


# Bond map


def bond_map(plan: Floorplan, bond_um: float) -> tuple[str, list[dict[str, Any]]]:
    """The bond map as CSV and as records: pad number, side, net, kind, IO cell, bond
    pad centre and size."""
    rows = [
        {
            "number": p.number,
            "side": p.side,
            "net": p.pad.net or p.pad.kind,
            "kind": p.pad.kind,
            "cell": p.cell.cell,
            "instance": p.cell.inst,
            "x_um": p.bond_center[0],
            "y_um": p.bond_center[1],
            "size_um": bond_um,
        }
        for p in plan.pads
    ]
    buf = textio.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["number"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue(), rows


def dump(data: Any) -> str:
    return json.dumps(data, indent=2) + "\n"
