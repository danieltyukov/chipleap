"""Self-contained HTML report with inline SVG charts. No scripts, no external assets."""

from __future__ import annotations

import html
import math
from collections.abc import Sequence

from chipleap import __version__
from chipleap.explore.engine import Candidate, ExploreResult
from chipleap.model.evaluate import Evaluation
from chipleap.report.text import headline, ip_note, num

BOUND_COLORS = {
    "external memory": "var(--c-ext)",
    "accelerator ports": "var(--c-port)",
    "compute": "var(--c-comp)",
    "on-chip memory": "var(--c-on)",
    "control": "var(--c-ctl)",
}
AREA_COLORS = [
    "var(--c-a1)",
    "var(--c-a2)",
    "var(--c-a3)",
    "var(--c-a4)",
    "var(--c-a5)",
    "var(--c-a6)",
    "var(--c-a7)",
]

CSS = """
:root{--bg:#fbfaf7;--ink:#141414;--muted:#5b5a56;--faint:#8a8883;--rule:#e4e1d9;
--accent:#17784e;--accent-ink:#13603f;--code-bg:#f3f1ec;--c-ext:#b8692e;--c-port:#7a5ea8;--c-comp:#17784e;
--c-on:#5a7fa8;--c-ctl:#a7a39a;--c-a1:#17784e;--c-a2:#5a7fa8;--c-a3:#b8692e;--c-a4:#8a6fb0;
--c-a5:#c9a227;--c-a6:#6c9c8b;--c-a7:#a7a39a;--dot:#c9c5bb;
--serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Charter,Georgia,serif;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace}
@media (prefers-color-scheme:dark){:root{--bg:#131312;--ink:#eeece6;--muted:#a6a39c;
--faint:#7a7873;--rule:#2a2925;--accent:#3fbf83;--accent-ink:#62d29c;--code-bg:#1c1c1a;
--c-ext:#e0955a;--c-port:#a993d6;--c-comp:#3fbf83;--c-on:#86a8d0;--c-ctl:#6f6c66;--c-a1:#3fbf83;
--c-a2:#86a8d0;--c-a3:#e0955a;--c-a4:#b39ad6;--c-a5:#e2c25a;--c-a6:#8cc3b0;--c-a7:#6f6c66;
--dot:#3d3b37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.55 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:920px;margin:0 auto;padding:40px 20px 64px}
h1{font:400 clamp(30px,5vw,44px)/1.1 var(--serif);margin:0 0 10px;letter-spacing:-.01em}
h2{font:400 12.5px var(--sans);letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
margin:44px 0 14px;padding-top:28px;border-top:1px solid var(--rule)}
p{color:var(--muted);max-width:680px}.lead{font-size:18px}
code{font-family:var(--mono);font-size:.88em;background:var(--code-bg);padding:1px 5px;border-radius:3px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:22px 0 4px}
.kpi{border:1px solid var(--rule);border-radius:6px;padding:12px 14px}
.kpi b{display:block;font:400 26px/1.15 var(--serif)}.kpi span{font-size:13px;color:var(--faint)}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th,td{text-align:left;padding:8px 10px 8px 0;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-weight:600}td{color:var(--muted)}td.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}ol.trace{padding-left:22px;margin:0}ol.trace li{margin:0 0 12px;color:var(--muted);max-width:760px}
ol.trace li b{color:var(--ink);font-weight:600}.tag{font-size:12px;color:var(--faint)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:var(--muted);margin:8px 0 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
svg text{font:12px var(--sans);fill:var(--muted)}svg .ax{stroke:var(--rule)}
.bad{color:#b3261e}footer{margin-top:48px;font-size:13px;color:var(--faint)}
"""


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _kpis(ev: Evaluation) -> str:
    u = ev.workload.unit
    items = [
        (num(ev.throughput), f"{u}s per second"),
        (num(ev.energy_per_unit_mj), f"mJ per {u}"),
        (num(ev.power_mw), "mW average"),
        (num(ev.total_area), "mm2 core area"),
        (ev.bound, "main bottleneck"),
    ]
    return (
        '<div class="kpis">'
        + "".join(
            f'<div class="kpi"><b>{esc(v)}</b><span>{esc(label)}</span></div>' for v, label in items
        )
        + "</div>"
    )


