"""Cycle-accurate checks of the generated accelerator. Needs Verilator."""

import shutil
import subprocess
from dataclasses import replace

import pytest

from chipleap.hw import Accel
from chipleap.verify import sim

pytestmark = [
    pytest.mark.rtl,
    pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator not installed"),
]

ACCELS = [
    Accel("gemv", 4, 8, 8, ports=2, abuf_kib=4, wbuf_kib=2),
    Accel("gemv", 8, 4, 4, ports=4, abuf_kib=1, wbuf_kib=1, double_buffer=False),
    Accel("ws", 4, 4, 8, ports=2, abuf_kib=2, wbuf_kib=2),
    Accel("ws", 2, 8, 4, ports=1, abuf_kib=1, wbuf_kib=1, double_buffer=False),
    Accel("os", 4, 4, 8, ports=2, abuf_kib=2, wbuf_kib=2),
    Accel("os", 8, 2, 4, ports=4, abuf_kib=1, wbuf_kib=1),
    # More ports than words in many rows: each port steps across several rows at a time.
    Accel("gemv", 2, 8, 4, ports=8, abuf_kib=1, wbuf_kib=1),
    Accel("os", 4, 4, 8, ports=16, abuf_kib=1, wbuf_kib=1, double_buffer=False),
]


@pytest.mark.parametrize("accel", ACCELS, ids=lambda a: f"{a.dataflow}{a.rows}x{a.cols}p{a.ports}")
def test_bit_exact_and_cycle_exact(accel):
    results = sim.run(accel, sim.default_cases(accel, seed=21))
    assert all(r.ok for r in results), [r.case for r in results if not r.ok]
    for r in results:
        if r.case.stall_pct == 0:
            assert r.cycles == r.predicted, r.case
        else:
            assert abs(r.error) < 0.08, (r.case, r.cycles, r.predicted)


# The buffers as SRAM banks (chipleap.gen.banks), each macro simulated by ChipLeap's model of
# its ports, which stops the simulation if a bank is read and written in one cycle. The
# lists of macros force every way of building a bank: one macro, half a macro, macros
# stacked and macros side by side.
def _sram(words, bits, height):
    from chipleap.macro.sram import Sram

    return Sram(f"RM_IHPSG13_1P_{words}x{bits}_c2_bm_bist", words, bits, 400.0, height, True)


SRAM_ACCELS = [
    (Accel("gemv", 1, 8, 4, ports=1, abuf_kib=2, wbuf_kib=2), [_sram(256, 64, 118.78)]),
    (
        Accel("gemv", 4, 4, 8, ports=1, abuf_kib=4, wbuf_kib=2, double_buffer=False),
        [_sram(256, 32, 118.78)],
    ),
    (Accel("gemv", 2, 16, 8, ports=1, abuf_kib=2, wbuf_kib=2), [_sram(64, 64, 64.36)]),
    (
        Accel("gemv", 8, 8, 8, ports=1, abuf_kib=4, wbuf_kib=4),
        [_sram(256, 64, 118.78), _sram(64, 64, 64.36)],
    ),
]


@pytest.mark.parametrize(
    ("accel", "srams"),
    SRAM_ACCELS,
    ids=lambda a: f"sram-{a.rows}x{a.cols}" if isinstance(a, Accel) else None,
)
def test_sram_banks_bit_exact_and_cycle_exact(accel, srams):
    cases = sim.default_cases(accel, seed=21)
    results = sim.run(accel, cases, srams=srams)
    assert all(r.ok for r in results), [r.case for r in results if not r.ok]
    # The same cycles as the flop buffers, stalls and all, and the model's without stalls.
    flops = sim.run(accel, cases)
    assert [r.cycles for r in results] == [r.cycles for r in flops]
    for r in results:
        if r.case.stall_pct == 0:
            assert r.cycles == r.predicted, r.case


