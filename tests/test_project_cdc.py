"""Clock and reset domain crossings, checked on the Yosys netlist."""

from pathlib import Path

import pytest
import yaml

from chipleap.rtl import cdc
from chipleap.rtl.project import load_project
from chipleap.verify.tools import yosys

FIXTURES = Path(__file__).parent / "fixtures/cdc"
pytestmark = pytest.mark.skipif(not yosys().found, reason="needs Yosys")
TWO_CLOCKS = [{"port": "clk_a", "period_ns": 10}, {"port": "clk_b", "period_ns": 7}]
FIFO_CLOCKS = [{"port": "wclk", "period_ns": 10}, {"port": "rclk", "period_ns": 7}]
# The reading side's input belongs to rclk; the others to wclk, the first clock.
FIFO = {"clocks": FIFO_CLOCKS, "cdc": {"input_domains": {"pop_i": "rclk"}}}


def check(tmp_path: Path, fixture: str, **extra):
    root = tmp_path / fixture
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / f"{fixture}.sv").write_text((FIXTURES / f"{fixture}.sv").read_text())
    data = {
        "top": fixture,
        "sources": [f"rtl/{fixture}.sv"],
        "clocks": TWO_CLOCKS,
        "resets": [{"port": "rst_ni"}],
        **extra,
    }
    (root / "project.yaml").write_text(yaml.safe_dump(data))
    return cdc.cdc(load_project(root))


def rules(r, severity="error"):
    return sorted(
        (f["rule"], f["from"], f["to"])
        for f in r.data["findings"]
        if f["severity"] == severity and not f.get("waived")
    )


def accepted(r):
    return sorted((a["rule"], a["from"], a["to"]) for a in r.data["accepted"])


def test_an_unsynchronized_crossing_is_an_error_with_its_path(tmp_path):
    r = check(tmp_path, "unsync")
    assert r.status == "fail"
    assert rules(r) == [("unsynchronized", "a_q", "q_o")]
    (f,) = [f for f in r.data["findings"] if f["rule"] == "unsynchronized"]
    assert (f["from_domain"], f["to_domain"]) == ("clk_a", "clk_b")
    assert f["path"][0].startswith("a_q") and f["path"][-1].startswith("q_o")
    assert "a_q (clk_a) reaches q_o (clk_b) without a synchronizer" in r.summary


@pytest.mark.parametrize(("fixture", "stages"), [("sync2", 2), ("sync3", 3)])
def test_a_flop_chain_synchronizes(tmp_path, fixture, stages):
    r = check(tmp_path, fixture)
    assert r.status == "pass", r.summary
    assert rules(r) == []
    (sync,) = [a for a in r.data["accepted"] if a["rule"] == "synchronizer"]
    assert sync["from"] == "a_q" and sync["stages"] == stages


def test_logic_before_a_synchronizer_is_an_error(tmp_path):
    r = check(tmp_path, "comb_sync")
    assert rules(r) == [("logic_before_synchronizer", "a_q", "meta_q")]


def test_data_qualified_by_a_synchronized_request_passes(tmp_path):
    r = check(tmp_path, "handshake")
    assert r.status == "pass", r.summary
    assert ("handshake", "data_q", "data_o") in accepted(r)
    assert ("synchronizer", "req_q", "req_meta_q") in accepted(r)
    (h,) = [a for a in r.data["accepted"] if a["rule"] == "handshake"]
    assert h["qualifier"] == "req_sync_q" and h["bits"] == 8


def test_an_asynchronous_fifo_with_gray_pointers_passes(tmp_path):
    r = check(tmp_path, "fifo_gray", **FIFO)
    assert r.status == "pass", r.summary
    found = accepted(r)
    assert ("gray_synchronizer", "wptr", "wptr_r1") in found
    assert ("gray_synchronizer", "rptr", "rptr_w1") in found
    assert ("fifo_memory", "mem", "rdata_o") in found


