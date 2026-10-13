"""Macro packages in a chip: copied, checked, and converted into views ChipLeap writes, with
everything in a package treated as untrusted."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from chipleap.macro import PdkLibs, load_manifest, parse_gds
from chipleap.macro import layer as macros
from chipleap.macro.layer import MacroImportError, Reserved
from chipleap.macro.views import parse_liberty, spice_subckt
from chipleap.rtl.project import _walk, read_inside

ROOT = Path(__file__).resolve().parents[1]
STRONGARM = ROOT / "examples" / "uart" / "macros" / "strongarm.macro"
COUNTER = ROOT / "tests" / "fixtures" / "macro" / "tiny_counter.macro"
BROKEN = ROOT / "tests" / "fixtures" / "macro" / "broken"


def project_with(tmp_path: Path, package: Path, name: str | None = None) -> tuple[Path, Path]:
    root = tmp_path / "proj"
    dest = root / "macros" / (name or package.name)
    shutil.copytree(package, dest)
    (root / "build").mkdir()
    return root, dest


NO_NAMES = Reserved(frozenset(), ())


def generated(*extra: str) -> Reserved:
    """The names the flow itself uses, and ``extra``, without a PDK's."""
    names = frozenset(n.casefold() for n in (*macros.GENERATED, *extra))
    return Reserved(names, spaces=tuple(n.casefold() for n in extra))


def import_one(
    root: Path,
    package: Path,
    pdk: str = "ihp-sg13g2",
    reserved: Reserved | None = None,
    more: tuple[tuple[str, Path], ...] = (),
    libs: PdkLibs | None = None,
) -> macros.Imported:
    return macros.import_packages(
        [("m1", package), *more],
        lambda d: [real for _, real in _walk(d, (root,))],
        lambda p: read_inside(p, (root,)),
        root / "build",
        pdk,
        reserved if reserved is not None else generated(),
        libs=libs if libs is not None else PdkLibs(),
    )


# LEF


def test_the_lef_is_rebuilt_with_its_origin_at_zero():
    text = (STRONGARM / "strongarm.lef").read_text()
    lef, size, pins, origin = macros.rewrite_lef(text, "strongarm")
    assert origin == (10.95, 0.6) and size == (29.1, 40.8)
    assert "ORIGIN 0 0 ;" in lef and "FOREIGN strongarm 0 0 ;" in lef
    vss = next(p for p in pins if p.name == "vss")
    assert vss.shapes == [("Metal4", (0.0, 0.0, 0.3, 0.65))]
    assert "ANTENNAGATEAREA 1.04 ;" in lef and "OBS" in lef and "NOWIREEXTENSIONATPIN" not in lef
    _, _, pins2, origin2 = macros.rewrite_lef(lef, "strongarm")
    assert origin2 == (0.0, 0.0) and [p.shapes for p in pins2] == [p.shapes for p in pins]


LEF = """\
MACRO m
  CLASS BLOCK ;
  SIZE 10 BY 10 ;
  PIN a
    DIRECTION INPUT ;
    PORT
      LAYER Metal2 ;
        RECT 0 0 1 1 ;
    END
  END a
END m
"""


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("LAYER Metal2", "LAYER Metal9", "not one of IHP SG13G2's"),
        ("RECT 0 0 1 1 ;", "VIA 0 0 Via1_YX ;", "reads only LAYER, RECT and POLYGON"),
        ("CLASS BLOCK", "CLASS CORE", "not BLOCK"),
        ("  SIZE 10 BY 10 ;\n", "", "no SIZE"),
        ("RECT 0 0 1 1", "RECT 0 0 1", "malformed RECT"),
        ("RECT 0 0 1 1", "RECT 0 0 1 1e9", "out of range"),
        ("PIN a\n", "PIN a$b\n", "not a name or a bit"),
        ("END m\n", "", "does not end"),
    ],
)
def test_a_lef_outside_what_chipleap_reads_is_refused(old, new, message):
    with pytest.raises(MacroImportError, match=message):
        macros.rewrite_lef(LEF.replace(old, new), "m")


# GDS


