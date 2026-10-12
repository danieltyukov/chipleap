"""Where everything on the die goes: the pad ring, the core, the macros, the bond pads and
the seal ring. Pure geometry, from the chip section and the cells' sizes; no tool runs.

From the die edge inward: a band for the bond pads, the IO ring (corner cells, pads at
the pitch, fillers in every gap), then the core, centred. Coordinates are micrometres
with the die's lower-left corner at the origin, and every position is computed in
nanometres so nothing drifts off the manufacturing grid. A cell's position is the
lower-left corner of its outline after orientation, as in DEF.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chipleap.chip.config import SIDES, ChipConfig, Pad
from chipleap.chip.io import CORNER, FILLERS, PAD_CELLS, IoCell, fillers

BOND_UM = 70  # the bond pads' band between the die edge and the IO ring
SEAL_GAP_UM = 60  # from the die edge out to the seal ring's outline
CORE_MARGIN_UM = 50  # at least, between the core and the IO ring: the core's power ring
DEFAULT_MARGIN_UM = 100  # without core_um: room for the routes out to the pads as well
HALO_UM = 10  # around each macro, kept free of standard cells and other macros
SITE_NM = (480, 3780)  # IHP SG13G2's core site
# Each side's orientation: the IO cells' bond side faces the die edge.
SIDE_ORIENT = {"south": "N", "north": "S", "west": "E", "east": "W"}
CORNER_ORIENT = {"sw": "N", "se": "FN", "nw": "FS", "ne": "S"}

Rect = tuple[float, float, float, float]


class FloorplanError(ValueError):
    pass


@dataclass(frozen=True)
class Placed:
    inst: str
    cell: str
    x: float  # lower left of the outline after orientation, um
    y: float
    orient: str
    w: float  # outline after orientation, um
    h: float

    def box(self) -> Rect:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


@dataclass(frozen=True)
class PadSite:
    number: int  # in the bond map: 1 is the top pad of the west side, then counter-clockwise
    side: str
    slot: int
    pad: Pad
    cell: Placed
    bond_center: tuple[float, float]


@dataclass(frozen=True)
class Floorplan:
    die: tuple[float, float]
    core: Rect
    ring_inner: Rect  # the IO ring's inner (core-side) edge
    pads: tuple[PadSite, ...]
    ring: tuple[Placed, ...]  # every IO cell: pads, fillers, corners
    macros: tuple[Placed, ...]
    seal: Rect  # the chip's outline: the seal ring's outer edge
    pitch: float
    pad_depth: float
    bond_um: float = BOND_UM

    def to_dict(self) -> dict[str, object]:
        return {
            "die_um": list(self.die),
            "core_um": list(self.core),
            "outline_um": list(self.seal),
            "pad_pitch_um": self.pitch,
            "pads": [
                {
                    "number": p.number,
                    "side": p.side,
                    "slot": p.slot,
                    "kind": p.pad.kind,
                    "net": p.pad.net,
                    "cell": p.cell.cell,
                    "instance": p.cell.inst,
                    "bond_center_um": list(p.bond_center),
                }
                for p in self.pads
            ],
            "macros": [
                {"instance": m.inst, "cell": m.cell, "at_um": [m.x, m.y], "orientation": m.orient}
                for m in self.macros
            ],
            "ring_cells": len(self.ring),
        }


def _um(nm: int) -> float:
    return nm / 1000


def _nm(um: float) -> int:
    return round(um * 1000)


def pad_cell(pad: Pad, directions: Mapping[str, str]) -> str:
    """The IO cell of a pad: by its kind, and for a signal by the direction of its port
    (``directions``: net -> ``in`` or ``out``); "" for a signal whose direction is not
    known yet."""
    if pad.kind == "signal":
        d = directions.get(pad.net, "")
        return PAD_CELLS[d] if d in ("in", "out") else ""
    return PAD_CELLS.get(pad.kind, "")


def _side_xy(side: str, along: int, die: tuple[int, int], depth: int) -> tuple[int, int]:
    """Lower left of a ring cell ``along`` nm from the ring's corner edge on ``side``."""
    b = BOND_UM * 1000
    a = b + depth + along
    if side == "south":
        return a, b
    if side == "north":
        return a, die[1] - b - depth
    if side == "west":
        return b, a
    return die[0] - b - depth, a


def _bond_center(side: str, x: int, y: int, die: tuple[int, int], pad_w: int) -> tuple[int, int]:
    """The bond pad's centre: in the band, against the IO cell's bond side."""
    b = BOND_UM * 1000
    half = b // 2
    if side == "south":
        return x + pad_w // 2, b - half
    if side == "north":
        return x + pad_w // 2, die[1] - b + half
    if side == "west":
        return b - half, y + pad_w // 2
    return die[0] - b + half, y + pad_w // 2


