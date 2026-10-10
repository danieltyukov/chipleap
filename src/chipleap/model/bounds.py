"""Inverse questions: what does a throughput target demand of the hardware?

Before any design point exists, a target such as "5 tokens per second" already fixes
lower bounds on external bandwidth, MAC rate, capacity and CPU work. These bounds rule
whole regions of the design space in or out (no PSRAM part can feed a 1B model at five
tokens per second, at any clock), which is often the most useful thing to know first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chipleap.hw.cpu import CPUS
from chipleap.hw.memory import MEMORIES
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.workload.ops import Workload


@dataclass(frozen=True)
class MemoryOption:
    key: str
    # Channels needed for bandwidth and capacity at each technology whose PHY and I/O can
    # run this memory, at the rate that I/O allows (as the evaluator derates it). Nodes
    # that would need more than four channels are left out.
    channels_by_node: dict[str, int] = field(default_factory=dict)
    read_only: bool = False

    @property
    def channels(self) -> int:
        """The fewest channels that work at any node, 0 if none does."""
        return min(self.channels_by_node.values(), default=0)

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(self.channels_by_node)

    def describe(self) -> str:
        groups: dict[int, list[str]] = {}
        for node, ch in self.channels_by_node.items():
            groups.setdefault(ch, []).append(node)
        where = "; ".join(f"x{ch} at {', '.join(nodes)}" for ch, nodes in sorted(groups.items()))
        return f"{self.key}{', read only' if self.read_only else ''} ({where})"


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
                + "; ".join(m.describe() for m in ok)
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
        by_node: dict[str, int] = {}
        for k, t in TECHNOLOGIES.items():
            if not m.available(t.node_nm, t.max_io_mts):
                continue
            bps = m.sustained_bps_at(t.max_io_mts)
            for ch in (1, 2, 4):
                if bps * ch >= bw and m.capacity_mib * 2**20 * ch >= cap:
                    by_node[k] = ch
                    break
        mems.append(MemoryOption(key, by_node, m.read_only))
    return Requirements(
        workload, throughput, steps, stream, bw, mac_rate, cap, cpu, per_cycle, mems
    )
