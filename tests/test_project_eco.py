"""ECO: the base state of a routed run, the edits OpenROAD applies, the resumed flow, and
the timing paths to choose changes from; a real ECO when LibreLane runs are allowed."""

import json
import os

import pytest

from chipleap.rtl import eco as E
from chipleap.rtl.project import EcoChange, load_project
from chipleap.verify.tools import librelane, openroad

STEPS = [
    "Yosys.Synthesis",
    "OpenROAD.ResizerTimingPostGRT",
    "OpenROAD.DetailedRouting",
    "OpenROAD.FillInsertion",
    "KLayout.DRC",
    "Netgen.LVS",
]

REPORT = """
CHIPLEAP-PATHS begin
Startpoint: _255_ (rising edge-triggered flip-flop clocked by clk_i)
Endpoint: _270_ (rising edge-triggered flip-flop clocked by clk_i)
Path Group: clk_i
Path Type: max

  Delay    Time   Description
---------------------------------------------------------
   0.0000    0.0000   clock clk_i (rise edge)
   0.3309    0.3309   clock network delay (propagated)
   0.0000    0.3309 ^ _255_/CLK (sg13g2_dfrbpq_2)
   0.2756    0.6065 ^ _255_/Q (sg13g2_dfrbpq_2)
   0.3360    0.9425 v _150_/Y (sg13g2_nand4_1)
   0.0001    0.9426 v _270_/D (sg13g2_dfrbpq_1)
             0.9426   data arrival time

  10.0000   10.0000   clock clk_i (rise edge)
   0.3000   10.3000   clock network delay (propagated)
   0.0000   10.3000 ^ _270_/CLK (sg13g2_dfrbpq_1)
            -0.1000   library setup time
             9.8000   data required time
---------------------------------------------------------
             8.8574   slack (MET)

Startpoint: tx_valid_i (input port clocked by clk_i)
Endpoint: _1_ (rising edge-triggered flip-flop clocked by clk_i)
Path Group: clk_i
Path Type: max
   0.0000    0.0000   clock clk_i (rise edge)
   2.0000    2.0000 ^ tx_valid_i (in)
   0.5000    2.5000 ^ _1_/D (sg13g2_dfrbpq_1)
             2.5000   data arrival time
            -0.2500   slack (VIOLATED)
CHIPLEAP-PATHS end
"""


def routed(project_dir, state=None):
    run = project_dir / "build/pnr/runs/chipleap"
    for n, step in enumerate(STEPS, 1):
        d = run / f"{n:02d}-{step.lower().replace('.', '-')}"
        d.mkdir(parents=True)
        (d / "config.json").write_text(json.dumps({"meta": {"step": step}}))
    route = run / "03-openroad-detailedrouting"
    (route / "counter.def").write_text("VERSION 5.8 ;\n")
    (route / "state_in.json").write_text(
        json.dumps(
            state or {"def": str(route / "counter.def"), "sdf": {"typ": None}, "metrics": {"a": 1}}
        )
    )
    (run / "final").mkdir()
    (run / "final/metrics.json").write_text("{}")
    (project_dir / "build/pnr/config.json").write_text(json.dumps({"DESIGN_NAME": "counter"}))
    return run


def test_base_resumes_at_detailed_routing(counter_project):
    p = load_project(counter_project)
    run = routed(counter_project)
    got_run, state, steps = E.base(p)
    assert got_run == run and steps == STEPS[2:]
    assert state["def"].endswith("counter.def") and state["metrics"] == {"a": 1}


