"""Macro packages from someone else: every limit ends in a finding, quickly.

aispice runs the same cases in crates/aispice-core/tests/macro_limits.rs. Files built
to be large are made here, never committed; most limits are lowered so small files
reach them.
"""

import contextlib
import dataclasses
import importlib.util
import os
import random
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest

from chipleap.macro import check_package, parse_gds
from chipleap.macro.gds import GdsCycleError, GdsError
from chipleap.macro.limits import LIMITS, LimitError, Limits, json_depth, toml_depth
from chipleap.macro.manifest import parse_toml
from chipleap.macro.views import (
    ViewError,
    parse_lef,
    parse_liberty,
    spice_subckt,
    verilog_ports,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro"
_spec = importlib.util.spec_from_file_location("macro_fixtures", FIXTURES / "generate.py")
assert _spec is not None and _spec.loader is not None
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)
Cell, METAL1 = gen.Cell, gen.METAL1

SMALL = dataclasses.replace(LIMITS, tokens=50, pins=3, nesting=4)


def _gds(*cells: "gen.Cell") -> bytes:
    return gen.gds("t", list(cells))


def _copy(tmp_path: Path, name: str = "tiny_counter.macro") -> Path:
    pkg = tmp_path / name
    shutil.copytree(FIXTURES / name, pkg)
    return pkg


def _findings(report) -> list[tuple[str, str, str]]:
    return [(f.rule, f.file, f.message) for f in report.findings]


# Nesting, measured before parsing


def test_toml_depth():
    # The same table as aispice's toml_depth_bounds_every_way_to_nest.
    cases = {
        "": 0,
        "a = 1\n": 1,
        "a = 1.5\n": 2,
        "a.b.c = 1\n": 3,
        "a = [[1], [2]]\n": 3,
        "a = { b = { c = 1 } }\n": 3,
        "[a.b]\nc = 1\n": 3,
        "[[a]]\nb = 1\n": 3,
        "[[a.b]]\nc.d = 1\n": 5,
        'a = "[[[["\n': 1,
        "a = '''\n[[[[\n'''\n": 1,
        'a = """x"""" , [[1]]\n': 3,
        'a = "\\"[[" # [[[[\n': 1,
        "  [a]\n": 1,
        "a = [\n[1]]\n": 3,
        "a.b.c = [[1, {x = [2]}]]\n": 7,
        "[a.b.c]\nd.e = { f = [[1]] }\n": 8,
    }
    for text, want in cases.items():
        assert toml_depth(text) == want, text


def test_toml_depth_bounds_what_parsers_build():
    # Whatever the depth says, the parsed document nests no deeper.
    def depth(v, d=0):
        if isinstance(v, dict):
            return max((depth(x, d + 1) for x in v.values()), default=d)
        if isinstance(v, list):
            return max((depth(x, d + 1) for x in v), default=d)
        return d

    for text in (
        "a.b.c = [[1, {x = [2]}]]\n",
        "[a.b.c]\nd.e = { f = [[1]] }\n",
        "[[a.b]]\nc = 1\n[[a.b]]\nd.e.f = 2\n",
        'a = """]]]"""\nb = [[[3]]]\n',
    ):
        assert depth(parse_toml(text)) <= toml_depth(text), text


def test_json_depth_skips_strings():
    assert json_depth('{"a": [1, {"b": "[[[["}]}') == 3
    assert json_depth('["\\"]]]", [[]]]') == 3
    assert json_depth("]]][[") == 2
    assert json_depth('"abc\\') == 0


def test_numbers_read_as_aispice_reads_them():
    with pytest.raises(ValueError, match="64 bits"):
        parse_toml("x = 99999999999999999999\n")
    with pytest.raises(ValueError, match="overflowed"):
        parse_toml("x = 1e400\n")
    assert parse_toml("x = inf\ny = 9223372036854775807\n") == {
        "x": float("inf"),
        "y": 9223372036854775807,
    }


# GDS hierarchies


def test_gds_cycles_are_errors():
    with pytest.raises(GdsCycleError, match=r'^cell "a" contains itself: "a" -> "a"$'):
        parse_gds(_gds(Cell("top").sref("a", 0, 0), Cell("a").sref("a", 0, 0)))
    two = _gds(Cell("top").sref("a", 0, 0), Cell("a").sref("b", 0, 0), Cell("b").sref("a", 0, 0))
    with pytest.raises(GdsCycleError, match=r'^cell "a" contains itself: "a" -> "b" -> "a"$'):
        parse_gds(two)
    with pytest.raises(GdsCycleError, match="contains itself"):
        parse_gds((FIXTURES / "gds" / "loop.gds").read_bytes())


