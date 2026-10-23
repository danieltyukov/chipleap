"""Macro packages (macro/v1): the validator, its readers and the shared contract."""

import asyncio
import copy
import hashlib
import json
import runpy
import shutil
import struct
import sys
from pathlib import Path

import pytest

from chipleap import mcp_server
from chipleap.cli import main
from chipleap.macro import (
    MacroError,
    Manifest,
    PdkLibs,
    check_package,
    load_manifest,
    make_evidence,
    parse_gds,
    parse_pdk_option,
    schema,
    write_evidence,
)
from chipleap.macro.gds import structure_identities
from chipleap.macro.layer import _pdk_libs, pdk_libs
from chipleap.macro.manifest import read_toml
from chipleap.macro.schema import validate_def
from chipleap.macro.text import fmt_g
from chipleap.macro.views import parse_lef, parse_liberty, spice_subckt, verilog_ports

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "macro"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())

# The contract both repositories share. aispice pins the same hashes in
# crates/aispice-core/tests/macro_contract.rs; change both together (CONTRIBUTING.md).
CONTRACT = {
    "docs/macro-package.md": "256a3cab336c8d629538ca2cc43023b08681335ce1bcadc2ce56497e0aaef24b",
    "src/chipleap/macro/macro-v1.schema.json": "01fadd5205677b27ef3dcc5a00c086aaf00f2dba63d398d17fc046dd67254327",
    "tests/fixtures/macro/SHA256SUMS": "3170942c1f149c7e5781d5ee0571b9d86debd09ce7c19f647600481c9fc660ae",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_id(case: dict) -> str:
    return (
        case["package"]
        + (f"@{case['pdk']}" if case["pdk"] else "")
        + (f"+{case['pdk_libs']}" if case.get("pdk_libs") else "")
    )


@pytest.mark.parametrize("case", EXPECTED["cases"], ids=_case_id)
def test_fixture_reports_exactly_what_expected_json_says(case):
    libs = pdk_libs(FIXTURES / case["pdk_libs"]) if case.get("pdk_libs") else None
    report = check_package(FIXTURES / case["package"], pdk=case["pdk"], pdk_libs=libs)
    assert (report.name, report.kind) == (case["name"], case["kind"])
    assert (report.passed, report.errors, report.warnings) == (
        case["pass"],
        case["errors"],
        case["warnings"],
    )
    got = [f.to_dict() for f in report.findings]
    assert len(got) == len(case["findings"]), got
    for g, want in zip(got, case["findings"], strict=True):
        assert (g["rule"], g["severity"], g["file"]) == (
            want["rule"],
            want["severity"],
            want["file"],
        )
        if "message" in want:
            assert g["message"] == want["message"]
        else:
            assert g["message"].startswith(want["message_prefix"])


def test_every_fixture_package_has_a_case():
    packages = {c["package"] for c in EXPECTED["cases"]}
    on_disk = {"tiny_ota.macro", "tiny_counter.macro"} | {
        p.relative_to(FIXTURES).as_posix() for p in (FIXTURES / "broken").glob("*/*")
    }
    assert packages == on_disk


def test_fixture_files_match_sha256sums():
    listed = {}
    for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
        digest, rel = line.split("  ", 1)
        listed[rel] = digest
    on_disk = {
        p.relative_to(FIXTURES).as_posix()
        for p in FIXTURES.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS" and "__pycache__" not in p.parts
    }
    assert set(listed) == on_disk
    for rel, digest in listed.items():
        assert _sha(FIXTURES / rel) == digest, f"{rel} changed; run generate.py"


def test_the_shared_contract_has_not_drifted():
    for rel, digest in CONTRACT.items():
        assert _sha(ROOT / rel) == digest, (
            f"{rel} changed. The spec, the schema and the fixtures are shared with aispice: "
            "make the same change there and update the hash in both repositories."
        )


def test_generator_reproduces_the_fixtures(tmp_path):
    work = tmp_path / "macro"
    work.mkdir()
    for name in ("generate.py", "README.md", "expected.json"):
        shutil.copy(FIXTURES / name, work / name)
    runpy.run_path(str(work / "generate.py"), run_name="__main__")
    assert (work / "SHA256SUMS").read_text() == (FIXTURES / "SHA256SUMS").read_text()


# The schema interpreter against a full JSON Schema implementation.


def _mutations():
    base = read_toml(FIXTURES / "tiny_counter.macro" / "macro.toml")
    yield "valid", base

    def mut(fn):
        d = copy.deepcopy(base)
        fn(d)
        return d

    yield "no name", mut(lambda d: d.pop("name"))
    yield "bad name", mut(lambda d: d.update(name="1x"))
    yield "name with newline", mut(lambda d: d.update(name="abc\n"))
    yield "long name", mut(lambda d: d.update(name="a" * 65))
    yield "kind", mut(lambda d: d.update(kind="mixed"))
    yield "unknown field", mut(lambda d: d.update(colour="red"))
    yield "commit", mut(lambda d: d["pdk"].update(commit="XYZ"))
    yield "size type", mut(lambda d: d["size"].update(width_um="20"))
    yield "size zero", mut(lambda d: d["size"].update(height_um=0))
    yield "no pins", mut(lambda d: d.update(pins=[]))
    yield "pin direction", mut(lambda d: d["pins"][0].update(direction="in"))
    yield "pin no domain", mut(lambda d: d["pins"][0].pop("domain"))
    yield "supply domain", mut(lambda d: d["pins"][4].update(domain="core"))
    yield "supply width", mut(lambda d: d["pins"][4].update(width=2))
    yield "supply direction", mut(lambda d: d["pins"][4].update(direction="input"))
    yield "width zero", mut(lambda d: d["pins"][3].update(width=0))
    yield "width float", mut(lambda d: d["pins"][3].update(width=2.5))
    yield "width bool", mut(lambda d: d["pins"][3].update(width=True))
    yield "corner voltage", mut(lambda d: d["corners"][1]["voltages"].update(core=-1))
    yield "corner liberty path", mut(lambda d: d["corners"][1].update(liberty="../x.lib"))
    yield "analog without corners", mut(lambda d: (d.update(kind="analog"), d.pop("corners")))
    yield "domain power empty", mut(lambda d: d["domains"][0].update(power=[]))
    yield "producer extra", mut(lambda d: d["producer"].update(url="x"))
    yield "extra is free", mut(lambda d: d["extra"].update(anything={"a": [1, 2]}))
    yield "schema version", mut(lambda d: d.update(schema="macro/v0"))


@pytest.mark.parametrize(
    "label,doc", list(_mutations()), ids=lambda x: x if isinstance(x, str) else ""
)
def test_schema_interpreter_agrees_with_jsonschema(label, doc):
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(schema())
    if label == "name with newline":
        # JSON Schema patterns are ECMA-262, where $ matches only at the end. The
        # jsonschema package uses Python's re, whose $ also matches before a final
        # newline, so it accepts this name; aispice's Rust regex rejects it, as we do.
        assert validate_def("", doc) and validator.is_valid(doc)
        return
    assert bool(validate_def("", doc)) == (not validator.is_valid(doc)), label


def test_schema_documents_agree_with_jsonschema():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema())
    for name, pattern in (("evidence", "*/evidence/*.json"), ("specs_file", "*.specs.toml")):
        sub = {"$ref": f"#/$defs/{name}", "$defs": schema()["$defs"]}
        validator = jsonschema.Draft202012Validator(sub)
        for path in sorted(FIXTURES.rglob(pattern)):
            try:
                doc = json.loads(path.read_text()) if path.suffix == ".json" else read_toml(path)
            except ValueError:
                continue  # a fixture cut short on purpose
            if doc.get("schema") == "openada.result/v0alpha1":
                continue
            assert bool(validate_def(name, doc)) == (not validator.is_valid(doc)), path


