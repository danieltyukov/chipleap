"""macro/v1's validator against packages built to get a wrong answer from it: files
reached through a symbolic link, files that are not regular, and views that two readers
read differently. Where a tool is installed, the test shows what that tool reads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from chipleap.macro import check_package
from chipleap.macro.views import ViewError, parse_lef, parse_liberty, verilog_ports
from chipleap.verify.tools import openroad, yosys

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro"
BROKEN = FIXTURES / "broken"


def found(report, rule):
    return [(f.file, f.message) for f in report.findings if f.rule == rule]


def sibling_pair(tmp_path, linked):
    """A valid package beside a copy of it whose ``linked`` entries are symbolic links
    into the valid one, its sibling."""
    sibling = tmp_path / "pkgs" / "other.macro"
    shutil.copytree(FIXTURES / "tiny_counter.macro", sibling)
    pkg = tmp_path / "pkgs" / "tiny_counter.macro"
    pkg.mkdir()
    for f in sibling.iterdir():
        if f.name in linked:
            (pkg / f.name).symlink_to(Path("..") / "other.macro" / f.name)
        elif f.is_dir():
            shutil.copytree(f, pkg / f.name)
        else:
            shutil.copy(f, pkg / f.name)
    return sibling, pkg


def test_files_in_a_sibling_directory_are_not_read_through_a_link(tmp_path):
    sibling, pkg = sibling_pair(tmp_path, ("evidence", "tiny_counter.gds"))
    # Unchecked, the links reach the sibling's files, and those pass every check.
    assert (pkg / "evidence" / "drc.json").read_bytes() == (
        sibling / "evidence" / "drc.json"
    ).read_bytes()
    assert check_package(sibling).passed
    report = check_package(pkg)
    assert not report.passed
    assert found(report, "file.unreadable") == [
        ("tiny_counter.gds", "cannot read the file: it is a symbolic link"),
        ("evidence", "cannot read the folder: it is a symbolic link"),
    ]
    assert found(report, "evidence.missing") == [
        ("evidence", "no summary for drc"),
        ("evidence", "no summary for lvs"),
    ]


def test_a_summary_that_links_elsewhere_is_not_read(tmp_path):
    sibling, pkg = sibling_pair(tmp_path, ())
    (pkg / "evidence" / "drc.json").unlink()
    (pkg / "evidence" / "drc.json").symlink_to(sibling / "evidence" / "drc.json")
    report = check_package(pkg)
    assert found(report, "file.unreadable") == [
        ("evidence/drc.json", "cannot read the file: it is a symbolic link")
    ]
    assert found(report, "evidence.missing") == [("evidence", "no summary for drc")]


def test_an_evidence_input_that_links_out_of_the_package_is_not_hashed(tmp_path):
    """A summary naming a package file that is a link to a file elsewhere: hashing
    through it would read that file, and the stale-evidence message would print the
    start of its SHA-256."""
    _, pkg = sibling_pair(tmp_path, ())
    secret = tmp_path / "secret.txt"
    secret.write_text("not the package's\n")
    (pkg / "notes.txt").symlink_to(secret)
    drc = json.loads((pkg / "evidence" / "drc.json").read_text())
    drc["inputs"].append({"path": "notes.txt", "sha256": "0" * 64})
    (pkg / "evidence" / "drc.json").write_text(json.dumps(drc))
    assert (pkg / "notes.txt").read_text() == "not the package's\n"  # what a link reaches
    report = check_package(pkg)
    assert found(report, "file.unreadable") == [
        ("notes.txt", "cannot read the file: it is a symbolic link")
    ]
    assert found(report, "evidence.stale") == []


def test_a_fifo_does_not_stall_the_check(tmp_path):
    _, pkg = sibling_pair(tmp_path, ())
    (pkg / "tiny_counter.v").unlink()
    os.mkfifo(pkg / "tiny_counter.v")
    reports = []
    worker = threading.Thread(target=lambda: reports.append(check_package(pkg)), daemon=True)
    worker.start()
    worker.join(30)
    assert reports, "the check blocked on a FIFO"
    assert found(reports[0], "file.unreadable") == [
        ("tiny_counter.v", "cannot read the file: it is not a regular file")
    ]


def test_a_symlinked_package_directory_is_still_checked(tmp_path):
    """The directory given to the validator may itself be a link; only what is inside
    the package is read without following links."""
    link = tmp_path / "tiny_counter.macro"
    link.symlink_to(FIXTURES / "tiny_counter.macro")
    assert check_package(link).passed


# Views two readers read differently


def test_lef_keywords_are_read_in_any_case():
    text = (FIXTURES / "tiny_counter.macro" / "tiny_counter.lef").read_text()
    for kw in ("MACRO", "CLASS", "SIZE", "BY", "PIN", "DIRECTION", "USE", "PORT", "LAYER"):
        text = text.replace(f"{kw} ", f"{kw.lower()} ")
    text = text.replace("RECT", "rect").replace("END", "End").replace("BLOCK", "block")
    m = parse_lef(text)["tiny_counter"]
    assert m.cls == "BLOCK" and m.size == (20.0, 10.0)
    assert m.pins["q[0]"].direction == "OUTPUT" and m.pins["VDD"].use == "POWER"
    assert m.pins["clk"].shapes == 1 and m.pins["clk"].layers == ["Metal2"]


def test_a_view_that_defines_something_twice_cannot_be_read():
    with pytest.raises(ViewError, match="PIN a of MACRO m is defined twice"):
        parse_lef("MACRO m\nPIN a\nEND a\npin a\nEND a\nEND m\n")
    lib = "library (l) { cell (m) { %s } }"
    with pytest.raises(ViewError, match="pin a of cell m is defined twice"):
        parse_liberty(lib % "pin (a) { } pg_pin (a) { }")
    with pytest.raises(ViewError, match="pin a of cell m is defined twice"):
        parse_liberty(lib % "pin (a, a) { }")
    bus = "type (t) { bit_from : 0 ; bit_to : 1 ; } "
    with pytest.raises(ViewError, match=r"pin d\[0\] of cell m is defined twice"):
        parse_liberty(
            "library (l) { " + bus + "cell (m) { pin (d[0]) { } bus (d) { bus_type : t ; } } }"
        )
    with pytest.raises(ViewError, match=r"pin d\[1\] of cell m is defined twice"):
        parse_liberty(
            "library (l) { "
            + bus
            + "cell (m) { bus (d) { bus_type : t ; pin (d[1]) { } pin (d[1]) { } } } }"
        )
    # A bus's own pin groups describe its bits, once each.
    cells = parse_liberty(
        "library (l) { " + bus + "cell (m) { bus (d) { bus_type : t ; direction : input ; "
        "pin (d[1]) { direction : output ; } } } }"
    )
    assert cells["m"]["d[1]"].direction == "output" and cells["m"]["d[0]"].direction == "input"
    with pytest.raises(ViewError, match='module "m" is declared more than once'):
        verilog_ports("module m (input a); endmodule\nmodule m (output a); endmodule\n", "m")
    ports = verilog_ports("module m (a, b);\ninput a;\noutput a;\ninput b;\nendmodule\n", "m")
    assert ports is not None and [(p.name, p.declared) for p in ports] == [("a", 2), ("b", 1)]


def test_yosys_reads_one_side_of_the_ifdef(tmp_path):
    tool = yosys()
    if not tool.found or tool.path is None:
        pytest.skip("Yosys is not installed")
    shutil.copy(BROKEN / "verilog-ifdef-port" / "tiny_counter.macro" / "tiny_counter.v", tmp_path)
    out = subprocess.run(
        [tool.path, "-q", "-p", "read_verilog tiny_counter.v; proc; write_json ports.json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if out.returncode != 0:
        pytest.skip(f"Yosys failed: {(out.stdout + out.stderr).strip()[-200:]}")
    data = json.loads((tmp_path / "ports.json").read_text())
    # The validator's manifest says input; Yosys, with SYNTHESIS defined, reads an output.
    assert data["modules"]["tiny_counter"]["ports"]["en"]["direction"] == "output"


def test_openroad_reads_another_macro_and_cell_than_the_validator_did(tmp_path):
    tool = openroad()
    if not tool.found or tool.path is None:
        pytest.skip("OpenROAD is not installed")
    for case, name in (
        ("lef-duplicate-macro", "tiny_counter.lef"),
        ("liberty-duplicate-cell", "tiny_counter.lib"),
    ):
        shutil.copy(BROKEN / case / "tiny_counter.macro" / name, tmp_path / name)
    (tmp_path / "t.tcl").write_text(
        "read_lef tiny_counter.lef\n"
        "set m [[ord::get_db] findMaster tiny_counter]\n"
        'puts "LEF [$m getWidth] [llength [$m getMTerms]]"\n'
        "read_liberty tiny_counter.lib\n"
        "set p [get_lib_pins */tiny_counter/en]\n"
        'puts "LIB [get_property $p direction]"\n'
    )
    out = subprocess.run(
        [tool.path, "-no_init", "-exit", "t.tcl"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if "LEF " not in out.stdout:
        pytest.skip(f"OpenROAD failed: {(out.stdout + out.stderr).strip()[-200:]}")
    # The lower-case macro first: 30 um wide (in database units) and without pins, where
    # the validator read the 20 um macro with all of them.
    assert "LEF 30000 0" in out.stdout
    # The last cell of the name, where en is an output.
    assert "LIB output" in out.stdout
