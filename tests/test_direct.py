"""The accelerator's direct read path to external memory: which designs have it, the files
a bundle carries for it, and what the model assumes of it. Its RTL is simulated in
test_rtl.py."""

from pathlib import Path

import pytest

from chipleap.cli import load_spec
from chipleap.gen.bundle import write_bundle
from chipleap.gen.rtl import rtl_params
from chipleap.hw import Accel, Design
from chipleap.hw.tech import MEM_ARB_KGE, MEM_CDC_KGE, get_tech
from chipleap.model.accel import direct_outstanding, ext_latency
from chipleap.model.evaluate import direct_kge, evaluate
from chipleap.verify import consistency

# The SmolLM2 example's SoC: the routed X-HEEP SoC's clock, two channels of octal PSRAM.
SMOLLM2 = Design(
    name="direct",
    freq_mhz=35.0,
    banks=1,
    bus="onetoM",
    ext_mem="octal-psram",
    ext_channels=2,
    accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
)


def test_only_an_x_heep_design_with_external_memory_has_the_direct_path():
    assert SMOLLM2.direct_memory
    assert not SMOLLM2.with_(ext_mem="none").direct_memory
    assert not SMOLLM2.with_(accel=None).direct_memory
    # The standalone IP's AXI4 masters reach memory through the host's interconnect.
    assert not SMOLLM2.with_(platform="standalone").direct_memory


def test_the_queue_covers_external_memorys_latency_at_the_accelerators_clock():
    assert ext_latency(35e6) == 8 and ext_latency(90e6) == 9 and ext_latency(1.2e9) == 76
    assert direct_outstanding(SMOLLM2) == 16
    assert direct_outstanding(SMOLLM2.with_(accel_mhz=100.0)) == 16
    # 76 cycles of latency need 78 reads in flight per port: the next power of two.
    assert direct_outstanding(SMOLLM2.with_(freq_mhz=1200.0)) == 128


def test_the_window_and_the_queue_must_be_powers_of_two():
    with pytest.raises(ValueError, match="aligned"):
        rtl_params(SMOLLM2.accel, True, (0xC0001000, 0x40000000))
    with pytest.raises(ValueError, match="power of two"):
        rtl_params(SMOLLM2.accel, True, outstanding=24)


@pytest.mark.parametrize("accel_mhz", [0.0, 90.0])
def test_a_bundle_carries_the_memory_block_and_its_window(tmp_path, accel_mhz):
    design = SMOLLM2.with_(accel_mhz=accel_mhz)
    write_bundle(tmp_path, design)
    rtl = tmp_path / "rtl"
    assert (rtl / "chipleap_mem.sv").is_file()
    assert "chipleap_mem.sv" in (rtl / "chipleap_accel.core").read_text()
    for name in ("chipleap_accel_top.sv", "chipleap_accel_xheep.sv"):
        text = (rtl / name).read_text()
        assert ".EXT_DIRECT(1)" in text and ".EXT_BASE(32'hC0000000)" in text
        assert ".EXT_OUTSTANDING(16)" in text
    assert "ext_resp_i" in (rtl / "chipleap_accel_xheep.sv").read_text()
    assert "Direct read masters" in (tmp_path / "DATASHEET.md").read_text()
    assert consistency.check(tmp_path, design) == []
    # A queue too short for the memory, or the path left out, is drift.
    top = rtl / "chipleap_accel_top.sv"
    top.write_text(top.read_text().replace(".EXT_OUTSTANDING(16)", ".EXT_OUTSTANDING(8)"))
    assert consistency.check(tmp_path, design) == [
        "rtl/chipleap_accel_top.sv: EXT_OUTSTANDING is 8, design.yaml and the X-HEEP "
        "configuration imply 16"
    ]
    (rtl / "chipleap_mem.sv").unlink()
    assert "rtl/chipleap_mem.sv is missing" in consistency.check(tmp_path, design)


def test_a_bundle_without_external_memory_has_no_direct_path(tmp_path):
    design = SMOLLM2.with_(ext_mem="none")
    write_bundle(tmp_path, design)
    assert not (tmp_path / "rtl" / "chipleap_mem.sv").exists()
    text = (tmp_path / "rtl" / "chipleap_accel_top.sv").read_text()
    assert "EXT_DIRECT" not in text and ".ext_gnt_i('0)" in text
    assert consistency.check(tmp_path, design) == []


def _evaluate(design):
    # The SmolLM2 example's workload and firmware, as `chipleap explore` reads them.
    spec = load_spec(str(Path(__file__).parents[1] / "examples/smollm2-135m-open-pdk.yaml"))
    wl = spec.workload(spec.default_weight_format())
    return evaluate(wl, design, firmware_kib=spec.firmware_kib)


def test_the_model_streams_over_the_direct_path_at_its_ports_clock():
    shared, own = _evaluate(SMOLLM2), _evaluate(SMOLLM2.with_(accel_mhz=90.0))
    # On the SoC's clock a 32-bit port takes in 4 bytes a cycle, less than the memory
    # gives; on the accelerator's 90 MHz the port could take 360 MB/s and the memory's
    # 248 MB/s bounds it. Neither stream crosses a clock.
    assert shared.bandwidth["external"] == pytest.approx(4 * 35e6)
    assert own.bandwidth["external"] == pytest.approx(2 * 124e6, rel=0.01)
    assert 1 / shared.time_s == pytest.approx(1.10, abs=0.01)
    assert 1 / own.time_s == pytest.approx(1.67, abs=0.01)
    # What remains is the CPU's work, at the SoC's clock in both.
    cpu = [sum(r.time_s for r in ev.ops if r.unit == "cpu") for ev in (shared, own)]
    assert cpu[0] == pytest.approx(cpu[1]) and cpu[0] == pytest.approx(0.233, abs=0.002)


def test_the_direct_path_is_priced_in_the_accelerators_logic():
    tech = get_tech("ihp-sg13g2")
    with_path, without = _evaluate(SMOLLM2), _evaluate(SMOLLM2.with_(ext_mem="none"))
    added = with_path.area_mm2["accelerator logic"] - without.area_mm2["accelerator logic"]
    assert added == pytest.approx(tech.logic_mm2(direct_kge(SMOLLM2)))
    assert 2.0 < direct_kge(SMOLLM2) < 3.0
    # On its own clock X-HEEP's side crosses instead of the stream: a few kGE, not a
    # crossing on the accelerator's port.
    own = direct_kge(SMOLLM2.with_(accel_mhz=90.0))
    assert own - MEM_ARB_KGE - MEM_CDC_KGE < direct_kge(SMOLLM2) - MEM_ARB_KGE
    assert direct_kge(SMOLLM2.with_(ext_mem="none")) == 0.0
