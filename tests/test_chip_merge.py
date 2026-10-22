"""Names where the chip is merged: what LibreLane actually merged is checked, so a name two
sources define is refused even when the predicted names at import time miss it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from chipleap.chip import merge
from chipleap.chip.config import ChipConfig, ChipMacro, Pad
from chipleap.chip.merge import MergeError
from chipleap.chip.netlist import Port, bind, chip_top
from chipleap.macro import layer
from chipleap.macro.layer import MacroImportError, MacroPin, MacroView

ROOT = Path(__file__).resolve().parents[1]


def gen():
    path = ROOT / "tests" / "fixtures" / "macro" / "generate.py"
    spec = importlib.util.spec_from_file_location("macro_fixture_generator", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def view(cell: str) -> MacroView:
    p = Path("x")
    pins = (MacroPin("a", "input", "signal", (("Metal4", (0.0, 0.0, 0.3, 1.0)),)),)
    return MacroView(cell, "analog", 20.0, 40.0, pins, p, p, p, p, (cell,), (cell,))


# Verilog


def test_a_macro_named_like_a_module_of_the_core_is_refused():
    listed = "top\ntop/w\n$paramod\\fifo\\DEPTH=4\nfifo/q\n\\esc\nodd/name\nodd/name/w\n"
    modules = merge.listed_modules(listed)
    assert modules == ["top", "fifo", "esc"]
    # Left unchecked, the chip top instantiates fifo for the macro, and the core's RTL
    # defines a module fifo: synthesis binds the instance to the RTL, not the macro.
    pads = {"south": (Pad("signal", "a"), Pad("vdd"), Pad("vss"), Pad("iovdd"), Pad("iovss"))}
    cfg = ChipConfig("c", pads, (ChipMacro("m", Path("x"), connect={"a": "a"}),), 100.0, None)
    ports = {"a": Port("a", "input", 1)}
    b = bind(cfg, ports, {"fifo": view("fifo")}, {"m": "fifo"}, ("a",), "top")
    from chip_fakes import io_lef
    from chipleap.chip import floorplan, io

    plan = floorplan.plan(cfg, io.read_lef(io_lef()), {"m": ("fifo", 20.0, 40.0)}, b.directions)
    assert "  fifo m (.a(a_core));" in chip_top(cfg, b, plan, "top", {})
    with pytest.raises(MergeError, match="module fifo is defined by macro fifo"):
        merge.check_modules(modules, {"fifo": view("fifo")}, "c", ["sg13g2_IOPadIn"])
    merge.check_modules(modules, {"strongarm": view("strongarm")}, "c", ["sg13g2_IOPadIn"])
    # Without a chip top or IO cells, as an RTL project with macros checks its modules.
    with pytest.raises(MergeError, match="module fifo is defined by macro fifo"):
        merge.check_modules(modules, ["fifo"])
    merge.check_modules(modules, ["strongarm"])


def test_a_module_list_too_long_is_refused(monkeypatch):
    monkeypatch.setattr(merge, "MODULES_MAX", 3)
    assert merge.listed_modules("a\nb\nc\n") == ["a", "b", "c"]
    with pytest.raises(MergeError, match="more than 3 modules"):
        merge.listed_modules("a\nb\nc\nd\n")


# Layout and netlist


def flow_case(tmp_path: Path, macro_cell: str, gds_cells: list[str], vias: list[str]):
    """A PDK with a standard-cell library, a flow directory with one macro, LibreLane's
    resolved configuration naming both, and a DEF whose VIAS are ``vias``."""
    g = gen()
    pdk = tmp_path / "pdk"
    std = pdk / "ihp-sg13g2" / "libs.ref" / "sg13g2_stdcell"
    for sub in ("gds", "cdl", "lef"):
        (std / sub).mkdir(parents=True)
    (std / "gds" / "sg13g2_stdcell.gds").write_bytes(
        g.gds("std", [g.Cell("sg13g2_inv_1").rect(g.METAL1, 0, 0, 10, 10)])
    )
    (std / "cdl" / "sg13g2_stdcell.cdl").write_text(".SUBCKT sg13g2_inv_1 A Y VDD VSS\n.ENDS\n")
    (std / "lef" / "sg13g2_tech.lef").write_text("VIA Via1_YX DEFAULT\nEND Via1_YX\n")
    flow = tmp_path / "flow"
    views = flow / "macros" / "m" / "views"
    views.mkdir(parents=True)
    cells = [g.Cell(c).rect(g.METAL1, 0, 0, 30, 30) for c in gds_cells]
    (views / f"{macro_cell}.gds").write_bytes(g.gds(macro_cell, cells))
    (views / f"{macro_cell}.spice").write_text(f".subckt {macro_cell} a\n.ends\n")
    resolved = flow / "resolved.json"
    resolved.write_text(
        json.dumps(
            {
                "PDK_ROOT": str(pdk),
                "CELL_GDS": [str(std / "gds" / "sg13g2_stdcell.gds")],
                "CELL_CDLS": [str(std / "cdl" / "sg13g2_stdcell.cdl")],
                "TECH_LEFS": {"nom_*": str(std / "lef" / "sg13g2_tech.lef")},
                "EXTRA_GDS": [],
                "EXTRA_CDLS": [str(views / f"{macro_cell}.spice")],
                "MACROS": {
                    macro_cell: {
                        "gds": [str(views / f"{macro_cell}.gds")],
                        "spice": [str(views / f"{macro_cell}.spice")],
                    }
                },
            }
        )
    )
    def_text = "VIAS 1 ;\n" + "".join(f"    - {v} + VIARULE x ;\n" for v in vias) + "END VIAS\n"
    return resolved, flow, def_text


def test_what_librelane_merged_holds_each_name_once(tmp_path):
    resolved, flow, def_text = flow_case(tmp_path, "cmp", ["cmp", "cmp_unit"], ["via1_2_x"])
    counts = merge.check_merges(resolved, flow, "chip", def_text)
    assert counts["gds_cells"] >= 4 and counts["subcircuits"] == 3


def test_a_package_cell_named_like_a_via_of_the_routed_design_is_refused(tmp_path):
    cell = "via1_2_8000_440_1_19_410_410"
    resolved, flow, def_text = flow_case(tmp_path, cell, [cell], [cell])
    # Left unchecked, the package's GDS goes into the stream-out beside the via cell of
    # the same name, which the routed design's DEF defines.
    data = json.loads(resolved.read_text())
    assert data["MACROS"][cell]["gds"][0].endswith(f"{cell}.gds") and cell in def_text
    with pytest.raises(MergeError, match=f"GDS cell {cell} is defined by the stream-out"):
        merge.check_merges(resolved, flow, "chip", def_text)
    # The early check refuses the name as well.
    with pytest.raises(MacroImportError, match="a name the PDK or ChipLeap uses"):
        layer.Reserved(frozenset()).check_package("via1_2")


def test_a_package_cell_named_like_a_standard_cell_is_refused_at_the_merge(tmp_path):
    resolved, flow, def_text = flow_case(tmp_path, "cmp", ["cmp", "sg13g2_inv_1"], [])
    with pytest.raises(MergeError, match="GDS cell sg13g2_inv_1"):
        merge.check_merges(resolved, flow, "chip", def_text)


def test_a_subcircuit_folded_onto_a_standard_cells_is_refused(tmp_path):
    resolved, flow, def_text = flow_case(tmp_path, "cmp", ["cmp"], [])
    (flow / "macros/m/views/cmp.spice").write_text(
        ".subckt cmp a\n.ends\n.subckt SG13G2_INV_1 a\n.ends\n"
    )
    with pytest.raises(MergeError, match="subcircuit SG13G2_INV_1"):
        merge.check_merges(resolved, flow, "chip", def_text)


def test_only_the_pdks_and_chipleaps_files_are_merged(tmp_path):
    resolved, flow, def_text = flow_case(tmp_path, "cmp", ["cmp"], [])
    data = json.loads(resolved.read_text())
    stray = tmp_path / "stray.gds"
    stray.write_bytes((flow / "macros/m/views/cmp.gds").read_bytes())
    resolved.write_text(json.dumps({**data, "EXTRA_GDS": [str(stray)]}))
    with pytest.raises(MergeError, match="ChipLeap did not put there"):
        merge.check_merges(resolved, flow, "chip", def_text)
    resolved.write_text(json.dumps({**data, "CELL_GDS": [str(stray)]}))
    with pytest.raises(MergeError, match="outside its PDK"):
        merge.check_merges(resolved, flow, "chip", def_text)


def test_the_same_file_or_the_same_content_is_one_definition():
    a = merge.Source("a", {"x": "d1"}, Path("/pdk/a.gds"))
    merge.unique([a, merge.Source("b", {"x": "d2"}, Path("/pdk/a.gds"))], False, "cell")
    merge.unique([a, merge.Source("b", {"x": "d1"}, Path("/other.gds"))], False, "cell")
    with pytest.raises(MergeError):
        merge.unique([a, merge.Source("b", {"x": "d2"}, Path("/other.gds"))], False, "cell")
    with pytest.raises(MergeError):
        merge.unique([a, merge.Source("b", {"X": ""})], True, "subcircuit")


def test_gds_names_carry_their_contents():
    g = gen()
    one = g.gds("l", [g.Cell("c").rect(g.METAL1, 0, 0, 10, 10)])
    two = g.gds("l", [g.Cell("c").rect(g.METAL1, 0, 0, 20, 10)])
    assert merge.gds_names(one)["c"] != merge.gds_names(two)["c"]
    assert merge.gds_names(one) == merge.gds_names(
        g.gds("other", [g.Cell("c").rect(g.METAL1, 0, 0, 10, 10)])
    )


def shared_cell(g, via: str, *, reorder: bool = False, presentation: bool = False):
    """A library cell ``shared`` as two macros' generators write it: a rectangle, a
    label and a via placed twice, the via's structure named ``via``. ``reorder`` writes
    the elements in another order and the rectangle from another vertex the other way
    round; ``presentation`` gives the label a font and justification."""
    import struct

    rect = (
        g._rec(0x08, 0x00)
        + g._rec(0x0D, 0x02, g._i16(8))
        + g._rec(0x0E, 0x02, g._i16(0))
        + g._rec(
            0x10,
            0x03,
            g._i32(10, 0, 10, 20, 0, 20, 0, 0, 10, 0)
            if reorder
            else g._i32(0, 0, 10, 0, 10, 20, 0, 20, 0, 0),
        )
        + g._rec(0x11, 0x00)
    )
    label = (
        g._rec(0x0C, 0x00)
        + g._rec(0x0D, 0x02, g._i16(63))
        + g._rec(0x16, 0x02, g._i16(0))
        + (g._rec(0x17, 0x01, struct.pack(">H", 5)) if presentation else b"")
        + g._rec(0x10, 0x03, g._i32(5, 5))
        + g._rec(0x19, 0x06, g._str("pmos"))
        + g._rec(0x11, 0x00)
    )
    cell = g.Cell("shared")
    refs = g.Cell("_").sref(via, 2, 2).sref(via, 8, 18, angle=90).body
    cell.body = refs + label + rect if reorder else rect + label + refs
    return [g.Cell(via).rect(g.METAL1, 0, 0, 4, 4).rect(g.METAL2, 0, 0, 4, 4), cell]


def test_one_layout_written_two_ways_is_one_definition():
    """IHP's SRAM macros each hold the same library cells, written in another order, with
    labels in another font and vias their generator named per macro: one layout, which
    either macro's copy gives the chip."""
    g = gen()
    a = merge.gds_names(g.gds("a", shared_cell(g, "via_CDNS_755")))
    b = merge.gds_names(g.gds("b", shared_cell(g, "via_CDNS_686", reorder=True, presentation=True)))
    assert a["shared"] == b["shared"] and a["via_CDNS_755"] == b["via_CDNS_686"]
    sources = [merge.Source("macro package a", a), merge.Source("macro package b", b)]
    merge.unique(sources, False, "GDS cell")
    # Not the same records: macro/v1's identity, which a package's copy of a PDK cell
    # must match, tells them apart.
    from chipleap.macro.gds import structure_identities

    raw_a = dict(structure_identities(g.gds("a", shared_cell(g, "via_CDNS_755"))))
    raw_b = dict(structure_identities(g.gds("b", shared_cell(g, "via_CDNS_686"))))
    assert raw_a[b"shared"] != raw_b[b"shared"]


