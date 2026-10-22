from types import SimpleNamespace

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


def test_ihp_clock_above_the_accelerators_timing_is_a_violation(smol):
    fast = Design(
        tech="ihp-sg13g2",
        freq_mhz=100,
        bus="NtoM",
        il_banks=4,
        ext_mem="octal-psram",
        ext_channels=2,
        accel=Accel("gemv", 1, 8, 8, ports=8, abuf_kib=2, wbuf_kib=2),
    )
    ev = evaluate(smol, fast)
    assert any("closes timing" in v for v in ev.violations)
    slow = evaluate(smol, fast.with_(freq_mhz=75))
    assert not any("closes timing" in v for v in slow.violations)
    # The weight-stationary 4 x 4 engine routed at 100 MHz with one port, and its buffers are
    # no banks of macros.
    ws = evaluate(smol, fast.with_(accel=Accel("ws", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2)))
    assert not any("closes timing" in v for v in ws.violations)


def test_a_gemv_engine_with_macro_buffers_is_held_to_the_macro_builds_clock(smol):
    """A GEMV engine whose buffers the model prices as banks of macros is clocked as the
    routed macro builds are: with 2048-word macros, at about 85 MHz."""
    from chipleap.hw.tech import get_tech
    from chipleap.model.evaluate import bank_read_ns

    ihp = get_tech("ihp-sg13g2")
    d = Design(
        tech="ihp-sg13g2",
        freq_mhz=100,
        bus="NtoM",
        il_banks=4,
        ext_mem="octal-psram",
        ext_channels=2,
        accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=32, wbuf_kib=32),
    )
    assert bank_read_ns(d.accel, ihp) == pytest.approx(8.994)
    ev = evaluate(smol, d)
    assert any("SRAM macros that read in 9.0 ns" in v for v in ev.violations)
    assert not any("closes timing" in v for v in evaluate(smol, d.with_(freq_mhz=75)).violations)
    # The flop-buffer 2 x 8 engine with four ports routed at 100 MHz; with its buffers in
    # macros, as the model prices them, it is held to the macro builds' clock.
    four = d.with_(accel=Accel("gemv", 2, 8, 8, ports=4, abuf_kib=2, wbuf_kib=2))
    assert bank_read_ns(four.accel, ihp) == pytest.approx(5.1522)  # its weight lanes in 64 x 64
    assert any("closes timing" in v for v in evaluate(smol, four).violations)
    assert not any("closes timing" in v for v in evaluate(smol, four.with_(freq_mhz=90)).violations)
    # Other dataflows and nodes have no banks.
    assert bank_read_ns(Accel("ws", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2), ihp) == 0
    assert bank_read_ns(d.accel, get_tech("generic-28nm")) == 0


