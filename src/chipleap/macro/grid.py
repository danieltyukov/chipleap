"""The power grid over a PDK macro: pdngen's macro grid for IHP's SRAMs.

An SRAM of ``sg13g2_sram`` has its supply pins (``VDD!``, ``VDDARRAY!``, ``VSS!``) as
vertical Metal4 stripes across the whole block, under obstructions on Metal1 to Metal4.
LibreLane's own grid for IHP (TopMetal1 vertical stripes, TopMetal2 horizontal ones,
Metal1 rails) connects none of them. A macro grid does, for each cell placed:

- every supply pin of every instance globally connected to the design's supply nets
  (LibreLane's ``VDD_NET`` and ``GND_NET``: a connection to a net that does not exist
  crashes OpenROAD's connectivity check);
- Metal5 horizontal straps across the macro, which cross every Metal4 stripe, and
  TopMetal1 vertical stripes over it, which cross the straps;
- connections Metal4 to Metal5, Metal5 to TopMetal1, and TopMetal1 to the core's
  TopMetal2 stripes, which run across the core over the macro.

So each macro must lie under a whole VDD and VSS pair of the core's TopMetal2 stripes
(``tm2_pairs``); a macro taller than ``ALWAYS_PAIRED_UM`` always does, and ``nudge``
moves a shorter one up until it does.

A macro whose supply pins are not on Metal4 is refused: the straps would find nothing.
Names reach the script only as identifiers (instances may be hierarchical, ``u_a.u_b``,
as Yosys flattens them) and as regular expressions built from them, in braces.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from chipleap.macro.layer import MacroView
from chipleap.rtl import quote as q

# LibreLane's grid for IHP SG13G2 (its PDK's librelane/config.tcl): TopMetal2 stripes
# 2.2 um wide, a VDD and a VSS 4 um apart, every 75.6 um from 13.6 um above the core's
# bottom edge (stripe centres, as pdngen draws them).
TM2_OFFSET_UM = 13.6
TM2_PITCH_UM = 75.6
TM2_WIDTH_UM = 2.2
TM2_SPACING_UM = 4.0
# The macro grid's own stripes: what the prototype measured clean (spec, fact 3).
STRAP_LAYER = "Metal5"
STRAP_WIDTH_UM = 2.0
STRAP_PITCH_UM = 10.0
STRAP_OFFSET_UM = 4.0
STRAP_SPACING_UM = 3.0
STRIPE_LAYER = "TopMetal1"
STRIPE_WIDTH_UM = 2.2
STRIPE_PITCH_UM = 37.8
STRIPE_OFFSET_UM = 13.6
STRIPE_SPACING_UM = 4.0
# How far inside the macro the grid's stripes reach, at most, from its top and bottom.
INSET_UM = 10.0
SUPPLY_LAYER = "Metal4"
HALO_UM = 10.0  # kept free of standard cells around a macro: its rows are cut this far
# pdngen clears the core's straps this far around a macro grid. It must be less than the
# halo the rows are cut to: with the same 10 um, the core's TopMetal1 straps stopped
# about 9 um short of the first rails beside a row of macros, and pdngen could not repair
# the rails it left unconnected there (PDN-0178, PDN-0179).
GRID_HALO_UM = 5.0
INSTANCE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,7}")
PIN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*!?")
CELL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PAIR_UM = TM2_WIDTH_UM + TM2_SPACING_UM + TM2_WIDTH_UM
ALWAYS_PAIRED_UM = TM2_PITCH_UM + PAIR_UM + 2 * INSET_UM


class GridError(ValueError):
    pass


@dataclass(frozen=True)
class Placement:
    """A macro instance on the die: its flattened instance name, its views, the lower
    left of its outline in um and its orientation."""

    instance: str
    view: MacroView
    x: float
    y: float
    orient: str = "N"

    @property
    def height(self) -> float:
        return self.view.height


def supply_pins(view: MacroView) -> list[tuple[str, str]]:
    """(pin, kind) for each supply pin of a macro the grid connects; refuses a macro
    whose supply pins are not on Metal4 or have names a script could misread."""
    pins = []
    for pin in view.pins:
        if not pin.is_supply:
            continue
        if not PIN_RE.fullmatch(pin.name):
            raise GridError(f"{view.cell}: supply pin {pin.name!r} is not a plain name")
        layers = {layer for layer, _ in pin.shapes}
        if layers != {SUPPLY_LAYER}:
            raise GridError(
                f"{view.cell}: supply pin {pin.name} is on {', '.join(sorted(layers)) or 'no layer'}"
                f"; the macro grid connects supply stripes on {SUPPLY_LAYER}"
            )
        pins.append((pin.name, pin.kind))
    if not {k for _, k in pins} >= {"power", "ground"}:
        raise GridError(f"{view.cell} has no power and ground pins to connect")
    return pins


def _nm(um: float) -> int:
    return round(um * 1000)


def _windows(core_y0: float, height: float, until: int) -> Iterator[tuple[int, int]]:
    """For each of the core's TopMetal2 VDD stripes from the lowest, the lowest and the
    highest bottom edge (nm, die coordinates) at which a macro of ``height`` holds the
    stripe's whole VDD and VSS pair with the inset to spare; up to the first window that
    starts above ``until``."""
    w, s, inset = _nm(TM2_WIDTH_UM), _nm(TM2_SPACING_UM), _nm(INSET_UM)
    c = _nm(core_y0) + _nm(TM2_OFFSET_UM)
    while True:
        lo = c + w // 2 + s + w + inset - _nm(height)
        yield lo, c - w // 2 - inset
        if lo > until:
            return
        c += _nm(TM2_PITCH_UM)


def tm2_pairs(core_y0: float, y0: float, y1: float) -> list[float]:
    """Centres of the core's TopMetal2 VDD stripes whose whole VDD and VSS pair lies
    between ``y0`` and ``y1`` (um, die coordinates) with the grid's inset to spare."""
    w, s, inset = _nm(TM2_WIDTH_UM), _nm(TM2_SPACING_UM), _nm(INSET_UM)
    lo, hi = _nm(y0) + inset, _nm(y1) - inset
    out: list[float] = []
    c = _nm(core_y0) + _nm(TM2_OFFSET_UM)
    while c - w // 2 <= hi:
        if c - w // 2 >= lo and c + s + w + w // 2 <= hi:
            out.append(c / 1000)
        c += _nm(TM2_PITCH_UM)
    return out


