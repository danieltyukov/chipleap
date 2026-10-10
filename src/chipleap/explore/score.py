"""How well a candidate meets a spec, as a sortable key and in words."""

from __future__ import annotations

import math
from dataclasses import dataclass

from chipleap.hw.design import Design
from chipleap.model.evaluate import Evaluation
from chipleap.spec import Spec


@dataclass(frozen=True)
class Point:
    """A design together with the weight precision it runs the workload at."""

    design: Design
    weights: str


@dataclass(frozen=True)
class Assessment:
    violations: tuple[str, ...]  # hard: the design cannot work as specified
    misses: tuple[str, ...]  # soft: targets or budgets not met
    penalty: float  # sum of log ratios by which targets and budgets are missed
    objective: float  # smaller is better
    severity: int = 0  # weighted violations: data with nowhere to live counts double

    @property
    def feasible(self) -> bool:
        return not self.violations

    @property
    def meets(self) -> bool:
        return self.feasible and not self.misses

    @property
    def key(self) -> tuple[int, float, float]:
        return (self.severity, round(self.penalty, 9), self.objective)


def objective_value(spec: Spec, ev: Evaluation) -> float:
    if spec.objective == "area":
        return ev.total_area
    if spec.objective == "throughput":
        return 1.0 / ev.throughput if ev.throughput > 0 else math.inf
    if spec.objective == "edp":
        return ev.energy_per_unit_mj * ev.latency_ms
    return ev.energy_per_unit_mj


def objective_label(spec: Spec) -> str:
    return {
        "energy": "energy per unit",
        "area": "area",
        "throughput": "throughput",
        "edp": "energy-delay product",
    }[spec.objective]


def assess(spec: Spec, point: Point, ev: Evaluation) -> Assessment:
    d = point.design
    violations = list(ev.violations)
    if d.tech not in spec.techs:
        violations.append(f"technology {d.tech} is not allowed by the spec")
    if d.ext_mem not in spec.memories:
        violations.append(f"external memory {d.ext_mem} is not allowed by the spec")
    if d.cpu not in spec.cpus:
        violations.append(f"CPU {d.cpu} is not allowed by the spec")
    if spec.max_freq_mhz and d.freq_mhz > spec.max_freq_mhz:
        violations.append(f"{d.freq_mhz:g} MHz is above the spec limit {spec.max_freq_mhz:g} MHz")
    if point.weights not in spec.allowed_weight_formats():
        violations.append(f"weight format {point.weights} is not allowed by the spec")

    misses: list[str] = []
    penalty = 0.0
    unit = ev.workload.unit
    if spec.throughput and ev.throughput < spec.throughput:
        misses.append(f"{ev.throughput:.3g} {unit}/s is below the {spec.throughput:g} target")
        penalty += math.log(spec.throughput / max(ev.throughput, 1e-12))
    if spec.latency_ms and ev.latency_ms > spec.latency_ms:
        misses.append(f"{ev.latency_ms:.3g} ms is above the {spec.latency_ms:g} ms target")
        penalty += math.log(ev.latency_ms / spec.latency_ms)
    if spec.area_mm2 and ev.total_area > spec.area_mm2:
        misses.append(f"{ev.total_area:.3g} mm2 is above the {spec.area_mm2:g} mm2 budget")
        penalty += math.log(ev.total_area / spec.area_mm2)
    if spec.power_mw and ev.power_mw > spec.power_mw:
        misses.append(f"{ev.power_mw:.3g} mW is above the {spec.power_mw:g} mW budget")
        penalty += math.log(ev.power_mw / spec.power_mw)
    if spec.energy_mj and ev.energy_per_unit_mj > spec.energy_mj:
        misses.append(
            f"{ev.energy_per_unit_mj:.3g} mJ/{unit} is above the {spec.energy_mj:g} mJ budget"
        )
        penalty += math.log(ev.energy_per_unit_mj / spec.energy_mj)
    severity = ev.severity + (len(violations) - len(ev.violations))
    return Assessment(
        tuple(violations), tuple(misses), penalty, objective_value(spec, ev), severity
    )
