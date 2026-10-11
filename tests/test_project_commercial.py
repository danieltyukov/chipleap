"""RTL projects on commercial tools: scripts, staging and a stand-in for a remote run."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from chipleap.rtl import commercial
from chipleap.rtl import sim as sim_mod
from chipleap.rtl.project import load_project
from chipleap.rtl.verify import verify_project
from chipleap.verify.remote import Remote
from chipleap.verify.tools import iverilog


@pytest.fixture
def project(counter_project):
    (counter_project / "rtl/inc").mkdir()
    (counter_project / "rtl/inc/defs.svh").write_text("`define ONE 1\n")
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "include_dirs: [rtl/inc]\ndefines: {FAST: null, N: 3}\n")
    return load_project(counter_project)


def test_genus_script(project, tmp_path):
    files, includes = commercial.stage_rtl(project, tmp_path)
    assert files == ["src/rtl/counter.sv"] and includes == ["src/rtl/inc"]
    assert (tmp_path / "src/rtl/counter.sv").is_file()
    assert (tmp_path / "src/rtl/inc/defs.svh").is_file()
    s = commercial.genus_script(project, files, includes, "lib.lib", "syn", ("t.lef", "c.lef"))
    assert "set_db init_hdl_search_path {src/rtl/inc}" in s
    assert "read_hdl -sv -define {FAST N=3} {src/rtl/counter.sv}" in s
    assert "elaborate counter -parameters {{W 6}}" in s
    assert "create_clock -name clk_i -period 10 [get_ports clk_i]" in s
    assert "[remove_from_collection [all_inputs] [get_ports {clk_i}]]" in s
    assert "read_physical -lef {t.lef c.lef}" in s and "write_hdl > counter_syn.v" in s


def test_dc_script(project):
    s = commercial.dc_script(project, ["src/rtl/counter.sv"], ["src/rtl/inc"], "lib.db", "syn")
    assert "set_app_var search_path [concat $search_path {src/rtl/inc}]" in s
    assert "analyze -format sverilog -define {FAST N=3} {src/rtl/counter.sv}" in s
    assert 'elaborate counter -parameters "W=6"' in s and "current_design counter" in s


def test_missing_tools_skip_with_a_way_forward(project, monkeypatch):
    monkeypatch.setattr(commercial.shutil, "which", lambda name: None)
    r = commercial.synth_step(project, "genus")
    assert r.status == "skipped" and "--remote HOST" in r.summary
    r = commercial.pnr_step(project)
    assert r.status == "skipped" and "Innovus" in r.summary
    r = sim_mod.simulate(project, "xcelium")
    assert r.status == "skipped" and "xrun" in r.summary


@pytest.mark.skipif(
    sim_mod.cocotb_python() is None or not iverilog().found, reason="needs cocotb and Icarus"
)
def test_remote_simulation_is_self_contained(project, monkeypatch):
    """A remote run gets a work directory with everything in it, named relatively. The
    stand-in server runs it on Icarus in place of Xcelium; results map back to the
    project's own paths."""
    seen = {}

    def fake_run_steps(steps, workdir, remote, timeout):
        seen["argv"] = steps[0].argv
        m = json.loads((workdir / "manifest.json").read_text())
        seen["manifest"] = dict(m)
        assert not any(Path(s).is_absolute() for s in m["sources"] + m["python_path"])
        m["simulator"] = "icarus"
        (workdir / "manifest.json").write_text(json.dumps(m))
        env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        cmd = [sys.executable, *steps[0].argv[1:]]
        return subprocess.run(cmd, cwd=workdir, env=env, capture_output=True, text=True)

    monkeypatch.setattr(sim_mod, "run_steps", fake_run_steps)
    t = project.tests.dir / "test_counter.py"
    t.write_text(t.read_text().replace("assert int(dut.q_o.value) == 0", "assert False, 'remote'"))
    r = sim_mod.simulate(load_project(project.root), "xcelium", Remote("server"))
    assert seen["argv"] == ("python3", "cocotb_run.py", "manifest.json")
    assert seen["manifest"]["simulator"] == "xcelium"
    assert seen["manifest"]["sources"] == ["src/rtl/counter.sv"]
    assert seen["manifest"]["includes"] == ["src/rtl/inc"]
    assert seen["manifest"]["python_path"] == ["src/test", "src/model"]
    assert r.status == "fail" and "test_counter.holds_when_disabled" in r.summary
    d = r.data["diagnostics"][0]
    assert d["file"] == "test/test_counter.py" and d["test"] == "test_counter.holds_when_disabled"


def test_verify_project_routes_commercial_steps(project, monkeypatch):
    calls = []
    monkeypatch.setattr(
        commercial, "synth_step", lambda p, tool, remote: calls.append((tool, remote)) or _ok()
    )
    monkeypatch.setattr(
        commercial, "pnr_step", lambda p, remote: calls.append(("innovus", remote)) or _ok()
    )
    srv = Remote("server")
    verify_project(
        project.root, steps="synth,pnr", synth_tool="genus", pnr_tool="innovus", remote=srv
    )
    assert calls == [("genus", srv), ("innovus", srv)]


def _ok():
    from chipleap.verify.result import StepResult

    return StepResult("x", "pass", "ok")


def test_a_simulator_that_stops_early_says_why(project, monkeypatch):
    def fake_run_steps(steps, workdir, remote, timeout):
        (workdir / "outcome.json").write_text('{"stage": "test", "error": "exit 2"}')
        (workdir / "test.log").write_text(
            "xmsim: *F,NOLICN: Unable to checkout license for the simulation.\n"
        )

    monkeypatch.setattr(sim_mod, "run_steps", fake_run_steps)
    r = sim_mod.simulate(project, "xcelium", Remote("server"))
    assert r.status == "fail"
    assert r.summary == (
        "Xcelium: no test results: error: Unable to checkout license for the simulation. [NOLICN]"
    )
    assert r.data["diagnostics"][0]["code"] == "NOLICN"
