"""The chip section, the floorplan, the nets, the PDN script and the checks' parsers, on
stand-ins: no tool and no PDK needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chip_fakes import COUNTER_CHIP, fake_pdk, io_lef
from chipleap.chip import assemble, checks, floorplan, io, netlist, pdn
from chipleap.chip.config import ChipConfig, ChipMacro, Pad
from chipleap.chip.floorplan import FloorplanError, Placed
from chipleap.chip.netlist import NetlistError, Port
from chipleap.macro.check import PdkLibs, Report
from chipleap.macro.layer import MacroPin, MacroView
from chipleap.rtl.project import ProjectError, load_project

CELLS = io.read_lef(io_lef())
ROOT = Path(__file__).resolve().parents[1]
FLOW_DIR = Path("/build/chip/flow")


def counter_chip(root: Path, extra: str = "") -> Path:
    (root / "project.yaml").write_text((root / "project.yaml").read_text() + COUNTER_CHIP + extra)
    return root


# The chip section


def test_a_chip_section_is_read_and_written_back(counter_project):
    p = load_project(counter_chip(counter_project))
    assert p.chip is not None and p.chip.name == "counter_chip"
    assert p.chip.pads["south"][0] == Pad("signal", "clk_i")
    assert p.chip.pads["west"][2] == Pad("analog", "vin")
    assert p.chip.pads["north"][3] == Pad("empty")
    d = p.to_dict()["chip"]
    assert d["pads"]["west"][2] == {"analog": "vin"} and d["pads"]["north"][3] is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("  pad_pitch_um: 50\n", "at least 90"),
        ("  core_um: [100]\n", "core_um must be"),
        ("  name: 9chip\n", "chip.name"),
        ("  macros: [{instance: pad_x, package: rtl}]\n", "starts like ChipLeap's own"),
        ("  macros: [{instance: m, package: ../elsewhere}]\n", "package"),
        ("  macros: [{instance: m, package: rtl, orientation: E}]\n", "orientation"),
        ("  macros: [{instance: m, package: rtl, at_um: [-1, 0]}]\n", "at_um"),
        ("  macros: [{instance: m, package: rtl, connect: {a: '$x'}}]\n", "must be a net"),
        ("  macros: [{instance: m, package: rtl}, {instance: m, package: rtl}]\n", "twice"),
    ],
)
def test_bad_chip_sections_are_refused(counter_project, extra, message):
    with pytest.raises(ProjectError, match=message):
        load_project(counter_chip(counter_project, extra))


def test_pads_are_checked(counter_project):
    text = (counter_project / "project.yaml").read_text()
    for pads, message in (
        ("south: [clk_i, clk_i, vdd, vss, iovdd, iovss]", "on a pad already"),
        ("south: ['a b', vdd, vss, iovdd, iovss]", "not a port bit"),
        ("south: [clk_i, vdd, vss, iovdd]", "needs at least one iovss"),
        ("south: [{analog: v}, {analog: v}, vdd, vss, iovdd, iovss]", "on a pad already"),
        ("south: [{digital: v}, vdd, vss, iovdd, iovss]", "unknown key"),
    ):
        (counter_project / "project.yaml").write_text(text + f"chip:\n  pads:\n    {pads}\n")
        with pytest.raises(ProjectError, match=message):
            load_project(counter_project)


def test_a_package_symlinked_out_of_the_project_is_refused(counter_project, tmp_path):
    outside = tmp_path / "outside.macro"
    outside.mkdir()
    (counter_project / "macros").mkdir()
    (counter_project / "macros" / "m.macro").symlink_to(outside, target_is_directory=True)
    extra = "  macros: [{instance: m, package: macros/m.macro}]\n"
    with pytest.raises(ProjectError, match="outside"):
        load_project(counter_chip(counter_project, extra))


# The IO library


def test_the_io_lef_is_read():
    pad = CELLS["sg13g2_IOPadIn"]
    assert (pad.width, pad.height) == (80.0, 180.0)
    assert pad.directions["p2c"] == "OUTPUT" and pad.uses["vdd"] == "POWER"
    assert len(pad.core_ports("p2c")) == 1 and pad.core_ports("pad") == []


def test_the_analog_pad_loses_its_bond_side_port():
    lef = io.chip_lef(io_lef())
    cells = io.read_lef(lef)
    assert len(CELLS["sg13g2_IOPadAnalog"].pins["pad"]) == 2
    assert len(cells["sg13g2_IOPadAnalog"].pins["pad"]) == 1
    assert cells["sg13g2_IOPadAnalog"].core_ports("pad")
    # The other cells are as they were.
    assert cells["sg13g2_IOPadIn"].pins == CELLS["sg13g2_IOPadIn"].pins


def test_a_changed_analog_pad_is_refused():
    lef = io_lef().replace(
        "RECT 5.0 0.0 75.0 3.0 ;\n    END\n    PORT", "RECT 5.0 0.0 75.0 3.0 ;", 1
    )
    with pytest.raises(io.IoError):
        io.chip_lef(lef.replace("RECT 26.105 179.71 50.875 180.0", "RECT 26.105 1.0 50.875 2.0"))


def test_fillers_fill_any_gap_exactly():
    for gap in (0, 1, 7, 49, 50, 117, 999):
        assert sum(w for w, _ in io.fillers(gap)) == gap
    assert [w for w, _ in io.fillers(117)] == [50, 50, 10, 5, 2]
    with pytest.raises(io.IoError):
        io.fillers(-1)


def test_the_io_liberty_has_supplies_and_directions():
    text = io.liberty("io_cells", [CELLS["sg13g2_IOPadIn"], CELLS["sg13g2_Corner"]], {"vdd": 1.2})
    assert "input_threshold_pct_rise" in text and "cell (sg13g2_Corner)" in text
    assert "pin (p2c) {\n      direction : output ;" in text
    assert "pg_pin (iovdd)" in text and "voltage_map (vdd, 1.2)" in text


def test_the_pdk_is_found_from_the_environment(tmp_path, monkeypatch):
    pdk = fake_pdk(tmp_path)
    monkeypatch.setenv(io.PDK_ENV, str(pdk))
    assert io.pdk_dir() == pdk
    monkeypatch.setenv(io.PDK_ENV, "relative/ihp-sg13g2")
    assert io.pdk_dir() is None


def test_the_pdk_commit_is_read_without_git(tmp_path):
    root = tmp_path / "IHP-Open-PDK"
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "refs" / "heads" / "main").write_text("22f2a25" + "0" * 33 + "\n")
    assert io.pdk_commit(root / "ihp-sg13g2") == "22f2a25" + "0" * 33
    (root / ".git" / "HEAD").write_text("ref: refs/../../etc/passwd\n")
    assert io.pdk_commit(root / "ihp-sg13g2") == ""


# The floorplan


def chip(pads=None, macros=(), core=(400.0, 400.0), pitch=100.0) -> ChipConfig:
    s = lambda n: Pad("signal", n)  # noqa: E731
    pads = pads or {
        "south": (s("a"), Pad("vdd"), Pad("vss"), s("b")),
        "east": (s("c"), Pad("iovdd"), Pad("iovss")),
        "north": (s("d"), Pad("empty"), s("e")),
        "west": (Pad("analog", "v"), s("f")),
    }
    return ChipConfig("c", pads, tuple(macros), pitch, core)


def test_the_ring_is_closed_and_on_whole_micrometres():
    plan = floorplan.plan(chip(), CELLS)
    w, h = plan.die
    # The core sets the spans: 400 um on the site grid, a 50 um margin and a site of
    # slack on each side, rounded up to whole micrometres.
    assert (w, h) == (2 * (70 + 180) + 501.0, 2 * (70 + 180) + 505.0)
    for side in ("south", "north", "west", "east"):
        cells = [c for c in plan.ring if c.inst.startswith(("pad_" + side, "fill_" + side))]
        along = sum(c.w if side in ("south", "north") else c.h for c in cells)
        span = (w if side in ("south", "north") else h) - 2 * (70 + 180)
        assert along == pytest.approx(span)
    for c in plan.ring:
        assert all(abs(v * 1000 - round(v * 1000)) < 1e-6 for v in (c.x, c.y))
    assert len([c for c in plan.ring if c.inst.startswith("corner_")]) == 4
    assert plan.seal == (-60.0, -60.0, w + 60, h + 60)


def test_pads_face_the_die_edge_and_bond_pads_sit_against_them():
    plan = floorplan.plan(chip(), CELLS)
    by = {p.cell.inst: p for p in plan.pads}
    south, west = by["pad_south_0"], by["pad_west_0"]
    assert south.cell.orient == "N" and south.cell.y == 70.0
    assert south.bond_center == (south.cell.x + 40, 35.0)
    assert west.cell.orient == "E" and west.cell.x == 70.0 and west.bond_center[0] == 35.0
    assert by["pad_north_0"].cell.orient == "S" and by["pad_east_0"].cell.orient == "W"


def test_the_bond_map_runs_counter_clockwise_from_the_top_of_the_west_side():
    plan = floorplan.plan(chip(), CELLS)
    order = [(p.side, p.slot) for p in plan.pads]
    assert order[:2] == [("west", 1), ("west", 0)]
    assert order[2:6] == [("south", 0), ("south", 1), ("south", 2), ("south", 3)]
    assert order[6:9] == [("east", 0), ("east", 1), ("east", 2)]
    assert order[9:] == [("north", 2), ("north", 0)]
    assert [p.number for p in plan.pads] == list(range(1, 12))


def test_the_core_is_centred_on_the_site_grid():
    plan = floorplan.plan(chip(), CELLS)
    x0, y0, x1, _y1 = plan.core
    assert round(x0 * 1000) % 480 == 0 and round(y0 * 1000) % 3780 == 0
    assert abs((x0 + x1) / 2 - plan.die[0] / 2) < 0.5
    assert x0 - plan.ring_inner[0] >= floorplan.CORE_MARGIN_UM


def test_without_a_core_size_the_core_fills_the_ring():
    plan = floorplan.plan(chip(core=None), CELLS)
    assert plan.core[0] - plan.ring_inner[0] == pytest.approx(floorplan.DEFAULT_MARGIN_UM, abs=1)


def test_pad_cells_follow_the_ports_directions():
    plan = floorplan.plan(chip(), CELLS, directions={"a": "in", "b": "out"})
    cells = {p.cell.inst: p.cell.cell for p in plan.pads}
    assert cells["pad_south_0"] == "sg13g2_IOPadIn" and cells["pad_south_3"] == "sg13g2_IOPadOut4mA"
    assert cells["pad_east_0"] == "" and cells["pad_west_0"] == "sg13g2_IOPadAnalog"


def test_macros_are_placed_in_the_core_apart_by_their_halos():
    m = [ChipMacro("m1", Path("x")), ChipMacro("m2", Path("x"), at_um=(100.0, 50.0))]
    plan = floorplan.plan(chip(macros=m), CELLS, {"m1": ("a", 20.0, 40.0), "m2": ("a", 20.0, 40.0)})
    m1, m2 = plan.macros
    assert m1.y + m1.h <= plan.core[3] - floorplan.HALO_UM + 1e-6
    assert m2.x == pytest.approx(plan.core[0] + 100.32) and m2.cell == "a"
    with pytest.raises(FloorplanError, match="does not fit"):
        floorplan.plan(
            chip(macros=[ChipMacro("m", Path("x"), at_um=(390.0, 0.0))]),
            CELLS,
            {"m": ("a", 20.0, 40.0)},
        )
    near = [
        ChipMacro("m1", Path("x"), at_um=(0.0, 0.0)),
        ChipMacro("m2", Path("x"), at_um=(25.0, 0.0)),
    ]
    with pytest.raises(FloorplanError, match="halo"):
        floorplan.plan(chip(macros=near), CELLS, {"m1": ("a", 20.0, 40.0), "m2": ("a", 20.0, 40.0)})


def test_the_cores_macros_are_placed_with_the_chips_but_stay_inside_the_core():
    m = [ChipMacro("m1", Path("x"))]
    sizes = {"m1": ("a", 20.0, 40.0), "u_core.u_mem": ("ram", 100.0, 60.0)}
    inner = [("u_core.u_mem", (40.0, 20.0), "N")]
    plan = floorplan.plan(chip(macros=m), CELLS, sizes, core_macros=inner)
    # The chip top instantiates its own macros; the core's are inside u_core.
    assert [x.inst for x in plan.macros] == ["m1"]
    (mem,) = plan.core_macros
    assert mem.inst == "u_core.u_mem" and mem.cell == "ram"
    assert mem.x == pytest.approx(plan.core[0] + 40.32) and mem.y >= plan.core[1] + 20.0
    assert [x.inst for x in plan.every_macro()] == ["m1", "u_core.u_mem"]
    assert "u_core.u_mem" in netlist.placement(plan)
    assert plan.to_dict()["macros"][1]["instance"] == "u_core.u_mem"
    # Kept apart from the chip's by the same halos.
    with pytest.raises(FloorplanError, match="halo"):
        floorplan.plan(
            chip(macros=[ChipMacro("m1", Path("x"), at_um=(0.0, 0.0))]),
            CELLS,
            sizes,
            core_macros=[("u_core.u_mem", (25.0, 0.0), "N")],
        )


def test_an_sram_along_the_top_edge_moves_down_under_a_supply_pair():
    from chipleap.macro import grid

    sizes = {"u_core.u_mem": ("ram", 236.8, 74.1)}
    inner = [("u_core.u_mem", None, "N")]
    plan = floorplan.plan(chip(), CELLS, sizes, core_macros=inner, gridded=frozenset(sizes))
    (mem,) = plan.core_macros
    assert grid.tm2_pairs(plan.core[1], mem.y, mem.y + mem.h)
    # On the row grid, and the same macro left ungridded stays at the edge.
    assert round((mem.y - plan.core[1]) * 1000) % 3780 == 0
    edge = floorplan.plan(chip(), CELLS, sizes, core_macros=inner).core_macros[0]
    assert edge.y > mem.y


def test_a_library_without_a_cell_is_refused():
    cells = dict(CELLS)
    del cells["sg13g2_Filler200"]
    with pytest.raises(FloorplanError, match="Filler200"):
        floorplan.plan(chip(), cells)


# The nets


def ports(**kw: tuple[str, int]) -> dict[str, Port]:
    return {n: Port(n, d, w) for n, (d, w) in kw.items()}


PORTS = ports(
    a=("input", 1),
    b=("output", 1),
    c=("input", 1),
    d=("output", 1),
    e=("output", 1),
    f=("input", 1),
)


def view(pins: list[MacroPin], cell: str = "cmp") -> MacroView:
    p = Path("x")
    return MacroView(
        cell, "analog", 20.0, 40.0, tuple(pins), p, p, p, p, (cell,), (cell,), Report("x")
    )


CMP = view(
    [
        MacroPin("clk", "input", "clock", (("Metal4", (7.2, 0.0, 7.5, 7.7)),)),
        MacroPin("inp", "input", "analog", (("Metal4", (4.8, 0.0, 5.1, 17.0)),)),
        MacroPin("out", "output", "signal", (("Metal4", (3.6, 0.0, 3.9, 20.0)),)),
        MacroPin("outn", "output", "signal", (("Metal4", (2.4, 0.0, 2.7, 20.0)),)),
        MacroPin("vdd", "inout", "power", (("Metal4", (1.2, 0.0, 1.5, 28.0)),)),
        MacroPin("vss", "inout", "ground", (("Metal4", (0.0, 0.0, 0.3, 0.6)),)),
    ]
)


BIND_PADS = {
    "south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("signal", "b")),
    "east": (Pad("iovdd"), Pad("iovss")),
    "north": (Pad("signal", "d"), Pad("signal", "e")),
    "west": (Pad("analog", "v"), Pad("signal", "f")),
}


def bind(macro_connect=None, port_map=PORTS, pads=BIND_PADS, clocks=("a",)):
    macros = ()
    if macro_connect is not None:
        macros = (ChipMacro("u1", Path("x"), connect=macro_connect),)
    cfg = chip(pads=pads, macros=macros)
    return cfg, netlist.bind(cfg, port_map, {"cmp": CMP}, {"u1": "cmp"}, clocks)


def test_every_pad_and_macro_pin_gets_its_net():
    pads = {
        "south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("signal", "b")),
        "east": (Pad("iovdd"), Pad("iovss")),
        "north": (Pad("signal", "d"), Pad("signal", "e")),
        "west": (Pad("analog", "v", resistor=True), Pad("signal", "f")),
    }
    _cfg, b = bind({"clk": "a", "inp": "v", "out": "c"}, pads=pads)
    assert b.io["pad_south_0"] == {"p2c": "a_core", "pad": "pad_a"}
    assert b.io["pad_south_3"] == {"c2p": "b_core", "pad": "pad_b"}
    assert b.io["pad_west_0"] == {"padres": "v", "pad": "v_pad"}
    assert b.macros["u1"] == {"clk": "a_core", "inp": "v", "out": "c_core", "outn": "u1_outn"}
    assert b.directions == {"a": "in", "b": "out", "d": "out", "e": "out", "f": "in"}
    assert b.clocks == {"a": "pad_south_0"}
    assert any("outn is not connected" in w for w in b.warnings)
    assert any("core input f" in w for w in b.warnings) is False


@pytest.mark.parametrize(
    ("connect", "message"),
    [
        ({"clk": "a", "out": "c"}, "input inp is not connected"),
        ({"clk": "a", "inp": "v", "out": "c", "nope": "x"}, "does not have"),
        ({"clk": "a", "inp": "v", "out": "c", "vdd": "x"}, "supply pin"),
        ({"clk": "a", "inp": "v", "out": "c", "outn": "a"}, "2 drivers"),
        ({"clk": "a", "inp": "v", "out": "b"}, "core output b is also driven"),
        ({"clk": "a", "inp": "v"}, "core input c is not driven"),
        ({"clk": "a", "inp": "v", "out": "c", "outn": "a[3]"}, "not a bit of core port a"),
    ],
)
def test_wrong_nets_are_refused(connect, message):
    with pytest.raises(NetlistError, match=message):
        bind(connect)


def test_pads_name_bits_of_the_core():
    bus = {"q": Port("q", "output", 4), "up": Port("up", "input", 2, offset=1, upto=True)}
    assert bus["q"].bits() == ["q[3]", "q[2]", "q[1]", "q[0]"]
    assert bus["up"].bits() == ["up[1]", "up[2]"]
    pads = {
        "south": (Pad("signal", "q[9]"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss")),
    }
    with pytest.raises(NetlistError, match="not a port bit"):
        bind(pads=pads, port_map=bus, clocks=())
    with pytest.raises(NetlistError, match="inout"):
        bind(port_map={"io": Port("io", "inout", 1)}, clocks=())


def test_a_clock_must_come_from_an_input_pad():
    with pytest.raises(NetlistError, match="clock c must be on an input pad"):
        bind({"clk": "a", "inp": "v", "out": "c"}, clocks=("c",))


def test_yosys_ports_are_read():
    data = {
        "modules": {
            "top": {
                "ports": {
                    "x": {"direction": "input", "upto": 1, "bits": [2, 3, 4, 5]},
                    "y": {"direction": "input", "offset": 1, "bits": [6, 7]},
                    "z": {"direction": "output", "bits": [8]},
                }
            }
        }
    }
    p = netlist.parse_ports(data, "top")
    assert p["x"].bits() == ["x[0]", "x[1]", "x[2]", "x[3]"]
    assert p["y"].bits() == ["y[2]", "y[1]"] and p["z"].bits() == ["z"]
    data["modules"]["top"]["ports"]["$bad"] = {"direction": "input", "bits": [1]}
    with pytest.raises(NetlistError):
        netlist.parse_ports(data, "top")


def test_the_chip_top_holds_the_ring_the_core_and_the_macros():
    cfg, b = bind({"clk": "a", "inp": "v", "out": "c"})
    plan = floorplan.plan(cfg, CELLS, {"u1": ("cmp", 20.0, 40.0)}, b.directions)
    v = netlist.chip_top(cfg, b, plan, "core", {"W": 6})
    assert v.startswith("// c: written by ChipLeap")
    assert "(* keep *) sg13g2_IOPadIn pad_south_0 (.p2c(a_core), .pad(pad_a));" in v
    assert "(* keep *) sg13g2_Corner corner_sw ();" in v
    assert "core #(.W(6)) u_core (" in v and ".c(c_core)" in v
    assert "cmp u1 (.clk(a_core), .inp(v), .out(c_core), .outn(u1_outn));" in v
    assert "inout pad_a;" in v and "wire v_padres;" in v


def test_names_that_collide_are_refused():
    pads = {
        "south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss")),
        "west": (Pad("analog", "a_core"),),
    }
    with pytest.raises(NetlistError, match="a_core is the net of core port bit a"):
        bind(pads=pads, port_map=ports(a=("input", 1)))
    pads["west"] = (Pad("analog", "wire"),)
    with pytest.raises(NetlistError, match="cannot be a net name"):
        bind(pads=pads, port_map=ports(a=("input", 1)))


def test_the_sdc_times_the_pads_core_side_pins():
    _, b = bind({"clk": "a", "inp": "v", "out": "c"})
    sdc = netlist.sdc(b, {"a": 10.0})
    assert "create_clock -name a -period 10 [get_pins pad_south_0/p2c]" in sdc
    assert "pad_south_0/p2c pad" not in sdc  # the clock pad has no input delay
    outputs = next(ln for ln in sdc.splitlines() if ln.startswith("set_output_delay"))
    assert "pad_south_3/c2p" in outputs and "p2c" not in outputs


def test_pins_sit_outside_the_bond_side_on_the_grid():
    cfg, b = bind({"clk": "a", "inp": "v", "out": "c"})
    plan = floorplan.plan(cfg, CELLS, {"u1": ("cmp", 20.0, 40.0)}, b.directions)
    text = netlist.pins_def(cfg, b, plan, (80.0, 180.0))
    by = {c.inst: c for c in plan.ring}
    for line in text.splitlines():
        if "FIXED" in line:
            x, y = (int(v) for v in line.split("FIXED ( ")[1].split(" )")[0].split())
            assert x % 5 == 0 and y % 5 == 0
    south = by["pad_south_0"]
    assert f"FIXED ( {round((south.x + 39) * 1000)} 67500 )" in text


@pytest.mark.parametrize(
    ("orient", "expected"),
    [
        ("N", (10.0, 20.0, 11.0, 22.0)),
        ("S", (69.0, 158.0, 70.0, 160.0)),
        ("E", (20.0, 69.0, 22.0, 70.0)),
        ("W", (158.0, 10.0, 160.0, 11.0)),
        ("FN", (69.0, 20.0, 70.0, 22.0)),
        ("FS", (10.0, 158.0, 11.0, 160.0)),
    ],
)
def test_shapes_follow_the_cells_orientation(orient, expected):
    c = Placed("i", "c", 0.0, 0.0, orient, 0.0, 0.0)
    assert netlist.transform(c, (10.0, 20.0, 11.0, 22.0), (80.0, 180.0)) == pytest.approx(expected)


# The power grid


def test_supply_pins_are_strapped_to_the_rail_beyond_their_edge():
    placed = Placed("u1", "cmp", 100.0, 200.0, "N", 20.0, 40.0)
    hooks = pdn.hookups(placed, CMP)
    assert hooks == [
        ("VDD", "Metal4", 101.35, 200.0, "down"),
        ("VSS", "Metal4", 100.15, 200.0, "down"),
    ]
    flipped = pdn.hookups(Placed("u1", "cmp", 100.0, 200.0, "S", 20.0, 40.0), CMP)
    assert flipped[0] == ("VDD", "Metal4", 118.65, 240.0, "up")
    mirrored = pdn.hookups(Placed("u1", "cmp", 100.0, 200.0, "FS", 20.0, 40.0), CMP)
    assert mirrored[1] == ("VSS", "Metal4", 100.15, 240.0, "up")


def test_a_supply_pin_away_from_the_edges_is_refused():
    inner = view([MacroPin("vdd", "inout", "power", (("Metal4", (1.0, 5.0, 2.0, 30.0)),))])
    with pytest.raises(pdn.PdnError, match="bottom or top edge"):
        pdn.hookups(Placed("u1", "cmp", 0.0, 0.0, "N", 20.0, 40.0), inner)
    top = view([MacroPin("vdd", "inout", "power", (("TopMetal1", (1.0, 0.0, 2.0, 30.0)),))])
    with pytest.raises(pdn.PdnError):
        pdn.hookups(Placed("u1", "cmp", 0.0, 0.0, "N", 20.0, 40.0), top)


def test_the_pdn_script_joins_the_core_to_the_ring_and_the_macros():
    placed = Placed("u1", "cmp", 100.0, 200.0, "N", 20.0, 40.0)
    text = pdn.script({"u1": (placed, CMP)})
    assert "-connect_to_pads -connect_to_pad_layers {TopMetal1 TopMetal2}" in text
    assert "add_global_connection -net VDD -inst_pattern {^u1$} -pin_pattern {^vdd$} -power" in text
    assert (
        "chipleap_hookup VSS Metal4 {{Via3_YX} {Via2_YX} {Via1_YX}} 100.1500 200.0000 down" in text
    )
    assert "-pins {TopMetal2}" in text


# The checks


IO_CDL = """\
.SUBCKT sg13g2_IOPadIn iovdd iovss pad p2c vdd vss
.ENDS
.SUBCKT sg13g2_IOPadAnalog iovdd iovss pad padres vdd vss
.ENDS
"""


def lvs_case():
    s = lambda n: Pad("signal", n)  # noqa: E731
    cfg = chip(
        pads={
            "south": (s("a"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss")),
            "west": (Pad("analog", "v"),),
        }
    )
    plan = floorplan.plan(cfg, CELLS, directions={"a": "in"})
    cdl = """\