def test_a_fifos_word_stored_straight_into_a_memory_of_the_reading_clock_passes(tmp_path):
    fifo = {"clocks": FIFO_CLOCKS, "cdc": {"input_domains": {"baddr_i": "rclk"}}}
    r = check(tmp_path, "fifo_into_memory", **fifo)
    assert r.status == "pass", r.summary
    assert ("fifo_memory", "mem", "buffer") in accepted(r)


def test_binary_pointers_through_synchronizers_are_an_error(tmp_path):
    r = check(tmp_path, "fifo_binary", **FIFO)
    assert r.status == "fail"
    assert rules(r) == [
        ("multibit_synchronizer", "rptr", "rptr_w1"),
        ("multibit_synchronizer", "wptr", "wptr_r1"),
    ]


def test_asynchronous_inputs_need_a_synchronizer(tmp_path):
    one = [{"port": "clk_i", "period_ns": 10}]
    async_rx = {"async_inputs": ["rx_i"]}
    assert check(tmp_path, "async_in", clocks=one, cdc=async_rx).status == "pass"
    bad = check(tmp_path / "b", "async_in_bad", clocks=one, cdc=async_rx)
    assert rules(bad) == [("unsynchronized", "rx_i", "q_o")]
    # Not declared asynchronous, the input belongs to the first clock.
    assert check(tmp_path / "c", "async_in_bad", clocks=one).status == "pass"


def test_reset_crossings_and_reset_release(tmp_path):
    one = [{"port": "clk_i", "period_ns": 10}]
    resets = [{"port": "rst_a_ni"}, {"port": "rst_b_ni"}]
    r = check(tmp_path, "rdc", clocks=one, resets=resets)
    assert rules(r) == [("reset_crossing", "a_q", "q_o")]
    # Both flops are reset straight from a port: released asynchronously, a warning.
    assert {f["rule"] for f in r.data["findings"] if f["severity"] == "warning"} == {
        "reset_release"
    }
    ok = check(tmp_path / "s", "reset_sync", clocks=one)
    assert ok.status == "pass" and rules(ok, "warning") == []
    assert ("reset_synchronizer", "rst_ni", "r2_q") in accepted(ok)
    # A reset synchronized outside the design is not warned about.
    outside = check(tmp_path / "o", "async_in", clocks=one, cdc={"reset_synchronized": ["rst_ni"]})
    assert rules(outside, "warning") == []


def test_waived_findings_do_not_fail_the_step(tmp_path):
    waive = {"waive": [{"from": "a_*", "to": "q_o", "reason": "a_q only changes in reset"}]}
    r = check(tmp_path, "unsync", cdc=waive)
    assert r.status == "pass", r.summary
    (f,) = [f for f in r.data["findings"] if f["rule"] == "unsynchronized"]
    assert f["waived"] == "a_q only changes in reset"


def test_a_generated_clock_is_its_own_domain(tmp_path):
    root = tmp_path / "gen"
    (root / "rtl").mkdir(parents=True)
    (root / "rtl/gen.sv").write_text(
        """module gen (input logic clk_i, input logic rst_ni, input logic d_i, output logic q_o);
  logic div_q, a_q;
  always_ff @(posedge clk_i or negedge rst_ni) if (!rst_ni) div_q <= 1'b0; else div_q <= !div_q;
  always_ff @(posedge clk_i or negedge rst_ni) if (!rst_ni) a_q <= 1'b0; else a_q <= d_i;
  always_ff @(posedge div_q or negedge rst_ni) if (!rst_ni) q_o <= 1'b0; else q_o <= a_q;
endmodule
"""
    )
    (root / "project.yaml").write_text(
        "top: gen\nsources: [rtl/gen.sv]\nclocks: [{port: clk_i, period_ns: 10}]\n"
        "resets: [{port: rst_ni}]\ncdc: {reset_synchronized: [rst_ni]}\n"
    )
    r = cdc.cdc(load_project(root))
    assert ("generated_clock", "div_q", "q_o") in rules(r, "warning")
    assert ("unsynchronized", "a_q", "q_o") in rules(r)
    assert r.data["domains"] == {"clk_i": 2, "div_q": 1}


