"""The chip's power grid: OpenROAD's pdngen script that LibreLane runs (``PDN_CFG``).

The supply pads feed the IO ring's own rails, which run unbroken through every IO cell,
filler and corner: ``vdd`` and ``vss`` on TopMetal1 140 to 158 um and 160 to 178 um
from the ring's outer edge. A core ring on TopMetal1 and TopMetal2 is joined to them by
``add_pdn_ring -connect_to_pads``, which drops TopMetal2 straps with a TopVia2 onto each
rail; stripes on both top metals cross the core, and Metal1 follows the standard-cell
rows.

A macro from aispice has its supply pins as narrow columns reaching one edge, which
pdngen's macro grid finds nothing to connect to (PDN-0232). The script draws each
hookup itself: a strap on the pin's layer from the pin straight to the nearest
standard-cell rail of its net beyond that edge, and a via stack down to Metal1 there.
"""

from __future__ import annotations

from collections.abc import Mapping

from chipleap.chip.floorplan import Placed
from chipleap.macro import grid
from chipleap.macro.layer import HOOKUP_LAYERS, MacroView
from chipleap.rtl import quote as q
from chipleap.rtl.project import IDENT_RE

EDGE_UM = 0.001
# Each routing layer's via down to the layer below it.
VIA_DOWN = {"Metal2": "Via1_YX", "Metal3": "Via2_YX", "Metal4": "Via3_YX", "Metal5": "Via4_YX"}
SUPPLY_NETS = {"power": "VDD", "ground": "VSS"}


class PdnError(ValueError):
    pass


def hookups(placed: Placed, view: MacroView) -> list[tuple[str, str, float, float, str]]:
    """Where each supply pin of a placed macro is joined to the rails: (net, layer, x, y,
    direction), x and y in um on the die at the middle of the pin's shape and the edge it
    reaches, direction ``down`` or ``up``."""
    if not view.supply_hookup:
        raise PdnError(
            f"{view.cell}: its supply pins run across the block, which needs pdngen's macro "
            "grid; ChipLeap straps only supply pins that reach an edge"
        )
    out = []
    flipped = placed.orient in ("S", "FS")  # the macro's bottom edge is on top
    for pin in view.pins:
        if not pin.is_supply:
            continue
        if not IDENT_RE.fullmatch(pin.name):
            raise PdnError(f"{view.cell}: supply pin {pin.name} is not a plain name")
        at = None
        for layer, (x0, y0, x1, y1) in pin.shapes:
            if layer not in HOOKUP_LAYERS:
                continue
            if y0 <= EDGE_UM:
                at = (layer, (x0 + x1) / 2, 0.0, "down")
            elif y1 >= view.height - EDGE_UM:
                at = (layer, (x0 + x1) / 2, view.height, "up")
            if at is not None:
                break
        if at is None:
            raise PdnError(
                f"{view.cell}: supply pin {pin.name} has no shape on "
                f"{', '.join(HOOKUP_LAYERS)} that reaches the macro's bottom or top edge; "
                "ChipLeap connects a macro's supply pins there"
            )
        layer, x, y, direction = at
        if placed.orient in ("FN", "S"):  # mirrored left to right
            x = view.width - x
        if flipped:
            y = view.height - y
            direction = "up" if direction == "down" else "down"
        out.append(
            (
                SUPPLY_NETS[pin.kind],
                layer,
                round(placed.x + x, 4),
                round(placed.y + y, 4),
                direction,
            )
        )
    return out


HOOKUP_PROC = r"""
# A macro supply pin's hookup: a strap on the pin's layer from the pin's edge at (x, y)
# to the nearest standard-cell rail of the net beyond it, and a via stack to Metal1.
proc chipleap_hookup {net layer vias x_um y_um dir} {
    set block [ord::get_db_block]
    set tech [[ord::get_db] getTech]
    set dbu [$block getDbUnitsPerMicron]
    set xc [expr {round($x_um * $dbu)}]
    set ye [expr {round($y_um * $dbu)}]
    set best ""
    foreach row [$block getRows] {
        set rb [$row getBBox]
        if { [$rb xMin] > $xc - $dbu || [$rb xMax] < $xc + $dbu } { continue }
        if { $dir == "down" } {
            if { [$rb yMax] > $ye } { continue }
            if { $best == "" || [$rb yMax] > [lindex $best 0] } {
                set best [list [$rb yMax] [$rb yMin] [$row getOrient]]
            }
        } else {
            if { [$rb yMin] < $ye } { continue }
            if { $best == "" || [$rb yMin] < [lindex $best 1] } {
                set best [list [$rb yMax] [$rb yMin] [$row getOrient]]
            }
        }
    }
    if { $best == "" } { error "chipleap: no standard-cell row $dir of the macro pin at $x_um $y_um" }
    lassign $best ytop ybot orient
    # R0 rows have VSS at the bottom and VDD at the top; flipped rows the other way.
    set vdd_top [expr {$orient == "R0"}]
    if { ($net == "VDD") == $vdd_top } { set y $ytop } else { set y $ybot }
    set n [$block findNet $net]
    set sw [odb::dbSWire_create $n ROUTED]
    set hw [expr {int(0.15 * $dbu)}]
    set ext [expr {int(0.3 * $dbu)}]
    set top [$tech findLayer $layer]
    if { $dir == "down" } {
        odb::dbSBox_create $sw $top [expr {$xc - $hw}] [expr {$y - $ext}] [expr {$xc + $hw}] [expr {$ye + $ext}] STRIPE
    } else {
        odb::dbSBox_create $sw $top [expr {$xc - $hw}] [expr {$ye - $ext}] [expr {$xc + $hw}] [expr {$y + $ext}] STRIPE
    }
    foreach v $vias {
        set via [$tech findVia $v]
        set lower [[$via getBottomLayer] getName]
        if { $lower != "Metal1" } {
            odb::dbSBox_create $sw [$tech findLayer $lower] [expr {$xc - $hw}] [expr {$y - $ext}] [expr {$xc + $hw}] [expr {$y + $ext}] STRIPE
        }
        odb::dbSBox_create $sw $via $xc $y STRIPE
    }
    puts "\[INFO\] chipleap: $net hookup at [expr {double($xc) / $dbu}] um to the rail at [expr {double($y) / $dbu}] um"
}
"""


