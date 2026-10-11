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
    check_package,
    load_manifest,
    make_evidence,
    parse_gds,
    parse_pdk_option,
    schema,
    write_evidence,
)
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
    "docs/macro-package.md": "d76762d1f1435bf7582440e4bdf94079a2579e297a27379c0bcd75d1abf20b69",
    "src/chipleap/macro/macro-v1.schema.json": "01fadd5205677b27ef3dcc5a00c086aaf00f2dba63d398d17fc046dd67254327",
    "tests/fixtures/macro/SHA256SUMS": "9e62a9d5f133f8f8f3591611244ea3d968dd3e8f797ef76b28782713e44a324c",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_id(case: dict) -> str:
    return case["package"] + (f"@{case['pdk']}" if case["pdk"] else "")


@pytest.mark.parametrize("case", EXPECTED["cases"], ids=_case_id)
def test_fixture_reports_exactly_what_expected_json_says(case):
    report = check_package(FIXTURES / case["package"], pdk=case["pdk"])
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
            doc = json.loads(path.read_text()) if path.suffix == ".json" else read_toml(path)
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


def _gds(cells: list[bytes]) -> bytes:
    gen = runpy.run_path(str(FIXTURES / "generate.py"))
    return gen["gds"]("t", cells)


def test_gds_bbox_through_the_hierarchy_matches_klayout():
    gen = runpy.run_path(str(FIXTURES / "generate.py"))
    Cell, layer = gen["Cell"], gen["METAL1"]
    pair = Cell("pair").rect(layer, 0, 0, 2000, 1000).rect(layer, 400, -200, 600, 1200)
    cells = [
        pair,
        Cell("rot90").sref("pair", 5000, 2000, angle=90),
        Cell("mirror").sref("pair", 7000, 6000, mirror=True),
        Cell("mirror_rot270").sref("pair", 100, 100, angle=270, mirror=True),
        Cell("rot45").sref("pair", 0, 0, angle=45),
        Cell("arr").aref("pair", 3, 2, 1000, 500, 2500, 1500),
        Cell("path0").path(layer, 200, 0, 0, 5000, 4000, 5000, 4000, 8000),
        Cell("path2").path(layer, 200, 2, 0, 5000, 4000, 5000, 4000, 8000),
        Cell("nested")
        .sref("rot90", 10, 20, angle=180)
        .sref("arr", -3000, 0, mirror=True, angle=90),
    ]
    lib = parse_gds(gen["gds"]("t", cells))
    # Values from KLayout's dbbox() of the same file.
    expected = {
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
    for name, box in expected.items():
        assert lib.bbox(name) == pytest.approx(box, abs=1e-4), name
    assert set(lib.top_cells()) == {"mirror", "mirror_rot270", "rot45", "path0", "path2", "nested"}


def test_gds_rejects_broken_streams():
    good = (FIXTURES / "tiny_ota.macro" / "tiny_ota.gds").read_bytes()
    with pytest.raises(ValueError, match=r"truncated|ENDLIB"):
        parse_gds(good[:-4])
    with pytest.raises(ValueError, match="record length"):
        parse_gds(struct.pack(">HBB", 2, 0, 2))
    gen = runpy.run_path(str(FIXTURES / "generate.py"))
    loop = gen["gds"]("t", [gen["Cell"]("a").sref("b", 0, 0), gen["Cell"]("b").sref("a", 0, 0)])
    with pytest.raises(ValueError, match="contains itself"):
        parse_gds(loop).bbox("a")


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