def test_the_same_name_over_another_layout_is_refused():
    g = gen()
    a = merge.gds_names(g.gds("a", shared_cell(g, "via_x")))
    # The via under the cell differs: the cell differs, whatever the via's name.
    other = shared_cell(g, "via_y")
    other[0] = g.Cell("via_y").rect(g.METAL1, 0, 0, 4, 4).rect(g.METAL2, 0, 0, 4, 5)
    b = merge.gds_names(g.gds("b", other))
    assert a["shared"] != b["shared"]
    with pytest.raises(MergeError, match="GDS cell shared is defined by macro package b"):
        merge.unique(
            [merge.Source("macro package a", a), merge.Source("macro package b", b)],
            False,
            "GDS cell",
        )
    # So does a label, a layer or the place of a via.
    for change in (
        lambda c: c.text(g.METAL1_TEXT, 1, 1, "x"),
        lambda c: c.rect((9, 0), 0, 0, 10, 20),
        lambda c: c.sref("via_x", 3, 2),
    ):
        cells = shared_cell(g, "via_x")
        change(cells[1])
        assert merge.gds_names(g.gds("c", cells))["shared"] != a["shared"]


def test_two_layouts_whose_parts_run_together_are_told_apart():
    """Each part of a layout's identity is hashed with its kind and length. Without that,
    these two cells hash the same bytes: the first's outline has a fifth vertex whose
    coordinates are the bytes of a property record that the second has after its outline."""
    import struct

    g = gen()
    prop = g._rec(0x2C, 0x06, b"abc\n")
    fifth = struct.unpack(">ii", b"\n" + prop[:-1])

    def boundary(xy: tuple[int, ...], after_xy: bytes = b"") -> bytes:
        return (
            g._rec(0x08, 0x00)
            + g._rec(0x0D, 0x02, g._i16(8))
            + g._rec(0x0E, 0x02, g._i16(0))
            + g._rec(0x10, 0x03, g._i32(*xy))
            + after_xy
            + g._rec(0x11, 0x00)
        )

    five = boundary((0, 0, 0, 10, 10, 10, 10, 0, *fifth, 0, 0))
    four = boundary((0, 0, 0, 10, 10, 10, 10, 0, 0, 0), after_xy=prop)

    def run_together(element: bytes) -> bytes:
        """The element's parts one after the other, as ChipLeap hashed them before."""
        out, pos = b"", 0
        while pos < len(element):
            r = element[pos : pos + struct.unpack(">H", element[pos : pos + 2])[0]]
            pos += len(r)
            out += b"OUTLINE " + merge._outline(r) + b"\n" if r[2] == 0x10 else r
        return out

    assert five != four and run_together(five) == run_together(four)
    ids = []
    for body in (five, four):
        cell = g.Cell("c")
        cell.body = body
        ids.append(merge.gds_names(g.gds("l", [cell]))["c"])
    assert ids[0] != ids[1]


