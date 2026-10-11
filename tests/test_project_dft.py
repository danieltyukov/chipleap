"""DFT: the scan script, OpenROAD's plan, Fault's vectors on the chains, the pattern files
and the testbench; a real run when OpenROAD, Fault and Icarus 13 are installed."""

import json
import os

import pytest

from chipleap.rtl import dft as D
from chipleap.rtl.netlist import Chain, parse_netlist
from chipleap.rtl.pnr import config
from chipleap.rtl.project import load_project
from chipleap.rtl.provenance import file_digest, record
from chipleap.verify.tools import fault, gl_iverilog, openroad, quaigh

SCAN = r"""
module counter (clk_i, rst_ni, en_i, q_o, scan_enable_0, scan_in_0, scan_out_0);
 input clk_i;
 input rst_ni;
 input en_i;
 output [1:0] q_o;
 input scan_enable_0;
 input scan_in_0;
 output scan_out_0;
 sg13g2_sdfrbpq_1 _5_ (.Q(q_o[0]), .CLK(clk_i), .RESET_B(rst_ni), .SCD(scan_in_0), .D(_1_), .SCE(scan_enable_0));
 sg13g2_sdfrbpq_1 _6_ (.Q(q_o[1]), .CLK(clk_i), .RESET_B(rst_ni), .SCD(q_o[0]), .D(_2_), .SCE(scan_enable_0));
 sg13g2_xor2_1 _7_ (.A(q_o[0]), .B(en_i), .X(_1_));
 sg13g2_and2_1 _8_ (.A(q_o[0]), .B(en_i), .X(_3_));
 sg13g2_xor2_1 _9_ (.A(q_o[1]), .B(_3_), .X(_2_));
 assign scan_out_0 = q_o[1];
endmodule
"""

PLAN_LOG = """
CHIPLEAP-DFT-PLAN
***************************
Report DFT Plan
Number of chains: 1
Clock domain: No Mix
***************************

Scan chain 'chain_0' has 2 cells (2 bits)

  _5_ (clk_i, rising)
  _6_

CHIPLEAP-DFT-END
"""

# Fault's vectors for the counter cut at its flip-flops: inputs en_i, _5_, _6_; outputs
# q_o[1:0], then each flip-flop's D.
VECTORS = {
    "inputs": [
        {"name": "en_i", "from": 0, "to": 0},
        {"name": "_5_", "from": 0, "to": 0},
        {"name": "_6_", "from": 0, "to": 0},
    ],
    "outputs": [
        {"name": "q_o", "from": 1, "to": 0},
        {"name": "\\_5_.d", "from": 0, "to": 0},
        {"name": "\\_6_.d", "from": 0, "to": 0},
    ],
    "coverageList": [
        # en=1, q=01: q_o = 01 -> bits q_o[0]=1, q_o[1]=0; next _5_ = 0, _6_ = 1.
        {"vector": [["+", 1], ["+", 1], ["+", 0]], "goldenOutput": format(0b1001, "x")},
        {"vector": [["+", 0], ["+", 0], ["+", 1]], "goldenOutput": format(0b1010, "x")},
    ],
}


@pytest.fixture
def scan_counter(counter_project, tmp_path):
    p = load_project(counter_project)
    top = parse_netlist(SCAN).top("counter")
    chains, clocks = D.chains_of(top, ["clk_i"])
    scan = D.ScanResult(
        tmp_path / "counter.scan.v",
        tmp_path / "pre.v",
        tuple(chains),
        clocks,
        "scan_enable_0",
        2,
        (),
        {},
    )
    return p, top, scan


def test_scan_script_quotes_every_value(counter_project):
    p = load_project(counter_project)
    files = {
        "tech_lef": "inputs/t.lef",
        "cell_lefs": ["inputs/c.lef"],
        "liberty": {"typ": "inputs/l.lib"},
    }
    text = D.scan_script(
        p, files, "inputs/counter.synth.v", D.DftConfig(max_chain_length=20, max_chains=2)
    )
    assert "read_verilog {inputs/counter.synth.v}" in text and "link_design {counter}" in text
    assert "create_clock -name {clk_i} -period 10 [get_ports {clk_i}]" in text
    assert "set_dft_config -max_length 20 -clock_mixing {no_mix} -max_chains 2" in text
    assert "execute_dft_plan" in text and "insert_dft" in text
    assert text.index("scan_replace") < text.index("execute_dft_plan")


def test_plan_and_trace(scan_counter):
    _, _, scan = scan_counter
    assert D.parse_plan(PLAN_LOG) == {"chain_0": ["_5_", "_6_"]}
    assert scan.chains == (Chain("chain_0", "scan_in_0", "scan_out_0", ("_5_", "_6_")),)
    assert scan.clocks == {"chain_0": "clk_i"}


def test_vectors_on_the_chain(scan_counter):
    _, top, scan = scan_counter
    pats = D.map_patterns(VECTORS, scan, top)
    # Sorted by vector: [0,0,1] before [1,1,0].
    assert [p.load["chain_0"] for p in pats] == ["01", "10"]
    assert pats[1].inputs == {"en_i": "1"}
    assert pats[1].outputs == {"q_o[0]": "1", "q_o[1]": "0"}
    assert pats[1].unload == {"chain_0": "01"}
    broken = {**VECTORS, "coverageList": [{"vector": [["+", 1]], "goldenOutput": "0"}]}
    with pytest.raises(D.DftError, match="one value per input"):
        D.map_patterns(broken, scan, top)


