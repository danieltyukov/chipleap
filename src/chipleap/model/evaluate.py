"""Analytical evaluation of a workload on a design point.

``evaluate`` returns throughput, latency, energy, power and area with a per-operator
breakdown and the reason each operator takes the time it does. The model is meant to
rank design points quickly and to explain them, not to replace simulation: the
accelerator's cycle model is checked against RTL simulation by ``chipleap verify``, and
docs/model.md states every assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache

from chipleap.hw.cpu import CPUModel, get_cpu
from chipleap.hw.design import Accel, Design
from chipleap.hw.memory import MEMORIES, ExternalMemory, get_memory
from chipleap.hw.tech import (
    AXI_ADAPTER_KGE,
    Technology,
    accel_bank_kge,
    accel_logic_kge,
    get_tech,
)
from chipleap.model.accel import Bandwidth, TilingError, job_timing
from chipleap.workload.ops import MatMul, Operand, VectorKind, VectorOp, Workload

# Logic outside the CPU, memories and accelerator: debug module, DMA, boot ROM, power
# manager, timers, interrupt controllers, UART, SPI, I2C, GPIO, pad control.
XHEEP_BASE_KGE = 110.0
# Area entries that make up the generated accelerator IP (see Evaluation.ip_area).
IP_AREA_KEYS = ("accelerator logic", "accelerator buffers", "axi adapter")
# Firmware code, data and stack kept in the first banks.
DEFAULT_FIRMWARE_KIB = 32
# CPU cycles to program and start one accelerator job and take its completion interrupt.
JOB_SETUP_CYCLES = 80
# X-HEEP's 4 GiB address space leaves at most 2 GiB for external memory.
MAX_EXT_WINDOW = 2 * 1024**3
# Fraction of active-cycle energy a core burns while waiting in WFI with its clock gated.
CPU_IDLE_FRACTION = 0.1


class Issues(list[str]):
    """Violation messages with a severity: data that has nowhere to live (it does not fit,
    exceeds a memory or the address window) weighs 2, every other violation 1."""

    severity: int = 0

    def add(self, msg: str, weight: int = 1) -> None:
        self.append(msg)
        self.severity += weight


@dataclass
class OpResult:
    name: str
    unit: str  # "accel" or "cpu"
    count: int
    macs: int
    time_s: float
    energy_pj: float
    ext_bytes: float
    onchip_bytes: float
    bound: str  # compute | external memory | accelerator ports | on-chip memory | control
    parts: dict[str, float] = field(default_factory=dict)  # seconds by component
    utilization: float = 0.0
    note: str = ""


@dataclass
class Evaluation:
    design: Design
    workload: Workload
    time_s: float
    energy_pj: float
    area_mm2: dict[str, float]
    energy_pj_by: dict[str, float]
    time_by_bound: dict[str, float]
    ops: list[OpResult]
    placement: dict[str, float]  # fraction of each storage class held on chip
    ext_bytes: float
    onchip_kib_used: float
    violations: list[str]
    warnings: list[str]
    bandwidth: dict[str, float]  # bytes per second of each data path
    severity: int = 0  # weighted violations, see Issues

    @property
    def feasible(self) -> bool:
        return not self.violations

    @property
    def throughput(self) -> float:
        """Units (tokens, inferences) per second."""
        return self.workload.tokens_per_step / self.time_s if self.time_s > 0 else 0.0

    @property
    def latency_ms(self) -> float:
        return self.time_s * 1e3

    @property
    def energy_per_unit_mj(self) -> float:
        return self.energy_pj / 1e9 / self.workload.tokens_per_step

    @property
    def power_mw(self) -> float:
        return self.energy_pj / 1e9 / self.time_s if self.time_s > 0 else 0.0

    @property
    def total_area(self) -> float:
        return sum(self.area_mm2.values())

    @property
    def ip_area(self) -> float:
        """What ChipLeap generates by itself: the accelerator and, on the standalone
        platform, its AXI4 adapter. On X-HEEP the rest of the chip is generated too; on
        the standalone platform it is the host system the model assumes."""
        return sum(v for k, v in self.area_mm2.items() if k in IP_AREA_KEYS)

    @property
    def bound(self) -> str:
        return max(self.time_by_bound.items(), key=lambda kv: kv[1])[0]

    def bound_share(self) -> dict[str, float]:
        total = sum(self.time_by_bound.values()) or 1.0
        return {k: v / total for k, v in self.time_by_bound.items()}

    def metrics(self) -> dict[str, float]:
        return {
            "throughput": self.throughput,
            "latency_ms": self.latency_ms,
            "energy_mj": self.energy_per_unit_mj,
            "power_mw": self.power_mw,
            "area_mm2": self.total_area,
            "ip_area_mm2": self.ip_area,
        }


@dataclass(frozen=True)
class _Ctx:
    design: Design
    tech: Technology
    cpu: CPUModel
    mem: ExternalMemory | None
    f: float  # Hz
    bw: Bandwidth  # per accelerator cycle
    cpu_dma_ext: float  # bytes per second the X-HEEP DMA moves from external memory
    ext_limit: str  # what caps the accelerator's external stream: memory or its own ports
    frac_on: dict[str, float]
    sram_pj: float
    e_cpu_cycle: float


def _bandwidth(
    d: Design, mem: ExternalMemory | None, f: float, io_mts: int
) -> tuple[Bandwidth, float, str]:
    ports = d.accel.ports if d.accel else 1
    # Consecutive words land in different interleaved banks; a continuous bank serves one
    # word per cycle no matter how many masters ask.
    par_banks = max(1, d.il_banks)
    on_wpc = min(ports, par_banks) if d.bus == "NtoM" else 1
    x = 4.0 * on_wpc
    ext_bps = 0.0
    ext_limit = "external memory"
    if mem is not None:
        ext_ports = ports if d.bus == "NtoM" else 1
        mem_bps = mem.sustained_bps_at(io_mts) * d.ext_channels
        port_bps = 4.0 * ext_ports * f
        ext_bps = min(mem_bps, port_bps)
        if d.accel and port_bps < mem_bps:
            # The memory could deliver more than the accelerator's 32-bit ports take in, so
            # more ports (or a faster clock), not a faster memory, would speed the stream.
            ext_limit = "accelerator ports"
    lat_ext = 24 if mem is None else max(8, round(60e-9 * f) + 4)
    # Without external memory, data that does not fit is a violation. Its timing then
    # assumes the slowest interface (quad SPI flash), so an unbuildable design never looks
    # faster than a buildable one.
    floor_bps = MEMORIES["qspi-flash"].sustained_bps_at(io_mts)
    bw = Bandwidth(
        x=x,
        w_on=x,
        w_ext=(ext_bps if ext_bps else floor_bps) / f,
        y=4.0,
        lat_on=2,
        lat_ext=lat_ext,
    )
    cpu_dma = min(mem.sustained_bps_at(io_mts) * d.ext_channels, 4.0 * f) if mem else floor_bps
    return bw, cpu_dma, ext_limit


def _place(
    w: Workload, d: Design, mem: ExternalMemory | None, firmware_kib: float, violations: Issues
) -> tuple[dict[str, float], float]:
    """Decide what lives in on-chip SRAM. Activations come first (each byte is touched
    several times per step), then the KV cache, then weights; whatever does not fit goes
    to external memory."""
    sram = d.sram_kib * 1024.0
    avail = sram - firmware_kib * 1024
    continuous = d.banks * d.bank_kib
    if continuous < firmware_kib:
        # X-HEEP links code and stack into the continuous banks, not the interleaved ones.
        violations.add(
            f"the continuous banks ({continuous} KiB) cannot hold the {firmware_kib:g} KiB of "
            "firmware",
            2,
        )
        avail = max(0.0, avail)
    elif avail < 0:
        violations.add(
            f"on-chip SRAM ({d.sram_kib} KiB) cannot hold the {firmware_kib:g} KiB of firmware", 2
        )
        avail = 0.0
    act = w.storage.activations
    act_on = min(1.0, avail / act) if act else 1.0
    avail -= act_on * act
    kv_on = min(1.0, avail / w.storage.kv) if w.storage.kv else 1.0
    avail -= kv_on * w.storage.kv
    w_on = min(1.0, avail / w.storage.weights) if w.storage.weights else 1.0
    avail -= w_on * w.storage.weights
    used = sram - max(avail, 0.0)

    ext_act = (1 - act_on) * act
    ext_kv = (1 - kv_on) * w.storage.kv
    ext_w = (1 - w_on) * w.storage.weights
    ext_need = ext_act + ext_kv + ext_w
    if ext_need > 1:
        if mem is None:
            violations.add(
                f"needs {(ext_need + sram) / 2**20:.2f} MiB but the design has "
                f"{d.sram_kib} KiB on chip and no external memory",
                2,
            )
        else:
            cap = mem.capacity_mib * 2**20 * d.ext_channels
            if mem.read_only and ext_kv + ext_act > 1:
                violations.add(
                    f"{mem.name} is read only, so the KV cache and activations "
                    f"({(ext_kv + ext_act) / 2**20:.2f} MiB that do not fit) must stay on chip"
                )
            if ext_need > cap:
                violations.add(
                    f"external data {ext_need / 2**20:.0f} MiB exceeds {d.ext_channels} x "
                    f"{mem.capacity_mib} MiB of {mem.name}",
                    2,
                )
            if ext_need > MAX_EXT_WINDOW:
                violations.add(
                    f"external data {ext_need / 2**30:.2f} GiB exceeds the 2 GiB window a "
                    "32-bit X-HEEP can map",
                    2,
                )
    return {"act": act_on, "kv": kv_on, "weights": w_on}, used


def _matmul_fmt_bits(op: MatMul) -> int:
    # Storage bits include group scales (4.25); the core computes on the integer part.
    return int(op.w_bits) if op.w_bits >= 8 else (4 if op.w_bits >= 4 else 2)


def _accel_matmul(op: MatMul, c: _Ctx) -> OpResult | None:
    a = c.design.accel
    if a is None:
        return None
    fmt = _matmul_fmt_bits(op)
    if fmt > a.w_bits:
        return None
    frac = c.frac_on["kv" if op.w_kind is Operand.KV else "weights"]
    if op.w_kind is Operand.ACT:
        frac = c.frac_on["act"]
    act_on = c.frac_on["act"]
    bw = c.bw
    if act_on < 1.0:
        # Activations that did not fit on chip stream from external memory.
        ext = min(bw.w_ext, bw.x)
        bw = replace(
            bw,
            x=1 / (act_on / bw.x + (1 - act_on) / ext),
            y=1 / (act_on / bw.y + (1 - act_on) / min(bw.y, ext)),
        )
    try:
        t = job_timing(a, op.m, op.k, op.n, fmt, bw, frac)
    except TilingError:
        return None
    # Group scales travel with the weights even though the core does not use them.
    scale_bytes = t.w_bytes * (op.w_bits - fmt) / fmt
    w_ext_bytes = (t.w_bytes + scale_bytes) * (1 - frac)
    act_ext_bytes = (t.x_bytes + t.y_bytes) * (1 - act_on)
    extra_ext_cycles = scale_bytes * (1 - frac) / c.bw.w_ext
    setup = JOB_SETUP_CYCLES
    cycles = t.total + extra_ext_cycles + setup
    time = cycles * op.count / c.f
    tech = c.tech

    # The generated PEs multiply 8 x 8 whatever the weight format.
    mac_e = tech.mac_pj(op.x_bits, 8) * (1.25 if a.dataflow != "gemv" else 1.0)
    buf_e = tech.sram_energy_per_byte(max(a.abuf_kib, a.wbuf_kib))
    onchip = (t.x_bytes + t.y_bytes) * act_on + t.w_bytes * frac
    ext_bytes = w_ext_bytes + act_ext_bytes
    energy = (
        op.m * op.k * op.n * mac_e
        + (t.x_bytes + t.w_bytes + t.buffer_reads) * buf_e
        + onchip * c.sram_pj
        + ext_bytes * 8 * (c.mem.pj_per_bit if c.mem else 0.0)
        + setup * c.e_cpu_cycle
        + (t.total - setup) * c.e_cpu_cycle * CPU_IDLE_FRACTION
    ) * op.count

    def secs(x: float) -> float:
        return x * op.count / c.f

    act_io = t.load_x + t.store
    parts = {
        "compute": secs(t.compute),
        c.ext_limit: secs(t.load_w_ext + extra_ext_cycles + act_io * (1 - act_on)),
        "on-chip memory": secs(act_io * act_on + t.load_w_on),
        "control": secs(t.overhead + setup),
    }
    if a.double_buffer and t.plan.tiles > 1:
        # Overlapped loads hide behind compute (or the reverse); attribute the total to
        # the dominant side so the shares add up to the operator's time.
        loads = t.load_x + t.load_w_on + t.load_w_ext + extra_ext_cycles
        hidden = min(loads, t.compute)
        if loads >= t.compute:
            parts["compute"] = max(0.0, parts["compute"] - secs(hidden))
        else:
            scale = max(0.0, 1 - hidden / loads) if loads else 0.0
            parts[c.ext_limit] = secs(
                (t.load_w_ext + extra_ext_cycles + t.load_x * (1 - act_on)) * scale
                + t.store * (1 - act_on)
            )
            parts["on-chip memory"] = secs(
                (t.load_x * act_on + t.load_w_on) * scale + t.store * act_on
            )
    total_parts = sum(parts.values()) or 1.0
    parts = {k: v * time / total_parts for k, v in parts.items()}
    bound = max(parts.items(), key=lambda kv: kv[1])[0]
    peak = a.macs * cycles * op.count
    util = op.macs / peak if peak else 0.0
    return OpResult(
        op.name,
        "accel",
        op.count,
        op.macs,
        time,
        energy,
        ext_bytes * op.count,
        onchip * op.count,
        bound,
        parts,
        util,
        f"tile {t.plan.tm}x{t.plan.tn}x{t.plan.tk}" + (", X resident" if t.plan.x_resident else ""),
    )


def _cpu_matmul(op: MatMul, c: _Ctx, why: str) -> OpResult:
    fmt = _matmul_fmt_bits(op)
    cycles = op.macs * c.cpu.mac_cycles(fmt)
    compute_t = cycles / c.f
    frac = c.frac_on["kv" if op.w_kind is Operand.KV else "weights"]
    if op.w_kind is Operand.ACT:
        frac = 1.0
    w_ext = op.w_bytes * (1 - frac)
    dma_t = w_ext / c.cpu_dma_ext if w_ext else 0.0
    time = max(compute_t, dma_t)
    onchip = op.w_bytes + op.macs / 4 + op.y_elements
    energy = (
        cycles * c.e_cpu_cycle
        + onchip * c.sram_pj
        + w_ext * 8 * (c.mem.pj_per_bit if c.mem else 0.0)
    )
    bound = "compute" if compute_t >= dma_t else "external memory"
    parts = {"compute": compute_t, "external memory": max(0.0, dma_t - compute_t)}
    return OpResult(
        op.name, "cpu", op.count, op.macs, time, energy, w_ext, onchip, bound, parts, 0.0, why
    )


def _vector(op: VectorOp, c: _Ctx) -> OpResult:
    cycles = op.total_elements * c.cpu.element_cycles(op.kind)
    time = cycles / c.f
    traffic = op.total_elements * op.bits / 8 * (1 if op.kind is VectorKind.COPY else 2)
    parts = {"compute": time}
    # Activations that live off chip are moved in and out by the DMA.
    ext = traffic * (1 - c.frac_on["act"])
    if ext:
        t_act = ext / c.cpu_dma_ext
        parts["external memory"] = t_act
        time += t_act
    traffic -= ext
    if op.kv_bytes:
        kv_ext = op.kv_bytes * op.count * (1 - c.frac_on["kv"])
        if kv_ext:
            t_ext = kv_ext / c.cpu_dma_ext
            parts["external memory"] = parts.get("external memory", 0.0) + t_ext
            time += t_ext
            ext += kv_ext
    energy = (
        cycles * c.e_cpu_cycle
        + traffic * c.sram_pj
        + ext * 8 * (c.mem.pj_per_bit if c.mem else 0.0)
    )
    bound = max(parts.items(), key=lambda kv: kv[1])[0]
    return OpResult(op.name, "cpu", op.count, 0, time, energy, ext, traffic, bound, parts)


def _requant(outputs: int, c: _Ctx) -> OpResult:
    op = VectorOp("requant", VectorKind.REQUANT, outputs)
    r = _vector(op, c)
    r.energy_pj += outputs * 4 * c.sram_pj  # int32 read back
    return r


def buffer_mm2(a: Accel, tech: Technology) -> float:
    """The accelerator's activation and weight buffers. On IHP SG13G2 they are priced
    with the PDK's own macros (``chipleap.macro.sram.IHP_1P``). A GEMV engine gets the
    banks the layout gives a macro-backed build (``chipleap.gen.banks``: a bank of COLS
    bytes per slot, and per lane for the weights), whatever its number of DMA ports,
    since more ports read the same words; for a one-port build these are exactly the
    macros the layout places. Any other build, and one whose banks' floorplan does not
    route (``floorplan_reason``), gets each slot of each buffer as the macros of 32-bit
    words ``choose`` picks. Either way a 1 KiB slot of a double buffer
    costs the macro that holds it, not 1 KiB of the largest macro's density. Other nodes
    use ``Technology.sram_mm2``."""
    if tech.key == "ihp-sg13g2":
        return _ihp_buffer_mm2(a)
    return tech.sram_mm2(a.abuf_kib) + tech.sram_mm2(a.wbuf_kib)


def accel_kge(a: Accel, tech: Technology) -> float:
    """The accelerator's logic. On IHP SG13G2, a GEMV engine whose buffers are priced as
    the banks of a macro-backed build (``buffer_mm2``) also has those banks' interface
    (``accel_bank_kge``): the macros' write path and the registers that read a chunk a
    cycle ahead."""
    kge = accel_logic_kge(a.dataflow, a.rows, a.cols, a.ports)
    if tech.key == "ihp-sg13g2" and _banked(a):
        kge += accel_bank_kge(a.rows, a.cols, a.double_buffer)
    return kge


def _banked(a: Accel) -> bool:
    """Whether ``a``'s buffers are banks of IHP macros, as a one-port build of it lays them
    out; more ports read the same words, so they get the same banks."""
    return _layout_reason(a) is None


@lru_cache(maxsize=4096)
def _layout_reason(a: Accel) -> str | None:
    from chipleap.gen import banks
    from chipleap.macro.sram import IHP_1P

    return banks.layout_reason(replace(a, ports=1), IHP_1P)


def floorplan_reason(a: Accel, tech: Technology) -> str | None:
    """On IHP SG13G2, why a GEMV engine whose core could read its buffers as banks of macros
    gets flop buffers in ChipLeap's layout instead: their floorplan does not route
    (``chipleap.gen.banks.too_flat``), and place and route keeps flop buffers, so the model
    prices and times it as a build without banks. None for any other build or node."""
    from chipleap.gen import banks

    if tech.key != "ihp-sg13g2" or banks.unsupported(replace(a, ports=1)) is not None:
        return None
    return _layout_reason(a)


def bank_read_ns(a: Accel, tech: Technology) -> float:
    """On IHP SG13G2, the slowest read at the slow corner of the macros a GEMV engine's
    buffers are priced as (``buffer_mm2``), which caps its clock (``accel_signoff_mhz``);
    0 for any other build or node."""
    if tech.key != "ihp-sg13g2" or not _banked(a):
        return 0.0
    return _ihp_bank_read_ns(a)


@lru_cache(maxsize=4096)
def _ihp_bank_read_ns(a: Accel) -> float:
    from chipleap.gen import banks
    from chipleap.macro.sram import IHP_1P, IHP_1P_READ_NS

    plan = banks.plan(replace(a, ports=1), IHP_1P)
    return max(IHP_1P_READ_NS[b.macro.cell] for b in (plan.abuf, plan.wbuf))


@lru_cache(maxsize=4096)
def _ihp_buffer_mm2(a: Accel) -> float:
    from chipleap.gen import banks
    from chipleap.macro.sram import IHP_1P, choose

    if _banked(a):
        return banks.plan(replace(a, ports=1), IHP_1P).area_um2 / 1e6
    slots = 2 if a.double_buffer else 1
    total = 0.0
    for kib in (a.abuf_kib, a.wbuf_kib):
        words = int(kib * 1024) // 4 // slots
        if words > 0:
            total += slots * choose(IHP_1P, words, 32).area_um2
    return total / 1e6


def area_breakdown(
    d: Design, tech: Technology, cpu: CPUModel, mem: ExternalMemory | None
) -> dict[str, float]:
    area: dict[str, float] = {}
    area["cpu"] = tech.logic_mm2(cpu.kge)
    masters = 4 + 2 + (d.accel.ports + 1 if d.accel else 0)
    slaves = d.banks + d.il_banks + 6
    bus_kge = 8.0 if d.bus == "onetoM" else 0.6 * masters * slaves
    area["soc logic"] = tech.logic_mm2(XHEEP_BASE_KGE + bus_kge)
    area["sram"] = d.banks * tech.sram_mm2(d.bank_kib) + d.il_banks * tech.sram_mm2(d.il_bank_kib)
    if d.accel:
        a = d.accel
        area["accelerator logic"] = tech.logic_mm2(accel_kge(a, tech))
        area["accelerator buffers"] = buffer_mm2(a, tech)
        if d.platform == "standalone":
            area["axi adapter"] = tech.logic_mm2(AXI_ADAPTER_KGE)
    if mem is not None:
        area["memory interface"] = mem.phy_mm2(tech.node_nm) * d.ext_channels + tech.logic_mm2(
            mem.phy_kge * d.ext_channels
        )
    return area


def evaluate(
    workload: Workload, design: Design, *, firmware_kib: float = DEFAULT_FIRMWARE_KIB
) -> Evaluation:
    design.validate()
    tech = get_tech(design.tech)
    cpu = get_cpu(design.cpu)
    mem = get_memory(design.ext_mem)
    f = design.freq_mhz * 1e6
    violations = Issues()
    warnings: list[str] = []

    if design.freq_mhz > tech.max_freq_mhz:
        violations.add(
            f"{design.freq_mhz:g} MHz is above the {tech.max_freq_mhz:g} MHz practical at "
            f"{tech.name}"
        )
    elif design.accel and tech.accel_timing:
        a = design.accel
        read = bank_read_ns(a, tech)
        limit = tech.clock_limit_mhz(a.dataflow, a.cols, a.ports, a.rows, read)
        if design.freq_mhz > limit + 1e-9:
            buffers = f" and its buffers in SRAM macros that read in {read:.1f} ns" if read else ""
            violations.add(
                f"the {a.dataflow} {a.rows}x{a.cols} accelerator with {a.ports} port"
                f"{'s' if a.ports > 1 else ''}{buffers} closes timing at about {limit:.0f} MHz "
                f"on {tech.name} after routing, at the slow corner"
            )
    if mem is not None:
        if not mem.phy_exists(tech.node_nm):
            violations.add(
                f"no {mem.name} PHY is known at {tech.node_nm} nm (oldest known: "
                f"{mem.phy_max_node_nm} nm)"
            )
        elif not mem.runs_at(tech.max_io_mts):
            violations.add(
                f"{mem.name} needs at least {mem.min_mts} MT/s; I/O at {tech.name} runs at "
                f"about {tech.max_io_mts} MT/s"
            )
        elif mem.mts > tech.max_io_mts:
            warnings.append(
                f"{mem.name} runs at {tech.max_io_mts} MT/s instead of {mem.mts}, the I/O "
                f"limit at {tech.name}"
            )

    frac_on, used = _place(workload, design, mem, firmware_kib, violations)
    bw, cpu_dma, ext_limit = _bandwidth(design, mem, f, tech.max_io_mts)
    e_cpu = cpu.kge * tech.cpu_pj_per_kge_cycle
    ctx = _Ctx(
        design,
        tech,
        cpu,
        mem,
        f,
        bw,
        cpu_dma if cpu_dma else 1e-9,
        ext_limit,
        frac_on,
        tech.bank_energy_per_byte(design.bank_kib),
        e_cpu,
    )

    results: list[OpResult] = []
    accel_outputs = 0
    fallback: set[str] = set()
    for op in workload.ops:
        if isinstance(op, MatMul):
            r = _accel_matmul(op, ctx)
            if r is None:
                why = "no accelerator"
                if design.accel is not None:
                    why = f"int{_matmul_fmt_bits(op)} operand is wider than the int{design.accel.w_bits} PEs"
                    fallback.add(op.name)
                r = _cpu_matmul(op, ctx, why)
            else:
                accel_outputs += op.y_elements
            results.append(r)
        else:
            results.append(_vector(op, ctx))
    if accel_outputs:
        results.append(_requant(accel_outputs, ctx))
    if fallback:
        warnings.append(
            "running on the CPU because the accelerator cannot take their operands: "
            + ", ".join(sorted(fallback))
        )

    time = sum(r.time_s for r in results)
    area = area_breakdown(design, tech, cpu, mem)
    leak = tech.leak_mw_per_mm2 * sum(area.values()) * 1e-3 * time * 1e12  # pJ
    energy_by: dict[str, float] = {
        "accelerator": sum(r.energy_pj for r in results if r.unit == "accel"),
        "cpu": sum(r.energy_pj for r in results if r.unit == "cpu"),
        "leakage": leak,
    }
    ext_bytes = sum(r.ext_bytes for r in results)
    ext_e = ext_bytes * 8 * (mem.pj_per_bit if mem else 0.0)
    # Split external-memory energy out of the units that caused it, for the breakdown.
    if ext_e:
        energy_by["external memory"] = ext_e
        share_acc = sum(r.ext_bytes for r in results if r.unit == "accel") / ext_bytes
        energy_by["accelerator"] -= ext_e * share_acc
        energy_by["cpu"] -= ext_e * (1 - share_acc)

    by_bound: dict[str, float] = {}
    for r in results:
        for k, v in r.parts.items():
            by_bound[k] = by_bound.get(k, 0.0) + v

    if design.accel and (flat := floorplan_reason(design.accel, tech)):
        warnings.append(
            f"the accelerator's buffers are flop buffers in ChipLeap's layout, not banks of "
            f"SRAM macros: {flat}; the model prices each buffer slot in macros of 32-bit "
            "words and holds the clock to the flop-buffer build's, as for any build without "
            "banks"
        )
    if design.accel and design.accel.ports > max(1, design.il_banks) and design.bus == "NtoM":
        warnings.append(
            f"{design.accel.ports} accelerator ports but only {max(1, design.il_banks)} "
            "interleaved banks: on-chip reads cannot use every port"
        )

    bandwidth = {
        "on-chip": bw.x * f,
        "external": bw.w_ext * f if mem else 0.0,
    }
    return Evaluation(
        design=design,
        workload=workload,
        time_s=time,
        energy_pj=sum(energy_by.values()),
        area_mm2=area,
        energy_pj_by=energy_by,
        time_by_bound=by_bound,
        ops=results,
        placement=frac_on,
        ext_bytes=ext_bytes,
        onchip_kib_used=used / 1024,
        violations=list(violations),
        severity=violations.severity,
        warnings=warnings,
        bandwidth=bandwidth,
    )