def nudge(
    core_y0: float, y: float, height: float, grid_um: float = 0.005, down: bool = False
) -> float | None:
    """``y``, or the nearest y above it (below it with ``down``) on the ``grid_um`` grid
    at which a macro of ``height`` lies under a whole TopMetal2 VDD and VSS pair; None
    when there is none below. Refuses a macro too short for any pair."""
    if height < PAIR_UM + 2 * INSET_UM + grid_um:
        raise GridError(
            f"a macro {height:g} um tall cannot hold a TopMetal2 supply pair inside its grid"
        )
    if tm2_pairs(core_y0, y, y + height):
        return y
    g, at = _nm(grid_um), _nm(y)
    # Onto the grid, inside each window: the height check leaves every window a grid
    # step wide at least.
    fits = [(-(-lo // g) * g, hi // g * g) for lo, hi in _windows(core_y0, height, at)]
    if down:
        below = [hi for lo, hi in fits if hi <= at]
        return max(below) / 1000 if below else None
    return min(lo for lo, hi in fits if lo >= at) / 1000


def _inst_pattern(instance: str) -> str:
    if not INSTANCE_RE.fullmatch(instance):
        raise GridError(f"macro instance {instance!r} is not a plain hierarchical name")
    return "^" + instance.replace(".", r"\.") + "$"


def grid_lines(placements: Sequence[Placement], vdd: str, gnd: str) -> list[str]:
    """The global connections of every instance's supply pins to ``vdd`` and ``gnd`` (Tcl
    words: a net name, or a variable such as ``$::env(VDD_NET)``), and one macro grid per
    cell."""
    lines: list[str] = []
    cells: dict[str, MacroView] = {}
    for p in placements:
        pattern = _inst_pattern(p.instance)
        for pin, kind in supply_pins(p.view):
            net = vdd if kind == "power" else gnd
            lines.append(
                f"add_global_connection -net {net} -inst_pattern {{{pattern}}} "
                f"-pin_pattern {{^{pin}$}} -{kind}"
            )
        cells.setdefault(p.view.cell, p.view)
    lines.append("global_connect")
    for cell in sorted(cells):
        if not CELL_RE.fullmatch(cell):
            raise GridError(f"macro cell {cell!r} is not a plain name")
        name = q.plain(f"macro_{cell}")
        lines += [
            f"define_pdn_grid -macro -cells {{{cell}}} -name {name} "
            f"-starts_with POWER -voltage_domains CORE -halo {{{GRID_HALO_UM:g} {GRID_HALO_UM:g}}}",
            f"add_pdn_stripe -grid {name} -layer {STRAP_LAYER} -width {STRAP_WIDTH_UM:g} "
            f"-pitch {STRAP_PITCH_UM:g} -offset {STRAP_OFFSET_UM:g} "
            f"-spacing {STRAP_SPACING_UM:g} -starts_with POWER",
            f"add_pdn_stripe -grid {name} -layer {STRIPE_LAYER} -width {STRIPE_WIDTH_UM:g} "
            f"-pitch {STRIPE_PITCH_UM:g} -offset {STRIPE_OFFSET_UM:g} "
            f"-spacing {STRIPE_SPACING_UM:g} -starts_with POWER",
            f"add_pdn_connect -grid {name} -layers {{{SUPPLY_LAYER} {STRAP_LAYER}}}",
            f"add_pdn_connect -grid {name} -layers {{{STRAP_LAYER} {STRIPE_LAYER}}}",
            f"add_pdn_connect -grid {name} -layers {{{STRIPE_LAYER} TopMetal2}}",
        ]
    return lines


def check_pairs(placements: Iterable[Placement], core_y0: float) -> None:
    """Refuse a placement that leaves a macro without a whole TopMetal2 supply pair over
    it, which would leave its grid unconnected; the error says where the macro's bottom
    edge could go instead, from the core's bottom edge (as ``at_um`` gives it)."""
    for p in placements:
        if tm2_pairs(core_y0, p.y, p.y + p.height):
            continue
        at = [nudge(core_y0, p.y, p.height, down=True), nudge(core_y0, p.y, p.height)]
        where = " or ".join(
            f"{y - core_y0:g}" for y in at if y is not None and y >= core_y0 + HALO_UM
        )
        raise GridError(
            f"macro {p.instance}, {p.y - core_y0:g} um above the core's bottom edge, lies "
            f"under no whole VDD and VSS pair of the core's TopMetal2 stripes, so its "
            "supply grid would connect to nothing; "
            + (f"place it at {where} um" if where else "move it inside the core")
        )


def core_script(placements: Sequence[Placement], core_y0: float) -> str:
    """The PDN script for a design with PDK macros (LibreLane's ``PDN_CFG``): LibreLane's
    own grid, as it builds it, then the macros' grids."""
    check_pairs(placements, core_y0)
    lines = [
        "# Written by ChipLeap: LibreLane's power grid, and a macro grid for each SRAM.",
        "source $::env(SCRIPTS_DIR)/openroad/common/pdn_cfg.tcl",
        *grid_lines(placements, "$::env(VDD_NET)", "$::env(GND_NET)"),
    ]
    return "\n".join(lines) + "\n"