def test_gds_huge_array_is_measured_from_its_corners():
    start = time.monotonic()
    lib = parse_gds(
        _gds(Cell("top").aref("u", 32767, 32767, 0, 0, 1, 1), Cell("u").rect(METAL1, 0, 0, 10, 10))
    )
    assert lib.bbox("top") == pytest.approx((0, 0, 32.776, 32.776))
    assert time.monotonic() - start < 5


def test_gds_shared_cells_are_measured_once():
    # 40 levels, each placing the next twice: 2**40 paths, 41 cells.
    for angle in (0, 45):
        cells = [
            Cell(f"c{i}").sref(f"c{i + 1}", 0, 0, angle=angle).sref(f"c{i + 1}", 1, 1)
            for i in range(40)
        ]
        lib = parse_gds(_gds(*cells, Cell("c40").rect(METAL1, 0, 0, 1, 1)))
        start = time.monotonic()
        assert lib.bbox("c0") is not None
        assert time.monotonic() - start < 5


def test_gds_reference_depth():
    def chain(n: int) -> bytes:
        cells = [Cell(f"d{i}").sref(f"d{i + 1}", 0, 0) for i in range(n)]
        return _gds(*cells, Cell(f"d{n}").rect(METAL1, 0, 0, 1, 1))

    assert parse_gds(chain(64)).bbox("d0") == pytest.approx((0, 0, 0.001, 0.001))
    with pytest.raises(LimitError, match=r'^references nest more than 64 deep below cell "d0"$'):
        parse_gds(chain(65))


def test_gds_overflowing_magnification():
    def mag(child: str) -> bytes:
        return (
            gen._rec(0x0A, 0x00)
            + gen._rec(0x12, 0x06, gen._str(child))
            + gen._rec(0x1A, 0x01, struct.pack(">H", 0))
            + gen._rec(0x1B, 0x05, gen._real8(1e70))
            + gen._rec(0x10, 0x03, gen._i32(1, 1))
            + gen._rec(0x11, 0x00)
        )

    cells = []
    for i in range(6):
        c = Cell(f"m{i}")
        c.body += mag(f"m{i + 1}")
        cells.append(c)
    lib = parse_gds(_gds(*cells, Cell("m6").rect(METAL1, 0, 0, 1, 1)))
    with pytest.raises(LimitError, match="out of range"):
        lib.bbox("m0")


def test_gds_counts():
    good = (FIXTURES / "tiny_counter.macro" / "tiny_counter.gds").read_bytes()
    for field, message in (
        ("gds_records", "the GDS has more than 10 records"),
        ("gds_elements", "the GDS has more than 10 elements"),
        ("gds_cells", "the GDS has more than 10 cells"),
    ):
        many = _gds(*[Cell(f"c{i}").rect(METAL1, 0, 0, 1, 1) for i in range(11)])
        with pytest.raises(LimitError, match=f"^{message}$"):
            parse_gds(many, dataclasses.replace(LIMITS, **{field: 10}))
    rotated = _gds(Cell("top").sref("u", 0, 0, angle=45), Cell("u").rect(METAL1, 0, 0, 1, 1))
    with pytest.raises(LimitError, match="more than 4 points to measure"):
        parse_gds(rotated, dataclasses.replace(LIMITS, gds_points=4)).bbox("top")
    assert parse_gds(good).bbox("tiny_counter") is not None


# Text views


def test_truncated_views_are_errors():
    with pytest.raises(ViewError, match="end of file"):
        parse_lef("MACRO m\n PIN a\n DIRECTION INPUT")
    with pytest.raises(ViewError, match="end of file"):
        parse_lef("MACRO m\n OBS\n LAYER M1")
    with pytest.raises(ViewError, match="end of file inside a group"):
        parse_liberty("library (l) { cell (m) { pin (a) { direction : input ;")
    with pytest.raises(ViewError, match="unterminated string"):
        parse_liberty('library (l) { cell (m) { pin (a) { direction : "input')
    assert [p.name for p in verilog_ports("module m (input a, output", "m") or []] == ["a"]
    sub = spice_subckt(".subckt m a b\n+ c", "m")
    assert sub is not None and sub.ports == ["a", "b", "c"]