* CDL Netlist generated by OpenROAD
.SUBCKT c pad_a VSS VDD
Xpad_south_0 IOVDD IOVSS pad_a a_core VDD
+ VSS sg13g2_IOPadIn
Xpad_west_0 IOVDD IOVSS v v_padres VDD VSS sg13g2_IOPadAnalog
X_1_ a_core net1 VDD VSS sg13g2_inv_1
Xu1 net1 v VDD VSS cmp
.ENDS c
"""
    return plan, cdl


def test_the_lvs_netlist_leaves_out_the_io_cells_and_labels_their_core_side_pins():
    plan, cdl = lvs_case()
    got = checks.lvs_inputs(cdl, IO_CDL, plan, CELLS, [".SUBCKT cmp a b VDD VSS\n.ENDS"])
    assert got.ports == ["VDD", "VSS", "a_core", "v", "v_padres"]
    assert ".SUBCKT c VDD VSS a_core v v_padres" in got.netlist
    assert "Xpad_" not in got.netlist and "X_1_ a_core net1 VDD VSS sg13g2_inv_1" in got.netlist
    assert got.instances == 2
    nets = {p["net"]: p["rects"] for p in got.pins}
    assert set(nets) == {"a_core", "v", "v_padres"}
    pad = next(p.cell for p in plan.pads if p.cell.inst == "pad_south_0")
    assert nets["a_core"][0] == [10, pytest.approx([pad.x + 39.83, 227.81, pad.x + 40.12, 250.0])]
    view_plan = checks.lvs_plan(plan, "c", got, Path("/in.gds"), Path("/out.gds"))
    assert view_plan["keep_box"][0] == plan.ring_inner[0] - 1
    assert "sg13g2_IOPad" in view_plan["drop_prefixes"]


def test_an_unplain_net_on_a_pad_is_refused():
    plan, cdl = lvs_case()
    with pytest.raises(checks.CheckError, match="plain name"):
        checks.lvs_inputs(cdl.replace(" a_core VDD\n", " a$core VDD\n"), IO_CDL, plan, CELLS, [])


def test_a_flattened_net_on_a_pad_gets_a_plain_name_in_the_netlist_and_on_its_label():
    """A core net driven straight from a cell (an SRAM's output) keeps the core's
    flattened name in LibreLane's netlist; the LVS netlist and the pin's label take one
    plain name for it, which no other net has."""
    plan, cdl = lvs_case()
    flat = cdl.replace("a_core", "u_core.a\\[3\\]").replace("net1", "u_core_a_3")
    got = checks.lvs_inputs(flat, IO_CDL, plan, CELLS, [])
    assert "u_core." not in got.netlist and "\\[" not in got.netlist
    assert got.ports == ["VDD", "VSS", "u_core_a_3_1", "v", "v_padres"]
    assert "X_1_ u_core_a_3_1 u_core_a_3 VDD VSS sg13g2_inv_1" in got.netlist
    assert {p["net"] for p in got.pins} == {"u_core_a_3_1", "v", "v_padres"}


PASS_LINE = "2026-10-12 04:17:43 +0200: Memory Usage (0K) : INFO : Congratulations! Netlists match."


def test_the_lvs_outcome_is_read_from_the_log():
    assert checks.lvs_outcome(f"start\n{PASS_LINE}\n") == "pass"
    assert checks.lvs_outcome("ERROR : Netlists don't match") == "fail"
    assert checks.lvs_outcome("") == "unknown"
    # A failure anywhere wins over a pass.
    assert checks.lvs_outcome(f"{PASS_LINE}\nERROR : Netlists don't match\n") == "fail"


def test_a_name_in_the_layout_cannot_print_a_pass():
    """A cell or net named like the deck's message, inside another log line, is no
    pass: only the deck's own whole line is."""
    forged = (
        "2026-10-12 04:17:43 +0200: Memory Usage (0K) : INFO : extracting cell "
        "x_Congratulations! Netlists match."
    )
    assert checks.lvs_outcome(forged) == "unknown"
    assert checks.lvs_outcome("Congratulations! Netlists match.") == "unknown"
    assert checks.lvs_outcome(PASS_LINE + " x") == "unknown"


def test_drc_reports_are_counted_by_rule(tmp_path):
    db = tmp_path / "a.lyrdb"
    db.write_text(
        "<report-database><items>"
        "<item><category>'M1.a'</category></item>"
        "<item><category>'M1.a'</category></item>"
        "<item><category>'Pad.d1R'</category></item>"
        "</items></report-database>"
    )
    r = checks.DrcResult()
    checks.read_lyrdb(db, r)
    assert r.violations == {"M1.a": 2, "Pad.d1R": 1} and r.total == 3


def test_supply_hookup_counts_top_vias_on_the_ring_rails():
    plan = floorplan.plan(chip(), CELLS)
    w = round(plan.die[0] * 1000)
    vdd_x = w - 70_000 - 149_000  # the middle of the east side's vdd rail
    vss_x = w - 70_000 - 169_000
    text = f"""VIAS 2 ;
    - via6_7_a + VIARULE viagen67 + LAYERS TopMetal1 TopVia2 TopMetal2 ;
    - via5_6_a + VIARULE viagen56 + LAYERS Metal5 TopVia1 TopMetal1 ;
END VIAS
SPECIALNETS 2 ;
- VDD ( * vdd )
  + ROUTED TopMetal2 3330 + SHAPE STRIPE ( 900000 500000 ) ( {w - 70_000} 500000 )
    NEW TopMetal1 0 + SHAPE STRIPE ( {vdd_x} 500000 ) via6_7_a
    NEW TopMetal1 0 + SHAPE STRIPE ( {vdd_x} 600000 ) via5_6_a ;
- VSS ( * vss )
  + ROUTED TopMetal1 0 + SHAPE STRIPE ( {vss_x} 520000 ) via6_7_a
    NEW TopMetal1 0 + SHAPE STRIPE ( {vdd_x} 540000 ) via6_7_a ;
END SPECIALNETS
"""
    got = checks.supply_hookup(text, plan)
    assert got == {"VDD": {"east": 1}, "VSS": {"east": 1}}


def test_precheck_verdicts():
    die = (0.0, 0.0, 1400.0, 1400.0)
    outline = (-60.0, -60.0, 1460.0, 1460.0)
    facts = {
        "top_cells": ["c"],
        "layers": [[8, 0], [189, 4]],
        "bad_cell_names": [],
        "zero_area_shapes": 0,
        "pr_boundary": [list(die)],
        "bbox_dbu": [-60000, -60000, 1460000, 1460000],
        "dbu_um": 0.001,
    }
    allowed = {(8, 0), (189, 4)}
    assert all(c["pass"] for c in checks.precheck(facts, "c", allowed, die, outline))
    bad = {
        **facts,
        "top_cells": ["c", "x"],
        "layers": [[8, 0], [255, 1]],
        "bad_cell_names": ["a#1"],
        "zero_area_shapes": 2,
        "pr_boundary": [list(outline)],
        "bbox_dbu": [-60001, -60000, 1460000, 1460000],
    }
    verdicts = {c["check"]: c["pass"] for c in checks.precheck(bad, "c", allowed, die, outline)}
    assert not any(verdicts.values()) and len(verdicts) == 6
    wide = {**facts, "bbox_dbu": [-70000, -60000, 1460000, 1460000]}
    verdicts = {c["check"]: c["pass"] for c in checks.precheck(wide, "c", allowed, die, outline)}
    assert verdicts["outline"] is False


def test_layers_come_from_the_layer_properties():
    text = "<properties><source>8/0</source></properties><properties><source>189/4@1</source>"
    assert checks.lyp_layers(text) == {(8, 0), (189, 4)}


def test_the_bond_map_lists_every_pad():
    plan = floorplan.plan(chip(), CELLS, directions={"a": "in"})
    text, rows = checks.bond_map(plan, 70.0)
    assert text.splitlines()[0] == "number,side,net,kind,cell,instance,x_um,y_um,size_um"
    assert rows[0]["number"] == 1 and rows[0]["side"] == "west" and rows[0]["net"] == "f"
    assert {r["kind"] for r in rows} >= {"signal", "vdd", "analog"}
    json.dumps(rows)


# The step


def test_the_step_is_skipped_without_a_chip_section_or_a_pdk(counter_project, monkeypatch):
    p = load_project(counter_project)
    assert assemble.chip(p).status == "skipped"
    p = load_project(counter_chip(counter_project))
    monkeypatch.setattr(io, "pdk_dir", lambda: None)
    r = assemble.chip(p)
    assert r.status == "skipped" and assemble.missing_pdk() == r.summary


def test_the_plan_needs_no_tool(counter_project, tmp_path, monkeypatch):
    monkeypatch.setenv(io.PDK_ENV, str(fake_pdk(tmp_path)))
    plan = assemble.plan_chip(load_project(counter_chip(counter_project)))
    assert plan["name"] == "counter_chip" and len(plan["pads"]) == 14
    assert plan["die_um"][0] == plan["die_um"][1]


# LibreLane's configuration


def flow_case():
    from chipleap.macro import layer

    cfg, b = bind({"clk": "a", "inp": "v", "out": "c"})
    plan = floorplan.plan(cfg, CELLS, {"u1": ("cmp", 20.0, 40.0)}, b.directions)
    p = FLOW_DIR / "macros/u1/views"
    v = MacroView(
        "cmp",
        "analog",
        20.0,
        40.0,
        CMP.pins,
        p / "cmp.lef",
        p / "cmp.gds",
        p / "cmp.lib",
        p / "cmp.spice",
        ("cmp",),
        ("cmp",),
        Report("x"),
    )
    imported = layer.Imported({"cmp": v}, {"u1": "cmp"})
    return cfg, plan, b, imported


def test_the_flow_places_the_ring_and_routes_around_it():
    from chipleap.chip import flow

    cfg, plan, b, imported = flow_case()
    c = flow.config(cfg, plan, b, imported, {"VERILOG_FILES": ["dir::src/core.v"]}, 10.0, FLOW_DIR)
    assert c["DESIGN_NAME"] == "c" and c["VERILOG_FILES"] == ["dir::src/c.v", "dir::src/core.v"]
    assert c["CLOCK_NET"] == "a_core" and c["CLOCK_PORT"] is None
    assert c["DIE_AREA"] == [0, 0, *plan.die] and c["CORE_AREA"] == list(plan.core)
    assert c["MACROS"]["cmp"]["instances"]["u1"]["orientation"] == "N"
    assert "dir::macros/u1/views/cmp.spice" in c["EXTRA_CDLS"]
    assert c["RUN_LVS"] is False and c["RT_MAX_LAYER"] == "Metal5"
    assert c["meta"]["substituting_steps"]["+OpenROAD.STAPostPNR"] == "OpenROAD.WriteCDL"
    obs = c["ROUTING_OBSTRUCTIONS"]
    assert len(obs) == 20 and obs[0] == ["Metal1", 70.0, 73.0, plan.die[0] - 70, 249.0]
    assert "KLAYOUT_DRC_THREADS" not in c


def test_a_value_tcl_could_read_as_syntax_is_refused():
    from chipleap.chip import flow
    from chipleap.verify.pnr import PnrError

    cfg, plan, b, imported = flow_case()
    with pytest.raises(PnrError, match="cannot go into a tool script"):
        flow.config(cfg, plan, b, imported, {"VERILOG_DEFINES": ["X=$(id)"]}, 10.0, FLOW_DIR)


def test_the_standard_cell_cdl_comes_from_librelanes_pdk(tmp_path):
    from chipleap.chip import flow
    from chipleap.verify.pnr import PnrError

    cdl = tmp_path / "pdk/ihp-sg13g2/libs.ref/sg13g2_stdcell/cdl/sg13g2_stdcell.cdl"
    cdl.parent.mkdir(parents=True)
    cdl.write_text("* cells\n")
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"CELL_CDLS": [str(cdl)]}))
    assert flow.std_cell_cdl(resolved) == cdl
    resolved.write_text(json.dumps({"CELL_CDLS": [str(tmp_path / "project/cells.cdl")]}))
    with pytest.raises(PnrError, match="no standard-cell CDL"):
        flow.std_cell_cdl(resolved)