def stacked_bar(parts: Sequence[tuple[str, float, str]], width: int = 880, unit: str = "") -> str:
    total = sum(v for _, v, _ in parts) or 1.0
    x = 0.0
    rects = []
    for label, v, color in parts:
        w = v / total * width
        if w <= 0:
            continue
        rects.append(
            f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="22" fill="{color}">'
            f"<title>{esc(label)}: {num(v)}{unit} ({v / total * 100:.0f}%)</title></rect>"
        )
        x += w
    legend = "".join(
        f'<span><i style="background:{c}"></i>{esc(lbl)} {v / total * 100:.0f}%</span>'
        for lbl, v, c in parts
        if v > 0
    )
    return (
        f'<svg viewBox="0 0 {width} 22" width="100%" role="img" aria-label="breakdown">'
        + "".join(rects)
        + f'</svg><div class="legend">{legend}</div>'
    )


def _log_ticks(lo: float, hi: float) -> list[float]:
    a, b = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
    return [10.0**e for e in range(a, b + 1)]


def scatter(points: Sequence[Candidate], best: Candidate, cloud: Sequence[Candidate]) -> str:
    """Throughput against energy per unit, both on log axes; marker area follows mm2."""
    pts = [c for c in cloud if c.assessment.feasible and c.evaluation.throughput > 0]
    allp = pts + list(points) + [best]
    xs = [c.evaluation.throughput for c in allp]
    ys = [c.evaluation.energy_per_unit_mj for c in allp]
    if not xs:
        return "<p>No feasible designs to plot.</p>"
    x0, x1 = min(xs) / 1.5, max(xs) * 1.5
    y0, y1 = min(ys) / 1.5, max(ys) * 1.5
    W, H, L, B, T, R = 880, 400, 64, 46, 12, 16

    def px(x: float) -> float:
        return L + (math.log10(x) - math.log10(x0)) / (math.log10(x1) - math.log10(x0)) * (
            W - L - R
        )

    def py(y: float) -> float:
        return T + (1 - (math.log10(y) - math.log10(y0)) / (math.log10(y1) - math.log10(y0))) * (
            H - T - B
        )

    out = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" '
        'aria-label="Pareto front: throughput against energy per unit">'
    ]
    for t in _log_ticks(x0, x1):
        if x0 <= t <= x1:
            out.append(
                f'<line class="ax" x1="{px(t):.1f}" y1="{T}" x2="{px(t):.1f}" y2="{H - B}"/>'
                f'<text x="{px(t):.1f}" y="{H - B + 16}" text-anchor="middle">{num(t)}</text>'
            )
    for t in _log_ticks(y0, y1):
        if y0 <= t <= y1:
            out.append(
                f'<line class="ax" x1="{L}" y1="{py(t):.1f}" x2="{W - R}" y2="{py(t):.1f}"/>'
                f'<text x="{L - 8}" y="{py(t) + 4:.1f}" text-anchor="end">{num(t)}</text>'
            )
    unit = best.evaluation.workload.unit
    out.append(
        f'<text x="{(L + W - R) / 2}" y="{H - 8}" text-anchor="middle">{esc(unit)}s per second</text>'
    )
    out.append(
        f'<text x="14" y="{(T + H - B) / 2}" text-anchor="middle" '
        f'transform="rotate(-90 14 {(T + H - B) / 2})">mJ per {esc(unit)}</text>'
    )
    for c in pts[:1500]:
        e = c.evaluation
        out.append(
            f'<circle cx="{px(e.throughput):.1f}" cy="{py(e.energy_per_unit_mj):.1f}" '
            'r="2" fill="var(--dot)"/>'
        )
    amax = max(c.evaluation.total_area for c in points) if points else 1.0
    for c in points:
        e = c.evaluation
        r = 3 + 6 * math.sqrt(e.total_area / amax)
        out.append(
            f'<circle cx="{px(e.throughput):.1f}" cy="{py(e.energy_per_unit_mj):.1f}" r="{r:.1f}" '
            'fill="var(--accent)" fill-opacity=".35" stroke="var(--accent)">'
            f"<title>{esc(c.design.summary())}: {num(e.throughput)} {esc(unit)}/s, "
            f"{num(e.energy_per_unit_mj)} mJ, {num(e.total_area)} mm2</title></circle>"
        )
    e = best.evaluation
    bx, by = px(e.throughput), py(e.energy_per_unit_mj)
    out.append(
        f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="9" fill="none" stroke="var(--ink)" stroke-width="2"/>'
        f'<text x="{bx + 13:.1f}" y="{by + 4:.1f}" style="fill:var(--ink)">recommended</text>'
    )
    out.append("</svg>")
    return "".join(out)


