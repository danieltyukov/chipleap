"""Cycle-accurate checks of the generated accelerator. Needs Verilator."""

import shutil
import subprocess

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
