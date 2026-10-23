"""KLayout DRC reports, read the same way by the chip step and by place and route.

IHP's rule deck checks that every drawn layer lies on the 5 nm grid, pin markers included
(``metal1_pin_Offgrid``, and the same rule on the other pin layers), and IHP's own SRAMs
draw some Metal1 pin markers off it (in ``RM_IHPSG13_1P_BLDRV``, a cell of every
single-port SRAM); the deck flags them on an SRAM on its own as well. A pin marker is no
mask layer. So an off-grid pin marker inside the cell tree of one of the PDK's own macros
is set apart: reported with its rule, cell and count, and left out of the verdict. The
same rule in any other cell, and every other rule inside the macro, counts.

A layout's GDS need not name the macros' cells as the PDK does: LibreLane's stream-out of
the SoC, with two kinds of SRAM, prefixed every cell inside them with two characters
(``EA_RM_IHPSG13_1P_BLDRV``) and kept only the macros' own names. And a report need not
keep the hierarchy: IHP's runner (``run_drc.py``, one table at a time, deep mode, KLayout
0.30.4 and 0.30.7 alike) put the off-grid pin markers of the SoC's 12 SRAMs in its top
cell, 8,192 points, where LibreLane 3.0.14's single run of the deck reported the same
markers as 32 in the renamed cells. So the cells only the PDK's macros place are read
from the checked GDS itself (``macro_layout``), and a marker in the top cell that lies
inside a placed macro's outline is that macro's.
"""

from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DB_MAX = 256 << 20
PIN_OFFGRID_RE = re.compile(r"[A-Za-z0-9]+_pin_Offgrid")
# KLayout names the variant of a cell a deep-mode check ran on after a colon: its
# orientation (RM_IHPSG13_1P_BLDRV:r0, :m90).
VARIANT_RE = re.compile(r":[rm]\d+$")
# A marker's shape in a report, in um in its cell: "polygon: (x,y;x,y;...)", "box:
# (x,y;x,y)", "edge-pair: (x,y;x,y)/(x,y;x,y)" and the like; its other values are
# properties ("[#m5_ratio] float: 186.78").
SHAPE_RE = re.compile(r"(?:polygon|box|edge|edge-pair|path|text): \(")
POINT_RE = re.compile(r"(-?\d+(?:\.\d+)?(?:e-?\d+)?),(-?\d+(?:\.\d+)?(?:e-?\d+)?)")
PLACES_KEPT = 20  # locations kept per rule
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 in um


class DrcReportError(RuntimeError):
    pass


@dataclass
class DrcResult:
    violations: dict[str, int] = field(default_factory=dict)  # rule -> count
    reports: int = 0
    # Off-grid pin markers inside the PDK's own macros, (rule, cell) -> count: reported,
    # not counted. ``held_by`` names the PDK macros each of those cells is part of.
    pin_markers: dict[tuple[str, str], int] = field(default_factory=dict)
    held_by: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Where the first PLACES_KEPT violations of each rule are: (cell, x, y), the centre of
    # the marker's bounding box in um, in that cell's coordinates.
    places: dict[str, list[tuple[str, float, float]]] = field(default_factory=dict)
    top: str = ""  # the checked layout's top cell, when the reader knows it

    @property
    def total(self) -> int:
        return sum(self.violations.values())

    @property
    def markers(self) -> int:
        return sum(self.pin_markers.values())

    def add(self, other: DrcResult) -> None:
        """Count ``other``'s report with this one's, as if read into it."""
        for rule, n in other.violations.items():
            self.violations[rule] = self.violations.get(rule, 0) + n
        for key, n in other.pin_markers.items():
            self.pin_markers[key] = self.pin_markers.get(key, 0) + n
        self.held_by.update(other.held_by)
        for rule, places in other.places.items():
            kept = self.places.setdefault(rule, [])
            kept.extend(places[: max(0, PLACES_KEPT - len(kept))])
        self.reports += other.reports

    def markers_data(self) -> list[dict[str, Any]]:
        """The set-apart pin markers for a report: rule, cell, the PDK macros that hold
        the cell, and count."""
        return [
            {"rule": rule, "cell": cell, "macros": list(self.held_by.get(cell, ())), "count": n}
            for (rule, cell), n in sorted(self.pin_markers.items())
        ]