def _trace_chart(r: ExploreResult) -> str:
    steps = [s for s in r.steps if s.after is not None]
    if not steps:
        return ""
    vals = [r.steps[0].before.evaluation.throughput] + [
        s.after.evaluation.throughput for s in steps if s.after
    ]
    vals = [max(v, 1e-9) for v in vals]
    W, H, L, B, T = 880, 160, 64, 28, 10
    lo, hi = min(vals) / 1.3, max(vals) * 1.3

    def py(v: float) -> float:
        return T + (1 - (math.log10(v) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))) * (
            H - T - B
        )

    n = len(vals)
    xs = [L + i * (W - L - 16) / max(1, n - 1) for i in range(n)]
    out = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="throughput after each agent step">'
    ]
    for t in _log_ticks(lo, hi):
        if lo <= t <= hi:
            out.append(
                f'<line class="ax" x1="{L}" y1="{py(t):.1f}" x2="{W - 16}" y2="{py(t):.1f}"/>'
                f'<text x="{L - 8}" y="{py(t) + 4:.1f}" text-anchor="end">{num(t)}</text>'
            )
    if r.spec.throughput and lo <= r.spec.throughput <= hi:
        y = py(r.spec.throughput)
        out.append(
            f'<line x1="{L}" y1="{y:.1f}" x2="{W - 16}" y2="{y:.1f}" stroke="var(--c-ext)" '
            f'stroke-dasharray="4 4"/><text x="{W - 18}" y="{y - 5:.1f}" text-anchor="end">target</text>'
        )
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{py(v):.1f}"
        for i, (x, v) in enumerate(zip(xs, vals, strict=True))
    )
    out.append(f'<path d="{path}" fill="none" stroke="var(--accent)" stroke-width="2"/>')
    for i, (x, v) in enumerate(zip(xs, vals, strict=True)):
        out.append(
            f'<circle cx="{x:.1f}" cy="{py(v):.1f}" r="3.5" fill="var(--accent)"/>'
            f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle">{i}</text>'
        )
    out.append("</svg>")
    return "".join(out)


