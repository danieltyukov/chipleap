"""The power grid over a PDK macro: pdngen's macro grid for IHP's SRAMs, its placement rule
and its use in the chip's power grid."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipleap.chip import pdn
from chipleap.chip.floorplan import Placed
from chipleap.macro import grid
from chipleap.macro.grid import GridError, Placement
from chipleap.macro.layer import MacroPin, MacroView

CELL = "RM_IHPSG13_1P_256x32_c2_bm_bist"


def sram(
    height: float = 118.78,
    cell: str = CELL,
    supply_layer: str = "Metal4",
    pins: tuple[MacroPin, ...] | None = None,
) -> MacroView:
    p = Path("x")
    if pins is None:
        pins = (
            MacroPin("VDD!", "inout", "power", ((supply_layer, (10.0, 0.0, 12.8, 38.8)),)),
            MacroPin("VDDARRAY!", "inout", "power", ((supply_layer, (20.0, 38.8, 22.8, height)),)),
            MacroPin("VSS!", "inout", "ground", ((supply_layer, (30.0, 0.0, 32.8, height)),)),
            MacroPin("A_DIN[0]", "input", "signal", (("Metal2", (40.0, 0.0, 40.2, 1.0)),)),
        )
    return MacroView(
        cell, "memory", 416.64, height, pins, p, p, p, p, (cell,), (cell,), None, "pdk", False
    )


# The grid's script


def test_the_core_script_is_librelanes_grid_and_a_macro_grid_per_cell():
    a = Placement("u_mem", sram(), 30.0, 30.0)
    b = Placement("u_core.u_mem", sram(), 30.0, 170.0, "FS")
    text = grid.core_script([a, b], 0.0)
    lines = text.splitlines()
    assert lines[1] == "source $::env(SCRIPTS_DIR)/openroad/common/pdn_cfg.tcl"
    # Every supply pin of every instance, to the design's own nets.
    assert (
        "add_global_connection -net $::env(VDD_NET) -inst_pattern {^u_mem$} "
        "-pin_pattern {^VDDARRAY!$} -power"
    ) in lines
    assert (
        r"add_global_connection -net $::env(GND_NET) -inst_pattern {^u_core\.u_mem$} "
        "-pin_pattern {^VSS!$} -ground"
    ) in lines
    assert sum("add_global_connection" in line for line in lines) == 6
    assert lines.index("global_connect") > max(
        i for i, line in enumerate(lines) if "add_global_connection" in line
    )
    # One macro grid for the cell, however many instances; Metal4 up to TopMetal2.
    assert sum(line.startswith("define_pdn_grid -macro") for line in lines) == 1
    assert (
        f"define_pdn_grid -macro -cells {{{CELL}}} -name macro_{CELL} -starts_with POWER "
        "-voltage_domains CORE -halo {10 10}"
    ) in lines
    for pair in ("Metal4 Metal5", "Metal5 TopMetal1", "TopMetal1 TopMetal2"):
        assert f"add_pdn_connect -grid macro_{CELL} -layers {{{pair}}}" in lines
    assert any(f"-grid macro_{CELL} -layer Metal5 -width 2 -pitch 10" in line for line in lines)


def test_two_cells_get_two_grids():
    small = sram(cell="RM_IHPSG13_1P_64x64_c2_bm_bist", height=64.36)
    text = grid.core_script(
        [Placement("a", sram(), 30.0, 30.0), Placement("b", small, 500.0, 0.0)], 0.0
    )
    assert text.count("define_pdn_grid -macro") == 2


def test_supply_pins_off_metal4_are_refused():
    with pytest.raises(GridError, match="VDD! is on Metal3; the macro grid connects"):
        grid.supply_pins(sram(supply_layer="Metal3"))
    no_ground = tuple(p for p in sram().pins if p.kind != "ground")
    with pytest.raises(GridError, match="no power and ground"):
        grid.supply_pins(sram(pins=no_ground))


@pytest.mark.parametrize(
    "instance", ["u mem", "u_mem}", "u_mem$", "a.b.c.d.e.f.g.h.i", ".u_mem", "u_mem[0]"]
)
def test_instance_names_reach_the_script_only_as_identifiers(instance):
    with pytest.raises(GridError, match="plain hierarchical name"):
        grid.grid_lines([Placement(instance, sram(), 30.0, 30.0)], "VDD", "VSS")


def test_odd_cell_and_pin_names_are_refused():
    with pytest.raises(GridError, match="not a plain name"):
        grid.grid_lines([Placement("u", sram(cell="RM}; exec"), 30.0, 30.0)], "VDD", "VSS")
    odd = (
        MacroPin("VDD$", "inout", "power", (("Metal4", (0.0, 0.0, 1.0, 10.0)),)),
        *sram().pins[1:],
    )
    with pytest.raises(GridError, match="not a plain name"):
        grid.supply_pins(sram(pins=odd))


# The TopMetal2 pair under each macro


def test_the_pairs_over_a_macro():
    # VDD stripes at 13.6 + k x 75.6 um above the core's bottom edge, VSS 6.2 um above
    # each; a pair counts when it lies 10 um inside the macro.
    assert grid.tm2_pairs(0.0, 0.0, 64.36) == [13.6]
    assert grid.tm2_pairs(0.0, 4.0, 68.36) == []
    assert grid.tm2_pairs(100.0, 100.0, 300.0) == [113.6, 189.2, 264.8]
    assert grid.tm2_pairs(0.0, 0.0, 23.4) == []
    assert grid.tm2_pairs(0.0, 2.5, 30.9) == [13.6]  # just: 10 um either side


def test_a_macro_as_tall_as_the_pitch_and_a_pair_is_always_paired():
    for k in range(400):
        y = k * 0.19
        assert grid.tm2_pairs(0.0, y, y + grid.ALWAYS_PAIRED_UM)
    assert round(grid.ALWAYS_PAIRED_UM, 6) == 104.0


def test_nudge_finds_the_nearest_placement_holding_a_pair():
    # A 64-word macro, 64.36 um: its bottom edge holds the first pair from -33.46 to 2.5
    # um, the second from 42.14 to 78.1.
    assert grid.nudge(0.0, 0.0, 64.36) == 0.0
    assert grid.nudge(0.0, 12.5, 64.36) == 42.14
    assert grid.nudge(0.0, 12.5, 64.36, down=True) == 2.5
    assert grid.nudge(0.0, 50.0, 64.36) == 50.0
    assert grid.nudge(1.0, 80.0, 64.36) == 118.74  # the next window, on the 5 nm grid
    assert grid.nudge(0.0, 80.0, 64.36, down=True) == 78.1
    # On a coarser grid (the core sites, 3.78 um), still inside the window.
    up = grid.nudge(0.0, 12.5, 64.36, grid_um=3.78)
    assert up is not None and up % 3.78 == pytest.approx(0) and grid.tm2_pairs(0, up, up + 64.36)
    for y in (3.0, 20.0, 41.0, 100.0, 333.3):
        for down in (False, True):
            at = grid.nudge(7.0, y, 70.0, down=down)
            assert at is not None and grid.tm2_pairs(7.0, at, at + 70.0)
            assert (at >= y) if not down else (at <= y)
    with pytest.raises(GridError, match="cannot hold"):
        grid.nudge(0.0, 0.0, 28.0)


def test_a_macro_under_no_pair_is_refused_with_where_to_put_it():
    with pytest.raises(
        GridError, match=r"12\.5 um above the core's bottom edge.* 2\.5 or 42\.14 um"
    ):
        grid.check_pairs([Placement("u_mem", sram(height=64.36), 30.0, 112.5)], 100.0)
    with pytest.raises(GridError, match="move it inside the core"):
        grid.check_pairs([Placement("u_mem", sram(height=64.36), 0.0, 60.0)], 100.0)
    grid.check_pairs([Placement("u_mem", sram(height=64.36), 0.0, 150.0)], 100.0)


# The chip's grid


def test_the_chip_grid_takes_the_macro_grid_for_an_sram():
    ram = sram()
    placed = Placed("u_mem", CELL, 300.0, 300.0, "N", ram.width, ram.height)
    text = pdn.script({"u_mem": (placed, ram)}, 250.0)
    assert "add_global_connection -net VDD -inst_pattern {^u_mem$} -pin_pattern {^VDD!$}" in text
    assert "add_global_connection -net VSS -inst_pattern {^u_mem$} -pin_pattern {^VSS!$}" in text
    assert f"define_pdn_grid -macro -cells {{{CELL}}}" in text
    assert not any(line.startswith("chipleap_hookup ") for line in text.splitlines())
    # The chip's own grid stays as it was, before the macro grid.
    assert text.index("-name stdcell_grid") < text.index("define_pdn_grid -macro")


def test_the_chip_grid_checks_the_srams_pair_and_needs_the_core():
    short = sram(height=64.36)
    placed = Placed("u_mem", CELL, 300.0, 262.5, "N", short.width, short.height)
    with pytest.raises(pdn.PdnError, match="no whole VDD and VSS pair"):
        pdn.script({"u_mem": (placed, short)}, 250.0)
    with pytest.raises(pdn.PdnError, match="core's position"):
        pdn.script({"u_mem": (placed, short)})
