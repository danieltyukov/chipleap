"""Yosys synthesis of RTL projects."""

import pytest

from chipleap.rtl.project import load_project
from chipleap.rtl.synth import liberty_for, synth
from chipleap.verify.tools import sv2v, yosys

pytestmark = [
    pytest.mark.synth,
    pytest.mark.skipif(
        not yosys().found or liberty_for("ihp-sg13g2") is None,
        reason="needs Yosys and the IHP SG13G2 liberty",
    ),
]


def test_synthesizes_with_parameters(counter_project):
    r = synth(load_project(counter_project))
    assert r.status == "pass", r.summary
    d = r.data
    assert d["flops"] == 6  # W = 6 from the project's parameters
    assert d["area_um2"] > 0 and d["cells"] > d["flops"]
    assert d["target_ns"] == 5.0 and d["clock_mhz"] == 100.0
    assert d["liberty"] == "sg13g2_stdcell_typ_1p20V_25C.lib"
    assert d["netlist"] == "build/synth/counter.synth.v"
    assert (counter_project / d["netlist"]).read_text().count("sg13g2_") > 6
    assert d["diagnostics"] == []  # the liberty's own warnings are not the design's
    assert "within the 100 MHz clock on clk_i" in r.summary
    assert synth(load_project(counter_project)).data == d  # deterministic


def test_errors_point_at_the_source(counter_project):
    src = counter_project / "rtl/counter.sv"
    src.write_text(src.read_text().replace("q_o <= '0;", "q_o <= '0"))
    r = synth(load_project(counter_project))
    assert r.status == "fail"
    first = r.data["diagnostics"][0]
    assert first["file"] == "rtl/counter.sv" and first["line"] in (10, 11)
    assert first["tool"] == ("sv2v" if sv2v().found else "yosys")


def test_missing_liberty_skips(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "tech: generic-28nm\n")
    r = synth(load_project(counter_project))
    assert r.status == "skipped" and "no liberty file for generic-28nm" in r.summary