def test_a_record_gdsii_does_not_have_is_refused():
    g = gen()
    cell = g.Cell("c").rect(g.METAL1, 0, 0, 1, 1)
    cell.body += g._rec(0x3C, 0x00)
    with pytest.raises(MergeError, match="unknown record type 0x3c"):
        merge.gds_names(g.gds("l", [cell]))


def test_a_structure_that_contains_itself_is_an_error():
    g = gen()
    data = g.gds("l", [g.Cell("a").sref("b", 0, 0), g.Cell("b").sref("a", 0, 0)])
    with pytest.raises(MergeError, match="contains itself"):
        merge.gds_names(data)
    twice = [g.Cell("a").rect(g.METAL1, 0, 0, 1, 1), g.Cell("a").rect(g.METAL1, 0, 0, 2, 1)]
    with pytest.raises(MergeError, match="defined twice in one file"):
        merge.gds_names(g.gds("l", twice))


def macro_view(tmp_path: Path, cell: str, cells, subckts: str) -> MacroView:
    g = gen()
    d = tmp_path / cell
    d.mkdir()
    (d / f"{cell}.gds").write_bytes(g.gds(cell, [*cells, g.Cell(cell).sref(cells[-1].name, 0, 0)]))
    (d / f"{cell}.cdl").write_text(subckts)
    v = view(cell)
    return MacroView(**{**v.__dict__, "gds": d / f"{cell}.gds", "spice": d / f"{cell}.cdl"})