def record(rtype: int, dtype: int, body: bytes = b"") -> bytes:
    return struct.pack(">HBB", 4 + len(body), rtype, dtype) + body


def name(text: str) -> bytes:
    b = text.encode()
    return b + b"\0" * (len(b) % 2)


def real8(v: float) -> bytes:
    # 1e-3 and 1e-9 in GDS's excess-64 base-16 form.
    exp = 64
    mant = v
    while mant >= 1:
        mant /= 16
        exp += 1
    while mant < 1 / 16:
        mant *= 16
        exp -= 1
    return bytes([exp]) + int(mant * (1 << 56)).to_bytes(7, "big")


def box(x0: int, y0: int, x1: int, y1: int) -> bytes:
    xy = struct.pack(">10i", x0, y0, x1, y0, x1, y1, x0, y1, x0, y0)
    return (
        record(0x08, 0)
        + record(0x0D, 2, struct.pack(">h", 8))
        + record(0x0E, 2, struct.pack(">h", 0))
        + record(0x10, 3, xy)
        + record(0x11, 0)
    )


def gds(cells: dict[str, bytes]) -> bytes:
    out = record(0x00, 2, struct.pack(">h", 600)) + record(0x01, 2, b"\0" * 24)
    out += record(0x02, 6, name("lib")) + record(0x03, 5, real8(1e-3) + real8(1e-9))
    for cell, body in cells.items():
        out += record(0x05, 2, b"\0" * 24) + record(0x06, 6, name(cell)) + body + record(0x07, 0)
    return out + record(0x04, 0) + b"\0" * 100  # padding after ENDLIB, as tools write


def test_the_gds_cell_moves_by_the_lef_origin_and_nothing_else_does():
    data = gds({"m": box(-8550, -600, 1000, 1000), "m_sub": box(0, 0, 10, 10)})
    assert macros.gds_cells(data) == ["m", "m_sub"]
    moved = parse_gds(macros.shift_gds(data, "m", 8.55, 0.6))
    assert moved.bbox("m") == pytest.approx((0.0, 0.0, 9.55, 1.6))
    assert moved.bbox("m_sub") == pytest.approx((0.0, 0.0, 0.01, 0.01))


def test_a_truncated_gds_is_refused():
    data = gds({"m": box(0, 0, 10, 10)})
    with pytest.raises(MacroImportError, match="runs past the end"):
        macros.shift_gds(data[:60], "m", 1.0, 1.0)


@pytest.mark.parametrize("cell", ["sg13g2_inv_1", "uart_chip", "other"])
def test_a_gds_cell_that_could_replace_a_chip_cell_is_refused(cell):
    with pytest.raises(MacroImportError, match="must be m or start with m_"):
        macros.check_gds_cells(["m", cell], "m")
    macros.check_gds_cells(["m", "m_unit"], "m")


# SPICE


def test_the_spice_view_keeps_devices_only():
    text = (
        "* a comment\n.include /etc/passwd\n.param w=1u\n"
        + (STRONGARM / "strongarm.spice").read_text()
        + ".end\n"
    )
    reduced, names = macros.reduce_spice(text, "strongarm")
    assert names == ["strongarm"] and ".include" not in reduced and ".param" not in reduced
    sub = spice_subckt(reduced, "strongarm")
    assert sub is not None and sub.ports == ["clk", "inm", "inp", "outn", "outp", "vdd", "vss"]
    assert sub.pininfo["clk"] == "I"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (".subckt m a\n.include x\n.ends\n", "only device lines"),
        (".subckt m a\nM1 a b c d nmos w=$(cat)\n.ends\n", "not a plain SPICE word"),
        (".subckt sg13g2_inv_1 a\n.ends\n", "must be m or start with m_"),
        (".subckt m a\nM1 a a a a nmos\n", "does not end"),
        (".subckt m_x a\n.ends\n", "no .subckt m"),
    ],
)
def test_a_spice_view_outside_what_chipleap_copies_is_refused(text, message):
    with pytest.raises(MacroImportError, match=message):
        macros.reduce_spice(text, "m")


# Liberty