def test_the_uart_example_has_one_clock_and_no_crossing(tmp_path):
    example = Path(__file__).parents[1] / "examples/uart"
    r = cdc.cdc(load_project(example))
    assert r.status == "pass", r.summary
    assert r.data["domains"] == {"clk_i": r.data["flops"]}


def _accelerator(tmp_path: Path, cdc_rtl: str | None = None, direct: bool = False):
    """ChipLeap's accelerator on a clock of its own, as the bundle's cdc step checks it:
    the SmolLM2 example's GEMV 1 x 8, the bus at 35 MHz and the accelerator at 90. With
    ``direct``, with its direct path to external memory and the memory block, whose
    memory ports run on the accelerator's clock and X-HEEP's side crosses into it."""
    from chipleap.hw import Accel
    from chipleap.verify import crossings

    accel = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    (tmp_path / "accel").mkdir()
    root = crossings.write_project(accel, tmp_path / "accel", 35.0, 90.0, cdc_rtl, direct)
    return cdc.cdc(load_project(root))


@pytest.mark.parametrize("direct", [False, True], ids=["alone", "with-memory-block"])
def test_the_accelerators_own_clock_crosses_only_through_synchronizers(tmp_path, direct):
    r = _accelerator(tmp_path, direct=direct)
    assert r.status == "pass", r.summary
    assert set(r.data["domains"]) == {"clk_i", "accel_clk_i"}
    rules_seen = {a["rule"] for a in r.data["accepted"]}
    assert rules_seen == {
        "synchronizer",
        "gray_synchronizer",
        "handshake",
        "fifo_memory",
        "reset_synchronizer",
    }
    # The register interface's request and acknowledge, and the interrupt.
    at = "u_accel." if direct else ""
    sync = {a["from"] for a in r.data["accepted"] if a["rule"] == "synchronizer"}
    assert sync == {f"{at}u_reg.b_req_q", f"{at}u_reg.a_ack_q", f"{at}a_irq_q"}
    wbuf = f"{at}u_accel.u_core.wbuf"
    assert ("fifo_memory", f"{at}g_rd[0].u_rd.u_rsp.mem", wbuf) in accepted(r)
    # X-HEEP's side into the memory ports: its two FIFOs' gray pointers, and a reset
    # synchronizer on each side.
    gray = {a["from"] for a in r.data["accepted"] if a["rule"] == "gray_synchronizer"}
    xside = {g for g in gray if g.startswith("u_mem.g_cdc.u_xcdc.")}
    assert len(xside) == (4 if direct else 0)
    resets = {a["to"] for a in r.data["accepted"] if a["rule"] == "reset_synchronizer"}
    assert {"u_mem.m_rst_n", "u_mem.x_rst_n"} <= resets if direct else len(resets) == 2
    assert r.data["warnings"] == 0


@pytest.mark.parametrize(
    ("old", "new", "rule"),
    [
        # The interrupt straight from the accelerator's gate into the synchronizer.
        (".d_i   (a_irq_q)", ".d_i   (a_irq)", "logic_before_synchronizer"),
        # The response FIFO's read side compares with the write pointer unsynchronized.
        ("assign empty_o = rptr == wptr_r;", "assign empty_o = rptr == wptr;", "unsynchronized"),
    ],
)
@pytest.mark.parametrize("direct", [False, True], ids=["alone", "with-memory-block"])
def test_a_broken_crossing_in_the_accelerators_is_found(tmp_path, old, new, rule, direct):
    from chipleap.gen.rtl import cdc_sv

    text = cdc_sv()
    assert text.count(old) == 1
    r = _accelerator(tmp_path, text.replace(old, new), direct)
    assert r.status == "fail"
    assert rule in {f["rule"] for f in r.data["findings"] if f["severity"] == "error"}