def test_bench_data_shift_order(scan_counter, tmp_path):
    p, top, scan = scan_counter
    pats = D.map_patterns(VECTORS, scan, top)
    bench = D._bench(scan, top, ["clk_i"], ["rst_ni"])
    assert bench.pi == ["en_i"] and bench.po == ["q_o[1]", "q_o[0]"] and bench.shift == 2
    t_flush, shift = D.write_bench_data(bench, pats, tmp_path)
    assert (t_flush, shift) == (10, 2)
    # Load "10" (scan-in cell first): the cell nearest scan-out is shifted in first.
    assert (tmp_path / "shift_in.mem").read_text().split()[2:4] == ["0", "1"]
    # Unload "01": scan-out shows the last cell first.
    assert (tmp_path / "shift_out.mem").read_text().split()[2:4] == ["1", "0"]
    assert (tmp_path / "flush_mask.mem").read_text().split()[:3] == ["0", "0", "1"]
    tb = D.testbench(p, top, scan, bench, t_flush, len(pats))
    assert "counter dut (" in tb and ".scan_in_0(sin[0])" in tb and ".rst_ni(1'b1)" in tb
    assert ".q_o({po_now[0], po_now[1]})" in tb


def test_pattern_files(scan_counter):
    p, top, scan = scan_counter
    pats = D.map_patterns(VECTORS, scan, top)
    data = D.patterns_json(scan, pats, None)
    assert data["chains"][0]["cells"] == ["_5_", "_6_"] and len(data["patterns"]) == 2
    text = D.stil(p, scan, pats, top)
    assert text.startswith("STIL 1.0;") and 'ScanChain "chain_0"' in text
    assert 'ScanCells "_5_" "_6_";' in text and text.count('Call "capture"') == 2


def test_simulation_log():
    log = "CHIPLEAP-SCAN flush-done errors=0\nCHIPLEAP-SCAN unload pattern=1 chain=0 cycle=1 got=0 want=1\nCHIPLEAP-SCAN done patterns=2 errors=1\n"
    assert D.parse_sim(log) == (True, 2, 1, ["unload pattern=1 chain=0 cycle=1 got=0 want=1"], True)
    assert D.parse_sim("crashed")[0] is False


def test_coverage_counts_both_stuck_at_values(tmp_path):
    vectors = tmp_path / "v.json"
    vectors.write_text(json.dumps(VECTORS))
    cov = "ratio: 0.75\nfaultPoints: [a, b]\nsa0Covered: [a, b]\nsa1Covered: [a]\nsa0Uncovered: [a]\nsa1Uncovered: [b]\n"
    log = "Kept 2 patterns, detecting 3/4 faults (75.00% coverage)\n"
    r = D.parse_atpg(log, cov, vectors, "0.9.4")
    assert (r.coverage_pct, r.faults, r.detected, r.patterns, r.generator_coverage_pct) == (
        75.0,
        4,
        3,
        2,
        75.0,
    )
    assert D.parse_atpg(log, cov.replace("0.75", "1e+0"), vectors).coverage_pct == 100.0


def test_step_needs_a_section(counter_project):
    r = D.dft(load_project(counter_project))
    assert r.status == "skipped" and "no dft section" in r.summary


def test_pnr_routes_the_scan_netlist(counter_project, tmp_path, monkeypatch):
    (counter_project / "project.yaml").write_text(
        (counter_project / "project.yaml").read_text() + "dft: {}\n"
    )
    p = load_project(counter_project)
    scanned = tmp_path / "counter.scan.v"
    scanned.write_text(SCAN)
    sealed = record(tmp_path, D.SCAN_MANIFEST, "scan", {scanned.name: file_digest(scanned)})
    fake = D.ScanResult(scanned, scanned, (), {}, "scan_enable_0", 2, (), {}, None, sealed)
    monkeypatch.setattr(D, "insert_scan", lambda project, work: fake)
    (tmp_path / "pnr").mkdir()
    cfg = config(p, tmp_path / "pnr")
    assert cfg["VERILOG_FILES"] == ["dir::src/counter.scan.v"] and cfg["RUN_LINTER"] is False
    assert "SYNTH_PARAMETERS" not in cfg and (tmp_path / "pnr/src/counter.scan.v").is_file()


needs_tools = pytest.mark.skipif(
    not (openroad().found and fault().found and quaigh().found and gl_iverilog().found),
    reason="needs OpenROAD, Fault, Quaigh and Icarus 13",
)


@needs_tools
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_IMPL"), reason="set CHIPLEAP_RUN_IMPL=1")
def test_counter_gets_a_scan_chain(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "dft: {max_chain_length: 4, patterns: 4}\n")
    r = D.dft(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert r.data["scanned"] == r.data["flops"] == 6 and len(r.data["chains"]) == 2
    assert r.data["atpg"]["coverage_pct"] > 90
    assert r.data["simulation"]["flush"] == "pass" and r.data["simulation"]["errors"] == 0