# Running tools


def test_paths_for_ihps_runners_must_be_plain(tmp_path):
    from chipleap.chip import run

    assert (
        run.plain(Path("/opt/pdk/ihp-sg13g2/run_drc.py"), "x") == "/opt/pdk/ihp-sg13g2/run_drc.py"
    )
    for bad in ("/a b/run.py", "/a'b/run.py", "/a$(id)/run.py", "/a;b/run.py"):
        with pytest.raises(run.ToolError, match="cannot pass to its shell"):
            run.plain(Path(bad), "x")
    with run.private_dir("chipleap-test-") as d:
        assert run.PLAIN_PATH.fullmatch(str(d)) and d.is_dir()
    assert not d.exists()


# The step and its build directory


@pytest.fixture
def chip_tools(monkeypatch, tmp_path):
    from chipleap.chip import run
    from chipleap.verify.tools import Tool

    found = Tool("stand-in", "/bin/true", "1", "tests")
    monkeypatch.setenv(io.PDK_ENV, str(fake_pdk(tmp_path / "pdk")))
    for name in ("yosys", "librelane", "klayout", "openroad"):
        monkeypatch.setattr(assemble, name, lambda: found)
    monkeypatch.setattr(assemble, "python_with_klayout", lambda: ("/bin/true", "/nonexistent"))
    return run