def test_the_macros_are_checked_before_librelane_runs(tmp_path):
    """Two macros with one cell of the same name over another layout stop the flow before
    LibreLane runs, not after its layout; the same layout passes."""
    g = gen()
    sub = ".SUBCKT shared a\n.ENDS\n"
    one = macro_view(tmp_path, "ram_a", shared_cell(g, "via_1"), sub)
    same = macro_view(tmp_path, "ram_b", shared_cell(g, "via_2", reorder=True), sub)
    merge.check_macros([one, same, one])
    other = shared_cell(g, "via_3")
    other[1].rect(g.METAL2, 0, 0, 1, 1)
    differs = macro_view(tmp_path, "ram_c", other, sub)
    with pytest.raises(MergeError, match="GDS cell shared is defined by macro package ram_c"):
        merge.check_macros([one, differs])
    netlist = macro_view(tmp_path, "ram_d", shared_cell(g, "via_4"), ".SUBCKT SHARED a b\n.ENDS\n")
    with pytest.raises(MergeError, match="subcircuit SHARED is defined by macro package ram_d"):
        merge.check_macros([one, netlist])


def test_ihps_srams_merge_where_their_cells_are_the_same():
    """On IHP's own macros: the SoC's 1024 x 32 and 256 x 64 share 72 cells, all the same
    layout; the 512 x 32 has its own RSC_IHPSG13_CDLYX1 (its outline on another layer), so
    it cannot go into a chip with any other of them."""
    from chipleap.chip import io

    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("no IHP PDK")
    ref = pdk / "libs.ref" / "sg13g2_sram"

    def macro(cell: str) -> MacroView:
        gds, cdl = ref / "gds" / f"{cell}.gds", ref / "cdl" / f"{cell}.cdl"
        return MacroView(**{**view(cell).__dict__, "gds": gds, "spice": cdl})

    big, buf = macro("RM_IHPSG13_1P_1024x32_c2_bm_bist"), macro("RM_IHPSG13_1P_256x64_c2_bm_bist")
    shared = set(merge.gds_names(big.gds.read_bytes())) & set(merge.gds_names(buf.gds.read_bytes()))
    assert "RM_IHPSG13_1P_BITKIT_CELL_SUB" in shared and len(shared) == 72
    merge.check_macros([big, buf])
    with pytest.raises(MergeError, match="RM_IHPSG13_1P_512x32_c2_bm_bist"):
        merge.check_macros([big, macro("RM_IHPSG13_1P_512x32_c2_bm_bist")])