def pdk_cells(
    macros: Iterable[tuple[str, Iterable[str]]], design: Iterable[str] = ()
) -> dict[str, tuple[str, ...]]:
    """The cells of the PDK's own macros, each with the macros that hold it: ``macros``
    is each PDK macro's name with the structures of its GDS, as the PDK's library has it.
    A name of ``design`` (the design's own cells, its top) is never one of them."""
    own = set(design)
    out: dict[str, set[str]] = {}
    for macro, structures in macros:
        for cell in {macro, *structures}:
            if cell not in own:
                out.setdefault(cell, set()).add(macro)
    return {cell: tuple(sorted(held)) for cell, held in out.items()}


def read_lyrdb(
    path: Path,
    into: DrcResult,
    pdk_cells: Mapping[str, tuple[str, ...]] | None = None,
    outlines: Sequence[tuple[str, Box]] = (),
    top: str = "",
) -> None:
    """Count a KLayout report database's items by rule; an off-grid pin marker inside one
    of ``pdk_cells`` (the PDK's own macros and their cells, from ``pdk_cells``), or in
    ``top`` and inside one of ``outlines`` (a placed macro's, from ``macro_layout``), is
    set apart, every other item counts."""
    held = pdk_cells or {}
    if path.stat().st_size > DB_MAX:
        raise DrcReportError(f"{path.name} is larger than expected")
    into.reports += 1
    try:
        for _, el in ET.iterparse(path, events=("end",)):
            if el.tag == "item":
                rule = (el.findtext("category") or "?").strip().strip("'")
                cell = VARIANT_RE.sub("", (el.findtext("cell") or "").strip())
                pin = PIN_OFFGRID_RE.fullmatch(rule) is not None
                macro = _inside(el, outlines) if pin and outlines and cell == top else None
                if macro is not None:
                    into.pin_markers[(rule, macro)] = into.pin_markers.get((rule, macro), 0) + 1
                    into.held_by[macro] = (macro,)
                elif cell in held and pin:
                    into.pin_markers[(rule, cell)] = into.pin_markers.get((rule, cell), 0) + 1
                    into.held_by[cell] = held[cell]
                else:
                    into.violations[rule] = into.violations.get(rule, 0) + 1
                    kept = into.places.setdefault(rule, [])
                    if len(kept) < PLACES_KEPT and (at := _centre(el)) is not None:
                        kept.append((cell, *at))
                el.clear()
    except ET.ParseError as exc:
        raise DrcReportError(f"{path.name} is not a report database: {exc}") from None


def _bbox(item: ET.Element) -> Box | None:
    """The bounding box of a report item's shape, or None without one."""
    for value in item.iter("value"):
        text = value.text or ""
        if SHAPE_RE.match(text):
            points = POINT_RE.findall(text)
            if points:
                xs = [float(x) for x, _ in points]
                ys = [float(y) for _, y in points]
                return min(xs), min(ys), max(xs), max(ys)
    return None


def _centre(item: ET.Element) -> tuple[float, float] | None:
    """The centre of the bounding box of a report item's shape, or None without one."""
    box = _bbox(item)
    return None if box is None else ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def _inside(item: ET.Element, outlines: Sequence[tuple[str, Box]]) -> str | None:
    """The macro whose outline holds all of a report item's shape, if one does."""
    box = _bbox(item)
    if box is None:
        return None
    eps = 1e-6
    for macro, (x0, y0, x1, y1) in outlines:
        if x0 - eps <= box[0] and y0 - eps <= box[1] and box[2] <= x1 + eps and box[3] <= y1 + eps:
            return macro
    return None


@dataclass(frozen=True)
class MacroLayout:
    """Where the PDK's own macros are in a layout's GDS: each cell only they place, under
    the name the GDS gives it, with the macros that hold it; and each macro the top cell
    places, with its outline there in um."""

    cells: dict[str, tuple[str, ...]]
    outlines: list[tuple[str, Box]]


