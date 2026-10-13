"""Single design changes the agent can make, each with the reason an engineer would try it.

Every move changes one decision (a few are deliberately composite, such as adding an
accelerator, which also needs the NtoM bus and interleaved banks to be useful). Moves
that would produce an invalid X-HEEP configuration are dropped before evaluation.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

from chipleap.explore.score import Point
from chipleap.hw.cpu import CPUS
from chipleap.hw.design import DATAFLOWS, Accel, Design, DesignError
from chipleap.hw.memory import MEMORIES, NONE_KEY
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.spec import Spec
from chipleap.workload.llm import format_bits
from chipleap.workload.ops import Workload

FREQ_STEPS = (25, 50, 75, 90, 100, 150, 200, 300, 400, 600, 800, 1000, 1200)
ARRAY_STEPS = (1, 2, 4, 8, 16, 32, 64)


@dataclass(frozen=True)
class Move:
    kind: str  # stable identifier, for grouping and tests
    label: str  # what changes, in words
    why: str  # the hypothesis behind trying it
    point: Point


def _mem_order(spec: Spec) -> list[str]:
    allowed = [m for m in spec.memories if m != NONE_KEY]
    return sorted(allowed, key=lambda k: MEMORIES[k].peak_gbps)


def _tech_order(spec: Spec) -> list[str]:
    # Oldest (largest) node first.
    return sorted(spec.techs, key=lambda k: -TECHNOLOGIES[k].node_nm)


def _freq_limit(spec: Spec, tech: str, accel: Accel | None = None) -> float:
    """The fastest clock allowed: the node's, the accelerator's measured timing where the
    node has it, and the spec's."""
    t = TECHNOLOGIES[tech]
    limit = (
        t.clock_limit_mhz(accel.dataflow, accel.cols, accel.ports, accel.rows)
        if accel
        else t.max_freq_mhz
    )
    return min(limit, spec.max_freq_mhz) if spec.max_freq_mhz else limit


def _within_clock(spec: Spec, design: Design) -> tuple[Design, str]:
    """``design`` with its clock lowered to the largest step its accelerator closes timing
    at, and a note for the decision log when that changed anything."""
    limit = _freq_limit(spec, design.tech, design.accel)
    if design.freq_mhz <= limit + 1e-9:
        return design, ""
    steps = [f for f in FREQ_STEPS if f <= limit]
    freq = float(steps[-1]) if steps else limit
    return design.with_(
        freq_mhz=freq
    ), f", clock {design.freq_mhz:g} -> {freq:g} MHz to close timing"


def matmul_rows(workload: Workload) -> float:
    """MAC-weighted average number of activation rows: low means GEMV-like work."""
    total = sum(op.macs for op in workload.matmuls) or 1
    return sum(op.m * op.macs for op in workload.matmuls) / total


def widest_operand(workload: Workload) -> int:
    bits = 2
    for op in workload.matmuls:
        b = int(op.w_bits) if op.w_bits >= 8 else (4 if op.w_bits >= 4 else 2)
        bits = max(bits, b)
    return bits


def default_accel(workload: Workload, ports: int = 2) -> Accel:
    if matmul_rows(workload) < 8:
        return Accel(
            "gemv",
            rows=4,
            cols=8,
            w_bits=widest_operand(workload),
            ports=ports,
            abuf_kib=8,
            wbuf_kib=4,
        )
    return Accel(
        "os", rows=8, cols=8, w_bits=widest_operand(workload), ports=ports, abuf_kib=8, wbuf_kib=8
    )


def _valid(d: Design) -> bool:
    try:
        d.validate()
    except DesignError:
        return False
    return True