def script(macros: Mapping[str, tuple[Placed, MacroView]], core_y0: float | None = None) -> str:
    """The PDN script; ``macros`` maps each macro instance to its placement and views. A
    macro whose supply stripes run across it (an SRAM) gets pdngen's macro grid
    (``chipleap.macro.grid``), which needs the core's bottom edge, ``core_y0``, to check
    a TopMetal2 supply pair crosses it."""
    lines = [
        "# Written by ChipLeap: the core's power grid, joined to the pad ring and the macros.",
        "source $::env(SCRIPTS_DIR)/openroad/common/set_global_connections.tcl",
        "set_global_connections",
        "foreach {net kind} {IOVDD POWER IOVSS GROUND} {",
        '    if { [[ord::get_db_block] findNet $net] == "NULL" } {',
        "        set n [odb::dbNet_create [ord::get_db_block] $net]",
        "        $n setSpecial",
        "        $n setSigType $kind",
        "    }",
        "}",
    ]
    for net, io_pin, kind in (
        ("VDD", "vdd", "power"),
        ("VSS", "vss", "ground"),
        ("IOVDD", "iovdd", "power"),
        ("IOVSS", "iovss", "ground"),
    ):
        lines.append(
            f"add_global_connection -net {net} -inst_pattern {{^(pad|fill|corner)_.*}} "
            f"-pin_pattern {{^{io_pin}$}} -{kind}"
        )
    hooks: list[tuple[str, str, float, float, str]] = []
    gridded: list[grid.Placement] = []
    for inst, (placed, view) in macros.items():
        if not view.supply_hookup:
            gridded.append(grid.Placement(inst, view, placed.x, placed.y, placed.orient))
            continue
        if not IDENT_RE.fullmatch(inst):
            raise PdnError(f"macro instance {inst!r} is not a plain name")
        for pin in view.pins:
            if pin.is_supply:
                # Both names are plain identifiers (checked here and in hookups), and the
                # braces keep Tcl from reading the regular expression's $.
                if not IDENT_RE.fullmatch(pin.name):
                    raise PdnError(f"{view.cell}: supply pin {pin.name} is not a plain name")
                lines.append(
                    f"add_global_connection -net {SUPPLY_NETS[pin.kind]} "
                    f"-inst_pattern {{^{inst}$}} -pin_pattern {{^{pin.name}$}} -{pin.kind}"
                )
        hooks += hookups(placed, view)
    lines += [
        "global_connect",
        "set_voltage_domain -name CORE -power VDD -ground VSS",
        "define_pdn_grid -name stdcell_grid -starts_with POWER -voltage_domain CORE "
        "-pins {TopMetal2}",
        "add_pdn_ring -grid stdcell_grid -layers {TopMetal1 TopMetal2} -widths {8 8} "
        "-spacings {4 4} -core_offsets {8 8} -connect_to_pads "
        "-connect_to_pad_layers {TopMetal1 TopMetal2}",
        "add_pdn_stripe -grid stdcell_grid -layer TopMetal1 -width 2.2 -pitch 75.6 "
        "-offset 13.6 -spacing 4 -starts_with POWER -extend_to_core_ring",
        "add_pdn_stripe -grid stdcell_grid -layer TopMetal2 -width 2.2 -pitch 75.6 "
        "-offset 13.6 -spacing 4 -starts_with POWER -extend_to_core_ring",
        "add_pdn_connect -grid stdcell_grid -layers {TopMetal1 TopMetal2}",
        "add_pdn_stripe -grid stdcell_grid -layer Metal1 -width 0.44 -followpins "
        "-extend_to_core_ring",
        "add_pdn_connect -grid stdcell_grid -layers {Metal1 TopMetal1}",
        HOOKUP_PROC.strip("\n"),
    ]
    if gridded:
        if core_y0 is None:
            raise PdnError("a macro grid needs the core's position")
        try:
            grid.check_pairs(gridded, core_y0)
            lines += grid.grid_lines(gridded, "VDD", "VSS")
        except grid.GridError as exc:
            raise PdnError(str(exc)) from None
    for net, layer, x, y, direction in hooks:
        vias = [VIA_DOWN[f"Metal{k}"] for k in range(int(layer[-1]), 1, -1)]
        lines.append(
            f"chipleap_hookup {net} {layer} {q.tcl_list(vias)} {x:.4f} {y:.4f} {direction}"
        )
    return "\n".join(lines) + "\n"
