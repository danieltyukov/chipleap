"""Where a block's SRAM macros go, and the die around them: a row along the top of the core
and a row along the bottom, the standard cells in the band between.

IHP's SRAMs have every signal pin on their bottom edge, along the whole width (784 um on
a 64-bit macro). So the top row stands upright (``N``) with its pins facing down into the
band, and the bottom row is mirrored (``FS``) with its pins facing up; no wire runs past a
macro to reach the logic. Each macro keeps its halo from the core's edges and from the
others, and lies under a whole TopMetal2 supply pair of the core's grid
(``chipleap.macro.grid``): a short one moves towards the band until it does.

OpenROAD takes the core to be the bounding box of its rows, and pdngen draws the core's
stripes only inside it. The rows under a row of macros as wide as the core are cut, which
would leave the macros outside it, with no TopMetal2 stripe over them for their grid to
join. So a column of rows ``SIDE_UM`` wide stays free of macros at the core's left edge:
the core keeps its height, and the core's first TopMetal1 VDD and VSS stripes (13.6 and
19.8 um in) fall in the column and strap its rails.

The band holds the standard cells at ``util`` of its area, from an estimate of their
area: the die is sized here, before synthesis, so the floorplan is ChipLeap's and its
numbers are known to the power grid.

No pin goes on the stretch of the die's top or bottom edge that a row covers, from its
first macro's halo to its last's (``Floorplan.no_pins``, LibreLane's
``IO_EXCLUDE_PIN_REGION``): a pin there reaches the band through the margin under the
macros or around their ends. LibreLane's IO placer put read-data inputs there, and their
nets failed antenna checks: one of 360 um in the GEMV 1 x 4 build (IHP's deck), two of
about 400 um in a SmolLM2 build (OpenROAD's check). The pins take the left and right
edges and the ends of the top and bottom edges the rows leave free.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from chipleap.macro import grid

SITE_UM = (0.48, 3.78)  # IHP SG13G2's core site
HALO_UM = grid.HALO_UM
MARGIN_UM = 20.0  # from the die's edge to the core: room for the pins and their routes
MIN_BAND_UM = 40.0
MIN_CORE_UM = 100.0
SIDE_UM = 30.0  # the column of rows at the core's left edge, before the first halo


class PlaceError(ValueError):
    pass


@dataclass(frozen=True)
class Macro:
    instance: str
    width: float
    height: float
    gridded: bool = True  # powered by a macro grid, so under a TopMetal2 supply pair


@dataclass(frozen=True)
class Placed:
    instance: str
    x: float
    y: float
    orient: str


@dataclass(frozen=True)
class Floorplan:
    die: tuple[float, float]
    core: tuple[float, float, float, float]
    macros: tuple[Placed, ...]
    # The edges' stretches no pin goes on: (edge, from, to), in um along the edge from the
    # die's lower left. A floorplan a project gave has none.
    no_pins: tuple[tuple[str, int, int], ...] = ()

    @property
    def core_um2(self) -> float:
        x0, y0, x1, y1 = self.core
        return (x1 - x0) * (y1 - y0)


def _up(v: float, step: float) -> float:
    return round(math.ceil(round(v / step, 6)) * step, 4)


def _down(v: float, step: float) -> float:
    return round(math.floor(round(v / step, 6)) * step, 4)


def given(die: tuple[float, float], macros: Sequence[tuple[Macro, float, float, str]]) -> Floorplan:
    """The floorplan of a die a project sized and macros it placed (each with its lower
    left from the core's, in um, and its orientation): the core inside the margins, on the
    site grid, the macros checked."""
    x0, y0 = _up(MARGIN_UM, SITE_UM[0]), _up(MARGIN_UM, SITE_UM[1])
    x1, y1 = _down(die[0] - MARGIN_UM, SITE_UM[0]), _down(die[1] - MARGIN_UM, SITE_UM[1])
    if x1 - x0 < MIN_CORE_UM or y1 - y0 < MIN_CORE_UM:
        raise PlaceError(f"a die of {die[0]:g} x {die[1]:g} um leaves too small a core")
    placed = tuple(
        Placed(m.instance, round(x0 + x, 4), round(y0 + y, 4), orient) for m, x, y, orient in macros
    )
    out = Floorplan(die, (x0, y0, x1, y1), placed)
    check(out, [m for m, _, _, _ in macros])
    return out


def rows(macros: Sequence[Macro], cell_um2: float, util: float = 0.45) -> Floorplan:
    """The floorplan for ``macros`` and ``cell_um2`` of standard cells at ``util``."""
    if not 0.05 <= util <= 0.95:
        raise PlaceError(f"a utilisation of {util:g} is not one to place at")
    top: list[Macro] = []
    bottom: list[Macro] = []

    def width(row: list[Macro]) -> float:
        # Each macro snaps right onto the site grid: a site of slack for each.
        return sum(m.width + SITE_UM[0] for m in row) + 2 * HALO_UM * max(0, len(row) - 1)

    # The widest first, each to the narrower row: the rows come out about as wide.
    for m in sorted(macros, key=lambda m: (-m.width, -m.height, m.instance)):
        (top if width(top) <= width(bottom) else bottom).append(m)
    x0, y0 = _up(MARGIN_UM, SITE_UM[0]), _up(MARGIN_UM, SITE_UM[1])

    def xs(row: list[Macro]) -> list[float]:
        """Each macro's left edge on the site grid, the row packed from the left."""
        out, x = [], x0 + SIDE_UM + HALO_UM
        for m in row:
            out.append(round(_up(x, SITE_UM[0]), 4))
            x = out[-1] + m.width + 2 * HALO_UM
        return out

    # The core ends at the widest row's last halo, so no sliver of rows is left between
    # it and the core's edge: pdngen cannot reach the rails of rows a few sites wide.
    ends = [
        x + m.width + HALO_UM for row in (top, bottom) for x, m in zip(xs(row), row, strict=True)
    ]
    core_w = _up(max(MIN_CORE_UM, *(e - x0 for e in ends)), SITE_UM[0])

    def depth(row: list[Macro]) -> float:
        return max((m.height for m in row), default=-2 * HALO_UM) + 2 * HALO_UM

    band = max(MIN_BAND_UM, cell_um2 / util / core_w)
    core_h = _up(max(MIN_CORE_UM, depth(top) + band + depth(bottom)), SITE_UM[1])
    x1, y1 = round(x0 + core_w, 4), round(y0 + core_h, 4)
    die = (_up(x1 + MARGIN_UM, SITE_UM[0]), _up(y1 + MARGIN_UM, SITE_UM[1]))

    placed: list[Placed] = []
    for row, orient in ((top, "N"), (bottom, "FS")):
        for x, m in zip(xs(row), row, strict=True):
            top_y = round(y1 - HALO_UM - m.height, 4)
            y: float | None = top_y if orient == "N" else round(y0 + HALO_UM, 4)
            if m.gridded and orient == "N":
                y = grid.nudge(y0, top_y, m.height, grid_um=0.005, down=True)
            elif m.gridded:
                y = grid.nudge(y0, y0 + HALO_UM, m.height, grid_um=0.005)
            if y is None or y < y0 + HALO_UM - 1e-6 or y + m.height > y1 - HALO_UM + 1e-6:
                raise PlaceError(
                    f"macro {m.instance} ({m.width:g} x {m.height:g} um) has no place under a "
                    "supply pair of the core's grid"
                )
            placed.append(Placed(m.instance, x, y, orient))
    # Each row's stretch of its edge, rounded out to whole um.
    no_pins = tuple(
        (edge, math.floor(xs(row)[0] - HALO_UM), math.ceil(xs(row)[-1] + row[-1].width + HALO_UM))
        for edge, row in (("top", top), ("bottom", bottom))
        if row
    )
    out = Floorplan(die, (x0, y0, x1, y1), tuple(placed), no_pins)
    check(out, macros)
    return out


def pin_regions(plan: Floorplan) -> list[str]:
    """LibreLane's ``IO_EXCLUDE_PIN_REGION`` for ``plan``: its ``no_pins``."""
    return [f"{edge}:{lo}-{hi}" for edge, lo, hi in plan.no_pins]


def check(plan: Floorplan, macros: Sequence[Macro]) -> None:
    """Every macro inside the core by its halo, and two halos from every other; for a
    floorplan ChipLeap made or one a project gave."""
    size = {m.instance: m for m in macros}
    x0, y0, x1, y1 = plan.core
    gap = 2 * HALO_UM - 1e-6
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    for p in plan.macros:
        m = size[p.instance]
        a = (p.x, p.y, p.x + m.width, p.y + m.height)
        inside = (
            x0 + HALO_UM - 1e-6,
            y0 + HALO_UM - 1e-6,
            x1 - HALO_UM + 1e-6,
            y1 - HALO_UM + 1e-6,
        )
        if a[0] < inside[0] or a[1] < inside[1] or a[2] > inside[2] or a[3] > inside[3]:
            raise PlaceError(f"macro {p.instance} is not inside the core by its halo")
        if a[0] < inside[0] + SIDE_UM:
            raise PlaceError(
                f"macro {p.instance} is {a[0] - x0:g} um from the core's left edge; keep it "
                f"{SIDE_UM + HALO_UM:g} um away, for the column of rows that keeps the core's "
                "power grid across the whole core"
            )
        for name, b in boxes:
            if not (
                a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1]
            ):
                raise PlaceError(f"macros {name} and {p.instance} are closer than two halos")
        boxes.append((p.instance, a))
