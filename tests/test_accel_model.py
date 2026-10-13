import math
import random
import statistics

import pytest

from chipleap.hw import Accel
from chipleap.model.accel import (
    DMA_FIXED,
    Bandwidth,
    TilingError,
    job_timing,
    k_align,
    plan_tiles,
    simulate_job,
    tile_compute_cycles,
    words_per_row,
)


def random_accel(rng):
    return Accel(
        rng.choice(["gemv", "ws", "os"]),
        rng.choice([1, 2, 4, 8, 16]),
        rng.choice([1, 2, 4, 8, 16]),
        8,
        rng.choice([1, 2, 4, 8]),
        rng.choice([1, 2, 4, 8, 16]),
        rng.choice([1, 2, 4, 8, 16]),
        rng.random() < 0.7,
    )


def test_plans_fit_buffers():
    rng = random.Random(1)
    for _ in range(2000):
        a = random_accel(rng)
        m, k, n = rng.randint(1, 64), rng.randint(1, 5000), rng.randint(1, 3000)
        fmt = rng.choice([8, 4, 2])
        try:
            p = plan_tiles(a, m, k, n, fmt)
        except TilingError:
            continue
        align = k_align(a, fmt)
        slots = 2 if a.double_buffer else 1
        assert p.k % align == 0 and p.k >= k
        assert p.tk % align == 0 and 0 < p.tk <= p.k
        assert p.tn * words_per_row(p.tk, fmt) * 4 <= a.wbuf_kib * 1024 // slots
        if p.x_resident:
            assert p.tm * p.k <= a.abuf_kib * 1024
        else:
            assert p.tm * p.tk <= a.abuf_kib * 1024 // slots


def test_format_wider_than_pe_rejected():
    with pytest.raises(TilingError):
        plan_tiles(Accel(w_bits=4), 1, 64, 8, 8)


def test_compute_cycles_per_dataflow():
    assert tile_compute_cycles(Accel("gemv", 8, 8), 1, 8, 64, True) == 64 // 8 + 3
    assert tile_compute_cycles(Accel("ws", 4, 8), 2, 8, 8, False) == 2 * (4 + 2 + 4 + 8) + 1
    assert tile_compute_cycles(Accel("os", 4, 8), 4, 8, 32, True) == 32 + 4 + 8


def test_closed_form_matches_schedule():
    rng = random.Random(3)
    errors = []
    for _ in range(800):
        a = random_accel(rng)
        m = rng.choice([1, 1, 2, 4, 8, 16, 64])
        k = rng.choice([64, 256, 1024, 4096])
        n = rng.choice([8, 64, 512, 4096, 16384])
        bw = Bandwidth(
            x=4 * min(a.ports, 4),
            w_on=4 * min(a.ports, 4),
            w_ext=rng.choice([1, 4, 16]),
            y=4,
            lat_on=2,
            lat_ext=rng.choice([8, 24]),
        )
        frac = rng.choice([0.0, 1.0])
        try:
            jt = job_timing(a, m, k, n, 8, bw, frac)
        except TilingError:
            continue
        exact = simulate_job(a, m, k, n, 8, bw, frac)
        errors.append(abs(jt.total - exact) / exact)
    assert statistics.mean(errors) < 0.01
    assert sorted(errors)[int(0.95 * len(errors))] < 0.02


def test_decode_gemv_is_bandwidth_bound():
    a = Accel("gemv", 8, 8, 8, 4, 16, 8, True)
    slow = Bandwidth(x=16, w_on=16, w_ext=2, y=4)
    fast = Bandwidth(x=16, w_on=16, w_ext=8, y=4)
    t_slow = job_timing(a, 1, 2048, 8192, 4, slow, 0.0).total
    t_fast = job_timing(a, 1, 2048, 8192, 4, fast, 0.0).total
    assert t_slow / t_fast == pytest.approx(4, rel=0.1)


def test_weight_stationary_wastes_decode():
    gemv = Accel("gemv", 16, 16, 8, 4, 16, 8, True)
    ws = Accel("ws", 16, 16, 8, 4, 16, 8, True)
    bw = Bandwidth(x=16, w_on=16, w_ext=16, y=4)
    assert (
        job_timing(ws, 1, 2048, 2048, 8, bw, 1.0).compute
        > 10 * job_timing(gemv, 1, 2048, 2048, 8, bw, 1.0).compute
    )


def test_stalls_wait_for_the_slowest_port():
    # Without stalls the number of ports changes nothing: n words take n / rate cycles.
    plain = Bandwidth(x=16, w_on=16, w_ext=16, y=4)
    four = Bandwidth(x=16, w_on=16, w_ext=16, y=4, ports=4)
    assert plain.dma(1024, 16, 2) == four.dma(1024, 16, 2) == 64 + 2 + DMA_FIXED
    # At a 70 % grant rate each of four ports reads 64 words in 64 / 0.7 cycles on average,
    # give or take sqrt(64 * 0.3) / 0.7, and the slowest is 1.0294 of those late.
    rate = 16 * 0.7
    stalled = Bandwidth(x=rate, w_on=rate, w_ext=rate, y=4, grant=0.7, ports=4)
    late = 1.0294 * math.sqrt(64 * 0.3) / 0.7
    assert stalled.dma(1024, rate, 2) == pytest.approx(92 + 2 + DMA_FIXED + late)
    # A single port waits for no other.
    one = Bandwidth(x=2.8, w_on=2.8, w_ext=2.8, y=4, grant=0.7, ports=1)
    assert one.dma(1024, 2.8, 2) == 366 + 2 + DMA_FIXED