@pytest.mark.parametrize(("accel", "srams"), SRAM_ACCELS[1:3])
def test_sram_top_lints_clean(tmp_path, accel, srams):
    from chipleap.gen import banks
    from chipleap.gen.rtl import accel_sv
    from chipleap.macro.sram import model

    plan = banks.plan(accel, srams)
    files = {"chipleap_accel.sv": accel_sv(), f"{banks.TOP}.v": banks.top_sv(plan)}
    files |= {f"{s.cell}.v": model(s) for s in plan.cells()}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    out = subprocess.run(
        ["verilator", "--lint-only", "-Wall", "--top-module", banks.TOP, *files],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "%" not in out.stdout + out.stderr, out.stderr


# The standalone platform: the same core behind its AXI4 wrapper, programmed over AXI4-Lite
# against an AXI4 memory model that stalls every ready at random and checks the protocol.
AXI_ACCELS = [
    Accel("gemv", 4, 8, 4, ports=2, abuf_kib=4, wbuf_kib=2),
    Accel("ws", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2, double_buffer=False),
    Accel("os", 4, 4, 8, ports=4, abuf_kib=2, wbuf_kib=2),
]


@pytest.mark.parametrize(
    "accel", AXI_ACCELS, ids=lambda a: f"axi-{a.dataflow}{a.rows}x{a.cols}p{a.ports}"
)
def test_axi_wrapper_bit_exact_and_cycle_exact(accel):
    results = sim.run(accel, sim.default_cases(accel, seed=5), platform="standalone")
    assert all(r.ok for r in results), [r.case for r in results if not r.ok]
    for r in results:
        if r.case.stall_pct == 0:
            assert r.cycles == r.predicted, r.case
        else:
            # The model leaves out stalls, so a job of a hundred-odd cycles is a few stalled
            # handshakes off: 10 %, or 20 cycles for the shortest jobs.
            slack = max(0.1 * r.predicted, 20)
            assert abs(r.cycles - r.predicted) <= slack, (r.case, r.cycles, r.predicted)


@pytest.mark.parametrize(
    ("dataflow", "rows", "cols", "ports"),
    [("gemv", 1, 8, 1), ("gemv", 8, 32, 16), ("ws", 1, 1, 1), ("ws", 4, 8, 2), ("os", 2, 8, 4)],
)
def test_axi_wrapper_lints_clean(tmp_path, dataflow, rows, cols, ports):
    from chipleap.gen.rtl import accel_sv, axi_sv, rtl_params

    (tmp_path / "chipleap_accel.sv").write_text(accel_sv())
    (tmp_path / "chipleap_accel_axi.sv").write_text(axi_sv())
    p = rtl_params(Accel(dataflow, rows, cols, 8, ports=ports, abuf_kib=1, wbuf_kib=1))
    out = subprocess.run(
        [
            "verilator",
            "--lint-only",
            "-Wall",
            "--top-module",
            "chipleap_accel_axi",
            *p.verilator_args(),
            "-GAXI_ID_WIDTH=4",
            str(tmp_path / "chipleap_accel.sv"),
            str(tmp_path / "chipleap_accel_axi.sv"),
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "%" not in out.stdout + out.stderr, out.stderr


# The accelerator on a clock of its own (chipleap_accel_cdc) beside a bus on another: the
# routed SoC's 35 MHz with the accelerator at 90, the reverse, equal clocks half a period
# apart, 42.3 and 100 MHz, and an odd ratio, each at an odd phase. Edges that fall
# differently cross a cycle earlier or later, so the counts are held to the model's
# crossing term (chipleap.model.accel.crossed) within its fitted bound, not cycle for cycle.
CDC_CLOCKS = [
    sim.Clocks(28571, 11111, 3000),
    sim.Clocks(11111, 28571, 5000),
    sim.Clocks(10000, 10000, 5000),
    sim.Clocks(23641, 10000, 6151),
    sim.Clocks(25000, 17857, 911),
]
CDC_ACCELS = [
    ACCELS[0],
    ACCELS[2],
    ACCELS[5],
    Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),  # the SmolLM2 example's
]


@pytest.mark.parametrize(
    "accel", CDC_ACCELS, ids=lambda a: f"cdc-{a.dataflow}{a.rows}x{a.cols}p{a.ports}"
)
def test_own_clock_bit_exact_and_within_the_model(accel):
    cases = sim.default_cases(accel, seed=21)
    for clocks in CDC_CLOCKS:
        results = sim.run(accel, cases, clocks=clocks)
        assert all(r.ok for r in results), (clocks, [r.case for r in results if not r.ok])
        for r in results:
            bound = 0.055 if r.case.stall_pct == 0 else 0.10
            assert abs(r.error) <= bound, (clocks, r.case, r.cycles, r.predicted)


def test_own_clock_is_the_model_cycle_for_cycle_with_a_slower_accelerator():
    # When the accelerator's clock is the slower, the bus side always keeps up and the
    # crossing term lands on the same cycle as the simulation.
    accel = CDC_ACCELS[-1]
    results = sim.run(accel, sim.default_cases(accel, seed=21), clocks=CDC_CLOCKS[1])
    assert all(r.ok for r in results)
    assert [r.cycles for r in results if not r.case.stall_pct] == [
        r.predicted for r in results if not r.case.stall_pct
    ]


@pytest.mark.parametrize(
    "params",
    [
        ["-GDATAFLOW=0", "-GROWS=1", "-GCOLS=8", "-GPORTS=1"],
        ["-GDATAFLOW=2", "-GROWS=8", "-GCOLS=2", "-GPORTS=4", "-GCDC_AW=1"],
        ["-GDATAFLOW=1", "-GROWS=4", "-GCOLS=8", "-GPORTS=2", "-GCDC_AW=4"],
    ],
)
def test_own_clock_top_lints_clean(tmp_path, params):
    from chipleap.gen.rtl import accel_sv, cdc_sv

    (tmp_path / "chipleap_accel.sv").write_text(accel_sv())
    (tmp_path / "chipleap_accel_cdc.sv").write_text(cdc_sv())
    out = subprocess.run(
        [
            "verilator",
            "--lint-only",
            "-Wall",
            "--top-module",
            "chipleap_accel_cdc",
            *params,
            "-GABUF_WORDS=512",
            "-GWBUF_WORDS=512",
            str(tmp_path / "chipleap_accel.sv"),
            str(tmp_path / "chipleap_accel_cdc.sv"),
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0 and "%" not in out.stdout + out.stderr, out.stderr


@pytest.mark.parametrize(("accel", "srams"), SRAM_ACCELS[:2], ids=["sram-1x8", "sram-4x4"])
def test_own_clock_with_sram_banks_is_the_flop_build_cycle_for_cycle(accel, srams):
    # The macro build behind the crossings, as it is laid out: the same counts as the flop
    # buffers behind them, every job bit-exact.
    cases = sim.default_cases(accel, seed=21)
    clocks = CDC_CLOCKS[3]  # X-HEEP's side at 42.3 MHz, the accelerator at 100
    banked = sim.run(accel, cases, srams=srams, clocks=clocks)
    assert all(r.ok for r in banked)
    assert [r.cycles for r in banked] == [r.cycles for r in sim.run(accel, cases, clocks=clocks)]


# The direct read path to external memory (chipleap_accel with EXT_DIRECT, chipleap_mem):
# weights stream from a memory model that keeps requests in flight, while X-HEEP's three
# external slave ports, driven by traffic generators, read and write regions of their own
# beside them. Every job is bit-exact, every X-HEEP read returns what its port last wrote
# and no write is lost, and the memory ports hold each request until its grant.
DIRECT_ACCELS = [
    Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),  # the SmolLM2 example's
    ACCELS[0],
    ACCELS[5],
]


def _direct_ok(results):
    for r in results:
        assert r.ok and r.obi_bad == 0 and r.x_bad == 0, r


@pytest.mark.parametrize(
    "accel", DIRECT_ACCELS, ids=lambda a: f"direct-{a.dataflow}{a.rows}x{a.cols}p{a.ports}"
)
def test_the_direct_path_streams_weights_with_reads_in_flight_cycle_for_cycle(accel):
    # X-HEEP idle: the model's count exactly, with as many reads in flight at the memory
    # port as its latency needs to keep a word a cycle coming.
    cases = [
        replace(c, ext_latency=lat)
        for lat in (4, 9, 14)
        for c in sim.direct_cases(accel, seed=5)
        if not c.x_pct and not c.stall_pct and c.place == 1
    ]
    results = sim.run_direct(accel, cases)
    _direct_ok(results)
    for r in results:
        assert r.cycles == r.predicted, (r.case, r.cycles, r.predicted)
    for lat in (4, 9, 14):
        assert max(r.max_inflight for r in results if r.case.ext_latency == lat) >= lat


@pytest.mark.parametrize(
    "accel", DIRECT_ACCELS, ids=lambda a: f"direct-{a.dataflow}{a.rows}x{a.cols}p{a.ports}"
)
def test_x_heeps_external_accesses_work_while_the_accelerator_streams(accel):
    al = sim.k_align(accel, min(4, accel.w_bits))
    fmt = min(4, accel.w_bits)
    cases = [
        sim.DirectCase(2, al * 64, 9, fmt, seed=s, stall_pct=stall, x_pct=x)
        for s, (stall, x) in enumerate([(0, 30), (0, 100), (30, 20), (50, 100)])
    ]
    results = sim.run_direct(accel, cases)
    _direct_ok(results)
    for r in results:
        assert r.x_reads > 20 and r.max_inflight >= 4, r
        # Round robin on one clock: never two grants in a row to the accelerator while
        # an X-HEEP request waits. A port waits at most for X-HEEP's other two ports and
        # for the accelerator's grant before each of the three: five cycles.
        assert r.accel_run <= 1, r
        if not r.case.stall_pct:
            assert r.x_max_wait <= 2 * sim.XHEEP_PORTS - 1, r
    # Sharing the port slows the stream: X-HEEP at full tilt costs the accelerator time.
    quiet = sim.run_direct(accel, [replace(cases[1], x_pct=0)])[0]
    assert quiet.cycles < results[1].cycles


@pytest.mark.parametrize(("accel", "srams"), SRAM_ACCELS[:2], ids=["sram-1x8", "sram-4x4"])
def test_the_direct_path_into_sram_banks_is_the_flop_build_cycle_for_cycle(accel, srams):
    cases = sim.direct_cases(accel, seed=9)
    banked = sim.run_direct(accel, cases, srams=srams)
    _direct_ok(banked)
    assert [r.cycles for r in banked] == [r.cycles for r in sim.run_direct(accel, cases)]


@pytest.mark.parametrize(
    "clocks", CDC_CLOCKS[:2] + CDC_CLOCKS[3:4], ids=["35-90", "90-35", "42-100"]
)
def test_the_direct_path_on_the_accelerators_clock_is_within_the_model(clocks):
    # The memory ports run on the accelerator's clock, so its weights cross nothing and
    # only the on-chip reads and stores cross; X-HEEP's side crosses into the memory
    # ports' clock and still works.
    accel = DIRECT_ACCELS[0]
    results = sim.run_direct(accel, sim.direct_cases(accel, seed=3), clocks=clocks)
    _direct_ok(results)
    for r in results:
        if not r.case.stall_pct and not r.case.x_pct and r.case.place == 1:
            assert abs(r.error) <= 0.055, (r.case, r.cycles, r.predicted)
    assert any(r.x_reads for r in results)


@pytest.mark.parametrize(("n", "depth"), [(2, 4), (3, 4), (2, 32)])
def test_the_arbiter_is_fair_keeps_order_and_holds_requests_under_backpressure(tmp_path, n, depth):
    from pathlib import Path

    from chipleap.gen.rtl import mem_sv

    (tmp_path / "chipleap_mem.sv").write_text(mem_sv())
    tb = Path(__file__).parent / "fixtures/mem/tb_obi_arb.cpp"
    out = subprocess.run(
        [
            "verilator",
            "--cc",
            "--exe",
            "--build",
            "-O1",
            "-Wno-fatal",
            "-Wno-lint",
            "-Wno-style",
            "--top-module",
            "chipleap_obi_arb",
            f"-GN={n}",
            f"-GDEPTH={depth}",
            "-CFLAGS",
            f"-DARB_N={n}",
            "chipleap_mem.sv",
            str(tb),
            "-j",
            "4",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    import json

    exe = tmp_path / "obj_dir" / "Vchipleap_obi_arb"
    # cycles, seed, request %, grant %, most cycles to a response
    for args in ("20000 1 100 100 1", "20000 2 100 50 8", "20000 3 40 70 12", "20000 4 100 100 40"):
        run = subprocess.run([str(exe), *args.split()], capture_output=True, text=True)
        d = json.loads(run.stdout)
        assert d["bad"] == 0 and d["obi_bad"] == 0, (args, d)
        # A waiting master sees at most n - 1 grants to the others before its own.
        assert d["worst_wait"] <= n - 1, (args, d)
        assert d["most_inflight"] <= depth, (args, d)
        if args.split()[2] == "100":  # every master always asks: equal shares
            assert max(d["grants"]) - min(d["grants"]) <= 1, (args, d)
    assert d["most_inflight"] == min(depth, 4 * n), d  # the slow slave fills the queue


@pytest.mark.parametrize(
    "params",
    [
        ["-GPORTS=1", "-GNX=3", "-GCDC=0"],
        ["-GPORTS=2", "-GNX=3", "-GCDC=1"],
        ["-GPORTS=4", "-GNX=1"],
    ],
)
def test_the_memory_block_lints_clean(tmp_path, params):
    from chipleap.gen.rtl import accel_sv, cdc_sv, mem_sv

    for name, text in (
        ("chipleap_accel.sv", accel_sv()),
        ("chipleap_accel_cdc.sv", cdc_sv()),
        ("chipleap_mem.sv", mem_sv()),
    ):
        (tmp_path / name).write_text(text)
    for top, extra in (
        ("chipleap_mem", params),
        ("chipleap_accel", ["-GEXT_DIRECT=1", "-GPORTS=2"]),
    ):
        out = subprocess.run(
            [
                "verilator",
                "--lint-only",
                "-Wall",
                "--top-module",
                top,
                *extra,
                "chipleap_accel.sv",
                "chipleap_accel_cdc.sv",
                "chipleap_mem.sv",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert out.returncode == 0 and "%" not in out.stdout + out.stderr, out.stderr


def test_the_x_heep_adapter_elaborates_with_its_direct_ports(tmp_path):
    # X-HEEP's own OBI and register types, as xheep_obi_pkg and xheep_reg_pkg declare them.
    from chipleap.gen.rtl import write_rtl

    write_rtl(DIRECT_ACCELS[1], "t", tmp_path, direct=True)
    (tmp_path / "top.sv").write_text(
        """
package p;
  typedef struct packed {logic req; logic we; logic [3:0] be; logic [31:0] addr;
                         logic [31:0] wdata;} obi_req_t;
  typedef struct packed {logic gnt; logic rvalid; logic [31:0] rdata;} obi_rsp_t;
  typedef struct packed {logic valid; logic write; logic [31:0] wdata; logic [31:0] addr;
                         logic [3:0] wstrb;} reg_req_t;
  typedef struct packed {logic error; logic ready; logic [31:0] rdata;} reg_rsp_t;
endpackage
module top (input logic clk_i, input logic rst_ni, input p::reg_req_t reg_req_i,
            output p::reg_rsp_t reg_rsp_o, output p::obi_req_t [1:0] rd_req_o,
            input p::obi_rsp_t [1:0] rd_resp_i, output p::obi_req_t wr_req_o,
            input p::obi_rsp_t wr_resp_i, output p::obi_req_t [1:0] ext_req_o,
            input p::obi_rsp_t [1:0] ext_resp_i, output logic irq_o);
  chipleap_accel_xheep #(.reg_req_t(p::reg_req_t), .reg_rsp_t(p::reg_rsp_t),
                         .obi_req_t(p::obi_req_t), .obi_resp_t(p::obi_rsp_t)) u (.*);
endmodule
"""
    )
    rtl = tmp_path / "rtl"
    out = subprocess.run(
        [
            "verilator",
            "--lint-only",
            "-Wno-lint",
            "-Wno-style",
            "--top-module",
            "top",
            str(rtl / "chipleap_accel.sv"),
            str(rtl / "chipleap_accel_xheep.sv"),
            str(tmp_path / "top.sv"),
        ],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr


def test_the_direct_paths_crossings_are_each_synchronized(tmp_path):
    from chipleap.hw import Design
    from chipleap.rtl import cdc as checker
    from chipleap.rtl.project import load_project
    from chipleap.verify import crossings
    from chipleap.verify.tools import yosys

    if not yosys().found:
        pytest.skip("needs Yosys")
    design = Design(
        name="d",
        freq_mhz=35.0,
        banks=1,
        ext_mem="octal-psram",
        ext_channels=2,
        accel=DIRECT_ACCELS[0],
        accel_mhz=90.0,
    )
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    r = crossings.check(design, tmp_path / "ok")
    assert r.status == "pass", r.summary
    assert r.summary.startswith(f"{sim.DIRECT_TOP}:")
    # X-HEEP's side crossing into the memory ports' clock without its FIFOs' second clock:
    # the arbiter then reads requests written on another clock, unsynchronized.
    from chipleap.gen.rtl import mem_sv

    broken = mem_sv().replace(".b_clk_i(accel_clk_i)", ".b_clk_i(clk_i)")
    assert broken != mem_sv()
    root = crossings.write_project(
        design.accel, tmp_path / "bad", 35.0, 90.0, direct=True, mem_rtl=broken
    )
    bad = checker.cdc(load_project(root))
    assert bad.status == "fail"
    assert any(f["rule"] == "unsynchronized" for f in bad.data["findings"])
