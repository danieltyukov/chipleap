"""Design-space exploration: an explainable agent loop plus global sampling.

The agent starts from a minimal X-HEEP, explains what limits it, evaluates every single
change it could make, takes the best one and writes down why. It stops when no change
improves the design. Greedy steps can stall in a local optimum, so a seeded random sample
of the whole space runs as well; the agent then polishes the best sampled design. Every
evaluated point feeds the Pareto front of throughput, energy and area.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from chipleap.explore.moves import (
    FREQ_STEPS,
    Move,
    _freq_limit,
    _mem_order,
    _tech_order,
    default_accel,
    neighbours,
    widest_operand,
)
from chipleap.explore.score import Assessment, Point, assess, objective_label
from chipleap.hw.cpu import CPUS
from chipleap.hw.design import DATAFLOWS, Accel, Design, DesignError
from chipleap.hw.memory import MEMORIES, NONE_KEY
from chipleap.hw.tech import TECHNOLOGIES
from chipleap.model.evaluate import Evaluation, evaluate
from chipleap.spec import Spec
from chipleap.workload.ops import Workload


@dataclass
class Candidate:
    point: Point
    evaluation: Evaluation
    assessment: Assessment
    origin: str = ""

    @property
    def design(self) -> Design:
        return self.point.design

    @property
    def key(self) -> tuple[int, float, float]:
        return self.assessment.key


class Evaluator:
    """Evaluates points against a spec, caching workloads and results."""

    def __init__(self, spec: Spec) -> None:
        self.spec = spec
        self._workloads: dict[str, Workload] = {}
        self._cache: dict[Point, Candidate] = {}

    def workload(self, weights: str) -> Workload:
        if weights not in self._workloads:
            self._workloads[weights] = self.spec.workload(weights)
        return self._workloads[weights]

    def __call__(self, point: Point, origin: str = "") -> Candidate:
        hit = self._cache.get(point)
        if hit is not None:
            return hit
        ev = evaluate(
            self.workload(point.weights), point.design, firmware_kib=self.spec.firmware_kib
        )
        c = Candidate(point, ev, assess(self.spec, point, ev), origin)
        self._cache[point] = c
        return c

    @property
    def evaluated(self) -> list[Candidate]:
        return list(self._cache.values())


@dataclass
class Step:
    index: int
    phase: str
    before: Candidate
    diagnosis: list[str]
    alternatives: list[tuple[Move, Candidate]]  # best few, chosen first
    chosen: Move | None
    after: Candidate | None
    reason: str


@dataclass
class ExploreResult:
    spec: Spec
    baseline: Candidate
    best: Candidate
    steps: list[Step]
    pareto: list[Candidate]
    evaluated: int
    sampled: int
    seconds: float
    notes: list[str] = field(default_factory=list)
    cloud: list[Candidate] = field(default_factory=list)  # other feasible points, for plots


def _share(ev: Evaluation) -> str:
    parts = sorted(ev.bound_share().items(), key=lambda kv: -kv[1])
    return ", ".join(f"{v * 100:.0f}% {k}" for k, v in parts if v >= 0.005)


def diagnose(c: Candidate, spec: Spec) -> list[str]:
    """What limits this design, in sentences."""
    ev, a = c.evaluation, c.assessment
    unit = ev.workload.unit
    lines = [
        f"{ev.throughput:.3g} {unit}/s, {ev.energy_per_unit_mj:.3g} mJ/{unit}, "
        f"{ev.power_mw:.3g} mW, {ev.total_area:.3g} mm2."
    ]
    lines += [f"Cannot work: {v}." for v in a.violations]
    lines += [f"Misses: {m}." for m in a.misses]
    lines.append(f"Time goes to: {_share(ev)}.")
    total = ev.time_s or 1.0
    top = sorted(ev.ops, key=lambda r: -r.time_s)[:3]
    lines.append(
        "Largest operators: "
        + "; ".join(
            f"{r.name} on the {r.unit} ({r.time_s / total * 100:.0f}%, {r.bound})" for r in top
        )
        + "."
    )
    if ev.design.ext_mem != NONE_KEY and ev.ext_bytes:
        lines.append(
            f"External memory delivers {ev.bandwidth['external'] / 1e9:.3g} GB/s; each "
            f"{unit} reads {ev.ext_bytes / 1e6:.4g} MB from it."
        )
    return lines


# A move must improve the objective by this fraction to be worth a step. Below it, a
# move is still taken when it saves at least AREA_GAIN of area without costing more
# than OBJ_SLACK of the objective, the way an engineer trims a design that already works.
OBJ_GAIN = 0.01
AREA_GAIN = 0.02
OBJ_SLACK = 0.002


def _better(a: Candidate, b: Candidate) -> bool:
    ka, kb = a.key, b.key
    if ka[0] != kb[0]:
        return ka[0] < kb[0]
    if not math.isclose(ka[1], kb[1], rel_tol=1e-6, abs_tol=1e-6):
        return ka[1] < kb[1]
    if ka[2] < kb[2] * (1 - OBJ_GAIN):
        return True
    area_a, area_b = a.evaluation.total_area, b.evaluation.total_area
    return ka[2] <= kb[2] * (1 + OBJ_SLACK) and area_a < area_b * (1 - AREA_GAIN)


def _reason(before: Candidate, after: Candidate, move: Move, spec: Spec) -> str:
    b, a = before.evaluation, after.evaluation
    unit = b.workload.unit
    bits = []
    if before.assessment.violations and len(after.assessment.violations) < len(
        before.assessment.violations
    ):
        bits.append(
            "resolves "
            + "; ".join(set(before.assessment.violations) - set(after.assessment.violations))
            or "a violation"
        )
    if a.throughput and b.throughput and not math.isclose(a.throughput, b.throughput, rel_tol=1e-3):
        bits.append(f"throughput {b.throughput:.3g} -> {a.throughput:.3g} {unit}/s")
    if not math.isclose(a.energy_per_unit_mj, b.energy_per_unit_mj, rel_tol=1e-3):
        bits.append(f"energy {b.energy_per_unit_mj:.3g} -> {a.energy_per_unit_mj:.3g} mJ/{unit}")
    if not math.isclose(a.total_area, b.total_area, rel_tol=1e-3):
        bits.append(f"area {b.total_area:.3g} -> {a.total_area:.3g} mm2")
    if a.bound != b.bound:
        bits.append(f"bottleneck moves from {b.bound} to {a.bound}")
    if after.assessment.meets and not before.assessment.meets:
        bits.append("now meets every target and budget")
    head = f"{move.label}: {move.why}."
    return head + (" Result: " + ", ".join(bits) + "." if bits else "")


def run_agent(
    ev: Evaluator, start: Candidate, *, max_steps: int, phase: str, first_index: int = 1
) -> tuple[Candidate, list[Step]]:
    spec = ev.spec
    current = start
    steps: list[Step] = []
    seen = {start.point}
    for i in range(max_steps):
        wl = ev.workload(current.point.weights)
        scored = []
        for move in neighbours(spec, current.point, wl):
            if move.point in seen:
                continue
            scored.append((move, ev(move.point, origin=phase)))
        scored.sort(key=lambda mc: mc[1].key)
        diag = diagnose(current, spec)
        improving = [mc for mc in scored if _better(mc[1], current)]
        if improving:
            # Among real improvements prefer the best objective; ties go to less area.
            improving.sort(key=lambda mc: (mc[1].key, mc[1].evaluation.total_area))
            scored = improving + [mc for mc in scored if mc not in improving]
        if not improving:
            reason = "No single change improves the design further" + (
                f" ({objective_label(spec)} is the objective)."
                if current.assessment.meets
                else "; the targets cannot be met within the allowed space."
            )
            steps.append(
                Step(first_index + i, phase, current, diag, scored[:3], None, None, reason)
            )
            break
        move, nxt = scored[0]
        steps.append(
            Step(
                first_index + i,
                phase,
                current,
                diag,
                scored[:4],
                move,
                nxt,
                _reason(current, nxt, move, spec),
            )
        )
        seen.add(nxt.point)
        current = nxt
    return current, steps


def baseline_point(spec: Spec) -> Point:
    if spec.start is not None:
        return Point(spec.start, spec.default_weight_format())
    tech = _tech_order(spec)[0]
    cpu = next(c for c in CPUS if c in spec.cpus)
    freq = float(max(f for f in FREQ_STEPS if f <= min(100.0, _freq_limit(spec, tech))))
    wl = spec.workload()
    design = Design(
        name=f"{spec.name}-baseline",
        tech=tech,
        freq_mhz=freq,
        cpu=cpu,
        platform=spec.platform,
        peripherals=spec.peripherals,
    )
    on_chip = design.sram_kib * 1024 - spec.firmware_kib * 1024
    if wl.storage.total > on_chip:
        io = TECHNOLOGIES[tech].max_io_mts
        options = [
            m for m in _mem_order(spec) if MEMORIES[m].available(TECHNOLOGIES[tech].node_nm, io)
        ] or _mem_order(spec)
        need = wl.storage.weights + wl.storage.kv
        fitting = [m for m in options if MEMORIES[m].capacity_mib * 2**20 >= need]
        if fitting or options:
            design = design.with_(ext_mem=(fitting or options)[-1 if not fitting else 0])
    return Point(design, spec.default_weight_format())


def random_point(spec: Spec, rng: random.Random, workload: Workload) -> Point | None:
    tech = rng.choice(spec.techs)
    limit = _freq_limit(spec, tech)
    freqs = [f for f in FREQ_STEPS if f <= limit]
    bus = rng.choice(("onetoM", "NtoM"))
    il = rng.choice((0, 2, 4, 8)) if bus == "NtoM" else 0
    banks = rng.choice((1, 2, 4, 8))
    if banks + il > 16:
        banks = 16 - il
    mem = rng.choice(spec.memories)
    accel = None
    if rng.random() < 0.8:
        ports = rng.choice((1, 2, 4, 8, 16)) if bus == "NtoM" else 1
        accel = Accel(
            dataflow=rng.choice(DATAFLOWS),
            rows=rng.choice((2, 4, 8, 16, 32)),
            cols=rng.choice((2, 4, 8, 16, 32)),
            w_bits=max(widest_operand(workload), rng.choice((4, 8))),
            ports=ports,
            abuf_kib=rng.choice((2, 4, 8, 16, 32)),
            wbuf_kib=rng.choice((2, 4, 8, 16, 32)),
            double_buffer=rng.random() < 0.75,
        )
    design = Design(
        name="sample",
        tech=tech,
        freq_mhz=float(rng.choice(freqs)),
        cpu=rng.choice(spec.cpus),
        bus=bus,
        banks=banks,
        bank_kib=rng.choice((16, 32, 64)),
        il_banks=il,
        il_bank_kib=rng.choice((8, 16, 32)),
        ext_mem=mem,
        ext_channels=rng.choice((1, 1, 2, 4)) if mem != NONE_KEY else 1,
        accel=accel,
        platform=spec.platform,
        peripherals=spec.peripherals,
    )
    limit = _freq_limit(spec, tech, accel)
    if design.freq_mhz > limit:
        steps = [f for f in FREQ_STEPS if f <= limit]
        design = design.with_(freq_mhz=float(steps[-1]) if steps else limit)
    try:
        design.validate()
    except DesignError:
        return None
    return Point(design, rng.choice(spec.allowed_weight_formats()))


def pareto_front(cands: list[Candidate]) -> list[Candidate]:
    """Non-dominated feasible candidates: more throughput, less energy, less area."""
    pts = [c for c in cands if c.assessment.feasible and c.evaluation.throughput > 0]

    def vec(c: Candidate) -> tuple[float, float, float]:
        e = c.evaluation
        return (-e.throughput, e.energy_per_unit_mj, e.total_area)

    front: list[Candidate] = []
    vs = [(vec(c), c) for c in pts]
    for v, c in vs:
        dominated = any(all(o <= s for o, s in zip(ov, v, strict=True)) and ov != v for ov, _ in vs)
        if not dominated:
            front.append(c)
    # Collapse near-identical points (same metrics to three digits).
    uniq: dict[tuple[float, float, float], Candidate] = {}
    for c in front:
        e = c.evaluation
        key = (round(e.throughput, 3), round(e.energy_per_unit_mj, 3), round(e.total_area, 3))
        if key not in uniq or c.key < uniq[key].key:
            uniq[key] = c
    return sorted(uniq.values(), key=lambda c: c.evaluation.throughput)


def explore(
    spec: Spec, *, samples: int = 2000, seed: int = 0, max_steps: int = 40
) -> ExploreResult:
    t0 = time.perf_counter()
    ev = Evaluator(spec)
    base = ev(baseline_point(spec), origin="baseline")
    best, steps = run_agent(ev, base, max_steps=max_steps, phase="agent")

    rng = random.Random(seed)
    wl = ev.workload(spec.default_weight_format())
    sampled: list[Candidate] = []
    for _ in range(samples):
        p = random_point(spec, rng, wl)
        if p is not None:
            sampled.append(ev(p, origin="sample"))
    notes: list[str] = []
    if sampled:
        top = min(sampled, key=lambda c: c.key)
        if _better(top, best):
            notes.append(
                "Random sampling found a better region than the greedy agent reached; "
                "the agent refined that design."
            )
            polished, more = run_agent(
                ev, top, max_steps=max_steps, phase="polish", first_index=len(steps) + 1
            )
            steps += more
            if _better(polished, best):
                best = polished
    overall = min(ev.evaluated, key=lambda c: c.key)
    if _better(overall, best):
        best = overall
    front = pareto_front(ev.evaluated)
    named = Design(**{**best.design.__dict__, "name": spec.name})
    best = Candidate(
        Point(named, best.point.weights), best.evaluation, best.assessment, best.origin
    )
    cloud = [c for c in ev.evaluated if c.assessment.feasible][:1500]
    return ExploreResult(
        spec,
        base,
        best,
        steps,
        front,
        len(ev.evaluated),
        len(sampled),
        time.perf_counter() - t0,
        notes,
        cloud,
    )


__all__ = [
    "Candidate",
    "Evaluator",
    "ExploreResult",
    "Step",
    "baseline_point",
    "default_accel",
    "diagnose",
    "explore",
    "pareto_front",
    "run_agent",
]
