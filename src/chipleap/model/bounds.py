"""Inverse questions: what does a throughput target demand of the hardware?

Before any design point exists, a target such as "5 tokens per second" already fixes
lower bounds on external bandwidth, MAC rate, capacity and CPU work. These bounds rule
whole regions of the design space in or out (no PSRAM part can feed a 1B model at five
tokens per second, at any clock), which is often the most useful thing to know first.
"""

from __future__ import annotations

from dataclasses import dataclass

from chipleap.hw.cpu import CPUS
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.workload.ops import Workload


@dataclass(frozen=True)
class MemoryOption:
    key: str
    channels: int  # channels needed for bandwidth and capacity, 0 if more than 4
    nodes: tuple[str, ...]  # technologies whose I/O can drive it at full rate


@dataclass(frozen=True)
class Requirements:
    workload: Workload
    throughput: float
    steps_per_s: float
    stream_bytes_per_step: float  # weights and KV read once per step
    ext_bandwidth: float  # bytes per second if nothing stays on chip
    mac_rate: float  # MACs per second
    capacity: float  # bytes resident in total
    cpu_cycles_per_s: dict[str, float]  # element-wise work per CPU type at the target
    macs_per_cycle: dict[str, float]  # at each technology's top clock
    memories: list[MemoryOption]

    def lines(self) -> list[str]:
        u = self.workload.unit
        out = [
            f"Target: {self.throughput:g} {u}/s ({self.steps_per_s:.3g} steps/s).",
            f"Each step reads {self.stream_bytes_per_step / 1e6:.4g} MB of weights and KV "
            f"cache; with none of it on chip that is {self.ext_bandwidth / 1e9:.3g} GB/s "
            "of sustained external bandwidth.",
            f"Compute: {self.mac_rate / 1e9:.3g} GMAC/s; "
            + ", ".join(
                f"{v:.3g} MAC/cycle at {TECHNOLOGIES[k].max_freq_mhz:g} MHz ({k})"
                for k, v in self.macs_per_cycle.items()
            )
            + ".",
            f"Capacity: {self.capacity / 2**20:.4g} MiB must be resident somewhere.",
            "CPU element-wise work: "
            + ", ".join(f"{k} {v / 1e6:.3g} M cycles/s" for k, v in self.cpu_cycles_per_s.items())
            + ".",
        ]
        ok = [m for m in self.memories if m.channels]
        if ok:
            out.append(
                "External memories that can deliver it: "
                + "; ".join(
                    f"{m.key} x{m.channels} (I/O fast enough at {', '.join(m.nodes) or 'no listed node'})"
                    for m in ok
                )
                + "."
            )
        else:
            out.append("No listed external memory reaches this bandwidth with four channels.")
        return out


def requirements(workload: Workload, throughput: float) -> Requirements:
    if throughput <= 0:
        raise ValueError("throughput must be positive")
    steps = throughput / workload.tokens_per_step
    stream = workload.weight_bytes_read + workload.kv_bytes_read
    bw = stream * steps
    cap = workload.storage.total
    mac_rate = workload.macs * steps
    cpu = {
        k: sum(op.total_elements * c.element_cycles(op.kind) for op in workload.vector_ops) * steps
        for k, c in CPUS.items()
    }
    per_cycle = {k: mac_rate / (t.max_freq_mhz * 1e6) for k, t in TECHNOLOGIES.items()}
    mems = []
    for key, m in MEMORIES.items():
        channels = 0
        for ch in (1, 2, 4):
            if m.sustained_bps * ch >= bw and m.capacity_mib * 2**20 * ch >= cap:
                channels = ch
                break
        nodes = tuple(
            k for k, t in TECHNOLOGIES.items() if m.phy_exists(t.node_nm) and t.max_io_mts >= m.mts
        )
        mems.append(MemoryOption(key, channels, nodes))
    return Requirements(
        workload, throughput, steps, stream, bw, mac_rate, cap, cpu, per_cycle, mems
    )
