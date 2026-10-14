"""KLayout DRC reports, read the same way by the chip step and by place and route.

IHP's rule deck checks that every drawn layer lies on the 5 nm grid, pin markers included
(``metal1_pin_Offgrid``, and the same rule on the other pin layers), and IHP's own SRAMs
draw some Metal1 pin markers off it (in ``RM_IHPSG13_1P_BLDRV``, a cell of every
single-port SRAM); the deck flags them on an SRAM on its own as well. A pin marker is no
mask layer. So an off-grid pin marker inside the cell tree of one of the PDK's own macros
is set apart: reported with its rule, cell and count, and left out of the verdict. The
same rule in any other cell, and every other rule inside the macro, counts.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DB_MAX = 256 << 20
PIN_OFFGRID_RE = re.compile(r"[A-Za-z0-9]+_pin_Offgrid")
# KLayout names the variant of a cell a deep-mode check ran on after a colon: its
# orientation (RM_IHPSG13_1P_BLDRV:r0, :m90).
VARIANT_RE = re.compile(r":[rm]\d+$")


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

    @property
    def total(self) -> int:
        return sum(self.violations.values())

    @property
    def markers(self) -> int:
        return sum(self.pin_markers.values())

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
    path: Path, into: DrcResult, pdk_cells: Mapping[str, tuple[str, ...]] | None = None
) -> None:
    """Count a KLayout report database's items by rule; an off-grid pin marker inside one
    of ``pdk_cells`` (the PDK's own macros and their cells, from ``pdk_cells``) is set
    apart, every other item counts."""
    held = pdk_cells or {}
    if path.stat().st_size > DB_MAX:
        raise DrcReportError(f"{path.name} is larger than expected")
    into.reports += 1
    try:
        for _, el in ET.iterparse(path, events=("end",)):
            if el.tag == "item":
                rule = (el.findtext("category") or "?").strip().strip("'")
                cell = VARIANT_RE.sub("", (el.findtext("cell") or "").strip())
                if cell in held and PIN_OFFGRID_RE.fullmatch(rule):
                    into.pin_markers[(rule, cell)] = into.pin_markers.get((rule, cell), 0) + 1
                    into.held_by[cell] = held[cell]
                else:
                    into.violations[rule] = into.violations.get(rule, 0) + 1
                el.clear()
    except ET.ParseError as exc:
        raise DrcReportError(f"{path.name} is not a report database: {exc}") from None


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