def test_a_symlinked_build_directory_is_refused(counter_project, tmp_path, chip_tools):
    from chipleap.rtl.verify import run_step

    home = tmp_path / "home"
    (home / "chip").mkdir(parents=True)
    (home / "chip" / "keep.txt").write_text("precious\n")
    (counter_project / "build").symlink_to(home, target_is_directory=True)
    r = run_step(load_project(counter_chip(counter_project)), "chip")
    assert r.status == "fail" and "symlink" in r.summary, r.summary
    assert (home / "chip" / "keep.txt").read_text() == "precious\n"


def test_a_symlinked_chip_directory_is_refused(counter_project, tmp_path, chip_tools):
    from chipleap.rtl.verify import run_step

    home = tmp_path / "home"
    home.mkdir()
    (home / "keep.txt").write_text("precious\n")
    (counter_project / "build").mkdir()
    (counter_project / "build" / "chip").symlink_to(home, target_is_directory=True)
    r = run_step(load_project(counter_chip(counter_project)), "chip")
    assert r.status == "fail" and "build/chip is a symlink" in r.summary, r.summary
    assert (home / "keep.txt").read_text() == "precious\n"


def test_chipleap_chip_plan_prints_the_floorplan(counter_project, tmp_path, monkeypatch, capsys):
    from chipleap.cli import main

    monkeypatch.setenv(io.PDK_ENV, str(fake_pdk(tmp_path)))
    root = counter_chip(counter_project)
    assert main(["chip", "plan", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("counter_chip: die ") and "14 pads at a 100 um pitch" in out
    assert main(["chip", "plan", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pads"][0]["number"] == 1


# IHP's own IO library


@pytest.fixture
def ihp_lef() -> str:
    """The LEF of IHP's sg13g2_io: CHIPLEAP_IO_LEF, or the installed PDK's."""
    import os

    given = os.environ.get("CHIPLEAP_IO_LEF", "")
    if given:
        return Path(given).read_text()
    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("no IHP sg13g2_io LEF: set CHIPLEAP_IO_LEF or install IHP-Open-PDK")
    return io.lef_path(pdk).read_text()


def test_ihps_io_library_is_what_the_chip_expects(ihp_lef):
    cells = io.read_lef(ihp_lef)
    for name in (*io.PAD_CELLS.values(), io.CORNER, *(c for _, c in io.FILLERS)):
        assert name in cells, name
    assert (cells["sg13g2_IOPadIn"].width, cells["sg13g2_IOPadIn"].height) == (80.0, 180.0)
    assert cells["sg13g2_IOPadIn"].core_ports("p2c") and cells["sg13g2_IOPadOut4mA"].core_ports(
        "c2p"
    )
    chip_cells = io.read_lef(io.chip_lef(ihp_lef))
    assert len(chip_cells["sg13g2_IOPadAnalog"].pins["pad"]) == 1
    assert chip_cells["sg13g2_IOPadAnalog"].core_ports("pad")
    # The UART chip's ring: every cell of the library fits the plan.
    plan = floorplan.plan(
        load_project(ROOT / "examples" / "uart").chip, cells, {"comp": ("strongarm", 20.93, 45.0)}
    )
    assert plan.die == (1400.0, 1400.0) and len(plan.pads) == 33 and len(plan.ring) == 80


def test_the_reports_show_the_chip(counter_project):
    from chipleap.rtl.report import project_html, project_markdown
    from chipleap.verify.result import StepResult

    data = {
        "outline_um": [-60.0, -60.0, 1460.0, 1460.0],
        "pads": 33,
        "macros": [{"instance": "comp"}],
        "lvs": {"result": "pass"},
        "drc": {"violations": 2, "rules": {"M1.a": 2}},
        "route_drc_errors": 0,
        "supply_hookup": {"VDD": {"east": 8}, "VSS": {"east": 5}},
        "setup_slack_ns": 3.2,
        "hold_slack_ns": 0.1,
        "precheck": [{"check": "top cell", "pass": True, "detail": "top cells: c"}],
        "gds": "build/chip/c.gds",
    }
    r = StepResult("chip", "fail", "c: 1.52 x 1.52 mm; 2 DRC violations", data)
    p = load_project(counter_project)
    md = project_markdown(p, [r])
    assert "## Chip" in md and "| DRC violations (IHP deck) | 2 |" in md and "| M1.a | 2 |" in md
    assert "GDS: `build/chip/c.gds`" in md
    assert "<h2>Chip</h2>" in project_html(p, [r])


# Review findings


def test_the_chip_step_imports_packages_with_the_pdks_libraries(counter_project, tmp_path):
    """A hardened block's standard cells pass the chip's import only when the PDK's
    libraries show them to be the PDK's own; a PDK whose libraries cannot be read stops
    the step before any package is read."""
    import shutil

    from chipleap.macro import layer
    from chipleap.rtl import builddir

    fixtures = ROOT / "tests" / "fixtures" / "macro"
    pdk = tmp_path / "pdk" / "ihp-sg13g2"
    shutil.copytree(fixtures / "pdk" / "ihp-sg13g2", pdk)
    pkg = counter_project / "macros" / "tiny_counter.macro"
    shutil.copytree(fixtures / "broken" / "std-cell-identical" / "tiny_counter.macro", pkg)
    extra = "  macros: [{instance: cnt, package: macros/tiny_counter.macro}]\n"
    p = load_project(counter_chip(counter_project, extra))
    flow = builddir.work_dir(p.work_dir("chip", empty=True), "flow")
    imported = assemble.import_macros(p, pdk, "ihp-sg13g2", flow)
    assert imported.views["tiny_counter"].pdk_cells == ("sg13g2_inv_1", "sg13g2_buf_1")
    shutil.rmtree(pkg)
    shutil.copytree(fixtures / "broken" / "std-cell-modified" / "tiny_counter.macro", pkg)
    with pytest.raises(layer.MacroImportError, match="differs from the cell of that name"):
        assemble.import_macros(p, pdk, "ihp-sg13g2", flow)
    shutil.rmtree(pdk / "libs.ref")
    layer._pdk_libs.cache_clear()
    shutil.rmtree(flow)
    with pytest.raises(layer.MacroImportError, match=r"no libs\.ref"):
        assemble.import_macros(p, pdk, "ihp-sg13g2", flow)
    assert not (flow / "macros").exists()


def test_a_macro_bus_connects_end_to_end(tmp_path):
    """tiny_counter's two-bit output q, imported as a package, drives a two-bit core
    input: bit 0 to the port's least significant bit."""
    import shutil

    from chipleap.macro import layer
    from chipleap.rtl.project import _walk, read_inside

    root = tmp_path / "proj"
    (root / "build").mkdir(parents=True)
    pkg = root / "macros" / "tiny_counter.macro"
    shutil.copytree(ROOT / "tests/fixtures/macro/tiny_counter.macro", pkg)
    imported = layer.import_packages(
        [("cnt", pkg)],
        lambda d: [r for _, r in _walk(d, (root,))],
        lambda p: read_inside(p, (root,)),
        root / "build",
        "ihp-sg13g2",
        layer.Reserved(frozenset()),
        libs=PdkLibs(),
    )
    view = imported.views["tiny_counter"]
    assert [p.name for p in view.bus("q")] == ["q[0]", "q[1]"]
    port_map = {
        "a": Port("a", "input", 1),
        "d": Port("d", "input", 2),
        "e": Port("e", "input", 1),
        "y": Port("y", "output", 1),
    }
    pads = {
        "south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss")),
        "east": (Pad("signal", "e"), Pad("signal", "y")),
    }
    m = ChipMacro("cnt", pkg, connect={"clk": "a", "rst_n": "e", "en": "e", "q": "d"})
    cfg = ChipConfig("c", pads, (m,), 100.0, (400.0, 400.0))
    b = netlist.bind(cfg, port_map, imported.views, imported.cells, ("a",), "core")
    assert b.macros["cnt"]["q[0]"] == "d_0_core" and b.macros["cnt"]["q[1]"] == "d_1_core"
    plan = floorplan.plan(cfg, CELLS, {"cnt": ("tiny_counter", 20.0, 10.0)}, b.directions)
    v = netlist.chip_top(cfg, b, plan, "core", {})
    assert (
        "tiny_counter cnt (.clk(a_core), .en(e_core), .q({d_1_core, d_0_core}), .rst_n(e_core));"
        in v
    )
    # One bit of a bus, and a bus of the wrong width.
    one = ChipMacro("cnt", pkg, connect={"clk": "a", "rst_n": "e", "en": "e", "q[1]": "f"})
    b = netlist.bind(
        ChipConfig("c", pads, (one,), 100.0, (400.0, 400.0)),
        {"a": port_map["a"], "e": port_map["e"], "y": port_map["y"], "f": Port("f", "input", 1)},
        imported.views,
        imported.cells,
        ("a",),
    )
    assert b.macros["cnt"]["q[1]"] == "f_core" and b.macros["cnt"]["q[0]"] == "cnt_q_0"
    wide = ChipMacro("cnt", pkg, connect={"clk": "a", "rst_n": "e", "en": "e", "q": "a"})
    with pytest.raises(NetlistError, match="2 bits and core port a 1"):
        netlist.bind(
            ChipConfig("c", pads, (wide,), 100.0, (400.0, 400.0)),
            port_map,
            imported.views,
            imported.cells,
            ("a",),
        )


def test_bus_pins_are_accepted_in_the_chip_section(counter_project):
    extra = "  macros: [{instance: m, package: rtl, connect: {'q[3]': x, q: y}}]\n"
    p = load_project(counter_chip(counter_project, extra))
    assert p.chip.macros[0].connect == {"q[3]": "x", "q": "y"}
    with pytest.raises(ProjectError, match="is not a pin"):
        load_project(
            counter_chip(
                counter_project, "  macros: [{instance: m, package: rtl, connect: {'q[': x}}]\n"
            )
        )


def test_port_bits_that_flatten_to_one_name_are_refused():
    port_map = {"a": Port("a", "input", 2), "a_1": Port("a_1", "input", 1)}
    with pytest.raises(NetlistError, match="would share the net a_1_core"):
        bind(port_map=port_map, clocks=())


@pytest.mark.parametrize("name", ["VDD", "vss", "IOVdd"])
def test_a_supplys_name_is_no_signal_net(name):
    pads = {
        "south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss")),
        "west": (Pad("analog", name),),
    }
    with pytest.raises(NetlistError, match="is a supply's name"):
        bind(pads=pads, port_map=ports(a=("input", 1)))