# The predicted names


def test_a_pdk_library_too_large_to_read_is_an_error(tmp_path, monkeypatch):
    from chip_fakes import fake_pdk

    pdk = fake_pdk(tmp_path)
    # Read whole, the IO library's LEF reserves its cells.
    assert "sg13g2_IOPadIn" in layer.reserved_names(pdk)
    layer._pdk_libs.cache_clear()
    monkeypatch.setattr(layer, "NAMES_FILE_MAX", 100)
    with pytest.raises(MacroImportError, match="larger than ChipLeap reads"):
        layer.reserved_names(pdk)
    layer._pdk_libs.cache_clear()
    with pytest.raises(MacroImportError, match=r"no libs\.ref"):
        layer.reserved_names(tmp_path / "nowhere")


def test_the_chips_and_cores_name_spaces_are_closed_to_packages(tmp_path):
    from chip_fakes import fake_pdk

    reserved = layer.reserved_names(fake_pdk(tmp_path), ("uart", "uart_chip"))
    for name in ("uart_x", "uart_chip_ram", "uart"):
        with pytest.raises(MacroImportError):
            reserved.check_package(name)
    reserved.check_package("strongarm")


def test_a_package_is_always_a_package(tmp_path):
    """Nothing in a package or the chip section makes a package a PDK macro: its view
    is a package's, with its supply pins strapped and every check run."""
    import shutil

    from chipleap.rtl.project import _walk, read_inside

    root = tmp_path / "proj"
    (root / "build").mkdir(parents=True)
    pkg = root / "macros" / "strongarm.macro"
    shutil.copytree(ROOT / "examples/uart/macros/strongarm.macro", pkg)
    text = (pkg / "macro.toml").read_text()
    (pkg / "macro.toml").write_text(
        text + '\n[extra.chipleap]\nsource = "pdk"\nsupply_hookup = false\n'
    )
    v = layer.import_packages(
        [("m", pkg)],
        lambda d: [r for _, r in _walk(d, (root,))],
        lambda p: read_inside(p, (root,)),
        root / "build",
        "ihp-sg13g2",
        layer.Reserved(frozenset()),
        libs=layer.PdkLibs(),
    ).views["strongarm"]
    assert v.source == "package" and v.supply_hookup and v.report is not None
    from chipleap.chip.config import ChipMacro as M
    from chipleap.rtl.project import _parse_chip  # noqa: F401  (packages are paths, nothing else)

    assert {f.name for f in M.__dataclass_fields__.values()} == {
        "instance",
        "package",
        "orientation",
        "at_um",
        "connect",
    }