def test_base_refuses_files_outside_the_run(counter_project, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("x\n")
    routed(counter_project, {"def": str(secret)})
    with pytest.raises(E.OpenroadError, match="not a file of the run"):
        E.base(load_project(counter_project))


def test_edits_and_the_resumed_flow(counter_project):
    routed(counter_project)
    p = load_project(counter_project)
    changes = [
        EcoChange("resize", "_150_", "sg13g2_nand4_2"),
        EcoChange("buffer", "\\u_rx.q[3]/D", "sg13g2_buf_2"),
        EcoChange("repair", "setup", "", 0.5),
        EcoChange("repair", "hold"),
    ]
    assert E.changes_file(changes) == (
        "resize\t_150_\tsg13g2_nand4_2\nbuffer\t\\u_rx.q[3]\tD\tsg13g2_buf_2\n"
    )
    cfg = E.eco_config(p, changes, STEPS[2:])
    assert cfg["meta"]["flow"] == [*E.PREFIX, E.REPAIR, *STEPS[2:]]
    assert cfg["GRT_RESIZER_SETUP_SLACK_MARGIN"] == 0.5
    assert cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] == 0.1
    assert E.eco_config(p, changes[:1], STEPS[2:])["meta"]["flow"][3] == "OpenROAD.DetailedRouting"
    # Names reach OpenROAD as data, never as script text.
    script = E.edit_script({"tech_lef": "t.lef", "cell_lefs": ["c.lef"]}, "in.def")
    assert "_150_" not in script and 'open "changes.txt" r' in script


def test_edit_results(counter_project):
    edits = [
        EcoChange("resize", "_1_", "sg13g2_inv_2"),
        EcoChange("buffer", "_2_/A", "sg13g2_buf_1"),
    ]
    log = "CHIPLEAP-ECO done 0 sg13g2_inv_1\nCHIPLEAP-ECO done 1 eco_buf_0\nCHIPLEAP-ECO end\n"
    assert E.parse_edits(log, edits) == ["sg13g2_inv_1", "eco_buf_0"]
    with pytest.raises(E.EcoError, match="resize _1_ to sg13g2_inv_2: the new cell's pins differ"):
        E.parse_edits("CHIPLEAP-ECO error 0 pins-differ sg13g2_a21oi_1\n", edits)
    with pytest.raises(E.EcoError, match="no instance of that name"):
        E.parse_edits("CHIPLEAP-ECO error 1 no-instance\n", edits)


def test_timing_paths_report():
    paths = E.parse_paths(REPORT, "max")
    assert [(p.startpoint, p.endpoint, p.slack_ns, p.met) for p in paths] == [
        ("_255_", "_270_", 8.8574, True),
        ("tx_valid_i", "_1_", -0.25, False),
    ]
    stages = paths[0].stages
    assert [(s.instance, s.cell) for s in stages] == [
        ("_255_", "sg13g2_dfrbpq_2"),
        ("_255_", "sg13g2_dfrbpq_2"),
        ("_150_", "sg13g2_nand4_1"),
        ("_270_", "sg13g2_dfrbpq_1"),
    ]
    assert paths[0].to_dict()["stages"][2] == {
        "pin": "_150_/Y",
        "instance": "_150_",
        "cell": "sg13g2_nand4_1",
        "edge": "fall",
        "delay_ns": 0.336,
        "time_ns": 0.9425,
    }
    assert paths[1].stages[0].pin == "tx_valid_i" and paths[1].stages[0].instance == ""


def test_step_skips(counter_project):
    p = load_project(counter_project)
    assert E.eco(p).status == "skipped"
    r = E.eco(p, [EcoChange("repair", "setup")])
    assert r.status == "skipped" and "no routed design" in r.summary


@pytest.mark.pnr
@pytest.mark.skipif(
    not (librelane().found and openroad().found), reason="needs LibreLane, OpenROAD"
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_PNR"), reason="set CHIPLEAP_RUN_PNR=1")
def test_counter_eco_signs_off_again(counter_project):
    from chipleap.rtl.pnr import pnr

    p = load_project(counter_project)
    assert pnr(p).status == "pass"
    paths, run = E.timing_paths(p, "slow", "setup", 3)
    assert run == "build/pnr/runs/chipleap" and paths
    cell = next(s for s in paths[0].stages if s.cell.endswith("_1") and s.instance)
    bigger = cell.cell[:-1] + "2"
    r = E.eco(p, [EcoChange("resize", cell.instance, bigger)])
    assert r.status == "pass", r.summary
    assert r.data["changes"][0]["result"] == f"was {cell.cell}"
    assert r.data["after"]["lvs_errors"] == 0