def test_schema_messages():
    assert validate_def("", {}) == [
        f"{k} is missing"
        for k in ("schema", "name", "kind", "pdk", "producer", "size", "domains", "pins")
    ]
    doc = read_toml(FIXTURES / "tiny_counter.macro" / "macro.toml")
    doc["pins"][4]["domain"] = "core"
    doc["pins"][3]["width"] = 0
    doc["size"]["width_um"] = "wide"
    doc["colour"] = 1
    assert validate_def("", doc) == [
        "pins[3].width must be at least 1",
        "pins[4].domain is not allowed here",
        "size.width_um must be a number",
        "colour is not a known field",
    ]
    assert validate_def("evidence", []) == ["the document must be a table"]


# Readers


def test_fmt_g_matches_printf():
    cases = {
        0.0: "0",
        20.0: "20",
        12.5: "12.5",
        -40.0: "-40",
        1e7: "1e+07",
        42.0e6: "4.2e+07",
        25e-6: "2.5e-05",
        0.0001: "0.0001",
        0.00012345678: "0.000123457",
        123456.5: "123456",
        999999.5: "1e+06",
        1234567.0: "1.23457e+06",
        0.1 + 0.2: "0.3",
        28.4: "28.4",
    }
    for x, want in cases.items():
        assert fmt_g(x) == want, x