def test_the_liberty_comes_from_the_manifest():
    m = load_manifest(COUNTER)
    text = macros.liberty("tiny_counter", m, 20.0, 10.0)
    cells = parse_liberty(text)
    pins = cells["tiny_counter"]
    assert pins["VDD"].pg and pins["VDD"].pg_type == "primary_power"
    assert pins["q[0]"].direction == "output" and pins["q[1]"].direction == "output"
    assert pins["clk"].direction == "input" and "clock : true" in text
    assert "input_threshold_pct_rise" in text


# Import


def test_a_package_is_copied_checked_and_converted(tmp_path):
    root, pkg = project_with(tmp_path, STRONGARM)
    imp = import_one(root, pkg, "ihp-sg13g2@22f2a25")
    v = imp.views["strongarm"]
    assert imp.cells == {"m1": "strongarm"}
    assert (v.width, v.height) == (29.1, 40.8) and v.warnings == []
    assert [p.name for p in v.pins if p.is_supply] == ["vdd", "vss"]
    assert v.lef.read_text().count("ORIGIN 0 0") == 1
    assert parse_gds(v.gds.read_bytes()).bbox("strongarm") == pytest.approx((0, 0, 29.1, 40.8))
    copy = root / "build" / "macros" / "m1" / "strongarm.macro"
    assert (copy / "macro.toml").is_file() and v.lef.parent.name == "views"


def test_another_pdk_commit_is_a_warning(tmp_path):
    root, pkg = project_with(tmp_path, STRONGARM)
    v = import_one(root, pkg, "ihp-sg13g2@cb7daaa").views["strongarm"]
    assert any("cb7daaa" in w for w in v.warnings)


def test_a_package_that_fails_its_checks_is_refused(tmp_path):
    root, pkg = project_with(tmp_path, BROKEN / "lef-missing-pin" / "tiny_ota.macro")
    with pytest.raises(MacroImportError, match="fails macro/v1's checks"):
        import_one(root, pkg)


def test_a_package_with_a_symlink_to_another_project_file_is_refused(tmp_path):
    root, pkg = project_with(tmp_path, STRONGARM)
    (root / "secret.txt").write_text("not for the chip\n")
    (pkg / "notes.txt").symlink_to(root / "secret.txt")
    with pytest.raises(MacroImportError, match="symlink"):
        import_one(root, pkg)


def test_a_symlink_out_of_the_project_is_never_read(tmp_path):
    root, pkg = project_with(tmp_path, STRONGARM)
    outside = tmp_path / "outside.txt"
    outside.write_text("private\n")
    (pkg / "evidence" / "extra.json").symlink_to(outside)
    import_one(root, pkg)
    copied = root / "build" / "macros" / "m1" / "strongarm.macro" / "evidence"
    assert not (copied / "extra.json").exists()


def test_a_macro_on_another_supply_is_refused(tmp_path):
    root, pkg = project_with(tmp_path, STRONGARM)
    text = (pkg / "macro.toml").read_text().replace("voltage = 1.2", "voltage = 3.3")
    (pkg / "macro.toml").write_text(text)
    with pytest.raises(MacroImportError, match=r"3\.3 V"):
        import_one(root, pkg)


# Names a package may not use