def test_view_limits():
    many = " ".join(["x"] * 51)
    with pytest.raises(LimitError, match=r"^the LEF has more than 50 tokens$"):
        parse_lef(many, SMALL)
    with pytest.raises(LimitError, match=r'^MACRO "m" has more than 3 pins$'):
        parse_lef("MACRO m\n" + "PIN a\nEND a\n" * 4 + "END m\n", SMALL)
    with pytest.raises(LimitError, match=r"^the liberty file has more than 50 tokens$"):
        parse_liberty(";" * 51, SMALL)
    with pytest.raises(LimitError, match=r"^groups nest more than 4 deep$"):
        parse_liberty("library (l) {" + "g () {" * 4 + "}" * 5, SMALL)
    with pytest.raises(LimitError, match=r'^cell "m" has more than 3 pins$'):
        parse_liberty("library (l) { cell (m) { pin (a, b, c, d) { } } }", SMALL)
    huge_bus = (
        "library (l) { type (t) { bit_from : 0 ; bit_to : 4611686018427387904 ; } "
        "cell (m) { bus (d) { bus_type : t ; } } }"
    )
    with pytest.raises(LimitError, match=r'^cell "m" has more than 10000 pins$'):
        parse_liberty(huge_bus)
    # Only the macro's cell is read, so another cell's pins cost nothing.
    assert parse_liberty(huge_bus, cell="other") == {}
    with pytest.raises(LimitError, match=r"^the Verilog file has more than 50 tokens$"):
        verilog_ports(many, "m", SMALL)
    with pytest.raises(LimitError, match=r'^module "m" has more than 3 ports$'):
        verilog_ports("module m (a, b, c, d); endmodule", "m", SMALL)
    with pytest.raises(LimitError, match=r"^the SPICE file has more than 50 lines$"):
        spice_subckt("\n" * 50, "m", SMALL)
    with pytest.raises(LimitError, match=r'^.subckt "m" has more than 3 ports$'):
        spice_subckt(".subckt m a b c d\n.ends\n", "m", SMALL)


def test_views_stay_linear():
    # Each took longer than any CI run before: a range search that copied the rest of
    # the file per declaration, and a continued SPICE line copied per continuation.
    start = time.monotonic()
    ports = verilog_ports("module m (a);\n" + "input [3:0 x ;\n" * 200_000 + "endmodule", "m")
    assert ports is not None and len(ports) == 1
    sub = spice_subckt(".subckt m a\n" + "+ b\n" * 1_000_000 + ".ends\n", "m", Limits(pins=10**7))
    assert sub is not None and len(sub.ports) == 1_000_001
    assert time.monotonic() - start < 30


# Whole packages


@pytest.mark.parametrize(
    ("rel", "limit"),
    [
        ("macro.toml", LIMITS.manifest_bytes),
        ("tiny_counter.gds", LIMITS.gds_bytes),
        ("tiny_counter.lef", LIMITS.view_bytes),
        ("tiny_counter.lib", LIMITS.liberty_bytes),
        ("tiny_counter.v", LIMITS.view_bytes),
        ("tiny_counter.spice", LIMITS.view_bytes),
        ("evidence/lvs.json", LIMITS.evidence_bytes),
    ],
)
def test_oversized_files_are_not_read(tmp_path, rel, limit):
    pkg = _copy(tmp_path)
    # Sparse: the size is there for stat() to see, the bytes are never written or read.
    os.truncate(pkg / rel, limit + 1)
    start = time.monotonic()
    report = check_package(pkg)
    assert time.monotonic() - start < 5
    message = f"file is {limit + 1} bytes, larger than the limit of {limit} bytes"
    assert ("file.too_large", rel, message) in _findings(report)
    # Reported once, though evidence names the GDS and SPICE files as inputs too.
    assert report.rules().count("file.too_large") == 1


def test_oversized_spec_table(tmp_path):
    pkg = _copy(tmp_path, "tiny_ota.macro")
    os.truncate(pkg / "tiny_ota.specs.toml", LIMITS.specs_bytes + 1)
    assert check_package(pkg).rules() == ["file.too_large"]


