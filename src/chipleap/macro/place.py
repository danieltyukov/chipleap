"""Where a block's SRAM macros go, and the die around them: a row along the top of the core
and a row along the bottom, the standard cells in the band between.

IHP's SRAMs have every signal pin on their bottom edge, along the whole width (784 um on
a 64-bit macro). So the top row stands upright (``N``) with its pins facing down into the
band, and the bottom row is mirrored (``FS``) with its pins facing up; no wire runs past a
macro to reach the logic. Each macro keeps its halo from the core's edges and from the
others, and lies under a whole TopMetal2 supply pair of the core's grid
(``chipleap.macro.grid``): a short one moves towards the band until it does.

The band holds the standard cells at ``util`` of its area, from an estimate of their
area: the die is sized here, before synthesis, so the floorplan is ChipLeap's and its
numbers are known to the power grid.
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


class PlaceError(ValueError):
    pass


@dataclass(frozen=True)
class Macro:
    instance: str
    width: float
    height: float


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

    @property
    def core_um2(self) -> float:
        x0, y0, x1, y1 = self.core
        return (x1 - x0) * (y1 - y0)


def _up(v: float, step: float) -> float:
    return round(math.ceil(round(v / step, 6)) * step, 4)


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
    core_w = _up(
        max(MIN_CORE_UM, width(top) + 2 * HALO_UM, width(bottom) + 2 * HALO_UM), SITE_UM[0]
    )

    def depth(row: list[Macro]) -> float:
        return max((m.height for m in row), default=-2 * HALO_UM) + 2 * HALO_UM

    band = max(MIN_BAND_UM, cell_um2 / util / core_w)
    core_h = _up(max(MIN_CORE_UM, depth(top) + band + depth(bottom)), SITE_UM[1])
    x0, y0 = _up(MARGIN_UM, SITE_UM[0]), _up(MARGIN_UM, SITE_UM[1])
    x1, y1 = round(x0 + core_w, 4), round(y0 + core_h, 4)
    die = (_up(x1 + MARGIN_UM, SITE_UM[0]), _up(y1 + MARGIN_UM, SITE_UM[1]))

    placed: list[Placed] = []
    for row, orient in ((top, "N"), (bottom, "FS")):
        x = x0 + HALO_UM
        for m in row:
            if orient == "N":
                y = grid.nudge(y0, y1 - HALO_UM - m.height, m.height, grid_um=0.005, down=True)
            else:
                y = grid.nudge(y0, y0 + HALO_UM, m.height, grid_um=0.005)
            if y is None or y < y0 + HALO_UM or y + m.height > y1 - HALO_UM:
                raise PlaceError(
                    f"macro {m.instance} ({m.width:g} x {m.height:g} um) has no place under a "
                    "supply pair of the core's grid"
                )
            placed.append(Placed(m.instance, round(_up(x, SITE_UM[0]), 4), y, orient))
            x = _up(x, SITE_UM[0]) + m.width + 2 * HALO_UM
    out = Floorplan(die, (x0, y0, x1, y1), tuple(placed))
    _check(out, macros)
    return out


def _check(plan: Floorplan, macros: Sequence[Macro]) -> None:
    """Every macro inside the core by its halo, and two halos from every other."""
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
        for name, b in boxes:
            if not (
                a[2] + gap <= b[0] or b[2] + gap <= a[0] or a[3] + gap <= b[1] or b[3] + gap <= a[1]
            ):
                raise PlaceError(f"macros {name} and {p.instance} are closer than two halos")
        boxes.append((p.instance, a))