def test_a_macro_cell_named_like_the_chip_or_its_core_is_refused():
    cfg, _ = bind({"clk": "a", "inp": "v", "out": "c"})
    with pytest.raises(NetlistError, match="name of the chip or of its core"):
        netlist.bind(cfg, PORTS, {"cmp": CMP}, {"u1": "cmp"}, ("a",), "cmp")


def test_a_pitch_of_part_of_a_micrometre_is_refused(counter_project):
    with pytest.raises(ProjectError, match="whole number of um"):
        load_project(counter_chip(counter_project, "  pad_pitch_um: 100.5\n"))
    with pytest.raises(FloorplanError, match="whole number of um"):
        floorplan.plan(chip(pitch=100.5), CELLS)


def _drc_dir(tmp_path, items: int, logs=("density", "sg13g2_maximal")):
    d = tmp_path / "drc"
    d.mkdir(parents=True)
    body = "".join("<item><category>'M1.a'</category></item>" for _ in range(items))
    (d / "chip_c_full.lyrdb").write_text(
        f"<report-database><items>{body}</items></report-database>"
    )
    for t in logs:
        (d / f"chip_c_{t}.log").write_text("ok\n")
    return d


def test_a_drc_run_must_finish_to_count(tmp_path):
    clean = assemble.drc_result(_drc_dir(tmp_path / "a", 0), "c", 0, "")
    assert clean.total == 0
    dirty = assemble.drc_result(_drc_dir(tmp_path / "b", 2), "c", 1, "")
    assert dirty.violations == {"M1.a": 2}
    from chipleap.chip.run import ToolError

    for case, code, items, logs in (
        ("crash", 1, 0, ("density", "sg13g2_maximal")),  # exit 1 and an empty report
        ("odd", 0, 3, ("density", "sg13g2_maximal")),  # exit 0 and violations
        ("signal", -9, 0, ("density", "sg13g2_maximal")),
        ("no-density", 0, 0, ("sg13g2_maximal",)),
    ):
        with pytest.raises(ToolError):
            assemble.drc_result(_drc_dir(tmp_path / case, items, logs), "c", code, "")
    empty = tmp_path / "none"
    empty.mkdir()
    with pytest.raises(ToolError, match="did not finish"):
        assemble.drc_result(empty, "c", 0, "")