def _sizes(io: Mapping[str, IoCell]) -> tuple[int, int, int]:
    """The pads' width and depth and the corner's size, in nm; every pad cell must have
    the same outline and the fillers the widths their names give."""
    try:
        pads = [io[c] for c in PAD_CELLS.values()]
        corner = io[CORNER]
        fill = [(w, io[c]) for w, c in FILLERS]
    except KeyError as exc:
        raise FloorplanError(f"the IO library has no {exc.args[0]}") from None
    if len({(_nm(p.width), _nm(p.height)) for p in pads}) != 1:
        raise FloorplanError("the IO library's pad cells differ in size")
    pad_w, depth = _nm(pads[0].width), _nm(pads[0].height)
    if _nm(corner.width) != depth or _nm(corner.height) != depth:
        raise FloorplanError("the IO library's corner is not as deep as its pads")
    for w, cell in fill:
        if _nm(cell.width) != w * 1000 or _nm(cell.height) != depth:
            raise FloorplanError(f"{cell.name} is not {w} um wide and as deep as the pads")
    return pad_w, depth, _nm(corner.width)


def plan(
    config: ChipConfig,
    io: Mapping[str, IoCell],
    macros: Mapping[str, tuple[str, float, float]] | None = None,
    directions: Mapping[str, str] | None = None,
) -> Floorplan:
    """The floorplan. ``io`` is the IO library's cells (``chipleap.chip.io.read_lef``),
    ``macros`` each macro instance's cell, width and height, ``directions`` each signal
    pad's port direction (``in`` or ``out``)."""
    macros = macros or {}
    directions = directions or {}
    pad_w, depth, corner = _sizes(io)
    pitch = _nm(config.pad_pitch_um)
    b = BOND_UM * 1000
    slots = {s: len(config.pads.get(s, ())) for s in SIDES}
    # Each side's span between the corners: its pads at the pitch, or what an explicit
    # core needs, whichever is more; a whole number of micrometres, so fillers fit it.
    span_x = max(slots["south"], slots["north"]) * pitch
    span_y = max(slots["west"], slots["east"]) * pitch
    if config.core_um is not None:
        core_w = _nm(config.core_um[0]) // SITE_NM[0] * SITE_NM[0]
        core_h = _nm(config.core_um[1]) // SITE_NM[1] * SITE_NM[1]
        # One site of slack on each side: the core is rounded onto the site grid.
        span_x = max(span_x, core_w + 2 * (CORE_MARGIN_UM * 1000 + SITE_NM[0]))
        span_y = max(span_y, core_h + 2 * (CORE_MARGIN_UM * 1000 + SITE_NM[1]))
    else:
        least = 2 * DEFAULT_MARGIN_UM * 1000 + 100_000  # a core of 100 um at the least
        span_x, span_y = max(span_x, least), max(span_y, least)
    span_x, span_y = -(-span_x // 1000) * 1000, -(-span_y // 1000) * 1000
    die = (2 * (b + corner) + span_x, 2 * (b + corner) + span_y)
    inner = (b + corner, b + corner, die[0] - b - corner, die[1] - b - corner)
    if config.core_um is None:
        m = DEFAULT_MARGIN_UM * 1000
        core_w = (span_x - 2 * m) // SITE_NM[0] * SITE_NM[0]
        core_h = (span_y - 2 * m) // SITE_NM[1] * SITE_NM[1]

    ring: list[Placed] = []
    sites: list[tuple[str, int, Pad, Placed, tuple[int, int]]] = []
    for side in SIDES:
        span = span_x if side in ("south", "north") else span_y
        pads = config.pads.get(side, ())
        lead = (span - len(pads) * pitch) // 2 // 1000 * 1000  # the slots, centred
        inset = (pitch - pad_w) // 2 // 1000 * 1000  # a pad, centred in its slot
        horizontal = side in ("south", "north")
        cursor = 0
        fill_no = 0

        def fill_to(stop: int, side: str = side, horizontal: bool = horizontal) -> None:
            nonlocal cursor, fill_no
            for width, cell in fillers((stop - cursor) // 1000):
                x, y = _side_xy(side, cursor, die, depth)
                w, h = (width * 1000, depth) if horizontal else (depth, width * 1000)
                inst = f"fill_{side}_{fill_no}"
                ring.append(Placed(inst, cell, _um(x), _um(y), SIDE_ORIENT[side], _um(w), _um(h)))
                fill_no += 1
                cursor += width * 1000

        for slot, pad in enumerate(pads):
            if pad.kind == "empty":
                continue
            start = lead + slot * pitch + inset
            fill_to(start)
            x, y = _side_xy(side, start, die, depth)
            w, h = (pad_w, depth) if horizontal else (depth, pad_w)
            placed = Placed(
                f"pad_{side}_{slot}",
                pad_cell(pad, directions),
                _um(x),
                _um(y),
                SIDE_ORIENT[side],
                _um(w),
                _um(h),
            )
            sites.append((side, slot, pad, placed, _bond_center(side, x, y, die, pad_w)))
            ring.append(placed)
            cursor = start + pad_w
        fill_to(span)
    for name, (x, y) in {
        "sw": (b, b),
        "se": (die[0] - b - corner, b),
        "nw": (b, die[1] - b - corner),
        "ne": (die[0] - b - corner, die[1] - b - corner),
    }.items():
        ring.append(
            Placed(
                f"corner_{name}", CORNER, _um(x), _um(y), CORNER_ORIENT[name], *(_um(corner),) * 2
            )
        )

    # The core, centred, on the site grid.
    cx = -(-((die[0] - core_w) // 2) // SITE_NM[0]) * SITE_NM[0]
    cy = -(-((die[1] - core_h) // 2) // SITE_NM[1]) * SITE_NM[1]
    core = (cx, cy, cx + core_w, cy + core_h)
    margin = CORE_MARGIN_UM * 1000
    if core_w <= 0 or core_h <= 0 or min(core[0] - inner[0], core[1] - inner[1]) < margin:
        raise FloorplanError("the core does not fit inside the pad ring")
    if min(inner[2] - core[2], inner[3] - core[3]) < margin:
        raise FloorplanError("the core does not fit inside the pad ring")

    placed_macros = _place_macros(config, macros, core)

    # Bond map order, counter-clockwise from the top of the west side: the west side
    # downward, the south side rightward, the east side upward, the north side leftward.
    def order(item: tuple[str, int, Pad, Placed, tuple[int, int]]) -> tuple[int, float]:
        side, _, _, p, _ = item
        along = {"west": -p.y, "south": p.x, "east": p.y, "north": -p.x}[side]
        return ("west", "south", "east", "north").index(side), along

    pad_sites = tuple(
        PadSite(i + 1, side, slot, pad, placed, (_um(bc[0]), _um(bc[1])))
        for i, (side, slot, pad, placed, bc) in enumerate(sorted(sites, key=order))
    )
    g = float(SEAL_GAP_UM)
    return Floorplan(
        die=(_um(die[0]), _um(die[1])),
        core=(_um(core[0]), _um(core[1]), _um(core[2]), _um(core[3])),
        ring_inner=(_um(inner[0]), _um(inner[1]), _um(inner[2]), _um(inner[3])),
        pads=pad_sites,
        ring=tuple(ring),
        macros=tuple(placed_macros),
        seal=(-g, -g, _um(die[0]) + g, _um(die[1]) + g),
        pitch=_um(pitch),
        pad_depth=_um(depth),
    )


def _place_macros(
    config: ChipConfig,
    macros: Mapping[str, tuple[str, float, float]],
    core: tuple[int, int, int, int],
) -> list[Placed]:
    """Each macro where the chip section puts it (from the core's lower left), or in a
    row along the core's top edge; snapped to the site grid, inside the core, apart by
    their halos."""
    halo = HALO_UM * 1000
    cw, ch = core[2] - core[0], core[3] - core[1]
    out: list[Placed] = []
    row_x = halo
    for m in config.macros:
        if m.instance not in macros:
            raise FloorplanError(f"no size for macro {m.instance}")
        cell, w_um, h_um = macros[m.instance]
        w, h = _nm(w_um), _nm(h_um)
        if m.at_um is not None:
            x, y = _nm(m.at_um[0]), _nm(m.at_um[1])
        else:
            x, y = row_x, ch - h - halo
            row_x += w + 2 * halo
        x = -(-x // SITE_NM[0]) * SITE_NM[0]
        # Onto the site grid: upward where the chip section puts it, downward along the
        # top edge, so the halo stays inside the core.
        y = (-(-y // SITE_NM[1]) if m.at_um is not None else y // SITE_NM[1]) * SITE_NM[1]
        if x < 0 or y < 0 or x + w > cw or y + h > ch:
            raise FloorplanError(
                f"macro {m.instance} ({w_um:g} x {h_um:g} um at {_um(x):g}, {_um(y):g} um in "
                f"the core) does not fit in the {_um(cw):g} x {_um(ch):g} um core; move it "
                "(at_um) or make the core larger (core_um)"
            )
        placed = Placed(
            m.instance, cell, _um(core[0] + x), _um(core[1] + y), m.orientation, w_um, h_um
        )
        a = placed.box()
        for other in out:
            o = other.box()
            if (
                a[0] - HALO_UM < o[2]
                and o[0] - HALO_UM < a[2]
                and a[1] - HALO_UM < o[3]
                and o[1] - HALO_UM < a[3]
            ):
                raise FloorplanError(
                    f"macros {other.inst} and {placed.inst} are closer than their {HALO_UM} um halo"
                )
        out.append(placed)
    return out
