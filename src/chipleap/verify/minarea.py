"""Routed metal shapes under a layer's minimum area, and the patches that fix them.

OpenROAD's detailed router meets each layer's minimum area (the tech LEF's ``AREA``,
0.144 um2 for Metal2 to Metal5 on IHP SG13G2) by adding patches of metal to short
shapes, and its own check accepts the result. On a short Metal2 stub between a Via1 and a
Via2 with a jog in it, it sometimes leaves the shape under the minimum: in 4 of 6 large
accelerator runs, one such shape failed IHP's rule M2.d at signoff. Twice its patch lay
almost wholly on the stub's own metal (0.0397 of a 0.04 um2 patch, and 0.029 of 0.08),
so the shape stayed at 0.13145 and 0.128725 um2. Twice the stub had no patch at all
(0.13875 and 0.143975 um2). Asking the router for a larger minimum area is no cure:
on the first stub a larger patch still lands on metal the stub already has.

``check`` finds every such shape in a routed DEF. It merges each net's wires, vias and
patches per layer and measures the shapes against the tech LEF's ``AREA``, the way the
rule deck measures merged metal. ``repair`` extends each one at an end along its layer's
routing direction, by a rectangle as wide as that end and long enough to pass the
minimum with a margin. It places the rectangle only where no other net's metal, power
grid, pin or cell with metal on that layer comes within the layer's spacing (plus a
margin). It gives up, naming the shape, when neither end has room. LibreLane's signoff
DRC checks the result as it checks everything else.
"""

from __future__ import annotations

import itertools
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 in DEF database units

# What a repaired shape reaches beyond the minimum, and what a patch keeps from other
# metal beyond the spacing rule, in um2 and um.
AREA_MARGIN_UM2 = 0.006
CLEARANCE_MARGIN_UM = 0.05
# The spacing a patch keeps from a foreign shape of a given width, from IHP's rules for
# Metal2 to Metal5 (M2.b, M2.e, M2.f and their equivalents): 0.21 um, 0.24 um next to
# metal 0.39 um wide or more, 0.6 um next to metal 10 um wide or more.
SPACING_UM = ((10.0, 0.6), (0.39, 0.24), (0.0, 0.21))


class MinAreaError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layer:
    name: str
    width: int  # default wire width, database units
    area_um2: float  # minimum area; 0 without a rule
    vertical: bool


@dataclass(frozen=True)
class Tech:
    dbu: int  # database units per um
    grid: int  # manufacturing grid, database units
    layers: dict[str, Layer]
    vias: dict[str, list[tuple[str, Rect]]]  # via name -> its metal and cut shapes


@dataclass(frozen=True)
class Shape:
    net: str
    layer: str
    area_um2: float
    rects: tuple[Rect, ...]

    @property
    def bbox(self) -> Rect:
        return (
            min(r[0] for r in self.rects),
            min(r[1] for r in self.rects),
            max(r[2] for r in self.rects),
            max(r[3] for r in self.rects),
        )


@dataclass(frozen=True)
class Patch:
    shape: Shape
    rect: Rect

    def statement(self) -> str:
        """The patch as a DEF routing statement of its net, as the router writes its own."""
        x0, y0, x1, y1 = self.rect
        return f"NEW {self.shape.layer} ( {x0} {y0} ) RECT ( 0 0 {x1 - x0} {y1 - y0} )"


# ---------------------------------------------------------------------------------------
# LEF


