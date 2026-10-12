"""The project file for user RTL: parsing, errors, filelists, digests and init."""

from pathlib import Path

import pytest
import yaml

from chipleap.rtl.project import (
    PROJECT_FILE,
    SCHEMA,
    ProjectError,
    find_project,
    init_project,
    load_project,
    parse_filelist,
    parse_project,
)

COUNTER = """\
module counter #(parameter int W = 4) (
  input  logic         clk_i,
  input  logic         rst_ni,
  input  logic         en_i,
  output logic [W-1:0] q_o
);
  always_ff @(posedge clk_i or negedge rst_ni)
    if (!rst_ni) q_o <= '0;
    else if (en_i) q_o <= q_o + 1'b1;
endmodule
"""

TOP = """\
// top wraps the counter; `counter u_cnt (` is an instance, not a definition
module top (input logic clk_i, input logic rst_ni, output logic [7:0] q_o);
  counter #(.W(8)) u_cnt (.clk_i, .rst_ni, .en_i(1'b1), .q_o);
endmodule
"""


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def rtl(tmp_path: Path) -> Path:
    return write(
        tmp_path,
        {
            "rtl/counter.sv": COUNTER,
            "rtl/top.sv": TOP,
            "rtl/inc/defs.svh": "`define ONE 1\n",
            "test/test_top.py": "import cocotb\n",
            "model/ref.py": "def step(q): return q + 1\n",
        },
    )


def test_minimal_project_and_defaults(rtl):
    p = parse_project({"top": "top", "sources": ["rtl/counter.sv", "rtl/top.sv"]}, rtl)
    assert p.name == "top" and p.top == "top" and p.tech == "ihp-sg13g2"
    assert [s.name for s in p.sources] == ["counter.sv", "top.sv"]
    assert p.timescale == ("1ns", "1ps") and p.clocks == () and p.tests.dir is None
    assert p.tests.toplevel == "top" and p.build_dir == rtl.resolve() / "build"
    assert p.rel(p.sources[0]) == "rtl/counter.sv"


def test_full_project(rtl):
    data = {
        "schema": SCHEMA,
        "name": "demo",
        "top": "top",
        "sources": ["rtl/*.sv"],
        "include_dirs": ["rtl/inc"],
        "defines": {"SYNTHESIS": None, "WIDTH": 8},
        "parameters": {"W": 8},
        "timescale": "1ns/10ps",
        "clocks": [{"port": "clk_i", "period_ns": 20}, {"port": "clk2", "freq_mhz": 250}],
        "resets": [{"port": "rst_ni"}, {"port": "srst", "sync": True}],
        "tests": {"dir": "test", "simulator": "icarus", "seed": 7},
        "golden": "model/ref.py",
        "pnr": {"utilisation": 30, "die_um": [200, 150], "librelane": {"FP_PDN_VPITCH": 25}},
    }
    p = parse_project(data, rtl)
    assert [s.name for s in p.sources] == ["counter.sv", "top.sv"]  # glob, sorted
    assert p.define_args() == ["SYNTHESIS", "WIDTH=8"]
    assert p.clocks[1].period_ns == 4.0 and p.fastest_clock == p.clocks[1]
    assert p.resets[0].active_low and not p.resets[0].sync
    assert not p.resets[1].active_low and p.resets[1].sync
    assert p.tests.modules == ("test_top",) and p.tests.seed == 7
    assert p.pnr.die_um == (200.0, 150.0) and p.pnr.librelane == {"FP_PDN_VPITCH": 25}
    # The mapping a project writes reads back to the same project.
    again = parse_project(yaml.safe_load(yaml.safe_dump(p.to_dict())), rtl)
    assert again == p


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"speed": 1}, "unknown top-level keys: speed"),
        ({"macros": {}}, "macros must be a list of {instance, cell, at_um, orientation}"),
        ({"chip": {}}, "chip.pads: needs at least one vdd, vss, iovdd, iovss pad"),
        ({"schema": "project/v9"}, "schema 'project/v9' is not supported"),
        ({"top": "2bad"}, "top must be a Verilog identifier"),
        ({"sources": ["rtl/missing.sv"]}, "sources: rtl/missing.sv is not a file"),
        ({"sources": ["rtl/*.vhd"]}, "sources: rtl/*.vhd matches no files"),
        ({"tech": "7nm"}, "tech: '7nm' is not one of"),
        ({"parameters": {"W": "eight"}}, "parameters.W must be an integer"),
        ({"clocks": [{"port": "clk_i"}]}, "clocks[0] needs period_ns or freq_mhz"),
        ({"clocks": [{"port": "clk_i", "period_ns": -1}]}, "clocks[0].period_ns must be"),
        ({"resets": [{"port": "rst_ni", "active": "sideways"}]}, "resets[0].active must be"),
        ({"tests": {"dir": "rtl"}}, "tests.dir: no test_*.py files in rtl"),
        ({"tests": {"dir": "test", "modules": ["test_x"]}}, "test_x.py not found"),
        ({"tests": {"dir": "test", "simulator": "modelsim"}}, "tests.simulator must be"),
        ({"pnr": {"utilisation": 120}}, "pnr.utilisation must be at most 95"),
        ({"pnr": {"librelane": {"lower": 1}}}, "pnr.librelane must map"),
        ({"timescale": "1ns"}, "timescale must look like 1ns/1ps"),
        ({"golden": "rtl/top.sv"}, "golden must be a Python file"),
        ({"name": "has space"}, "name 'has space' must be"),
    ],
)
def test_errors_name_the_key(rtl, change, message):
    data = {"top": "top", "sources": ["rtl/counter.sv", "rtl/top.sv"], **change}
    with pytest.raises(ProjectError) as exc:
        parse_project(data, rtl)
    assert message in str(exc.value)