def test_missing_metrics_are_no_pass():
    from chipleap.verify.pnr import PnrError

    good = {
        "route__drc_errors": 0,
        "timing__setup__ws": 1.0,
        "timing__hold__ws": 0.1,
        "antenna__violating__nets": 0,
    }
    assemble.check_metrics(good)
    for key in good:
        with pytest.raises(PnrError, match=key):
            assemble.check_metrics({k: v for k, v in good.items() if k != key})
    with pytest.raises(PnrError):
        assemble.check_metrics({**good, "route__drc_errors": "n/a"})


def test_antenna_violations_fail_the_chip():
    data = {
        "name": "c",
        "outline_um": [-60.0, -60.0, 1460.0, 1460.0],
        "pads": 4,
        "macros": [],
        "route_drc_errors": 0,
        "antenna_nets": 2,
        "lvs": {"result": "pass"},
        "drc": {"violations": 0, "rules": {}},
        "supply_hookup": {"VDD": {"east": 1}, "VSS": {"east": 1}},
        "precheck": [],
        "setup_slack_ns": 1.0,
        "hold_slack_ns": 0.1,
    }
    ok, summary = assemble.verdict(data)
    assert not ok and "2 nets with antenna violations" in summary
    ok, summary = assemble.verdict({**data, "antenna_nets": 0})
    assert ok and "0 macros" in summary