@dataclass
class _Ref:
    """A reference to a cell in a GDS structure, as its records give it."""

    array: bool
    name: str = ""
    mirror: bool = False
    mag: float = 1.0
    angle: float = 0.0
    xy: tuple[int, ...] = ()


def macro_layout(data: bytes, top: str, sizes: Mapping[str, tuple[float, float]]) -> MacroLayout:
    """The PDK's macros in a GDS stream: ``sizes`` names them, each with its outline's width
    and height in um from its LEF, whose origin is the GDS cell's. A cell the top reaches
    without passing through one of them is the layout's own, never a macro's."""
    from chipleap.macro.layer import _real8, gds_raw_name, gds_records

    children: dict[str, set[str]] = {}
    placed: list[_Ref] = []
    um = 0.001  # per database unit, from UNITS
    current = ""
    ref: _Ref | None = None
    for rtype, a, b in gds_records(data):
        if rtype == 0x03 and b - a == 16:  # UNITS: user units, metres per database unit
            um = _real8(data[a + 8 : b]) * 1e6
        elif rtype == 0x06:  # STRNAME
            current = gds_raw_name(data[a:b]).decode("ascii", "replace")
            children.setdefault(current, set())
        elif rtype in (0x0A, 0x0B):  # SREF, AREF
            ref = _Ref(array=rtype == 0x0B)
        elif ref is not None:
            if rtype == 0x12:  # SNAME
                ref.name = gds_raw_name(data[a:b]).decode("ascii", "replace")
            elif rtype == 0x1A:  # STRANS: reflection about x before rotation
                ref.mirror = bool(data[a] & 0x80)
            elif rtype == 0x1B:
                ref.mag = _real8(data[a:b])
            elif rtype == 0x1C:
                ref.angle = _real8(data[a:b])
            elif rtype == 0x10 and b - a >= 8:  # XY: the origin first
                ref.xy = struct.unpack(">2i", data[a : a + 8])
            elif rtype == 0x11:  # ENDEL
                children[current].add(ref.name)
                if current == top and ref.name in sizes and not ref.array and ref.xy:
                    placed.append(ref)
                ref = None
    macros = set(sizes) & set(children)
    own = _reach(children, top, macros)
    cells: dict[str, set[str]] = {}
    for m in macros:
        for c in _reach(children, m, set()) | {m}:
            if c not in own and c != top:
                cells.setdefault(c, set()).add(m)
    outlines = []
    for r in placed:
        w, h = sizes[r.name]
        turn = math.radians(r.angle)
        cos, sin = round(math.cos(turn), 12), round(math.sin(turn), 12)
        xs, ys = [], []
        for px, py in ((0.0, 0.0), (w, 0.0), (0.0, h), (w, h)):
            px, py = px * r.mag, (-py if r.mirror else py) * r.mag
            xs.append(r.xy[0] * um + px * cos - py * sin)
            ys.append(r.xy[1] * um + px * sin + py * cos)
        outlines.append((r.name, (min(xs), min(ys), max(xs), max(ys))))
    return MacroLayout({c: tuple(sorted(m)) for c, m in cells.items()}, outlines)


def _reach(children: Mapping[str, set[str]], start: str, stop: set[str]) -> set[str]:
    """The cells ``start`` places, at any depth, without passing through ``stop``."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        for c in children.get(todo.pop(), ()):
            if c not in seen and c not in stop:
                seen.add(c)
                todo.append(c)
    return seen


def describe_markers(markers: list[dict[str, Any]]) -> str:
    """One line on the set-apart pin markers (``DrcResult.markers_data``), for a step's
    summary; empty without any."""
    if not markers:
        return ""
    n = sum(m["count"] for m in markers)
    where = ", ".join(f"{m['count']} {m['rule']} in {m['cell']}" for m in markers)
    return (
        f"{n} pin marker{'s' if n != 1 else ''} off the grid inside the PDK's own macros, "
        f"not counted: {where}"
    )
