"""Plain-text rendering for the terminal and for Markdown reports."""

from __future__ import annotations

from collections.abc import Sequence

from chipleap.explore.engine import ExploreResult, Step
from chipleap.model.bounds import Requirements
from chipleap.model.evaluate import Evaluation


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], right: Sequence[int] = ()) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: Sequence[str]) -> str:
        out = []
        for i, c in enumerate(cells):
            out.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
        return "  ".join(out).rstrip()

    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in rows]
    return "\n".join(lines)


def md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(c.replace("|", "\\|") for c in r) + " |" for r in rows]
    return "\n".join(out)


def num(x: float, digits: int = 3) -> str:
    if x == 0:
        return "0"
    return f"{x:.{digits}g}"


def headline(ev: Evaluation) -> str:
    u = ev.workload.unit
    return (
        f"{num(ev.throughput)} {u}/s, {num(ev.latency_ms)} ms per step, "
        f"{num(ev.energy_per_unit_mj)} mJ/{u}, {num(ev.power_mw)} mW, "
        f"{num(ev.total_area)} mm2"
    )


def clock_note(ev: Evaluation) -> str:
    """The clock the SoC runs at and, where they were measured, what X-HEEP's SoC and the
    accelerator meet on their own after routing, at the slow corner."""
    d = ev.design
    accel = (
        f"the accelerator alone meets about {ev.accel_limit_mhz:.0f}" if ev.accel_limit_mhz else ""
    )
    if ev.accel_clock_mhz:
        own = f"the accelerator runs on its own clock at {ev.accel_clock_mhz:g}"
        accel = f"{own}; it alone meets about {ev.accel_limit_mhz:.0f}" if accel else own
    if ev.soc_mhz < d.freq_mhz:
        notes = [f"the design asks {d.freq_mhz:g}", *([accel] if accel else [])]
        return f"{ev.soc_mhz:.1f} MHz, what X-HEEP's SoC meets ({'; '.join(notes)})"
    notes = [f"X-HEEP's SoC meets about {ev.soc_limit_mhz:.0f}"] if ev.soc_limit_mhz else []
    notes += [accel] if accel else []
    return f"{d.freq_mhz:g} MHz" + (f" ({'; '.join(notes)})" if notes else "")


def ip_note(ev: Evaluation) -> str:
    """On the standalone platform, what is generated and what the model only assumes."""
    if ev.design.platform != "standalone":
        return ""
    return (
        f"Generated IP: the accelerator with AXI4 interfaces, {num(ev.ip_area)} mm2. The host "
        "CPU, its SRAM, interconnect and memory controller are assumed by the model, not "
        f"generated; with them the system estimate is {num(ev.total_area)} mm2."
    )


def op_rows(ev: Evaluation, limit: int = 12) -> list[list[str]]:
    total = ev.time_s or 1.0
    rows = []
    for r in sorted(ev.ops, key=lambda r: -r.time_s)[:limit]:
        rows.append(
            [
                r.name,
                r.unit,
                f"{r.time_s / total * 100:.1f}%",
                f"{r.time_s * 1e3:.3g}",
                r.bound,
                f"{r.utilization * 100:.0f}%" if r.unit == "accel" else "",
                r.note,
            ]
        )
    return rows


OP_HEADERS = ["operator", "unit", "share", "ms", "bound", "util", "note"]


def format_evaluation(ev: Evaluation) -> str:
    lines = [
        f"Design: {ev.design.summary()}",
        f"Workload: {ev.workload.description}",
        f"Result: {headline(ev)}",
        f"Clock: {clock_note(ev)}",
    ]
    if ev.violations:
        lines += ["", "Cannot work as specified:"] + [f"  {v}" for v in ev.violations]
    if ev.warnings:
        lines += ["", "Warnings:"] + [f"  {w}" for w in ev.warnings]
    share = sorted(ev.bound_share().items(), key=lambda kv: -kv[1])
    lines += ["", "Time by bottleneck: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in share)]
    lines += [
        "On chip: "
        + f"{ev.onchip_kib_used:.0f} of {ev.design.sram_kib} KiB used; "
        + f"KV cache {ev.placement['kv'] * 100:.0f}% and weights "
        + f"{ev.placement['weights'] * 100:.0f}% held on chip",
        f"External traffic: {ev.ext_bytes / 1e6:.4g} MB per step at "
        f"{ev.bandwidth['external'] / 1e9:.3g} GB/s"
        if ev.bandwidth["external"]
        else "External traffic: none",
        "",
        table(OP_HEADERS, op_rows(ev), right=(2, 3, 5)),
        "",
        "Area (mm2): " + ", ".join(f"{k} {v:.3g}" for k, v in ev.area_mm2.items()),
        "Energy per step (mJ): "
        + ", ".join(f"{k} {v / 1e9:.3g}" for k, v in ev.energy_pj_by.items()),
    ]
    if ip_note(ev):
        lines.append(ip_note(ev))
    return "\n".join(lines)


def format_step(st: Step, verbose: bool = False) -> str:
    lines = [f"{st.index:>2}. [{st.phase}] {st.reason}"]
    if verbose:
        lines += [f"      {d}" for d in st.diagnosis]
        for mv, c in st.alternatives[1:]:
            ev = c.evaluation
            lines.append(
                f"      also tried: {mv.label} -> {num(ev.throughput)} {ev.workload.unit}/s, "
                f"{num(ev.energy_per_unit_mj)} mJ, {num(ev.total_area)} mm2"
                + (" (infeasible)" if c.assessment.violations else "")
            )
    return "\n".join(lines)


def format_explore(r: ExploreResult, verbose: bool = False) -> str:
    b = r.best
    lines = [
        f"Spec: {r.spec.name} ({r.spec.workload().description})",
        f"Explored {r.evaluated} design points in {r.seconds:.1f} s "
        f"({r.sampled} sampled, the rest by the agent).",
        "",
        "Baseline: " + r.baseline.design.summary(),
        "  " + headline(r.baseline.evaluation),
        "",
        "Agent trace:",
    ]
    lines += [format_step(s, verbose) for s in r.steps]
    lines += [f"Note: {n}" for n in r.notes]
    lines += [
        "",
        "Recommended: " + b.design.summary() + f", weights {b.point.weights}",
        "  " + headline(b.evaluation),
        "  bottleneck: " + b.evaluation.bound,
    ]
    if ip_note(b.evaluation):
        lines.append("  " + ip_note(b.evaluation))
    if b.assessment.violations:
        lines += ["  cannot work: " + v for v in b.assessment.violations]
    if b.assessment.misses:
        lines += ["  misses: " + m for m in b.assessment.misses]
    if not b.assessment.violations and not b.assessment.misses:
        lines.append("  meets every target and budget in the spec")
    lines += ["", f"Pareto front: {len(r.pareto)} designs (throughput, energy, area)."]
    rows = []
    for c in r.pareto[-8:]:
        e = c.evaluation
        rows.append(
            [num(e.throughput), num(e.energy_per_unit_mj), num(e.total_area), c.design.summary()]
        )
    if rows:
        lines.append(
            table(
                [f"{r.best.evaluation.workload.unit}/s", "mJ", "mm2", "design"],
                rows,
                right=(0, 1, 2),
            )
        )
    return "\n".join(lines)


def format_requirements(req: Requirements) -> str:
    return "\n".join(req.lines())