# Values from KLayout's dbbox() of tests/fixtures/macro/gds/hierarchy.gds.
HIERARCHY_BOXES = {
    "pair": (0, -0.2, 2, 1.2),
    "rot90": (3.8, 2, 5.2, 4),
    "mirror": (7, 4.8, 9, 6.2),
    "mirror_rot270": (-1.1, -1.9, 0.3, 0.1),
    "rot45": (-0.7071, 0, 1.4142, 2.1213),
    "arr": (1, 0.3, 8, 3.2),
    "path0": (0, 4.9, 4.1, 8),
    "path2": (-0.1, 4.9, 4.1, 8.1),
    "nested": (-5.19, -3.98, 0.2, 8),
}


def test_gds_bbox_through_the_hierarchy_matches_klayout():
    lib = parse_gds((FIXTURES / "gds" / "hierarchy.gds").read_bytes())
    for name, box in HIERARCHY_BOXES.items():
        assert lib.bbox(name) == pytest.approx(box, abs=1e-4), name
    assert lib.top_cells() == ["mirror", "mirror_rot270", "rot45", "path0", "path2", "nested"]


def test_gds_rejects_broken_streams():
    good = (FIXTURES / "tiny_ota.macro" / "tiny_ota.gds").read_bytes()
    with pytest.raises(ValueError, match=r"truncated|ENDLIB"):
        parse_gds(good[:-4])
    with pytest.raises(ValueError, match="record length"):
        parse_gds(struct.pack(">HBB", 2, 0, 2))
    with pytest.raises(ValueError, match="too short"):
        parse_gds(good[:6] + struct.pack(">HBB", 6, 0x0F, 3) + b"\0\0" + good[6:])
    with pytest.raises(ValueError, match="contains itself"):
        parse_gds((FIXTURES / "gds" / "loop.gds").read_bytes()).bbox("a")


def test_lef_reader():
    text = """
    VERSION 5.8 ;
    # a comment ; with a semicolon
    MACRO other
      SIZE 1 BY 1 ;
    END other
    MACRO blk
      CLASS BLOCK ;
      ORIGIN 1.5 -2 ;
      SIZE 10.25 BY 4 ;
      PROPERTY foo "a ; b" ;
      PIN d[0]
        DIRECTION OUTPUT TRISTATE ;
        PORT
          LAYER Metal3 ;
            RECT 0 0 1 1 ;
          LAYER Metal4 ;
            POLYGON 0 0 1 0 1 1 ;
        END
      END d[0]
      PIN VPWR
        USE POWER ;
      END VPWR
      OBS
        LAYER Metal1 ;
          RECT 0 0 10 4 ;
      END
      DENSITY
        LAYER Metal1 ;
          RECT 0 0 1 1 50 ;
      END
    END blk
    END LIBRARY
    """
    macros = parse_lef(text)
    blk = macros["blk"]
    assert (blk.cls, blk.origin, blk.size) == ("BLOCK", (1.5, -2.0), (10.25, 4.0))
    d0 = blk.pins["d[0]"]
    assert (d0.direction, d0.use, d0.layers, d0.shapes) == ("OUTPUT", "", ["Metal3", "Metal4"], 2)
    assert blk.pins["VPWR"].use == "POWER" and blk.pins["VPWR"].shapes == 0
    with pytest.raises(ValueError, match="end of file"):
        parse_lef("MACRO x\n PIN a\n DIRECTION INPUT ;\n")
    with pytest.raises(ValueError, match="wrong name"):
        parse_lef("MACRO x\nEND y\n")


