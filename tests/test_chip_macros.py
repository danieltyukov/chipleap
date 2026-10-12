"""Macro packages in a chip: copied, checked, and converted into views ChipLeap writes, with
everything in a package treated as untrusted."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from chipleap.chip import macros
from chipleap.chip.config import ChipMacro
from chipleap.chip.macros import MacroImportError
from chipleap.macro import load_manifest, parse_gds
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


def import_one(root: Path, package: Path, pdk: str = "ihp-sg13g2") -> macros.Imported:
    return macros.import_macros(
        [ChipMacro("m1", package)],
        lambda d: [real for _, real in _walk(d, (root,))],
        lambda p: read_inside(p, (root,)),
        root / "build",
        pdk,
    )


# LEF


def test_the_lef_is_rebuilt_with_its_origin_at_zero():
    text = (STRONGARM / "strongarm.lef").read_text()
    lef, size, pins, origin = macros.rewrite_lef(text, "strongarm")
    assert origin == (8.55, 0.6) and size == (20.93, 45.0)
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
    assert (v.width, v.height) == (20.93, 45.0) and v.warnings == []
    assert [p.name for p in v.pins if p.is_supply] == ["vdd", "vss"]
    assert v.lef.read_text().count("ORIGIN 0 0") == 1
    assert parse_gds(v.gds.read_bytes()).bbox("strongarm") == pytest.approx((0, 0, 20.93, 45.0))
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