def read_tech(text: str, dbu: int = 1000) -> Tech:
    """The routing layers, their minimum area and direction, and the vias of a tech LEF."""
    layers: dict[str, Layer] = {}
    vias: dict[str, list[tuple[str, Rect]]] = {}
    grid = 5
    layer: dict[str, str] | None = None
    via: str | None = None
    via_layer = ""
    for line in text.splitlines():
        t = line.split()
        if not t or t[0].startswith("#"):
            continue
        key = t[0].upper()
        if key == "MANUFACTURINGGRID" and len(t) >= 2:
            grid = max(1, round(float(t[1]) * dbu))
        elif key == "LAYER" and via is None and layer is None and len(t) == 2:
            layer = {"name": t[1]}
        elif layer is not None:
            if key == "END" and len(t) >= 2 and t[1] == layer["name"]:
                if layer.get("type") == "ROUTING":
                    layers[layer["name"]] = Layer(
                        layer["name"],
                        round(float(layer.get("width", "0")) * dbu),
                        float(layer.get("area", "0")),
                        layer.get("direction", "") == "VERTICAL",
                    )
                layer = None
            elif key == "TYPE" and len(t) >= 2:
                layer["type"] = t[1].rstrip(";").upper()
            elif key == "DIRECTION" and len(t) >= 2:
                layer["direction"] = t[1].rstrip(";").upper()
            elif key == "WIDTH" and len(t) >= 2 and "width" not in layer:
                layer["width"] = t[1].rstrip(";")
            elif key == "AREA" and len(t) >= 2:
                layer["area"] = t[1].rstrip(";")
        elif key == "VIA" and len(t) >= 2:
            via, via_layer = t[1], ""
            vias[via] = []
        elif via is not None:
            if key == "END" and len(t) >= 2 and t[1] == via:
                via = None
            elif key == "LAYER" and len(t) >= 2:
                via_layer = t[1].rstrip(";")
            elif key == "RECT" and len(t) >= 5:
                x0, y0, x1, y1 = (round(float(v) * dbu) for v in t[1:5])
                vias[via].append((via_layer, (x0, y0, x1, y1)))
    return Tech(dbu, grid, layers, vias)


@dataclass(frozen=True)
class Master:
    width: int
    height: int
    layers: frozenset[str]  # layers with a pin or an obstruction


def read_masters(text: str, dbu: int = 1000) -> dict[str, Master]:
    """Each cell's size and the layers it has metal on, from a cell or macro LEF."""
    out: dict[str, Master] = {}
    name: str | None = None
    size = (0, 0)
    found: set[str] = set()
    for line in text.splitlines():
        t = line.split()
        if not t:
            continue
        key = t[0].upper()
        if key == "MACRO" and len(t) >= 2:
            name, size, found = t[1], (0, 0), set()
        elif name is None:
            continue
        elif key == "SIZE" and len(t) >= 4:
            size = (round(float(t[1]) * dbu), round(float(t[3]) * dbu))
        elif key == "LAYER" and len(t) >= 2:
            found.add(t[1].rstrip(";"))
        elif key == "END" and len(t) >= 2 and t[1] == name:
            out[name] = Master(size[0], size[1], frozenset(found))
            name = None
    return out


# ---------------------------------------------------------------------------------------
# DEF

_POINT = re.compile(r"\(\s*(-?\d+|\*)\s+(-?\d+|\*)(?:\s+(-?\d+))?\s*\)")
_RECT = re.compile(r"RECT\s*\(\s*(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s*\)")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^\s*{name} \d+ ;(.*?)^\s*END {name}\b", text, re.S | re.M)
    return m.group(1) if m else ""


def _statements(section: str) -> Iterator[str]:
    """The ``- ... ;`` items of a DEF section, each on one line."""
    for item in section.split(";"):
        item = " ".join(item.split())
        if item.startswith("- "):
            yield item