def test_liberty_reader():
    text = r"""
    library (l) {
      /* comment */ type (b4) { bit_width : 4 ; bit_from : 0 ; bit_to : 3 ; }
      date : "Mon Oct 11"
      cell ("blk") {
        pin (a, b) { direction : input ; }
        pin (y) { direction : output
          timing () { related_pin : "a" ; }
          timing () { related_pin : "b" ; } }
        bus (d) {
          bus_type : b4 ;
          direction : output ;
          timing () { related_pin : "a" ; }
          pin (d[2]) { direction : inout ; }
        }
        pg_pin (VDD) { pg_type : primary_power ; }
        comment : "multi \
          line" ;
      }
    }
    """
    pins = parse_liberty(text)["blk"]
    assert sorted(pins) == ["VDD", "a", "b", "d[0]", "d[1]", "d[2]", "d[3]", "y"]
    assert pins["y"].direction == "output" and pins["y"].timing == 2
    assert pins["d[0]"].direction == "output" and pins["d[0]"].timing == 1
    assert pins["d[2]"].direction == "inout"
    assert pins["VDD"].pg and pins["VDD"].pg_type == "primary_power"
    with pytest.raises(ValueError):
        parse_liberty("library (l) { cell (x) { pin (a) { direction : input ; }")


def test_verilog_reader():
    non_ansi = """
    module other(a); input a; endmodule
    module blk (clk, d, q, \\esc[0] , .alias(x));
      parameter W = 4;
      input clk;
      input wire [W-1:0] d;
      output reg [0:3] q;
      function f; input x; endfunction
      task t; output y; endtask
      inout \\esc[0] ;
    endmodule
    """
    ports = {p.name: (p.direction, p.width) for p in verilog_ports(non_ansi, "blk")}
    assert ports == {
        "clk": ("input", None),
        "d": ("input", 0),
        "q": ("output", 4),
        "esc[0]": ("inout", None),
        "alias": ("", None),
    }
    ansi = """
    module blk #(parameter W = 2) (
      input  logic        clk, rst,
      output logic [7:0]  a, b,
      input               c = 1'b0,
      inout  wreal        vin
    );
      always @(*) begin end
    endmodule
    """
    ports = {p.name: (p.direction, p.width) for p in verilog_ports(ansi, "blk")}
    assert ports == {
        "clk": ("input", None),
        "rst": ("input", None),
        "a": ("output", 8),
        "b": ("output", 8),
        "c": ("input", None),
        "vin": ("inout", None),
    }
    assert verilog_ports("module x; endmodule", "blk") is None
    assert verilog_ports("module blk; endmodule", "blk") == []


def test_spice_reader():
    text = """* header
.SUBCKT other a b
.ends
.subckt BLK in out
+ vdd vss params: w=1u
*.pininfo in:I out:O
+ vdd:B
X1 out in vdd vss inv ; inline comment
.ends BLK
"""
    sub = spice_subckt(text, "blk")
    assert sub is not None
    assert sub.ports == ["in", "out", "vdd", "vss"]
    assert sub.pininfo == {"in": "I", "out": "O", "vdd": "B"}
    assert spice_subckt(".subckt x a=1\n.ends\n", "x").ports == []
    assert spice_subckt(text, "missing") is None


