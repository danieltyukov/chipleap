import pytest

from chipleap.hw import Accel, Design
from chipleap.model.bounds import requirements
from chipleap.model.evaluate import evaluate
from chipleap.workload import LLMWorkloadSpec, build_llm


@pytest.fixture(scope="module")
def llama():
    return build_llm(LLMWorkloadSpec("llama-3.2-1b", context=1024, weights="int4"))


@pytest.fixture(scope="module")
def smol():
    return build_llm(LLMWorkloadSpec("smollm2-135m", context=512, weights="int4"))


LP4_GEMV = Design(
    tech="generic-28nm",
    freq_mhz=400,
    cpu="cv32e40p",
    bus="NtoM",
    banks=4,
    il_banks=4,
    ext_mem="lpddr4-x16",
    accel=Accel("gemv", 8, 8, 8, ports=4, abuf_kib=16, wbuf_kib=8),
)


def test_decode_with_accelerator_is_memory_bound(llama):
    e = evaluate(llama, LP4_GEMV)
    assert e.feasible
    assert e.bound == "external memory"
    # Bandwidth bound: bytes per token over sustained bandwidth.
    ideal = e.bandwidth["external"] / e.ext_bytes
    assert 0.7 * ideal < e.throughput < ideal


def test_cpu_only_is_compute_bound(smol):
    d = Design(tech="generic-28nm", freq_mhz=400, banks=4, ext_mem="lpddr4-x16")
    e = evaluate(smol, d)
    assert e.bound == "compute"
    assert all(r.unit == "cpu" for r in e.ops)


def test_faster_memory_helps_memory_bound_design(llama):
    base = evaluate(llama, LP4_GEMV).throughput
    faster = evaluate(llama, LP4_GEMV.with_(ext_mem="lpddr4x-x32")).throughput
    assert faster > 1.3 * base


def test_bigger_array_does_not_help_memory_bound_design(llama):
    base = evaluate(llama, LP4_GEMV).throughput
    bigger = evaluate(llama, LP4_GEMV.with_accel(rows=32)).throughput
    assert bigger < 1.1 * base


def test_violations(llama, smol):
    no_mem = evaluate(smol, Design())
    assert any("no external memory" in v for v in no_mem.violations)
    lp4_130 = evaluate(llama, LP4_GEMV.with_(tech="ihp-sg13g2", freq_mhz=100))
    assert any("PHY" in v for v in lp4_130.violations)
    too_fast = evaluate(llama, LP4_GEMV.with_(freq_mhz=2000))
    assert any("MHz" in v for v in too_fast.violations)
    flash = evaluate(smol, Design(ext_mem="qspi-flash"))
    assert any("read only" in v for v in flash.violations)
    big = build_llm(LLMWorkloadSpec("llama-3.2-3b", weights="int8"))
    window = evaluate(big, LP4_GEMV.with_(ext_mem="lpddr4x-x32"))
    assert any("2 GiB window" in v for v in window.violations)


def test_precision_wider_than_pe_falls_back_to_cpu():
    w = build_llm(LLMWorkloadSpec("smollm2-135m", weights="int8", kv="int8"))
    e = evaluate(w, LP4_GEMV.with_accel(w_bits=4))
    assert any("CPU" in x for x in e.warnings)
    assert any(r.unit == "cpu" and r.macs for r in e.ops)


def test_breakdowns_add_up(llama):
    e = evaluate(llama, LP4_GEMV)
    assert sum(e.time_by_bound.values()) == pytest.approx(e.time_s, rel=1e-6)
    assert sum(e.energy_pj_by.values()) == pytest.approx(e.energy_pj, rel=1e-9)
    assert all(v > 0 for v in e.area_mm2.values())
    assert e.power_mw == pytest.approx(e.energy_pj / 1e9 / e.time_s)


def test_on_chip_placement_cuts_external_traffic():
    tiny = build_llm(LLMWorkloadSpec("smollm2-135m", context=64, weights="int4", kv="int4"))
    d = LP4_GEMV.with_(banks=8, bank_kib=128, il_banks=8, il_bank_kib=128)
    e = evaluate(tiny, d)
    assert e.placement["kv"] == 1.0
    assert 0 < e.placement["weights"] < 1.0
    assert e.ext_bytes < tiny.weight_bytes_read


def test_requirements_match_hand_arithmetic(llama):
    req = requirements(llama, 10)
    # 0.62 GB of int4 weights, plus fp16 group scales and the KV cache.
    assert req.stream_bytes_per_step == pytest.approx(llama.weight_bytes_read + llama.kv_bytes_read)
    assert 6.0e9 < req.ext_bandwidth < 7.5e9
    ok = {m.key for m in req.memories if m.channels}
    assert "hyperram" not in ok and "lpddr4x-x32" in ok


def test_requirements_derate_serial_memories_like_the_evaluator():
    # SmolLM2 135M at 2 token/s needs about 0.17 GB/s. At 130 nm the pads run octal PSRAM
    # below its rated speed, and two channels still deliver it, as the explorer finds.
    req = requirements(build_llm(LLMWorkloadSpec("smollm2-135m", weights="int4")), 2)
    psram = next(m for m in req.memories if m.key == "octal-psram")
    assert psram.channels_by_node.get("ihp-sg13g2") == 2
    assert not next(m for m in req.memories if m.key == "lpddr4-x16").channels_by_node.get(
        "ihp-sg13g2"
    )
