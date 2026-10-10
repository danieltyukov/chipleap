"""Synthesis of the generated accelerator on IHP SG13G2 against the area model."""

import pytest

from chipleap.hw import Accel
from chipleap.hw.tech import accel_logic_kge, get_tech
from chipleap.verify import synth
from chipleap.verify.tools import ihp_liberty, yosys

pytestmark = [
    pytest.mark.synth,
    pytest.mark.skipif(not yosys().found, reason="Yosys not installed"),
    pytest.mark.skipif(ihp_liberty() is None, reason="IHP SG13G2 PDK not installed"),
]


def test_gemv_area_and_timing(tmp_path):
    a = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    r = synth.synthesize(a, workdir=tmp_path)
    model = accel_logic_kge("gemv", 1, 8, 1) * 1000 * get_tech("ihp-sg13g2").ge_um2 / 1e6
    assert abs(model - r.logic_mm2) / r.logic_mm2 < 0.1
    # Buffered and sized, the accelerator closes well above the 100 MHz the model allows
    # on this node; a zero delay would mean the timing report was not parsed.
    assert 1.0 < r.delay_ns < 9.0
    assert r.fmax_mhz > 100
    assert (tmp_path / "yosys-8.log").exists()