# Writing packages


def test_manifest_round_trips_through_toml():
    for pkg in ("tiny_ota.macro", "tiny_counter.macro"):
        m = load_manifest(FIXTURES / pkg)
        text = m.to_toml()
        again = Manifest.from_dict(read_toml_text(text))
        assert again == m
        assert again.to_toml() == text
    m = load_manifest(FIXTURES / "tiny_counter.macro")
    assert m.pin("q").bits() == ["q[0]", "q[1]"] and m.file("lef") == "tiny_counter.lef"
    m.name = "bad name"
    with pytest.raises(MacroError, match="does not match"):
        m.to_toml()


def read_toml_text(text: str) -> dict:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    return tomllib.loads(text)


def test_load_manifest_errors(tmp_path):
    with pytest.raises(MacroError, match="cannot read"):
        load_manifest(tmp_path)
    (tmp_path / "macro.toml").write_text('schema = "macro/v1"\n')
    with pytest.raises(MacroError, match="name is missing"):
        load_manifest(tmp_path)


def test_evidence_written_after_a_change_makes_the_package_pass_again(tmp_path):
    pkg = tmp_path / "tiny_ota.macro"
    shutil.copytree(FIXTURES / "broken" / "stale-evidence" / "tiny_ota.macro", pkg)
    assert check_package(pkg).rules() == ["evidence.stale", "evidence.stale"]
    m = load_manifest(pkg)
    for op, inputs in (("drc", ["tiny_ota.gds"]), ("lvs", ["tiny_ota.gds", "tiny_ota.spice"])):
        doc = make_evidence(
            pkg,
            op,
            "pass",
            f"{op} rerun",
            ("klayout", "0.30.4"),
            m.pdk,
            inputs=[pkg / f for f in inputs],
            artifacts=[pkg / "macro.toml"],
            data={"violations": 0},
        )
        assert doc["inputs"][0]["path"] == "tiny_ota.gds"
        write_evidence(pkg, doc)
    assert check_package(pkg).findings == []
    with pytest.raises(MacroError, match=r"engineering\.status"):
        make_evidence(pkg, "drc", "clean", "", ("t", "1"), m.pdk)


