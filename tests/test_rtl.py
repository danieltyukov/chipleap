"""Cycle-accurate checks of the generated accelerator. Needs Verilator."""

import shutil

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