def _ops_table(ev: Evaluation) -> str:
    total = ev.time_s or 1.0
    rows = []
    for r in sorted(ev.ops, key=lambda r: -r.time_s)[:14]:
        color = BOUND_COLORS.get(r.bound, "var(--c-ctl)")
        rows.append(
            f"<tr><td><code>{esc(r.name)}</code></td><td>{esc(r.unit)}</td>"
            f'<td class="n">{r.time_s / total * 100:.1f}%</td>'
            f'<td><i style="display:inline-block;width:9px;height:9px;border-radius:2px;'
            f'background:{color};margin-right:6px"></i>{esc(r.bound)}</td>'
            f'<td class="n">{(f"{r.utilization * 100:.0f}%" if r.unit == "accel" else "")}</td>'
            f"<td>{esc(r.note)}</td></tr>"
        )
    return (
        '<div class="scroll"><table><thead><tr><th>Operator</th><th>Unit</th><th>Time</th>'
        "<th>Bound</th><th>PE use</th><th>Mapping</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _design_table(c: Candidate) -> str:
    d = c.design
    rows = [
        ("Technology", d.tech),
        ("Clock", f"{d.freq_mhz:g} MHz"),
        ("CPU", d.cpu),
        ("Bus", d.bus),
        (
            "SRAM",
            f"{d.banks} x {d.bank_kib} KiB"
            + (f" + {d.il_banks} x {d.il_bank_kib} KiB interleaved" if d.il_banks else ""),
        ),
        ("External memory", d.ext_mem + (f" x {d.ext_channels}" if d.ext_channels > 1 else "")),
        ("Weights", c.point.weights),
    ]
    if d.accel:
        a = d.accel
        rows += [
            ("Accelerator", f"{a.dataflow}, {a.rows} x {a.cols} PEs, int{a.w_bits} weights"),
            ("Accelerator ports", str(a.ports)),
            (
                "Buffers",
                f"{a.abuf_kib} KiB activations, {a.wbuf_kib} KiB weights, "
                + ("double buffered" if a.double_buffer else "single buffered"),
            ),
        ]
    else:
        rows.append(("Accelerator", "none"))
    return (
        "<table><tbody>"
        + "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
        + "</tbody></table>"
    )


def explore_html(r: ExploreResult, cloud: Sequence[Candidate] = ()) -> str:
    b = r.best
    ev = b.evaluation
    time_parts = [
        (k, v, BOUND_COLORS.get(k, "var(--c-ctl)"))
        for k, v in sorted(ev.time_by_bound.items(), key=lambda kv: -kv[1])
    ]
    area_parts = [
        (k, v, AREA_COLORS[i % len(AREA_COLORS)])
        for i, (k, v) in enumerate(sorted(ev.area_mm2.items(), key=lambda kv: -kv[1]))
    ]
    energy_parts = [
        (k, v / 1e9, AREA_COLORS[i % len(AREA_COLORS)])
        for i, (k, v) in enumerate(sorted(ev.energy_pj_by.items(), key=lambda kv: -kv[1]))
    ]
    trace = []
    for s in r.steps:
        if s.chosen is None:
            trace.append(
                f'<li><b>Stop.</b> {esc(s.reason)} <span class="tag">{esc(s.phase)}</span></li>'
            )
            continue
        alts = "; ".join(
            f"{esc(mv.label)} ({num(c.evaluation.throughput)} {esc(ev.workload.unit)}/s, "
            f"{num(c.evaluation.energy_per_unit_mj)} mJ)"
            for mv, c in s.alternatives[1:3]
        )
        trace.append(
            f"<li><b>{esc(s.chosen.label)}.</b> {esc(s.reason.split(': ', 1)[-1])}"
            + (f' <span class="tag">Also tried: {alts}.</span>' if alts else "")
            + f' <span class="tag">{esc(s.phase)}</span></li>'
        )
    status = (
        "Meets every target and budget in the spec."
        if b.assessment.meets
        else " ".join(list(b.assessment.violations) + list(b.assessment.misses))
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>ChipLeap report: {esc(r.spec.name)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{esc(r.spec.name)}</h1>
<p class="lead">{esc(r.spec.workload().description)}. {esc(r.spec.description)}</p>
<p>Recommended design: <b>{esc(b.design.summary())}</b>, weights {esc(b.point.weights)}.
<span class="{"" if b.assessment.meets else "bad"}">{esc(status)}</span></p>
{_kpis(ev)}
{f"<p>{esc(ip_note(ev))}</p>" if ip_note(ev) else ""}
<h2>Pareto front</h2>
<p>{len(r.pareto)} designs that no other explored design beats on throughput, energy and area at once,
out of {r.evaluated} evaluated in {r.seconds:.1f} s. Marker size follows area. Grey dots are other feasible designs.</p>
{scatter(r.pareto, b, cloud)}
<h2>How the agent got there</h2>
<p>Throughput after each accepted step, starting from the baseline ({esc(headline(r.baseline.evaluation))}).</p>
{_trace_chart(r)}
<ol class="trace">{"".join(trace)}</ol>
{"".join(f"<p>{esc(n)}</p>" for n in r.notes)}
<h2>Where the time goes</h2>
{stacked_bar(time_parts, unit=" s")}
{_ops_table(ev)}
<h2>Area and energy</h2>
<p>Core area, excluding pads.</p>
{stacked_bar(area_parts, unit=" mm2")}
<p>Energy per step.</p>
{stacked_bar(energy_parts, unit=" mJ")}
<h2>Design</h2>
{_design_table(b)}
{"".join(f'<p class="tag">{esc(w)}</p>' for w in ev.warnings)}
<footer>Generated by ChipLeap {esc(__version__)}. Analytical estimates; see docs/model.md for assumptions
and <code>chipleap verify</code> for RTL simulation and synthesis checks.</footer>
</div></body></html>
"""
