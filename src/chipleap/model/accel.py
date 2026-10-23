"""Cycle model of the generated accelerator.

The generated RTL (``chipleap.gen.rtl``) and this module describe the same machine, so
the evaluator's numbers can be checked against a cycle-accurate simulation:

* A job computes ``Y[M, N] = X[M, K] * W[N, K]^T`` with int8 activations, int8, int4 or
  int2 weights packed into 32-bit words, and int32 outputs.
* The job is cut into tiles of ``tm x tn x tk``. The loop order is ``m0`` (outer), ``n0``,
  ``k0`` (inner). A tile loads its slice of W, and its slice of X unless the whole
  ``tm x K`` block of X fits in the activation buffer, in which case X is loaded once per
  ``m0`` and stays resident while every ``n0`` reuses it.
* With double buffering the weight buffer (and the activation buffer when X is not
  resident) is split into two slots, and the next tile loads while the core computes.
* Loads use every read port, one 32-bit word per port per cycle when the source keeps up.
  Port ``p`` reads words ``p``, ``p + ports``, ``p + 2 ports`` and so on of each transfer.
  Results leave through one write port, one word per cycle.

``plan_tiles`` is the single source of truth for tile sizes: the C driver generated in
``chipleap.gen.driver`` implements the same function and a test compares the two.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from chipleap.hw.design import Accel

# Accumulator rows for the gemv and ws cores: the most activation rows one tile holds.
ACC_ROWS = 8

# Fixed cycles of the generated controller, read off its state machines. A load stage is
# L_NEXT, L_W and L_PUSH (plus L_X when activations load); a DMA transfer spends one cycle
# allocating its first requests and one noticing it is done, on top of the response
# latency (plus a setup cycle for reads); the core stage is C_IDLE and C_FREE around C_RUN; storing a tile adds C_STORE
# and the same two DMA-style cycles; the job ends one cycle after the last tile frees.
LOAD_FIXED = 3
X_FIXED = 1
DMA_FIXED = 3  # a read transfer: setup, allocating the first requests, noticing it is done
STORE_DMA_FIXED = 2  # the store engine has no setup cycle
CORE_FIXED = 2
STORE_FIXED = 3
JOB_FIXED = 1
# Jobs with at most this many tiles use the exact schedule instead of the closed form:
# with few tiles the per-tile averages the closed form relies on are poor, and walking
# the tiles is still cheap.
EXACT_TILES = 256
# The expected largest of n independent standard normal draws, for n read ports.
_MAX_OF_NORMALS = {1: 0.0, 2: 0.5642, 4: 1.0294, 8: 1.4236, 16: 1.7660}


def words_per_row(elements: int, bits: int) -> int:
    return math.ceil(elements * bits / 32)


def k_align(accel: Accel, fmt_bits: int) -> int:
    """K granularity the core and the word-packed buffers need."""
    per_word_w = 32 // fmt_bits
    base = math.lcm(4, per_word_w)  # whole words of int8 X and of packed W
    if accel.dataflow == "gemv":
        return math.lcm(base, accel.cols)
    if accel.dataflow == "ws":
        return math.lcm(base, accel.rows)
    return base


@dataclass(frozen=True)
class TilePlan:
    m: int
    k: int
    n: int
    tm: int
    tn: int
    tk: int
    x_resident: bool
    fmt_bits: int

    @property
    def nm(self) -> int:
        return math.ceil(self.m / self.tm)

    @property
    def nn(self) -> int:
        return math.ceil(self.n / self.tn)

    @property
    def nk(self) -> int:
        return math.ceil(self.k / self.tk)

    @property
    def tiles(self) -> int:
        return self.nm * self.nn * self.nk

    def sizes(self, total: int, step: int) -> list[int]:
        full, rest = divmod(total, step)
        return [step] * full + ([rest] if rest else [])


class TilingError(ValueError):
    pass


def plan_tiles(accel: Accel, m: int, k: int, n: int, fmt_bits: int) -> TilePlan:
    """Choose tile sizes for one job. Mirrors ``chipleap_plan`` in the C driver."""
    if fmt_bits > accel.w_bits:
        raise TilingError(f"int{fmt_bits} weights need PEs of at least {fmt_bits} bits")
    align = k_align(accel, fmt_bits)
    k_pad = math.ceil(k / align) * align
    slots = 2 if accel.double_buffer else 1
    abuf = accel.abuf_kib * 1024
    wbuf = accel.wbuf_kib * 1024
    w_slot_elems = wbuf // slots * 8 // fmt_bits

    if accel.dataflow == "os":
        tm = min(m, accel.rows)
        tn = accel.cols
    else:
        tm = min(m, ACC_ROWS)
        tn = accel.rows if accel.dataflow == "gemv" else accel.cols

    # Keep the whole tm x K block of X resident in the activation buffer when it fits;
    # the weights are then tiled along K on their own.
    if tm * k_pad <= abuf:
        tk = min(k_pad, w_slot_elems // tn) // align * align
        if tk >= align:
            return TilePlan(m, k_pad, n, tm, tn, tk, True, fmt_bits)

    x_slot = abuf // slots
    tk = min(k_pad, w_slot_elems // tn, x_slot // tm)
    tk = tk // align * align
    while tk < align and tm > 1 and accel.dataflow != "os":
        tm = max(1, tm // 2)
        tk = min(k_pad, w_slot_elems // tn, x_slot // tm) // align * align
    if tk < align:
        raise TilingError(
            f"buffers too small: {accel.abuf_kib} KiB activations and {accel.wbuf_kib} KiB "
            f"weights cannot hold one {tm}x{tn}x{align} tile"
        )
    return TilePlan(m, k_pad, n, tm, tn, tk, False, fmt_bits)


def tile_compute_cycles(accel: Accel, tm: int, tn: int, tk: int, last_k: bool) -> int:
    """Cycles the core spends in C_RUN for one tile, as the generated RTL does."""
    r, c = accel.rows, accel.cols
    if accel.dataflow == "gemv":
        # One cycle per activation row and chunk of ``cols`` along K, then the pipeline
        # drains (multiply, then accumulate the last partial sum) and reports completion.
        return tm * (tk // c) + 3
    if accel.dataflow == "ws":
        # Per block of ``rows`` along K: load the weight rows (one per cycle), then stream
        # the tm activation rows through the skewed array until the last result leaves.
        # The activation feed is registered at the array edge, one more cycle per block.
        return (tk // r) * (r + tm + r + c) + 1
    # os: stream tk steps through the skewed array plus the skew of both edges. Results
    # stay in the PEs; the store engine reads them directly.
    # Both edge feeds are registered, one more cycle.
    return tk + r + c


@dataclass(frozen=True)
class Bandwidth:
    """Sustained bytes per accelerator cycle for each data path, and response latencies
    in cycles from grant to data."""

    x: float  # activations, on chip
    w_on: float  # weights or KV held on chip
    w_ext: float  # weights or KV in external memory
    y: float  # result stores
    lat_on: int = 1
    lat_ext: int = 24
    # Random stalls, as the testbench's memory model makes them: each read request is granted
    # with probability ``grant`` in a cycle, on each of ``ports`` read ports independently.
    # The rates above already include the average loss.
    grant: float = 1.0
    ports: int = 1

    def dma(self, nbytes: float, per_cycle: float, lat: int) -> float:
        if nbytes <= 0:
            return 0.0
        cycles = float(math.ceil(nbytes / per_cycle - 1e-9) + lat + DMA_FIXED)
        if self.grant < 1 and self.ports > 1:
            # Each port reads a fixed share of the words, so under random stalls a transfer
            # waits for the slowest port. n words at grant rate g take n / g cycles with a
            # standard deviation of sqrt(n (1 - g)) / g, and the slowest of the ports is late
            # by the expected largest of that many standard normal draws.
            n = math.ceil(nbytes / (4 * self.ports))
            spread = _MAX_OF_NORMALS.get(self.ports, math.sqrt(2 * math.log(self.ports)))
            cycles += spread * math.sqrt(n * (1 - self.grant)) / self.grant
        return cycles

    def w_dma(self, nbytes: float, w_on_frac: float) -> float:
        """A weight transfer that comes from on-chip SRAM for ``w_on_frac`` of the bytes."""
        if nbytes <= 0:
            return 0.0
        if w_on_frac >= 1.0:
            return self.dma(nbytes, self.w_on, self.lat_on)
        if w_on_frac <= 0.0:
            return self.dma(nbytes, self.w_ext, self.lat_ext)
        on = nbytes * w_on_frac
        ext = nbytes - on
        lat = w_on_frac * self.lat_on + (1 - w_on_frac) * self.lat_ext
        return on / self.w_on + ext / self.w_ext + lat + DMA_FIXED


# An accelerator on a clock of its own (chipleap_accel_cdc) reaches the bus through
# asynchronous FIFOs: words still cross at the bus's rate, and a request and its response
# each pass two synchronizer flops of the clock they enter. A response that comes L bus
# cycles after its grant reaches the accelerator k (L + CDC_BUS_CYCLES) + CDC_ACCEL_CYCLES
# of its cycles after the request, k its cycles per bus cycle. Both terms are fitted to
# 1,040 jobs simulated in Verilator without stalls (five accelerators, sixteen pairs of
# clocks with k from 0.39 to 4, each at a random phase): every cycle count is within 5.4 %
# of the model's, 0.1 % on average. The crossing's moment depends on where the clocks'
# edges fall, so no cycle-exact count exists across clocks.
CDC_BUS_CYCLES = 1.25
CDC_ACCEL_CYCLES = 3.0


def crossed(bw: Bandwidth, k: float, ports: int) -> Bandwidth:
    """``bw`` in bus cycles as an accelerator on its own clock sees it, ``k`` of its cycles
    to a bus cycle: rates scaled by the clocks and capped at a word per port (one for
    stores) per accelerator cycle, latencies through the crossings."""
    if k <= 0:
        raise ValueError("the clock ratio must be positive")
    reads = 4.0 * ports

    def lat(bus: int) -> int:
        return max(1, math.ceil(k * (bus + CDC_BUS_CYCLES) + CDC_ACCEL_CYCLES - 1e-9))

    return replace(
        bw,
        x=min(bw.x / k, reads),
        w_on=min(bw.w_on / k, reads),
        w_ext=min(bw.w_ext / k, reads),
        y=min(bw.y / k, 4.0),
        lat_on=lat(bw.lat_on),
        lat_ext=lat(bw.lat_ext),
    )


@dataclass(frozen=True)
class JobTiming:
    plan: TilePlan
    compute: float  # C_RUN cycles over all tiles
    load_x: float  # loader cycles spent on activations
    load_w_on: float  # loader cycles on weights from on-chip SRAM
    load_w_ext: float  # loader cycles on weights from external memory
    store: float  # store cycles
    overhead: float  # fixed controller cycles
    total: float
    x_bytes: float
    w_bytes: float
    y_bytes: float
    buffer_reads: float  # bytes the core reads from its buffers

    @property
    def loads(self) -> float:
        return self.load_x + self.load_w_on + self.load_w_ext


def _counted(total: int, step: int) -> list[tuple[int, int]]:
    """Distinct tile sizes along one dimension with how often each occurs."""
    full, rest = divmod(total, step)
    out = [(step, full)] if full else []
    if rest:
        out.append((rest, 1))
    return out


def job_timing(
    accel: Accel, m: int, k: int, n: int, fmt_bits: int, bw: Bandwidth, w_on_frac: float
) -> JobTiming:
    """Closed-form cycle estimate for one job. ``simulate_job`` gives the exact schedule.

    Sums run over distinct tile sizes (at most two per dimension) rather than over every
    tile, so a 128k-entry vocabulary costs the same as a 64-wide layer.
    """
    p = plan_tiles(accel, m, k, n, fmt_bits)
    ms, ns = _counted(m, p.tm), _counted(n, p.tn)
    ks = _counted(p.k, p.tk)
    k_entries: list[tuple[int, int, bool]] = []
    for i, (size, cnt) in enumerate(ks):
        if i == len(ks) - 1:
            if cnt > 1:
                k_entries.append((size, cnt - 1, False))
            k_entries.append((size, 1, True))
        else:
            k_entries.append((size, cnt, False))

    compute = 0.0
    load_w_on = load_w_ext = 0.0
    w_bytes = 0.0
    store = 0.0
    for tm, cm in ms:
        for tn, cn in ns:
            store += cm * cn * (math.ceil(tm * tn * 4 / bw.y - 1e-9) + bw.lat_on + STORE_FIXED)
            for tk, ck, last in k_entries:
                reps = cm * cn * ck
                compute += reps * tile_compute_cycles(accel, tm, tn, tk, last)
                wb = tn * words_per_row(tk, fmt_bits) * 4
                w_bytes += reps * wb
                cyc = bw.w_dma(wb, w_on_frac)
                load_w_on += reps * cyc * w_on_frac
                load_w_ext += reps * cyc * (1 - w_on_frac)

    if p.x_resident:
        x_bytes = float(m * p.k)
        load_x = sum(cm * (X_FIXED + bw.dma(tm * p.k, bw.x, bw.lat_on)) for tm, cm in ms)
    else:
        x_bytes = float(m * p.k * p.nn)
        load_x = sum(
            cm * p.nn * ck * (X_FIXED + bw.dma(tm * tk, bw.x, bw.lat_on))
            for tm, cm in ms
            for tk, ck in ks
        )
    y_bytes = float(m * n * 4)
    overhead = (LOAD_FIXED + CORE_FIXED) * p.tiles + JOB_FIXED

    load_stage = load_x + load_w_on + load_w_ext + LOAD_FIXED * p.tiles
    core_stage = compute + store + CORE_FIXED * p.tiles
    if accel.double_buffer and p.tiles > 1:
        lavg = load_stage / p.tiles
        cavg = core_stage / p.tiles
        total = lavg + (p.tiles - 1) * max(lavg, cavg) + cavg
        if p.x_resident and p.nm > 1:
            # A new block of X overwrites the resident one, so its load waits for the core
            # to finish the previous block: that load is exposed once per block.
            x_block = load_x / p.nm
            total += (p.nm - 1) * (x_block + max(0.0, lavg - max(0.0, lavg - cavg)))
    else:
        total = load_stage + core_stage
    total += JOB_FIXED
    if p.tiles <= EXACT_TILES:
        total = simulate_job(accel, m, k, n, fmt_bits, bw, w_on_frac)

    reads = w_bytes + float(m * p.k * p.nn)
    return JobTiming(
        p,
        compute,
        load_x,
        load_w_on,
        load_w_ext,
        store,
        overhead,
        total,
        x_bytes,
        w_bytes,
        y_bytes,
        reads,
    )


def simulate_job(
    accel: Accel, m: int, k: int, n: int, fmt_bits: int, bw: Bandwidth, w_on_frac: float = 1.0
) -> float:
    """Cycle-level schedule of one job, following the generated controller state by state.

    The loader and the core are two processes that hand tiles over through one or two
    buffer slots. Each tile's load stage starts when its slot is free (and, for a new
    resident block of X, when the core has drained), and its core stage starts when the
    load has pushed the descriptor and the core has freed the previous tile. Without
    memory stalls this reproduces the RTL's cycle count; with random stalls it uses the
    average rate.
    """
    p = plan_tiles(accel, m, k, n, fmt_bits)
    m_sizes, n_sizes, k_sizes = p.sizes(m, p.tm), p.sizes(n, p.tn), p.sizes(p.k, p.tk)
    slots = 2 if accel.double_buffer else 1
    free: list[int] = []  # cycle of C_FREE for each tile
    t_next = 1  # cycle in which the loader is first in L_NEXT for the coming tile
    core_free = 0
    i = 0
    for tm in m_sizes:
        for ni, tn in enumerate(n_sizes):
            for ki, tk in enumerate(k_sizes):
                first_in_m = ni == 0 and ki == 0
                ready = t_next
                if i >= slots:
                    ready = max(ready, free[i - slots] + 1)
                if p.x_resident and first_in_m and i > 0:
                    ready = max(ready, free[i - 1] + 1)
                dur = LOAD_FIXED
                if p.x_resident:
                    if first_in_m:
                        dur += X_FIXED + int(bw.dma(tm * p.k, bw.x, bw.lat_on))
                else:
                    dur += X_FIXED + int(bw.dma(tm * tk, bw.x, bw.lat_on))
                dur += math.ceil(bw.w_dma(tn * words_per_row(tk, fmt_bits) * 4, w_on_frac))
                push = ready + dur - 1
                t_next = push + 1
                last = ki == len(k_sizes) - 1
                idle = max(push + 1, core_free + 1)
                run = tile_compute_cycles(accel, tm, tn, tk, last)
                stage = CORE_FIXED + run
                if last:
                    stage += 1 + math.ceil(tm * tn * 4 / bw.y - 1e-9) + bw.lat_on + STORE_DMA_FIXED
                f = idle + stage - 1
                free.append(f)
                core_free = f
                i += 1
    return float(free[-1] + JOB_FIXED) if free else 0.0
