"""cocotb tests of RTL projects on Icarus and Verilator."""

import json

import pytest

from chipleap.rtl import sim as sim_mod
from chipleap.rtl.project import load_project
from chipleap.rtl.sim import (
    SimUnavailable,
    choose,
    cocotb_python,
    parse_results,
    run_tests,
    simulate,
)
from chipleap.verify.tools import Tool, iverilog, verilator

needs_icarus = pytest.mark.skipif(
    cocotb_python() is None or not iverilog().found, reason="needs cocotb and Icarus"
)


def _verilator_new_enough() -> bool:
    v = verilator()
    return v.found and sim_mod._version_tuple(v.version) >= sim_mod.VERILATOR_MIN


@needs_icarus
def test_passing_tests(counter_project):
    p = load_project(counter_project)
    r = simulate(p, "icarus")
    assert r.status == "pass", r.summary
    assert r.summary.startswith("Icarus") and "2 of 2 cocotb tests pass" in r.summary
    names = [t["name"] for t in r.data["tests"]]
    assert names == ["counts_when_enabled", "holds_when_disabled"]
    assert r.data["tests"][0]["module"] == "test_counter"
    assert r.data["tests"][0]["file"] == "test/test_counter.py"
    # Results carry no machine-specific paths, so a replay compares them exactly.
    assert str(counter_project) not in json.dumps(r.data)
    assert simulate(p, "icarus").data == r.data


@needs_icarus
def test_failing_test_is_named(counter_project):
    t = counter_project / "test/test_counter.py"
    t.write_text(
        t.read_text().replace(
            "assert int(dut.q_o.value) == 0",
            "assert int(dut.q_o.value) == 3, 'held the wrong value'",
        )
    )
    r = simulate(load_project(counter_project), "icarus")
    assert r.status == "fail"
    assert "1 of 2 cocotb tests fail: test_counter.holds_when_disabled" in r.summary
    assert "held the wrong value" in r.summary
    d = r.data["diagnostics"]
    assert len(d) == 1 and d[0]["test"] == "test_counter.holds_when_disabled"
    line = next(i for i, ln in enumerate(t.read_text().splitlines(), 1) if "held the wrong" in ln)
    assert d[0]["file"] == "test/test_counter.py" and d[0]["line"] == line
    assert r.data["passed"] == 1 and r.data["failed"] == 1


@needs_icarus
def test_build_errors_point_at_the_rtl(counter_project):
    src = counter_project / "rtl/counter.sv"
    src.write_text(src.read_text().replace("q_o <= '0;", "q_o <= '0"))
    r = simulate(load_project(counter_project), "icarus")
    assert r.status == "fail" and "does not build" in r.summary
    d = r.data["diagnostics"][0]
    assert d["tool"] == "icarus" and d["file"] == "rtl/counter.sv" and d["line"] in (10, 11)


@needs_icarus
def test_test_filter(counter_project):
    run = run_tests(load_project(counter_project), "icarus", test_filter="holds")
    assert [t.name for t in run.tests] == ["holds_when_disabled"] and run.ok


@pytest.mark.skipif(
    cocotb_python() is None or not _verilator_new_enough(),
    reason="needs cocotb and Verilator 5.036 or newer (CHIPLEAP_VERILATOR)",
)
def test_verilator(counter_project):
    r = simulate(load_project(counter_project), "verilator")
    assert r.status == "pass", r.summary
    assert r.summary.startswith("Verilator")


def test_simulator_choice(counter_project, monkeypatch):
    p = load_project(counter_project)
    old = Tool("verilator", "/usr/bin/verilator", "5.020", "")
    monkeypatch.setattr(sim_mod, "verilator", lambda: old)
    key, why = choose(p, "verilator", "2.1.0")
    assert key == "verilator" and "older than 5.036" in why
    assert choose(p, "verilator", "1.9.2") == ("verilator", "")
    monkeypatch.setattr(sim_mod, "iverilog", lambda: Tool("iverilog", None, "", ""))
    assert choose(p, "auto", "2.1.0")[0] == "verilator"
    assert choose(p, "xcelium") == ("xcelium", "")
    no = Tool("verilator", None, "", "")
    monkeypatch.setattr(sim_mod, "verilator", lambda: no)
    assert "neither Icarus nor Verilator" in choose(p, "auto")[1]


def test_no_tests_is_skipped(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text().replace("tests: {dir: test, seed: 3}\n", ""))
    p = load_project(counter_project)
    assert simulate(p).status == "skipped"
    with pytest.raises(SimUnavailable):
        run_tests(p)


def test_parse_results(counter_project):
    p = load_project(counter_project)
    root = str(counter_project)
    xml = f"""<testsuites><testsuite name="t">
<testcase classname="test_counter" name="ok" time="0.1">
 <properties><property name="file" value="{root}/test/test_counter.py"/>
 <property name="line" value="9"/><property name="sim_time_unit" value="ps"/>
 <property name="sim_time_duration" value="70000"/></properties></testcase>
<testcase classname="test_counter" name="bad" time="0.2">
 <failure message="q is 4" type="AssertionError">Traceback (most recent call last):
  File "{root}/test/test_counter.py", line 30, in bad
    check(dut)
  File "{root}/test/helpers.py", line 4, in check
    assert False, "q is 4"
  File "/usr/lib/python3/site-packages/cocotb/x.py", line 1, in inner
AssertionError: q is 4</failure>
 <properties><property name="file" value="{root}/test/test_counter.py"/>
 <property name="line" value="25"/></properties></testcase>
<testcase classname="test_counter" name="later" time="0"><skipped/></testcase>
</testsuite></testsuites>"""
    ok, bad, later = parse_results(xml, p)
    assert (ok.status, ok.file, ok.line, ok.sim_time_ns) == (
        "pass",
        "test/test_counter.py",
        9,
        70.0,
    )
    assert (bad.status, bad.message, bad.file, bad.line) == ("fail", "q is 4", "test/helpers.py", 4)
    assert later.status == "skip" and later.full_name == "test_counter.later"
    staged = xml.replace(root, "/tmp/chipleap-abc/src")
    assert parse_results(staged, p, staged=True)[1].file == "test/helpers.py"
