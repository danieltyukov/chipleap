"""Equivalence of a project's RTL and netlists: proof by matched points, then a bounded check
from reset of whatever the proof leaves."""

import dataclasses
import shutil
from pathlib import Path

import pytest

from chipleap.rtl import equiv
from chipleap.rtl.project import load_project
from chipleap.rtl.synth import liberty_for
from chipleap.verify.tools import eqy, yosys

ROUTED = Path(__file__).parent / "fixtures/uart_routed"
EXAMPLE = Path(__file__).parents[1] / "examples/uart"
needs_yosys = pytest.mark.skipif(
    not yosys().found or liberty_for("ihp-sg13g2") is None,
    reason="needs Yosys and the IHP SG13G2 liberty",
)
needs_eqy = pytest.mark.skipif(not eqy().found, reason="needs EQY (CHIPLEAP_EQY)")


def side(script: list[str]) -> equiv.Side:
    return equiv.Side("a design", script, [])


def test_scripts_come_from_checked_values():
    gold = side(['read_verilog -sv "/x y/counter.sv"', "synth -top counter -flatten"])
    gate = side(['read_liberty -ignore_miss_func "/l/c.lib"', 'read_verilog "/x y/n.v"'])
    text = equiv.yosys_script(gold, gate, "counter", 5)
    assert 'read_verilog -sv "/x y/counter.sv"' in text
    assert "equiv_make gold gate equiv" in text and "equiv_induct -seq 5" in text
    assert text.count("async2sync") == 2 and text.rstrip().endswith("equiv_status")
    miter = equiv.miter_script(gold, gate, "counter", 30, [("rst_ni", True)], Path("/w/cex.vcd"))
    assert "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter" in miter
    assert "sat -seq 30 -set-at 1 in_rst_ni 0" in miter and '-dump_vcd "/w/cex.vcd"' in miter
    eqy_text = equiv.eqy_text(gold, gate, "counter")
    assert eqy_text.startswith("[gold]\n") and "[strategy sat]\nuse sat\n" in eqy_text
    assert "gate-nomatch net*" in eqy_text
    with pytest.raises(ValueError):
        equiv.yosys_script(gold, gate, "counter; exec id", 5)
    with pytest.raises(ValueError):
        equiv.miter_script(gold, gate, "counter", 30, [("rst; x", True)], Path("/w/c.vcd"))


YOSYS_LOG = """\
Found 75 $equiv cells in equiv:
  Of those cells 74 are proven and 1 are unproven.
  Unproven $equiv $auto$equiv_make.cc:294:find_same_wires$2220: \\u_rx.state_q_gold [2] \\u_rx.state_q_gate [2]
Found a total of 1 unproven $equiv cells.
"""
EQY_LOG = """\
EQY 20:18:28 [rs2] Successfully proved equivalence of partition uart.u_tx.valid_i
EQY 20:18:28 [rs2] Warning: Failed to prove equivalence for 2/76 partitions:
EQY 20:18:28 [rs2] Failed to prove equivalence of partition uart.u_tx.tick_q.3
EQY 20:18:28 [rs2] Failed to prove equivalence of partition uart.tx_o
EQY 20:18:28 [rs2] DONE (FAIL, rc=2)
"""


def test_the_logs_say_what_was_proved():
    assert equiv.parse_yosys(YOSYS_LOG) == {
        "points": 75,
        "proved": 74,
        "unproven": ["u_rx.state_q[2]"],
    }
    assert (
        equiv.parse_yosys(
            "Equivalence successfully proven!\nFound 9 $equiv cells in equiv:\n"
            "  Of those cells 9 are proven and 0 are unproven.\n"
        )["proved"]
        == 9
    )
    assert equiv.parse_eqy(EQY_LOG) == {
        "points": 76,
        "proved": 74,
        "unproven": ["uart.tx_o", "uart.u_tx.tick_q.3"],
    }
    assert (
        equiv.parse_eqy("EQY [x] Successfully proved designs equivalent\nDONE (PASS, rc=0)\n")[
            "unproven"
        ]
        == []
    )
    assert equiv.parse_sat("SAT proof finished - no model found: SUCCESS!\n") == "equal"
    assert equiv.parse_sat("SAT proof finished - model found: FAIL!\n") == "differs"
    assert equiv.parse_sat("ERROR: Can't find cell\n") == "error"


def test_place_and_route_pairs_need_a_routed_run(counter_project):
    r = equiv.check_pair(load_project(counter_project), "synth-pnr")
    assert r["status"] == "skipped" and "--steps pnr" in r["reason"]


def changed_gate(netlist: Path, dest: Path) -> Path:
    """The netlist with its first AND gate turned into an OR gate."""
    text = netlist.read_text()
    cell = next(c for c in ("sg13g2_and2_1", "sg13g2_nand2_1", "sg13g2_and3_1") if c in text)
    swap = {"sg13g2_and2_1": "sg13g2_or2_1", "sg13g2_nand2_1": "sg13g2_nor2_1"}
    dest.write_text(text.replace(cell, swap.get(cell, "sg13g2_or3_1"), 1))
    return dest


@needs_yosys
@pytest.mark.parametrize("engine", ["yosys", pytest.param("eqy", marks=needs_eqy)])
def test_the_counter_netlist_equals_its_rtl_and_a_changed_gate_does_not(counter_project, engine):
    p = load_project(counter_project)
    p = dataclasses.replace(p, equivalence=dataclasses.replace(p.equivalence, engine=engine))
    good = equiv.check_pair(p, "rtl-synth")
    assert good["status"] == "proved", good
    assert good["proved"] == good["points"] > 0 and good["engine"] == engine
    netlist = counter_project / "build/synth/counter.synth.v"
    bad = equiv.check_pair(p, "rtl-synth", gate_netlist=changed_gate(netlist, netlist))
    assert bad["status"] == "differs", bad
    # The counterexample starts from reset and ends where the outputs disagree.
    assert (counter_project / bad["trace"]).is_file()
    signals = bad["values"]["signals"]
    last = bad["values"]["steps"] - 1
    assert signals["gold_q_o"][last] != signals["gate_q_o"][last]
    assert signals["in_rst_ni"][0] == 0


@needs_yosys
def test_place_and_route_kept_the_uart_logic(tmp_path):
    root = tmp_path / "uart"
    shutil.copytree(EXAMPLE, root, ignore=shutil.ignore_patterns("build"))
    run = root / "build/pnr/runs/chipleap"
    (run / "06-yosys-synthesis").mkdir(parents=True)
    (run / "final/nl").mkdir(parents=True)
    shutil.copy(ROUTED / "uart.synth.nl.v", run / "06-yosys-synthesis/uart.nl.v")
    shutil.copy(ROUTED / "uart.nl.v", run / "final/nl/uart.nl.v")
    r = equiv.check_pair(load_project(root), "synth-pnr")
    assert r["status"] == "proved", r
    assert r["points"] > 500 and r["gate"] == "build/pnr/runs/chipleap/final/nl/uart.nl.v"


@needs_yosys
def test_the_step_reports_every_pair(counter_project):
    r = equiv.equiv(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert [x["pair"] for x in r.data["pairs"]] == ["rtl-synth"]
    assert "rtl-synth: proved" in r.summary
