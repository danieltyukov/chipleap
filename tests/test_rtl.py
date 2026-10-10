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
            assert abs(r.error) < 0.1, (r.case, r.cycles, r.predicted)


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