def test_ihp_buffers_cost_the_macros_the_layout_places():
    """A 1 KiB slot of a double buffer is a 2 KiB 256x64 macro, half empty, as routed;
    the model prices what the layout places, so exploration sees that cost."""
    from chipleap.gen import banks
    from chipleap.hw.design import Accel
    from chipleap.hw.tech import get_tech
    from chipleap.macro.sram import IHP_1P
    from chipleap.model.evaluate import buffer_mm2

    ihp = get_tech("ihp-sg13g2")
    one = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    placed = banks.plan(one, IHP_1P)
    assert {s.cell for s in placed.cells()} == {"RM_IHPSG13_1P_256x64_c2_bm_bist"}
    assert buffer_mm2(one, ihp) == pytest.approx(4 * 784.48 * 118.78 / 1e6)
    assert buffer_mm2(one, ihp) > ihp.sram_mm2(2) * 2
    # More ports read the same banks: the same macros, whatever the layout keeps today.
    assert buffer_mm2(Accel("gemv", 1, 8, 8, ports=2, abuf_kib=2, wbuf_kib=2), ihp) == (
        buffer_mm2(one, ihp)
    )
    # A dataflow the banks do not serve: each slot in 32-bit macros, still whole macros.
    ws = Accel("ws", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    assert buffer_mm2(ws, ihp) == pytest.approx(4 * 416.64 * 118.78 / 1e6)
    # Other nodes keep the density model.
    other = get_tech("generic-16nm")
    assert buffer_mm2(one, other) == other.sram_mm2(2) * 2


def test_logic_of_a_macro_backed_engine_has_the_banks_interface():
    """Where the buffers are priced as banks of macros, the logic that writes them and
    reads them a cycle ahead is priced too."""
    from chipleap.hw.design import Accel
    from chipleap.hw.tech import accel_bank_kge, accel_logic_kge, get_tech
    from chipleap.model.evaluate import accel_kge

    ihp, other = get_tech("ihp-sg13g2"), get_tech("generic-16nm")
    for ports in (1, 2):
        one = Accel("gemv", 1, 8, 8, ports=ports, abuf_kib=2, wbuf_kib=2)
        flops = accel_logic_kge("gemv", 1, 8, ports)
        assert accel_kge(one, ihp) == pytest.approx(flops + accel_bank_kge(1, 8, True))
        assert accel_kge(one, other) == flops
    ws = Accel("ws", 4, 4, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    assert accel_kge(ws, ihp) == accel_logic_kge("ws", 4, 4, 1)


def test_the_model_and_place_and_route_agree_on_a_floorplan_too_flat_to_route(smol, tmp_path):
    """The GEMV 1 x 16's eight macros leave a core 7.4 times as wide as tall, which did not
    route: place and route keeps flop buffers, and the model prices and times the build as
    one without banks, for the same reason, which the explorer's result carries."""
    from chip_fakes import sram_1p
    from chipleap.hw.tech import accel_logic_kge, get_tech
    from chipleap.macro.sram import IHP_1P, choose
    from chipleap.model.evaluate import accel_kge, bank_read_ns, buffer_mm2, floorplan_reason
    from chipleap.report.text import format_explore
    from chipleap.verify import pnr

    ihp = get_tech("ihp-sg13g2")
    pdk = tmp_path / "ihp-sg13g2"
    for cell in ("RM_IHPSG13_1P_64x64_c2_bm_bist", "RM_IHPSG13_1P_256x64_c2_bm_bist"):
        s = next(m for m in IHP_1P if m.cell == cell)
        sram_1p(pdk, s.words, s.bits, (s.width_um, s.height_um))
    flat = Accel("gemv", 1, 16, 8, ports=1, abuf_kib=2, wbuf_kib=2)
    reason = pnr.sram_reason(flat, pdk)
    assert reason is not None and "(7.4 to 1), too flat to route" in reason
    assert floorplan_reason(flat, ihp) == reason
    # Priced and timed as a build without banks: 32-bit macros, no bank interface or read.
    slot = choose(IHP_1P, 256, 32).area_um2 / 1e6
    assert buffer_mm2(flat, ihp) == pytest.approx(4 * slot)
    assert accel_kge(flat, ihp) == accel_logic_kge("gemv", 1, 16, 1)
    assert bank_read_ns(flat, ihp) == 0
    d = Design(
        tech="ihp-sg13g2",
        freq_mhz=90,
        bus="NtoM",
        il_banks=4,
        ext_mem="octal-psram",
        ext_channels=2,
        accel=flat,
    )
    ev = evaluate(smol, d)
    assert any(reason in w for w in ev.warnings)
    result = SimpleNamespace(
        spec=SimpleNamespace(name="s", workload=lambda: smol),
        evaluated=1,
        sampled=1,
        seconds=0.0,
        baseline=SimpleNamespace(design=d, evaluation=ev),
        steps=[],
        notes=[],
        best=SimpleNamespace(
            design=d,
            evaluation=ev,
            point=SimpleNamespace(weights="int4"),
            assessment=SimpleNamespace(violations=(), misses=()),
        ),
        pareto=[],
    )
    assert reason in format_explore(result)
    # The same engine on a single buffer, two macros a row, and the 2 x 8 with rows of three
    # are banks for both.
    for a in (
        Accel("gemv", 1, 16, 8, ports=1, abuf_kib=1, wbuf_kib=1, double_buffer=False),
        Accel("gemv", 2, 8, 8, ports=1, abuf_kib=2, wbuf_kib=4),
    ):
        assert pnr.sram_reason(a, pdk) is None and floorplan_reason(a, ihp) is None
        assert bank_read_ns(a, ihp) > 0
        assert not any(
            "flop buffers in ChipLeap's layout" in w
            for w in evaluate(smol, d.with_(accel=a)).warnings
        )
