"""A tool that fails in an unexpected way fails its step, not the run; odd directories."""

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from chipleap.rtl import commercial
from chipleap.rtl import verify as verify_mod
from chipleap.rtl.lint import lint
from chipleap.rtl.project import load_project
from chipleap.rtl.sim import cocotb_python, simulate
from chipleap.rtl.synth import liberty_for, synth
from chipleap.verify import cpnr
from chipleap.verify.remote import RemoteError
from chipleap.verify.result import StepResult
from chipleap.verify.tools import iverilog, verilator, yosys


def _ok(name):
    return lambda *a, **k: StepResult(name, "pass", "fine")


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["yosys"], 1800),
        FileNotFoundError(2, "No such file or directory", "/nowhere/python"),
        ET.ParseError("no element found: line 3, column 0"),
        json.JSONDecodeError("Expecting value", "", 0),
        RemoteError("cannot reach server over ssh"),
    ],
    ids=["timeout", "missing-program", "truncated-xml", "bad-json", "remote"],
)
def test_a_failing_tool_fails_its_step_only(counter_project, monkeypatch, error):
    def boom(*a, **k):
        raise error

    monkeypatch.setattr(verify_mod, "lint", _ok("lint"))
    monkeypatch.setattr(verify_mod, "simulate", boom)
    monkeypatch.setattr(verify_mod, "synth", _ok("synth"))
    results = verify_mod.verify_project(counter_project, steps="lint,sim,synth")
    assert [(r.name, r.status) for r in results] == [
        ("lint", "pass"),
        ("sim", "fail"),
        ("synth", "pass"),
    ]
    assert results[1].summary.startswith(type(error).__name__)
    out = counter_project / "build/verify"
    assert (out / "report.html").is_file() and (out / "VERIFY.md").is_file()
    assert json.loads((out / "verify.json").read_text())["steps"][1]["status"] == "fail"


def test_commercial_results_reach_the_reports(counter_project, monkeypatch):
    data = {
        "tool": "Genus",
        "area_um2": 3527.2,
        "cells": 142,
        "slack_ns": 6.1,
        "critical_path_ns": 3.9,
        "fmax_mhz_estimate": 256.4,
    }
    monkeypatch.setattr(
        commercial, "synth_step", lambda p, t, r: StepResult("synth", "pass", "Genus", data)
    )
    results = verify_mod.verify_project(counter_project, steps="synth", synth_tool="genus")
    assert results[0].status == "pass"
    assert "setup slack" in (counter_project / "build/verify/VERIFY.md").read_text()


def test_innovus_gets_the_projects_floorplan(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "pnr: {utilisation: 30, density: 70}\n")
    p = load_project(counter_project)
    s = cpnr.script(
        "counter_pnr.v",
        ("t.lef",),
        node_nm=130,
        utilisation=p.pnr.utilisation / 100,
        density=p.pnr.density / 100,
        top=p.top,
    )
    assert "floorPlan -r 1.0 0.3 $margin" in s and "-place_global_max_density 0.7" in s
    s = cpnr.script("x.v", ("t.lef",), node_nm=130, die_um=(150.0, 120.0), top="x")
    assert "floorPlan -d 150 120 $margin" in s
    # The accelerator's script is unchanged.
    s = cpnr.script("x.v", ("t.lef",), node_nm=130)
    assert "floorPlan -r 1.0 0.6 $margin" in s and "max_density" not in s


@pytest.fixture
def spaced(counter_project, tmp_path):
    root = tmp_path / "with space" / "counter"
    shutil.copytree(counter_project, root)
    return root


@pytest.mark.skipif(
    not yosys().found or liberty_for("ihp-sg13g2") is None, reason="needs Yosys and the liberty"
)
def test_synthesis_in_a_directory_with_a_space(spaced):
    r = synth(load_project(spaced))
    assert r.status == "pass", r.summary
    assert r.data["flops"] == 6


@pytest.mark.skipif(not verilator().found, reason="needs Verilator")
def test_lint_in_a_directory_with_a_space(spaced):
    assert lint(load_project(spaced)).status == "pass"


@pytest.mark.skipif(cocotb_python() is None or not iverilog().found, reason="needs cocotb, Icarus")
def test_simulation_in_a_directory_with_a_space(spaced):
    r = simulate(load_project(spaced), "icarus")
    assert r.status == "pass", r.summary