def test_filelist(rtl):
    write(
        rtl,
        {
            "rtl/files.f": (
                "// packages first\n"
                "+incdir+inc\n"
                "+define+FAST+DEPTH=4\n"
                "-f more.f   # nested, relative to this file\n"
                "top.sv\n"
            ),
            "rtl/more.f": "-DSIM\ncounter.sv\n",
        },
    )
    fl = parse_filelist(rtl / "rtl/files.f")
    assert [s.name for s in fl.sources] == ["counter.sv", "top.sv"]
    assert [d.name for d in fl.include_dirs] == ["inc"]
    assert fl.defines == {"FAST": None, "DEPTH": "4", "SIM": None}
    p = parse_project({"top": "top", "filelist": "rtl/files.f"}, rtl)
    assert [s.name for s in p.sources] == ["counter.sv", "top.sv"]
    assert {f.name for f in p.filelists} == {"files.f", "more.f"}
    write(rtl, {"rtl/bad.f": "-y lib\n"})
    with pytest.raises(ProjectError, match="option -y is not supported"):
        parse_filelist(rtl / "rtl/bad.f")


def test_find_and_load(rtl):
    assert find_project(rtl) is None
    (rtl / PROJECT_FILE).write_text("top: top\nsources: [rtl/counter.sv, rtl/top.sv]\n")
    assert find_project(rtl) == rtl / PROJECT_FILE
    other = rtl / "alt.yaml"
    other.write_text(f"schema: {SCHEMA}\ntop: top\nsources: [rtl/top.sv, rtl/counter.sv]\n")
    assert find_project(other) == other
    (rtl / "spec.yaml").write_text("name: x\nworkload: {llm: {model: smollm2-135m}}\n")
    assert find_project(rtl / "spec.yaml") is None
    assert load_project(rtl).top == "top"
    (rtl / PROJECT_FILE).write_text("top: top\nsources: [nope.sv]\n")
    with pytest.raises(ProjectError, match=r"project\.yaml: sources: nope\.sv is not a file"):
        load_project(rtl)


def test_inputs_digest_follows_every_input(rtl):
    data = {
        "top": "top",
        "sources": ["rtl/counter.sv", "rtl/top.sv"],
        "include_dirs": ["rtl/inc"],
        "tests": {"dir": "test"},
        "golden": "model/ref.py",
    }
    (rtl / PROJECT_FILE).write_text(yaml.safe_dump(data))
    p = load_project(rtl)
    names = {p.rel(f) for f in p.input_files()}
    assert {"project.yaml", "rtl/top.sv", "rtl/inc/defs.svh", "test/test_top.py"} <= names
    assert "model/ref.py" in names
    first = p.inputs_digest()
    assert p.inputs_digest() == first
    for rel in ("rtl/top.sv", "rtl/inc/defs.svh", "test/test_top.py", "model/ref.py"):
        f = rtl / rel
        old = f.read_text()
        f.write_text(old + "\n")
        assert p.inputs_digest() != first, rel
        f.write_text(old)
    assert p.inputs_digest() == first


def test_init_guesses_and_writes(rtl):
    path, notes = init_project(rtl)
    assert path == rtl.resolve() / PROJECT_FILE
    p = load_project(rtl)
    assert p.top == "top" and {s.name for s in p.sources} == {"counter.sv", "top.sv"}
    assert p.clocks[0].port == "clk_i" and p.clocks[0].period_ns == 10.0
    assert p.resets[0].port == "rst_ni" and p.resets[0].active_low
    assert p.tests.dir is not None and p.tests.dir.name == "test"
    text = path.read_text()
    assert text.startswith("# ChipLeap project") and "top: top, the module" in text
    assert any("clock: clk_i" in n for n in notes)
    with pytest.raises(ProjectError, match="exists; use --force"):
        init_project(rtl)
    path, _ = init_project(
        rtl, top="counter", sources=["rtl/counter.sv"], clocks=[("clk_i", 5.0)], force=True
    )
    p = load_project(path)
    assert p.top == "counter" and p.clocks[0].period_ns == 5.0


def test_init_needs_a_clear_top(tmp_path):
    write(tmp_path, {"a.sv": "module a; endmodule\n", "b.sv": "module b; endmodule\n"})
    with pytest.raises(ProjectError, match="candidates: a, b"):
        init_project(tmp_path)