def _generator():
    import importlib.util

    path = ROOT / "tests" / "fixtures" / "macro" / "generate.py"
    spec = importlib.util.spec_from_file_location("macro_fixture_generator", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def counter_named(
    root: Path, name: str, extra_cells: tuple[str, ...] = (), std: bool = False
) -> Path:
    """The tiny_counter fixture under another name, valid for macro/v1 (its evidence
    hashes recomputed), with ``extra_cells`` defined in its GDS and placed in its top,
    and with ``std`` the stand-in PDK's buffer and inverter, as the PDK's."""
    gen = _generator()
    files = gen.counter()
    out: dict[str, bytes] = {}
    for rel, data in files.items():
        if rel.startswith("evidence/") or rel.endswith(".gds"):
            continue
        out[rel.replace("tiny_counter", name)] = data.replace(b"tiny_counter", name.encode())
    top = gen.Cell(name)
    top.rect(gen.PR_BOUNDARY, 0, 0, 20000, 10000)
    for pin, x0, y0 in (
        ("clk", 0, 2000),
        ("rst_n", 0, 3500),
        ("en", 0, 6500),
        ("q[0]", 19600, 3000),
        ("q[1]", 19600, 6000),
    ):
        top.rect(gen.METAL2, x0, y0, x0 + 400, y0 + 800).text(
            gen.METAL2_TEXT, x0 + 200, y0 + 400, pin
        )
    top.rect(gen.METAL1, 0, 9400, 20000, 10000).text(gen.METAL1_TEXT, 10000, 9700, "VDD")
    top.rect(gen.METAL1, 0, 0, 20000, 600).text(gen.METAL1_TEXT, 10000, 300, "VSS")
    cells = [top]
    for i, extra in enumerate(extra_cells):
        cells.append(gen.Cell(extra).rect(gen.METAL1, 0, 0, 100, 100))
        top.sref(extra, 1000 + 200 * i, 1000)
    if std:
        cells += [gen.std_inv(), gen.std_buf()]
        top.sref("sg13g2_buf_1", 15000, 3000)
    out[f"{name}.gds"] = gen.gds(name, cells)
    gen.with_evidence(out, name)
    dest = root / "macros" / f"{name}.macro"
    gen.write(dest, out)
    return dest


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "build").mkdir(parents=True)
    return root


def reserved_with(*names: str) -> Reserved:
    return generated(*names)


def test_a_package_named_like_the_pdk_cannot_replace_its_cells(proj):
    # IHP's own name spaces are refused by macro/v1 itself (pdk.cell_name).
    pkg = counter_named(proj, "sg13g2", extra_cells=("sg13g2_inv_1",))
    with pytest.raises(MacroImportError, match=r"pdk\.cell_name"):
        import_one(proj, pkg, reserved=NO_NAMES)
    # A cell of the PDK's libraries outside them (IHP's ntap pcell): left unchecked, the
    # package imports with a cell named like it, which would replace it when the layouts
    # merge; the chip's reserved names refuse it.
    pkg = counter_named(proj, "ntap")
    assert "ntap" in import_one(proj, pkg, reserved=NO_NAMES).views["ntap"].gds_cells
    with pytest.raises(MacroImportError, match="a name the PDK or ChipLeap uses"):
        import_one(proj, pkg, reserved=reserved_with("ntap"))


# A hardened block's standard cells

STAND_IN = ROOT / "tests" / "fixtures" / "macro" / "pdk" / "ihp-sg13g2"


def test_a_hardened_block_imports_with_the_pdks_own_standard_cells(proj):
    """Two blocks that hold the PDK's own inverter and buffer import, with the chip's
    reserved names in place; the chip's merge (chipleap.chip.merge) accepts their
    copies beside the standard cells, as identical."""
    from chipleap.chip.merge import gds_source, unique

    libs = macros.pdk_libs(STAND_IN)
    reserved = macros.reserved_names(STAND_IN, ("top",))
    assert "sg13g2_inv_1" in reserved
    a = counter_named(proj, "cnt_a", std=True)
    b = counter_named(proj, "cnt_b", std=True)
    imported = import_one(proj, a, reserved=reserved, libs=libs, more=(("m2", b),))
    for cell in ("cnt_a", "cnt_b"):
        view = imported.views[cell]
        assert view.pdk_cells == ("sg13g2_inv_1", "sg13g2_buf_1")
        assert set(view.gds_cells) == {cell, "sg13g2_inv_1", "sg13g2_buf_1"}
    std_gds = STAND_IN / "libs.ref" / "sg13g2_stdcell" / "gds" / "sg13g2_stdcell.gds"
    unique(
        [gds_source("the standard cells", std_gds)]
        + [gds_source(f"macro {c}", v.gds) for c, v in imported.views.items()],
        fold=False,
        what="GDS cell",
    )


