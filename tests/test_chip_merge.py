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
    modules = merge.yosys_modules("3 modules:\n  $paramod\\fifo\\DEPTH=4\n  top\n  \\esc\n")
    assert modules == ["fifo", "top", "esc"]
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
        merge.check_modules(modules, "c", ["sg13g2_IOPadIn"], {"fifo": view("fifo")})
    merge.check_modules(modules, "c", ["sg13g2_IOPadIn"], {"strongarm": view("strongarm")})


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


# The predicted names


def test_a_pdk_library_too_large_to_read_is_an_error(tmp_path, monkeypatch):
    from chip_fakes import fake_pdk

    pdk = fake_pdk(tmp_path)
    # Read whole, the IO library's LEF reserves its cells.
    assert "sg13g2_IOPadIn" in layer.reserved_names(pdk)
    layer._pdk_names.cache_clear()
    monkeypatch.setattr(layer, "NAMES_FILE_MAX", 100)
    with pytest.raises(MacroImportError, match="larger than ChipLeap reads"):
        layer.reserved_names(pdk)
    layer._pdk_names.cache_clear()
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
