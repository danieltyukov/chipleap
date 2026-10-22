"""Machine-readable results (JSON) and the Markdown report."""

from __future__ import annotations

from typing import Any

from chipleap import __version__
from chipleap.explore.engine import Candidate, ExploreResult, Step
from chipleap.model.bounds import Requirements
from chipleap.model.evaluate import Evaluation
from chipleap.report.text import (
    OP_HEADERS,
    clock_note,
    headline,
    ip_note,
    md_table,
    num,
    op_rows,
)


def evaluation_dict(ev: Evaluation, ops: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "design": ev.design.to_dict(),
        "summary": ev.design.summary(),
        "workload": ev.workload.description,
        "unit": ev.workload.unit,
        "feasible": ev.feasible,
        "metrics": {k: round(v, 6) for k, v in ev.metrics().items()},
        "clock_mhz": {
            "design": ev.design.freq_mhz,
            "soc": round(ev.soc_mhz, 2),
            "soc_limit": round(ev.soc_limit_mhz, 2) if ev.soc_limit_mhz else None,
            "accelerator_limit": round(ev.accel_limit_mhz, 2) if ev.accel_limit_mhz else None,
        },
        "bottleneck": ev.bound,
        "time_share": {k: round(v, 4) for k, v in ev.bound_share().items()},
        "area_mm2": {k: round(v, 5) for k, v in ev.area_mm2.items()},
        "soc_logic_mm2": {k: round(v, 5) for k, v in ev.soc_logic_mm2.items()},
        "energy_mj_per_step": {k: round(v / 1e9, 6) for k, v in ev.energy_pj_by.items()},
        "placement_on_chip": {k: round(v, 4) for k, v in ev.placement.items()},
        "external_mb_per_step": round(ev.ext_bytes / 1e6, 4),
        "bandwidth_gbps": {k: round(v / 1e9, 4) for k, v in ev.bandwidth.items()},
        "violations": ev.violations,
        "warnings": ev.warnings,
    }
    if ops:
        total = ev.time_s or 1.0
        out["operators"] = [
            {
                "name": r.name,
                "unit": r.unit,
                "count": r.count,
                "share": round(r.time_s / total, 5),
                "ms": round(r.time_s * 1e3, 6),
                "bound": r.bound,
                "utilization": round(r.utilization, 4),
                "mapping": r.note,
            }
            for r in sorted(ev.ops, key=lambda r: -r.time_s)
        ]
    return out


def candidate_dict(c: Candidate, ops: bool = False) -> dict[str, Any]:
    d = evaluation_dict(c.evaluation, ops=ops)
    d["weights"] = c.point.weights
    d["meets_spec"] = c.assessment.meets
    d["misses"] = list(c.assessment.misses)
    d["spec_violations"] = list(c.assessment.violations)
    return d


def step_dict(s: Step) -> dict[str, Any]:
    return {
        "index": s.index,
        "phase": s.phase,
        "diagnosis": s.diagnosis,
        "action": s.chosen.label if s.chosen else None,
        "kind": s.chosen.kind if s.chosen else None,
        "hypothesis": s.chosen.why if s.chosen else None,
        "reason": s.reason,
        "alternatives": [
            {
                "action": mv.label,
                "throughput": round(c.evaluation.throughput, 6),
                "energy_mj": round(c.evaluation.energy_per_unit_mj, 6),
                "area_mm2": round(c.evaluation.total_area, 5),
                "feasible": c.assessment.feasible,
            }
            for mv, c in s.alternatives
        ],
    }


def explore_dict(r: ExploreResult) -> dict[str, Any]:
    return {
        "chipleap": __version__,
        "spec": r.spec.to_dict(),
        "evaluated": r.evaluated,
        "sampled": r.sampled,
        "seconds": round(r.seconds, 3),
        "baseline": candidate_dict(r.baseline),
        "recommended": candidate_dict(r.best, ops=True),
        "steps": [step_dict(s) for s in r.steps],
        "notes": r.notes,
        "pareto": [candidate_dict(c) for c in r.pareto],
    }


def requirements_dict(req: Requirements) -> dict[str, Any]:
    return {
        "workload": req.workload.description,
        "throughput": req.throughput,
        "stream_mb_per_step": round(req.stream_bytes_per_step / 1e6, 4),
        "external_gbps_if_nothing_on_chip": round(req.ext_bandwidth / 1e9, 4),
        "gmac_per_s": round(req.mac_rate / 1e9, 4),
        "capacity_mib": round(req.capacity / 2**20, 3),
        "macs_per_cycle_at_max_clock": {k: round(v, 3) for k, v in req.macs_per_cycle.items()},
        "cpu_mcycles_per_s": {k: round(v / 1e6, 3) for k, v in req.cpu_cycles_per_s.items()},
        "memories": [
            {
                "memory": m.key,
                "channels_needed": m.channels or None,
                "channels_by_node": m.channels_by_node,
                "read_only": m.read_only,
            }
            for m in req.memories
        ],
        "summary": req.lines(),
    }


def explore_markdown(r: ExploreResult) -> str:
    b = r.best
    ev = b.evaluation
    lines = [
        f"# {r.spec.name}",
        "",
        f"{r.spec.workload().description}. {r.spec.description}".strip(),
        "",
        f"Recommended: **{b.design.summary()}**, weights {b.point.weights}.",
        "",
        f"{headline(ev)}. Main bottleneck: {ev.bound}. Clock: {clock_note(ev)}.",
        "",
        *([ip_note(ev), ""] if ip_note(ev) else []),
        "Meets every target and budget in the spec."
        if b.assessment.meets
        else "\n".join(f"- {x}" for x in (*b.assessment.violations, *b.assessment.misses)),
        "",
        "## Agent trace",
        "",
    ]
    for s in r.steps:
        lines.append(f"{s.index}. ({s.phase}) {s.reason}")
    lines += [""] + [f"{n}" for n in r.notes]
    lines += ["", "## Where the time goes", "", md_table(OP_HEADERS, op_rows(ev)), ""]
    lines += [
        "## Pareto front",
        "",
        md_table(
            [f"{ev.workload.unit}/s", "mJ", "mm2", "design"],
            [
                [
                    num(c.evaluation.throughput),
                    num(c.evaluation.energy_per_unit_mj),
                    num(c.evaluation.total_area),
                    c.design.summary(),
                ]
                for c in r.pareto
            ],
        ),
        "",
        f"Explored {r.evaluated} design points in {r.seconds:.1f} s.",
        "",
    ]
    return "\n".join(lines)