def test_manifest_limits(tmp_path):
    pkg = _copy(tmp_path)
    toml = (pkg / "macro.toml").read_text()
    (pkg / "macro.toml").write_text(toml + "\n[extra]\n" + ".".join(["k"] * 300_000) + " = 1\n")
    start = time.monotonic()
    assert _findings(check_package(pkg)) == [
        ("manifest.limit", "macro.toml", "tables and arrays nest more than 64 deep")
    ]
    assert time.monotonic() - start < 5
    corners = "".join(
        f'\n[[corners]]\nname = "c{i}"\nprocess = "tt"\ntemperature = 27\n' for i in range(65)
    )
    (pkg / "macro.toml").write_text(toml.replace("width = 2\n", "width = 9999\n") + corners)
    report = check_package(pkg)
    assert (report.name, report.kind) == ("tiny_counter", "digital")
    assert _findings(report) == [
        ("manifest.limit", "macro.toml", "the pins have more than 10000 bits"),
        ("manifest.limit", "macro.toml", "more than 64 corners"),
    ]


def test_evidence_limits(tmp_path):
    pkg = _copy(tmp_path)
    for i in range(LIMITS.evidence_files):
        shutil.copy(pkg / "evidence" / "drc.json", pkg / "evidence" / f"extra{i}.json")
    assert _findings(check_package(pkg)) == [
        ("evidence.limit", "evidence", "more than 64 summaries")
    ]
    for i in range(LIMITS.evidence_files):
        (pkg / "evidence" / f"extra{i}.json").unlink()
    few = dataclasses.replace(LIMITS, hashed_files=1)
    assert _findings(check_package(pkg, limits=few)) == [
        (
            "evidence.limit",
            "evidence/lvs.json",
            "the summaries name more than 1 package files as inputs",
        )
    ]


def test_findings_past_the_limit_are_counted(tmp_path):
    pkg = _copy(tmp_path)
    lef = (pkg / "tiny_counter.lef").read_text()
    extra = "".join(f"  PIN x{i}\n    USE SIGNAL ;\n  END x{i}\n" for i in range(7))
    (pkg / "tiny_counter.lef").write_text(lef.replace("  OBS", extra + "  OBS", 1))
    (pkg / "macro.toml").write_text(
        (pkg / "macro.toml").read_text().replace('kind = "clock"', 'kind = "signal"')
    )
    report = check_package(pkg, limits=dataclasses.replace(LIMITS, findings=5))
    assert report.rules() == ["lef.use"] + ["lef.extra_pin"] * 4 + ["check.limit"]
    assert report.findings[-1].to_dict() == {
        "rule": "check.limit",
        "severity": "warning",
        "file": ".",
        "message": "3 more findings not listed: 3 errors, 0 warnings",
    }
    assert (report.errors, report.warnings, report.passed) == (7, 2, False)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs file modes")
def test_unreadable_file(tmp_path):
    pkg = _copy(tmp_path)
    (pkg / "tiny_counter.v").chmod(0)
    report = check_package(pkg)
    assert report.rules() == ["file.unreadable"]
    assert report.findings[0].message.startswith("cannot read the file: ")


# Random bytes: whatever a file holds, a check returns a report.


@pytest.mark.parametrize(
    "rel",
    [
        "macro.toml",
        "tiny_counter.gds",
        "tiny_counter.lef",
        "tiny_counter.lib",
        "tiny_counter.v",
        "tiny_counter.spice",
        "evidence/drc.json",
    ],
)
def test_random_bytes_never_crash(tmp_path, rel):
    rng = random.Random(f"macro/{rel}")
    pkg = _copy(tmp_path)
    good = (pkg / rel).read_bytes()
    for _ in range(60):
        k = rng.randrange(4)
        if k == 0:
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(400)))
        elif k == 1:
            data = good[: rng.randrange(len(good) + 1)]
        else:
            data = bytearray(good)
            for _ in range(rng.randrange(1, 8)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            data = bytes(data)
        (pkg / rel).write_bytes(data)
        check_package(pkg)
    # The readers on their own, with random text.
    for _ in range(60):
        text = "".join(
            rng.choice("ab ;{}()[]:,\"'\\/*#\n+.=`$01-") for _ in range(rng.randrange(300))
        )
        for read in (
            lambda t: parse_lef(t, SMALL),
            lambda t: parse_liberty(t, SMALL),
            lambda t: verilog_ports(t, "a", SMALL),
            lambda t: spice_subckt(t, "a", SMALL),
        ):
            with contextlib.suppress(ViewError, LimitError):
                read(text)
        with contextlib.suppress(GdsError, LimitError):
            parse_gds(text.encode("latin-1"), SMALL)