def def_vias(text: str) -> dict[str, list[tuple[str, Rect]]]:
    """The metal shapes of the vias a DEF defines itself (generated via arrays)."""
    out: dict[str, list[tuple[str, Rect]]] = {}
    for item in _statements(_section(text, "VIAS")):
        name = item.split()[1]
        shapes: list[tuple[str, Rect]] = []
        for m in re.finditer(
            r"\+ RECT (\S+) \(\s*(-?\d+) (-?\d+)\s*\) \(\s*(-?\d+) (-?\d+)\s*\)", item
        ):
            x0, y0, x1, y1 = (int(v) for v in m.groups()[1:])
            shapes.append((m.group(1), (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))
        cut = re.search(r"CUTSIZE (\d+) (\d+)", item)
        lay = re.search(r"LAYERS (\S+) (\S+) (\S+)", item)
        space = re.search(r"CUTSPACING (\d+) (\d+)", item)
        enc = re.search(r"ENCLOSURE (\d+) (\d+) (\d+) (\d+)", item)
        if cut and lay and space and enc:
            rc = re.search(r"ROWCOL (\d+) (\d+)", item)
            rows, cols = (int(rc.group(1)), int(rc.group(2))) if rc else (1, 1)
            w = cols * int(cut.group(1)) + (cols - 1) * int(space.group(1))
            h = rows * int(cut.group(2)) + (rows - 1) * int(space.group(2))
            bx, by, tx, ty = (int(v) for v in enc.groups())
            shapes.append((lay.group(1), (-w // 2 - bx, -h // 2 - by, w // 2 + bx, h // 2 + by)))
            shapes.append((lay.group(3), (-w // 2 - tx, -h // 2 - ty, w // 2 + tx, h // 2 + ty)))
        out[name] = shapes
    return out


def _orient(rect: Rect, w: int, h: int, orient: str) -> Rect:
    """``rect`` in a cell of size w x h, placed at the origin with ``orient``."""
    x0, y0, x1, y1 = rect
    pts = [_move(x, y, w, h, orient) for x, y in ((x0, y0), (x1, y1))]
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[0] for p in pts),
        max(p[1] for p in pts),
    )


def _move(x: int, y: int, w: int, h: int, orient: str) -> tuple[int, int]:
    """A point of a w x h cell under a DEF orientation, the cell's box kept at the origin."""
    if orient == "S":
        return w - x, h - y
    if orient == "FN":
        return w - x, y
    if orient == "FS":
        return x, h - y
    if orient == "E":
        return y, w - x
    if orient == "W":
        return h - y, x
    if orient == "FE":
        return y, x
    if orient == "FW":
        return h - y, w - x
    return x, y


def _route(
    statement: str, tech: Tech, vias: dict[str, list[tuple[str, Rect]]], special: bool
) -> Iterator[tuple[str, Rect]]:
    """The shapes of one routing statement: ``LAYER [width] [+ SHAPE x] pts [via|RECT]``."""
    m = re.match(r"(?:\+ (?:ROUTED|FIXED|COVER|SHIELD \S+) |NEW )(\S+)(.*)", statement)
    if not m:
        return
    layer, rest = m.group(1), m.group(2)
    width = tech.layers[layer].width if layer in tech.layers else 0
    if special:
        w = re.match(r"\s*(\d+)", rest)
        if w:
            width, rest = int(w.group(1)), rest[w.end() :]
    rest = re.sub(r"\+ (SHAPE|MASK|STYLE) \S+", "", rest)
    pts: list[tuple[int, int, int | None]] = []
    last: tuple[int, int] = (0, 0)
    for p in _POINT.finditer(rest):
        x = last[0] if p.group(1) == "*" else int(p.group(1))
        y = last[1] if p.group(2) == "*" else int(p.group(2))
        pts.append((x, y, int(p.group(3)) if p.group(3) else None))
        last = (x, y)
    if not pts:
        return
    rect = _RECT.search(rest)
    if rect:
        x, y = pts[0][0], pts[0][1]
        a, b, c, d = (int(v) for v in rect.groups())
        yield layer, (x + min(a, c), y + min(b, d), x + max(a, c), y + max(b, d))
        return
    half = width // 2
    for (x0, y0, e0), (x1, y1, e1) in itertools.pairwise(pts):
        a0 = half if e0 is None else e0
        a1 = half if e1 is None else e1
        if x0 == x1:
            (lo, elo), (hi, ehi) = sorted([(y0, a0), (y1, a1)])
            yield layer, (x0 - half, lo - elo, x0 + half, hi + ehi)
        else:
            (lo, elo), (hi, ehi) = sorted([(x0, a0), (x1, a1)])
            yield layer, (lo - elo, y0 - half, hi + ehi, y0 + half)
    words = _POINT.sub(" ", rest).split()
    names = [w for w in words if w in vias]
    if names:
        x, y = pts[-1][0], pts[-1][1]
        do = re.search(r"DO (\d+) BY (\d+) STEP (\d+) (\d+)", rest)
        nx, ny, sx, sy = (int(v) for v in do.groups()) if do else (1, 1, 0, 0)
        for vl, (a, b, c, d) in vias[names[0]]:
            yield vl, (x + a, y + b, x + c + (nx - 1) * sx, y + d + (ny - 1) * sy)


def _routing(item: str) -> list[str]:
    """The routing statements of a net item: from ``+ ROUTED`` on, split at ``NEW``."""
    m = re.search(r"\+ (?:ROUTED|FIXED|COVER|SHIELD \S+) ", item)
    if not m:
        return []
    body = item[m.start() :]
    body = re.split(r" \+ (?:SOURCE|USE|WEIGHT|PROPERTY|ORIGINAL|PATTERN|NONDEFAULTRULE)\b", body)[
        0
    ]
    return [s.strip() for s in re.split(r"(?=\bNEW )", body) if s.strip()]


@dataclass
class Layout:
    """A routed DEF's metal by layer: each net's own shapes, and every other shape."""

    tech: Tech
    die: Rect
    nets: dict[tuple[str, str], list[Rect]]  # (net, layer) -> its wires, vias, patches, pins
    foreign: dict[str, list[tuple[str, Rect]]]  # layer -> (owner, shape) of everything else


def read_layout(text: str, tech: Tech, masters: dict[str, Master]) -> Layout:
    units = re.search(r"^\s*UNITS DISTANCE MICRONS (\d+)", text, re.M)
    if units and int(units.group(1)) != tech.dbu:
        raise MinAreaError(f"the DEF has {units.group(1)} units per um, the LEF {tech.dbu}")
    die_m = re.search(
        r"^\s*DIEAREA\s*\(\s*(-?\d+) (-?\d+)\s*\)\s*\(\s*(-?\d+) (-?\d+)\s*\)", text, re.M
    )
    die = tuple(int(v) for v in die_m.groups()) if die_m else (0, 0, 0, 0)
    vias = {**tech.vias, **def_vias(text)}
    nets: dict[tuple[str, str], list[Rect]] = defaultdict(list)
    foreign: dict[str, list[tuple[str, Rect]]] = defaultdict(list)
    for item in _statements(_section(text, "NETS")):
        net = item.split()[1]
        for st in _routing(item):
            for layer, r in _route(st, tech, vias, special=False):
                nets[(net, layer)].append(r)
    for item in _statements(_section(text, "SPECIALNETS")):
        net = item.split()[1]
        for st in _routing(item):
            for layer, r in _route(st, tech, vias, special=True):
                foreign[layer].append((net, r))
    for item in _statements(_section(text, "PINS")):
        net_m = re.search(r"\+ NET (\S+)", item)
        net = net_m.group(1) if net_m else item.split()[1]
        special = "+ SPECIAL" in item
        place_m = re.search(r"\+ (?:PLACED|FIXED|COVER) \(\s*(-?\d+) (-?\d+)\s*\) (\S+)", item)
        px, py, orient = (
            (int(place_m.group(1)), int(place_m.group(2)), place_m.group(3))
            if place_m
            else (0, 0, "N")
        )
        for m in re.finditer(
            r"\+ LAYER (\S+)(?: \S+ \d+)* \(\s*(-?\d+) (-?\d+)\s*\) \(\s*(-?\d+) (-?\d+)\s*\)", item
        ):
            x0, y0, x1, y1 = (int(v) for v in m.groups()[1:])
            r = _orient((x0, y0, x1, y1), 0, 0, orient)
            placed = (px + r[0], py + r[1], px + r[2], py + r[3])
            # A signal pin's shape merges with its net's routing; a supply pin's is the
            # power grid's.
            if special:
                foreign[m.group(1)].append((net, placed))
            else:
                nets[(net, m.group(1))].append(placed)
    for item in _statements(_section(text, "COMPONENTS")):
        t = item.split()
        master = masters.get(t[2]) if len(t) > 2 else None
        place_m = re.search(r"\+ (?:PLACED|FIXED|COVER) \(\s*(-?\d+) (-?\d+)\s*\) (\S+)", item)
        if master is None or not place_m or not master.layers:
            continue
        x, y = int(place_m.group(1)), int(place_m.group(2))
        bx = _orient(
            (0, 0, master.width, master.height), master.width, master.height, place_m.group(3)
        )
        box = (x + bx[0], y + bx[1], x + bx[2], y + bx[3])
        # A cell's pins and obstructions on a routing layer: the whole cell, to be safe.
        for layer in master.layers:
            foreign[layer].append((t[1], box))
    return Layout(tech, die, nets, foreign)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------
# Geometry


def union_area(rects: Iterable[Rect]) -> int:
    """The area the rectangles cover together, in square database units."""
    rects = list(rects)
    xs = sorted({x for r in rects for x in (r[0], r[2])})
    total = 0
    for x0, x1 in itertools.pairwise(xs):
        spans = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
        covered, lo, hi = 0, None, None
        for a, b in spans:
            if hi is None or a > hi:
                if hi is not None and lo is not None:
                    covered += hi - lo
                lo, hi = a, b
            else:
                hi = max(hi, b)
        if hi is not None and lo is not None:
            covered += hi - lo
        total += covered * (x1 - x0)
    return total


def merged(rects: list[Rect]) -> list[list[Rect]]:
    """The rectangles grouped into shapes: those that touch or overlap are one shape."""
    parent = list(range(len(rects)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    order = sorted(range(len(rects)), key=lambda i: rects[i][0])
    for n, a in enumerate(order):
        ra = rects[a]
        for b in order[n + 1 :]:
            rb = rects[b]
            if rb[0] > ra[2]:
                break
            if rb[1] <= ra[3] and ra[1] <= rb[3]:
                parent[find(a)] = find(b)
    groups: dict[int, list[Rect]] = defaultdict(list)
    for i, r in enumerate(rects):
        groups[find(i)].append(r)
    return list(groups.values())


def check(layout: Layout) -> list[Shape]:
    """Every net's shape on a routing layer under the layer's minimum area. Metal1 is left
    out: the router's shapes there merge with the cells' pins, which are not read."""
    tech = layout.tech
    out = []
    for (net, layer), rects in sorted(layout.nets.items()):
        rule = tech.layers.get(layer)
        if rule is None or rule.area_um2 <= 0 or layer == "Metal1":
            continue
        need = rule.area_um2 * tech.dbu * tech.dbu
        for group in merged(rects):
            a = union_area(group)
            if a < need - 0.5:
                out.append(Shape(net, layer, a / (tech.dbu * tech.dbu), tuple(group)))
    return out


def _spacing(rect: Rect, dbu: int) -> int:
    width_um = min(rect[2] - rect[0], rect[3] - rect[1]) / dbu
    space = next(s for w, s in SPACING_UM if width_um >= w)
    return round((space + CLEARANCE_MARGIN_UM) * dbu)


def _near(a: Rect, b: Rect, gap: int) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _ends(shape: Shape, vertical: bool) -> list[tuple[str, Rect]]:
    """The shape's two ends along the routing direction, each as the span of the
    rectangles that reach it: ``(side, (lo, edge, hi, edge))`` across the direction."""
    out = []
    x0, y0, x1, y1 = shape.bbox
    for side in ("high", "low"):
        if vertical:
            edge = y1 if side == "high" else y0
            touching = [r for r in shape.rects if (r[3] if side == "high" else r[1]) == edge]
            lo, hi = min(r[0] for r in touching), max(r[2] for r in touching)
        else:
            edge = x1 if side == "high" else x0
            touching = [r for r in shape.rects if (r[2] if side == "high" else r[0]) == edge]
            lo, hi = min(r[1] for r in touching), max(r[3] for r in touching)
        out.append((side, (lo, edge, hi, edge)))
    return out


def _patch_for(shape: Shape, layout: Layout) -> Patch | None:
    tech = layout.tech
    rule = tech.layers[shape.layer]
    need = (rule.area_um2 + AREA_MARGIN_UM2) * tech.dbu * tech.dbu
    current = union_area(shape.rects)
    others = [
        (owner, r) for owner, r in layout.foreign.get(shape.layer, []) if owner != shape.net
    ] + [
        (net, r)
        for (net, layer), rects in layout.nets.items()
        if layer == shape.layer and net != shape.net
        for r in rects
    ]
    for side, (lo, edge, hi, _) in _ends(shape, rule.vertical):
        span = hi - lo
        if span <= 0:
            continue
        length = math.ceil((need - current) / span / tech.grid) * tech.grid
        if rule.vertical:
            rect = (
                (lo, edge, hi, edge + length) if side == "high" else (lo, edge - length, hi, edge)
            )
        else:
            rect = (
                (edge, lo, edge + length, hi) if side == "high" else (edge - length, lo, edge, hi)
            )
        d = layout.die
        inside = rect[0] >= d[0] and rect[1] >= d[1] and rect[2] <= d[2] and rect[3] <= d[3]
        clear = not any(_near(rect, r, _spacing(r, tech.dbu)) for _, r in others)
        if inside and clear and union_area([*shape.rects, rect]) >= need - 0.5:
            return Patch(shape, rect)
    return None


def repair(layout: Layout) -> list[Patch]:
    """A patch for each shape under the minimum area. Raises MinAreaError naming the
    shapes neither of whose ends has room for one."""
    patches, stuck = [], []
    for s in check(layout):
        p = _patch_for(s, layout)
        if p is None:
            stuck.append(s)
        else:
            patches.append(p)
    if stuck:
        raise MinAreaError(
            "no room to bring these routed shapes up to the minimum area: "
            + "; ".join(describe(s, layout.tech.dbu) for s in stuck)
        )
    return patches


def describe(s: Shape, dbu: int = 1000) -> str:
    x0, y0, x1, y1 = (v / dbu for v in s.bbox)
    return f"{s.layer} of net {s.net}, {s.area_um2:.6f} um2 at ({x0:g}, {y0:g}) to ({x1:g}, {y1:g})"


def apply(text: str, patches: list[Patch]) -> str:
    """The DEF with each patch added to its net's routing, as a last ``NEW`` statement."""
    by_net: dict[str, list[Patch]] = defaultdict(list)
    for p in patches:
        by_net[p.shape.net].append(p)
    start = re.search(r"^\s*NETS \d+ ;", text, re.M)
    end = re.search(r"^\s*END NETS\b", text, re.M)
    if not start or not end:
        raise MinAreaError("the DEF has no NETS section")
    head, body, tail = text[: start.end()], text[start.end() : end.start()], text[end.start() :]
    out, pos, done = [], 0, set()
    for m in re.finditer(r"^\s*- (\S+)", body, re.M):
        net = m.group(1)
        if net not in by_net:
            continue
        semi = body.index(";", m.end())
        item = body[m.start() : semi]
        last = max(item.rfind("NEW "), item.rfind("+ ROUTED "), item.rfind("+ FIXED "))
        if last < 0 or re.search(
            r"\+ (?:USE|SOURCE|WEIGHT|PROPERTY|NONDEFAULTRULE)\b", item[last:]
        ):
            raise MinAreaError(f"net {net} does not end with its routing; it cannot take a patch")
        added = "".join(f"\n      {p.statement()}" for p in by_net[net])
        out.append(body[pos:semi].rstrip() + added + " ")
        pos = semi
        done.add(net)
    missing = set(by_net) - done
    if missing:
        raise MinAreaError(f"nets not found in the DEF: {', '.join(sorted(missing))}")
    out.append(body[pos:])
    return head + "".join(out) + tail


def lefs_of(resolved: dict[str, Any]) -> tuple[str, list[str]]:
    """The tech LEF and every other LEF a LibreLane configuration names."""
    tech = resolved.get("TECH_LEFS") or {}
    tech_lef = next((v for k, v in tech.items() if k.startswith("nom")), None) or next(
        iter(tech.values()), None
    )
    if not isinstance(tech_lef, str):
        raise MinAreaError("LibreLane's configuration names no tech LEF")
    others: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and v.endswith(".lef") and v not in tech.values():
            others.append(v)

    walk({k: v for k, v in resolved.items() if k != "TECH_LEFS"})
    return tech_lef, sorted(set(others))


def load(def_path: Path, resolved: dict[str, Any]) -> Layout:
    tech_lef, lefs = lefs_of(resolved)
    tech = read_tech(Path(tech_lef).read_text(errors="replace"))
    masters: dict[str, Master] = {}
    for lef in lefs:
        if Path(lef).is_file():
            masters.update(read_masters(Path(lef).read_text(errors="replace"), tech.dbu))
    return read_layout(def_path.read_text(errors="replace"), tech, masters)