def test_a_changed_or_unproven_standard_cell_is_refused_on_import(proj):
    """The same import refuses a changed copy of the PDK's inverter, as the merge would,
    and refuses the PDK's own copy without the PDK's libraries."""
    from chipleap.chip.merge import MergeError, gds_source, unique

    libs = macros.pdk_libs(STAND_IN)
    reserved = macros.reserved_names(STAND_IN, ("top",))
    root, pkg = project_with(
        proj.parent / "changed", BROKEN / "std-cell-modified" / "tiny_counter.macro"
    )
    with pytest.raises(MacroImportError, match="differs from the cell of that name"):
        import_one(root, pkg, reserved=reserved, libs=libs)
    std_gds = STAND_IN / "libs.ref" / "sg13g2_stdcell" / "gds" / "sg13g2_stdcell.gds"
    with pytest.raises(MergeError, match="GDS cell sg13g2_inv_1"):
        unique(
            [
                gds_source("the standard cells", std_gds),
                gds_source("macro", pkg / "tiny_counter.gds"),
            ],
            fold=False,
            what="GDS cell",
        )
    root, pkg = project_with(
        proj.parent / "plain", BROKEN / "std-cell-identical" / "tiny_counter.macro"
    )
    with pytest.raises(MacroImportError, match=r"pdk\.cell_name"):
        import_one(root, pkg, reserved=reserved, libs=PdkLibs())
    # A cell of the PDK's that is no standard cell still must carry the package's name.
    with pytest.raises(MacroImportError, match="must be tiny_counter or start with"):
        macros.check_gds_cells(["tiny_counter", "sg13g2_Corner"], "tiny_counter", ("sg13g2_inv_1",))


def test_a_package_whose_name_space_holds_a_reserved_name_is_refused(proj):
    pkg = counter_named(proj, "chipcell", extra_cells=("chipcell_x",))
    with pytest.raises(MacroImportError, match="would hold chipcell_x"):
        import_one(proj, pkg, reserved=Reserved(frozenset({"chipcell_x"}), ()))


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("Counter", "must be lowercase"),
        ("sg13g2x", "a name the PDK or ChipLeap uses"),
        ("rm_ihpsg13_ram", "a name the PDK or ChipLeap uses"),
        ("bondpad_big", "a name the PDK or ChipLeap uses"),
        ("via_stack", "a name the PDK or ChipLeap uses"),
        ("act", "would hold act_fill_cell"),
        ("uart_chip", "a name the PDK or ChipLeap uses"),
    ],
)
def test_reserved_package_names(name, message):
    with pytest.raises(MacroImportError, match=message):
        generated("uart_chip", "uart").check_package(name)
    generated("uart_chip", "uart").check_package("strongarm")


def test_the_pdks_cell_names_are_reserved(tmp_path):
    from chip_fakes import fake_pdk

    reserved = macros.reserved_names(fake_pdk(tmp_path), ("top",))
    assert "sg13g2_IOPadIn" in reserved and "SG13G2_IOPADIN" in reserved
    assert "top" in reserved and "bondpad" in reserved and "Met1_S_FILL_CELL" in reserved


def test_two_packages_whose_name_spaces_overlap_are_refused(proj):
    foo = counter_named(proj, "foo", extra_cells=("foo_bar",))
    foo_bar = counter_named(proj, "foo_bar")
    # Each alone imports, and both hold a cell named foo_bar: merged, one would replace
    # the other.
    assert "foo_bar" in import_one(proj, foo, reserved=NO_NAMES).views["foo"].gds_cells
    assert "foo_bar" in import_one(proj, foo_bar, reserved=NO_NAMES).views["foo_bar"].gds_cells
    with pytest.raises(MacroImportError, match="share a name space"):
        import_one(proj, foo, reserved=NO_NAMES, more=(("m2", foo_bar),))


def test_two_subcircuits_that_fold_to_one_name_are_refused():
    """SPICE folds case, so m_a and m_A are one subcircuit to LVS: which one it takes
    would depend on the tool."""
    text = ".subckt m x\n.ends\n.subckt m_a x\n.ends\n.subckt m_A x\n.ends\n"
    with pytest.raises(MacroImportError, match="defined twice"):
        macros.reduce_spice(text, "m")