def neighbours(spec: Spec, point: Point, workload: Workload) -> Iterator[Move]:
    d, wf = point.design, point.weights

    def mv(kind: str, label: str, why: str, design: Design, weights: str = wf) -> Move | None:
        design, note = _within_clock(spec, design)
        if design == d and weights == wf:
            return None
        if not _valid(design):
            return None
        return Move(kind, label + note, why, Point(design, weights))

    out: list[Move | None] = []
    add: Callable[..., None] = lambda *a, **k: out.append(mv(*a, **k))  # noqa: E731

    # Accelerator
    a = d.accel
    if a is None:
        acc = default_accel(workload, ports=2)
        add(
            "add_accel",
            f"add a {acc.dataflow} {acc.rows}x{acc.cols} accelerator with 2 ports",
            "matrix multiplies dominate and the CPU does them one MAC at a time",
            d.with_(accel=acc, bus="NtoM", il_banks=max(d.il_banks, 2)),
        )
    else:
        for name in ("rows", "cols"):
            v = getattr(a, name)
            i = ARRAY_STEPS.index(v) if v in ARRAY_STEPS else -1
            if 0 <= i < len(ARRAY_STEPS) - 1:
                add(
                    "grow_array",
                    f"{name} {v} -> {ARRAY_STEPS[i + 1]}",
                    "more MACs per cycle if the core is the bottleneck",
                    d.with_accel(**{name: ARRAY_STEPS[i + 1]}),
                )
            if i > 0:
                add(
                    "shrink_array",
                    f"{name} {v} -> {ARRAY_STEPS[i - 1]}",
                    "fewer MACs save area and energy when the core waits on memory",
                    d.with_accel(**{name: ARRAY_STEPS[i - 1]}),
                )
        for df in DATAFLOWS:
            if df != a.dataflow:
                add(
                    "dataflow",
                    f"dataflow {a.dataflow} -> {df}",
                    "a different dataflow reuses operands differently at this batch size",
                    d.with_accel(dataflow=df),
                )
        if a.ports < 16:
            p2 = a.ports * 2
            add(
                "more_ports",
                f"accelerator ports {a.ports} -> {p2}",
                "each 32-bit OBI port moves 4 bytes per cycle; more ports raise bandwidth",
                d.with_(
                    bus="NtoM",
                    il_banks=max(d.il_banks, min(8, p2)),
                    banks=min(d.banks, 16 - max(d.il_banks, min(8, p2))),
                    accel=replace(a, ports=p2),
                ),
            )
        if a.ports > 1:
            add(
                "fewer_ports",
                f"accelerator ports {a.ports} -> {a.ports // 2}",
                "spare ports cost bus area",
                d.with_accel(ports=a.ports // 2),
            )
        for buf in ("abuf_kib", "wbuf_kib"):
            v = getattr(a, buf)
            if v < 64:
                add(
                    "grow_buffer",
                    f"{buf} {v} -> {v * 2} KiB",
                    "larger tiles amortise per-tile latency and keep activations resident",
                    d.with_accel(**{buf: v * 2}),
                )
            if v > 1:
                add(
                    "shrink_buffer",
                    f"{buf} {v} -> {v // 2} KiB",
                    "smaller buffers save area when tiles are already large enough",
                    d.with_accel(**{buf: v // 2}),
                )
        add(
            "double_buffer",
            f"double buffering {'off' if a.double_buffer else 'on'}",
            "overlapping loads with compute hides the shorter of the two",
            d.with_accel(double_buffer=not a.double_buffer),
        )
        add(
            "remove_accel",
            "remove the accelerator",
            "the CPU may be enough when the workload is small",
            d.with_(accel=None),
        )

    # Clock
    limit = _freq_limit(spec, d.tech, d.accel)
    ups = [f for f in FREQ_STEPS if d.freq_mhz < f <= limit]
    downs = [f for f in FREQ_STEPS if f < d.freq_mhz]
    if ups:
        add(
            "raise_freq",
            f"clock {d.freq_mhz:g} -> {ups[0]:g} MHz",
            "a faster clock speeds up compute and every bus port",
            d.with_(freq_mhz=float(ups[0])),
        )
    if downs:
        add(
            "lower_freq",
            f"clock {d.freq_mhz:g} -> {downs[-1]:g} MHz",
            "a slower clock saves power when there is throughput to spare",
            d.with_(freq_mhz=float(downs[-1])),
        )

    # External memory
    order = _mem_order(spec)
    io = TECHNOLOGIES[d.tech].max_io_mts
    node = TECHNOLOGIES[d.tech].node_nm
    if d.ext_mem == NONE_KEY:
        for m in order:
            if MEMORIES[m].available(node, io):
                add(
                    "add_ext_mem",
                    f"add {m}",
                    "the working set does not fit in on-chip SRAM",
                    d.with_(ext_mem=m),
                )
                break
    else:
        i = order.index(d.ext_mem) if d.ext_mem in order else -1
        for m in order[i + 1 :]:
            if MEMORIES[m].available(node, io):
                add(
                    "faster_mem",
                    f"external memory {d.ext_mem} -> {m}",
                    "weights stream from external memory every step; bandwidth bounds throughput",
                    d.with_(ext_mem=m),
                )
                break
        if i > 0:
            add(
                "slower_mem",
                f"external memory {d.ext_mem} -> {order[i - 1]}",
                "a slower interface can be cheaper in area and energy per bit",
                d.with_(ext_mem=order[i - 1]),
            )
        if d.ext_channels < 4:
            add(
                "more_channels",
                f"external channels {d.ext_channels} -> {d.ext_channels * 2}",
                "parallel memory channels multiply bandwidth and capacity",
                d.with_(ext_channels=d.ext_channels * 2),
            )
        if d.ext_channels > 1:
            add(
                "fewer_channels",
                f"external channels {d.ext_channels} -> {d.ext_channels // 2}",
                "each channel costs pins, PHY area and idle power",
                d.with_(ext_channels=d.ext_channels // 2),
            )
        fits_on_chip = workload.storage.total <= (d.sram_kib - spec.firmware_kib) * 1024
        if NONE_KEY in spec.memories and fits_on_chip:
            add(
                "drop_ext_mem",
                "remove external memory",
                "everything may fit on chip",
                d.with_(ext_mem=NONE_KEY, ext_channels=1),
            )

    # Technology node, together with what the new node allows
    techs = _tech_order(spec)
    ti = techs.index(d.tech) if d.tech in techs else -1
    for direction, other in (
        ("newer", techs[ti + 1] if ti + 1 < len(techs) else None),
        ("older", techs[ti - 1] if ti > 0 else None),
    ):
        if other is None:
            continue
        t = TECHNOLOGIES[other]
        freq = min(
            d.freq_mhz * (2 if direction == "newer" else 1), _freq_limit(spec, other, d.accel)
        )
        freq = max(f for f in FREQ_STEPS if f <= freq) if freq >= FREQ_STEPS[0] else freq
        mem = d.ext_mem
        if mem != NONE_KEY:
            usable = [m for m in order if MEMORIES[m].available(t.node_nm, t.max_io_mts)]
            if direction == "newer" and usable:
                mem = usable[-1]
            elif not MEMORIES[mem].available(t.node_nm, t.max_io_mts):
                mem = usable[-1] if usable else NONE_KEY
        add(
            "node",
            f"technology {d.tech} -> {other}"
            + (f", {freq:g} MHz" if freq != d.freq_mhz else "")
            + (f", {mem}" if mem != d.ext_mem else ""),
            "a newer node allows faster clocks and memory interfaces at lower energy"
            if direction == "newer"
            else "an older node is cheaper and may be enough",
            d.with_(tech=other, freq_mhz=float(freq), ext_mem=mem),
        )

    # On-chip SRAM
    if d.banks * 2 + d.il_banks <= 16:
        add(
            "more_sram",
            f"banks {d.banks} -> {d.banks * 2}",
            "more SRAM keeps activations, KV cache or weights on chip",
            d.with_(banks=d.banks * 2),
        )
    if d.bank_kib < 128:
        add(
            "bigger_banks",
            f"bank size {d.bank_kib} -> {d.bank_kib * 2} KiB",
            "larger banks add capacity without more bus slaves",
            d.with_(bank_kib=d.bank_kib * 2),
        )
    if d.banks > 1:
        add(
            "less_sram",
            f"banks {d.banks} -> {d.banks // 2}",
            "unused SRAM costs area and leakage",
            d.with_(banks=d.banks // 2),
        )
    if d.bank_kib > 8:
        add(
            "smaller_banks",
            f"bank size {d.bank_kib} -> {d.bank_kib // 2} KiB",
            "unused SRAM costs area and leakage",
            d.with_(bank_kib=d.bank_kib // 2),
        )
    if d.bus == "NtoM":
        nxt = {0: 2, 2: 4, 4: 8}.get(d.il_banks)
        if nxt and d.banks + nxt <= 16:
            add(
                "interleave",
                f"interleaved banks {d.il_banks} -> {nxt}",
                "interleaving spreads consecutive words over banks so ports read in parallel",
                d.with_(il_banks=nxt),
            )
        prev = {8: 4, 4: 2, 2: 0}.get(d.il_banks)
        if prev is not None and not (prev == 0 and d.accel and d.accel.ports > 1):
            add(
                "deinterleave",
                f"interleaved banks {d.il_banks} -> {prev}",
                "fewer banks save area if ports do not need them",
                d.with_(il_banks=prev),
            )
    elif d.accel is None or d.accel.ports == 1:
        add(
            "bus",
            "bus onetoM -> NtoM",
            "the NtoM crossbar lets several masters reach different banks at once",
            d.with_(bus="NtoM"),
        )
    if d.bus == "NtoM" and d.il_banks == 0 and (d.accel is None or d.accel.ports == 1):
        add(
            "bus",
            "bus NtoM -> onetoM",
            "with one accelerator port and no interleaved banks the crossbar buys nothing",
            d.with_(bus="onetoM"),
        )

    # CPU
    cpus = [c for c in CPUS if c in spec.cpus]
    ci = cpus.index(d.cpu) if d.cpu in cpus else -1
    if 0 <= ci < len(cpus) - 1:
        add(
            "faster_cpu",
            f"CPU {d.cpu} -> {cpus[ci + 1]}",
            "norms, softmax and activations run on the CPU",
            d.with_(cpu=cpus[ci + 1]),
        )
    if ci > 0:
        add(
            "smaller_cpu",
            f"CPU {d.cpu} -> {cpus[ci - 1]}",
            "a smaller core saves area and energy if the CPU is not the bottleneck",
            d.with_(cpu=cpus[ci - 1]),
        )

    # Precision, when the spec lets the explorer choose it
    formats = sorted(spec.allowed_weight_formats(), key=lambda f: -format_bits(f)[0])
    if len(formats) > 1 and wf in formats:
        i = formats.index(wf)
        if i + 1 < len(formats):
            add(
                "lower_precision",
                f"weights {wf} -> {formats[i + 1]}",
                "fewer bits per weight cut the bytes streamed each step",
                d,
                formats[i + 1],
            )
        if i > 0:
            add(
                "higher_precision",
                f"weights {wf} -> {formats[i - 1]}",
                "more bits per weight keep accuracy when there is bandwidth to spare",
                d,
                formats[i - 1],
            )

    for move in out:
        if move is not None:
            yield move