def test_package_level_problems(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_package(tmp_path / "nope")
    assert check_package(tmp_path).rules() == ["file.missing"]
    pkg = tmp_path / "tiny_counter.macro"
    shutil.copytree(FIXTURES / "tiny_counter.macro", pkg)
    (pkg / "tiny_counter__ss.lib").unlink()
    shutil.rmtree(pkg / "evidence")
    (pkg / "macro.toml").write_text(
        (pkg / "macro.toml").read_text().replace('schema = "macro/v1"', 'schema = "macro/v2"')
    )
    assert check_package(pkg).rules() == ["manifest.version"]
    (pkg / "macro.toml").write_text(
        (pkg / "macro.toml").read_text().replace('schema = "macro/v2"', 'schema = "macro/v1"')
    )
    report = check_package(pkg)
    assert report.rules() == ["file.missing", "evidence.missing", "evidence.missing"]
    assert report.findings[0].file == "tiny_counter__ss.lib"
    with pytest.raises(ValueError):
        parse_pdk_option("ihp@XYZ")
    assert parse_pdk_option("ihp-sg13g2@22f2a25") == ("ihp-sg13g2", "22f2a25")


# Front ends


def test_cli_macro_check(capsys):
    good = str(FIXTURES / "tiny_ota.macro")
    assert main(["macro", "check", good]) == 0
    assert "pass, 0 errors, 0 warnings" in capsys.readouterr().out
    assert (
        main(["macro", "check", str(FIXTURES / "broken" / "wrong-size" / "tiny_counter.macro")])
        == 1
    )
    assert "lef.size" in capsys.readouterr().out
    assert main(["macro", "check", good, "--pdk", "sky130A", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == "macro.check/v1" and report["findings"][0]["rule"] == "pdk.expected"
    dirname = str(FIXTURES / "broken" / "dirname" / "counter_v2")
    assert main(["macro", "check", dirname]) == 0
    assert main(["macro", "check", dirname, "--strict"]) == 1
    capsys.readouterr()
    assert main(["macro", "check", str(FIXTURES / "missing")]) == 2
    assert main(["macro", "check", good, "--pdk", "x@y"]) == 2
    capsys.readouterr()
    assert main(["macro", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"].startswith("Macro package manifest")


def test_cli_macro_output_is_terminal_safe(tmp_path, capsys):
    pkg = tmp_path / "tiny_counter.macro"
    shutil.copytree(FIXTURES / "tiny_counter.macro", pkg)
    lef = pkg / "tiny_counter.lef"
    lef.write_text(lef.read_text().replace("DIRECTION OUTPUT", "DIRECTION \x1b[2J\u202eOUT", 1))
    assert main(["macro", "check", str(pkg)]) == 1
    out = capsys.readouterr().out
    assert "lef.direction" in out and "\x1b" not in out and "\u202e" not in out


def test_mcp_check_macro_tool(tmp_path):
    out = mcp_server.check_macro(str(FIXTURES / "broken" / "lef-missing-pin" / "tiny_ota.macro"))
    assert out["pass"] is False and out["findings"][0]["rule"] == "lef.missing_pin"
    with pytest.raises(ValueError, match="not a directory"):
        mcp_server.check_macro(str(tmp_path / "nope"))
    assert "check_macro" in mcp_server.SIDE_EFFECTS

    server = mcp_server.build_server(log_dir=str(tmp_path))
    try:
        from mcp import Client
    except ImportError:  # mcp 1.x
        pytest.skip("in-process client needs mcp 2.x")

    async def go():
        async with Client(server) as c:
            assert "check_macro" in {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool(
                "check_macro", {"path": str(FIXTURES / "tiny_ota.macro"), "pdk": "ihp-sg13g2"}
            )
            assert json.loads(r.content[0].text)["pass"] is True

    asyncio.run(go())


# pdk.cell_name: cells named like the PDK's own


def _generator():
    return runpy.run_path(str(FIXTURES / "generate.py"))


def _counter(tmp_path: Path, name: str, tap: str = "") -> Path:
    """tiny_counter renamed to ``name``, its tap cell named ``tap``, valid otherwise."""
    gen = _generator()
    files = gen["counter"]()
    out = {}
    for rel, data in files.items():
        if rel.startswith("evidence/") or rel.endswith(".gds"):
            continue
        out[rel.replace("tiny_counter", name)] = data.replace(b"tiny_counter", name.encode())
    out[f"{name}.gds"] = gen["counter_gds"](top=name, tap_name=tap or f"{name}_tap")
    gen["with_evidence"](out, name)
    dest = tmp_path / f"{name}.macro"
    gen["write"](dest, out)
    return dest


STAND_IN = FIXTURES / "pdk" / "ihp-sg13g2"
UNSHOWN = ", and without the PDK's libraries cannot be shown to be the PDK's own"


def test_a_package_named_in_the_pdks_name_space_is_refused(tmp_path):
    pkg = _counter(tmp_path, "sg13g2_counter")
    name = 'name "sg13g2_counter" begins with "sg13g2", as the cells of PDK "ihp-sg13g2" do'
    tap = 'GDS cell "sg13g2_counter_tap" begins with "sg13g2", as the cells of PDK "ihp-sg13g2" do'
    # Without the PDK's libraries its tap cell cannot be shown to be the PDK's: refused.
    report = check_package(pkg)
    assert [(f.severity, f.file, f.message) for f in report.findings] == [
        ("error", "macro.toml", name),
        ("error", "sg13g2_counter.gds", tap + UNSHOWN),
    ]
    report = check_package(pkg, pdk_libs=pdk_libs(STAND_IN))
    assert [(f.severity, f.message) for f in report.findings] == [("error", name), ("error", tap)]


def test_the_top_cell_is_never_the_pdks_own(tmp_path):
    """A package named like a standard cell is refused by its name, whatever its top
    cell holds: the exception is for the cells it places."""
    report = check_package(_counter(tmp_path, "sg13g2_inv_1"), pdk_libs=pdk_libs(STAND_IN))
    assert (report.findings[0].file, report.findings[0].message) == (
        "macro.toml",
        'name "sg13g2_inv_1" begins with "sg13g2", as the cells of PDK "ihp-sg13g2" do',
    )


def test_case_does_not_hide_a_pdk_cell_name(tmp_path):
    pkg = _counter(tmp_path, "counter", tap="RM_IHPSG13_tap")
    report = check_package(pkg, pdk_libs=PdkLibs())
    assert [f.message for f in report.findings] == [
        'GDS cell "RM_IHPSG13_tap" begins with "rm_ihpsg13", as the cells of PDK "ihp-sg13g2" do'
    ]
    # A standard cell's name in another case is not its name.
    pkg = _counter(tmp_path / "upper", "counter", tap="SG13G2_INV_1")
    report = check_package(pkg, pdk_libs=pdk_libs(STAND_IN))
    assert [f.message for f in report.findings] == [
        'GDS cell "SG13G2_INV_1" begins with "sg13g2", as the cells of PDK "ihp-sg13g2" do'
    ]


def _records(data: bytes) -> list[bytes]:
    out, i = [], 0
    while i + 4 <= len(data):
        n = (data[i] << 8) | data[i + 1]
        out.append(data[i : i + n])
        i += n
    return out


def _inv(data: bytes) -> str:
    return dict(structure_identities(data))[b"sg13g2_inv_1"]


def test_identity_covers_every_record_but_the_dates():
    """The package's copy of the inverter differs from the PDK's only in BGNSTR's dates:
    the same identity. A property, the order of its elements or the file's units make
    another. A chip's merges compare layouts (``chipleap.chip.merge.gds_names``), where
    the order of the elements makes no other."""
    from chipleap.chip.merge import gds_names

    def layout(data: bytes) -> str:
        return gds_names(data)["sg13g2_inv_1"]

    pdk = (STAND_IN / "libs.ref" / "sg13g2_stdcell" / "gds" / "sg13g2_stdcell.gds").read_bytes()
    same = (FIXTURES / "broken/std-cell-identical/tiny_counter.macro/tiny_counter.gds").read_bytes()
    assert pdk.count(b"\x07\xe9") and not same.count(b"\x07\xe9")  # 2025, 2026
    assert _inv(pdk) == _inv(same) != ""
    assert layout(pdk) == layout(same) != ""
    recs = _records(same)
    start = recs.index(next(r for r in recs if r[2] == 0x06 and r[4:] == b"sg13g2_inv_1"))
    ends = [k for k in range(start, len(recs)) if recs[k][2] == 0x11]  # ENDEL
    # A property on the first element.
    prop = struct.pack(">HBBh", 6, 0x2B, 0x02, 1) + struct.pack(">HBB", 6, 0x2C, 0x06) + b"x\0"
    with_prop = [*recs[: ends[0]], prop, *recs[ends[0] :]]
    assert _inv(b"".join(with_prop)) != _inv(same)
    assert layout(b"".join(with_prop)) != layout(same)
    # The first two elements swapped.
    first, second = recs[start + 1 : ends[0] + 1], recs[ends[0] + 1 : ends[1] + 1]
    swapped = recs[: start + 1] + second + first + recs[ends[1] + 1 :]
    assert len(swapped) == len(recs) and _inv(b"".join(swapped)) != _inv(same)
    assert layout(b"".join(swapped)) == layout(same)
    # A second UNITS record, or one after the first structure: no identity at all.
    units = next(r for r in recs if r[2] == 0x03)
    k = recs.index(units)
    assert _inv(b"".join([*recs[: k + 1], units, *recs[k + 1 :]])) == ""
    assert layout(b"".join([*recs[: k + 1], units, *recs[k + 1 :]])) == ""
    first_str = next(i for i, r in enumerate(recs) if r[2] == 0x05)
    moved = [*recs[:k], *recs[k + 1 : first_str + 1], units, *recs[first_str + 1 :]]
    assert _inv(b"".join(moved)) == ""


def test_only_the_standard_cell_gds_holds_the_pdks_own(tmp_path):
    """A copy of the inverter in another library's GDS, or a standard-cell GDS that
    defines it twice, makes no standard cell of it."""
    pdk_gds = (STAND_IN / "libs.ref" / "sg13g2_stdcell" / "gds" / "sg13g2_stdcell.gds").read_bytes()
    root = tmp_path / "ihp-sg13g2"
    (root / "libs.ref" / "sg13g2_pr" / "gds").mkdir(parents=True)
    (root / "libs.ref" / "sg13g2_pr" / "gds" / "sg13g2_pr.gds").write_bytes(pdk_gds)
    libs = pdk_libs(root)
    assert "sg13g2_inv_1" in libs.names and not libs.std_cells
    pkg = FIXTURES / "broken" / "std-cell-identical" / "tiny_counter.macro"
    assert check_package(pkg, pdk_libs=libs).errors == 2
    recs = _records(pdk_gds)
    inv = next(r for r in recs if r[2] == 0x06 and r[4:] == b"sg13g2_inv_1")
    begin = recs.index(inv) - 1
    end = next(i for i in range(begin, len(recs)) if recs[i][2] == 0x07)
    twice = recs[: end + 1] + recs[begin : end + 1] + recs[end + 1 :]
    std = root / "libs.ref" / "sg13g2_stdcell" / "gds"
    std.mkdir(parents=True)
    (std / "sg13g2_stdcell.gds").write_bytes(b"".join(twice))
    _pdk_libs.cache_clear()
    assert list(pdk_libs(root).std_cells) == ["sg13g2_buf_1"]


def test_a_cell_of_the_pdks_libraries_is_refused_when_the_validator_has_them(tmp_path):
    pkg = _counter(tmp_path, "counter", tap="nmos")
    assert check_package(pkg).passed
    report = check_package(pkg, pdk_libs=PdkLibs(frozenset({"nmos", "pmos"})))
    assert [f.message for f in report.findings] == ['GDS cell "nmos" is a cell of PDK "ihp-sg13g2"']
    assert check_package(pkg, pdk_libs=PdkLibs(frozenset({"NMOS"}))).errors == 1


def test_other_pdks_have_no_name_space_of_ihps(tmp_path):
    pkg = _counter(tmp_path, "sg13g2_counter")
    toml = pkg / "macro.toml"
    toml.write_text(toml.read_text().replace('name = "ihp-sg13g2"', 'name = "other-pdk"'))
    rules = [f.rule for f in check_package(pkg).findings]
    assert "pdk.cell_name" not in rules


def test_the_pdks_libraries_hold_names_and_standard_cells():
    libs = pdk_libs(STAND_IN)
    assert libs.names == {"sg13g2_inv_1", "sg13g2_buf_1", "sg13g2_corner"}
    assert sorted(libs.std_cells) == ["sg13g2_buf_1", "sg13g2_inv_1"]


def test_the_cli_reads_the_pdks_libraries(tmp_path, capsys):
    pkg = _counter(tmp_path, "counter", tap="nmos")
    libs = tmp_path / "pdk" / "ihp-sg13g2" / "libs.ref" / "sg13g2_pr" / "lef"
    libs.mkdir(parents=True)
    (libs / "sg13g2_pr.lef").write_text("MACRO nmos\nEND nmos\n")
    assert main(["macro", "check", str(pkg)]) == 0
    capsys.readouterr()
    assert (
        main(["macro", "check", str(pkg), "--pdk-libs", str(tmp_path / "pdk" / "ihp-sg13g2")]) == 1
    )
    assert "pdk.cell_name" in capsys.readouterr().out
    assert main(["macro", "check", str(pkg), "--pdk-libs", str(tmp_path / "none")]) == 2