def test_gds_names_are_checked_as_raw_bytes():
    gen = _generator()
    top = gen.Cell("m").rect(gen.METAL1, 0, 0, 10, 10)
    data = gen.gds("lib", [top])
    assert macros.gds_cells(data) == ["m"]
    # A name with a byte outside plain characters, two cells of one name, and a
    # reference to a cell the file does not hold.
    bad = data.replace(b"\x06\x06m\x00", b"\x06\x06\xff\x00")
    with pytest.raises(MacroImportError, match="not letters, digits"):
        macros.gds_cells(bad)
    twice = gen.gds("lib", [top, gen.Cell("m").rect(gen.METAL1, 0, 0, 20, 20)])
    with pytest.raises(MacroImportError, match="defined twice"):
        macros.gds_cells(twice)
    dangling = gen.gds("lib", [gen.Cell("m").sref("sg13g2_inv_1", 0, 0)])
    with pytest.raises(MacroImportError, match="referenced but not in the file"):
        macros.gds_cells(dangling)


def test_a_subcircuit_outside_the_name_space_as_written_is_refused():
    with pytest.raises(MacroImportError, match="as written"):
        macros.reduce_spice(".subckt M x\n.ends\n", "m")
    with pytest.raises(MacroImportError, match="as written"):
        macros.reduce_spice(".subckt m x\n.ends\n.subckt M_x y\n.ends\n", "m")


# Macros of the PDK


def test_an_sram_of_the_pdk_is_a_macro_view(tmp_path):
    from chip_fakes import fake_pdk, sram

    pdk = fake_pdk(tmp_path / "pdk")
    cell = sram(pdk)
    work = tmp_path / "build"
    work.mkdir()
    view = macros.pdk_macro(pdk, cell, work)
    assert view.source == "pdk" and not view.supply_hookup and view.kind == "memory"
    assert (view.width, view.height) == (100.0, 50.0)
    assert view.pin("VDD!") is not None and view.pin("VDD!").is_supply
    assert view.pin("A_DIN[0]").direction == "input"
    assert view.gds.read_bytes() and view.subckts == (cell,)
    from chipleap.chip import pdn
    from chipleap.chip.floorplan import Placed

    with pytest.raises(pdn.PdnError, match="macro grid"):
        pdn.hookups(Placed("ram", cell, 0.0, 0.0, "N", 100.0, 50.0), view)
    with pytest.raises(MacroImportError, match="not a macro library"):
        macros.pdk_macro(pdk, cell, work, library="sg13g2_stdcell")
    with pytest.raises(MacroImportError, match="no lef, gds, lib, spice view"):
        macros.pdk_macro(pdk, "RM_NONE", work)


def test_ihps_srams_are_macro_views(tmp_path):
    from chipleap.chip import io

    pdk = io.pdk_dir()
    if pdk is None or not (pdk / "libs.ref" / "sg13g2_sram").is_dir():
        pytest.skip("IHP-Open-PDK with sg13g2_sram is not installed")
    view = macros.pdk_macro(pdk, "RM_IHPSG13_1P_1024x16_c2_bm_bist", tmp_path)
    assert (view.width, view.height) == (236.8, 336.46)
    assert view.pin("VSS!").kind == "ground" and len(view.bus("A_DIN")) == 16


def test_librelane_gets_every_macro_with_its_instances(tmp_path):
    p = tmp_path / "flow" / "macros" / "u1" / "views"
    v = macros.MacroView(
        "cmp",
        "analog",
        20.0,
        40.0,
        (),
        p / "cmp.lef",
        p / "cmp.gds",
        p / "cmp.lib",
        p / "cmp.spice",
        ("cmp",),
        ("cmp",),
    )
    cfg, cdls = macros.librelane_config(
        {"cmp": v},
        [("u1", "cmp", 1.0, 2.0, "N"), ("u2", "cmp", 50.0, 2.0, "FN")],
        tmp_path / "flow",
    )
    assert cfg["cmp"]["lef"] == ["dir::macros/u1/views/cmp.lef"]
    assert cfg["cmp"]["instances"]["u2"] == {"location": [50.0, 2.0], "orientation": "FN"}
    assert cdls == ["dir::macros/u1/views/cmp.spice"]
    with pytest.raises(MacroImportError, match="not in the flow's directory"):
        macros.librelane_config({"cmp": v}, [("u1", "cmp", 0.0, 0.0, "N")], tmp_path / "other")
