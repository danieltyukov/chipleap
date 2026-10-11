"""Reports for an RTL project's verification: Markdown (VERIFY.md) and self-contained HTML
(report.html), from the step results ``chipleap.rtl.verify`` collects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chipleap import __version__
from chipleap.hw.tech import get_tech
from chipleap.report.html import CSS, esc
from chipleap.report.text import md_table
from chipleap.rtl.project import Project
from chipleap.verify.result import StepResult

STEP_TITLES = {
    "lint": "Lint",
    "sim": "Simulation",
    "coverage": "Coverage",
    "synth": "Synthesis",
    "pnr": "Place and route",
}
DIAG_ROWS = 100


def _loc(d: dict[str, Any]) -> str:
    f = d.get("file", "")
    return f"{f}:{d['line']}" if f and d.get("line") else f


def _project_rows(p: Project) -> list[tuple[str, str]]:
    rows = [
        ("Top", p.top),
        ("Technology", get_tech(p.tech).name),
        ("Sources", f"{len(p.sources)} files"),
        (
            "Clocks",
            ", ".join(f"{c.port} at {c.period_ns:g} ns ({c.mhz:g} MHz)" for c in p.clocks)
            or "none",
        ),
        (
            "Resets",
            ", ".join(
                f"{r.port} (active {'low' if r.active_low else 'high'}, "
                f"{'synchronous' if r.sync else 'asynchronous'})"
                for r in p.resets
            )
            or "none",
        ),
    ]
    if p.parameters:
        rows.append(("Parameters", ", ".join(f"{k}={v}" for k, v in p.parameters.items())))
    if p.tests.dir is not None:
        rows.append(
            (
                "Tests",
                f"cocotb, {', '.join(p.tests.modules)} in {p.rel(p.tests.dir)}/ (seed {p.tests.seed})",
            )
        )
    if p.golden is not None:
        rows.append(("Golden model", p.rel(p.golden)))
    return rows


def _diag_rows(diags: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [
            d.get("tool", ""),
            d.get("severity", ""),
            _loc(d),
            d.get("code", "") or d.get("test", ""),
            d.get("message", ""),
        ]
        for d in diags[:DIAG_ROWS]
    ]


DIAG_HEAD = ["tool", "severity", "where", "code or test", "message"]


def _kv(r: StepResult) -> list[tuple[str, str]]:
    """The headline numbers of a step, for both report formats."""
    d = r.data
    if r.name == "synth" and "area_um2" in d:
        return [
            ("cell area", f"{d['area_um2']:.0f} um2"),
            ("cells", str(d["cells"])),
            ("flip-flops", str(d["flops"])),
            ("critical path", f"{d['critical_path_ns']} ns"),
            ("Fmax estimate (typical)", f"{d['fmax_mhz_estimate']:.0f} MHz"),
        ]
    if r.name == "pnr" and "core_um2" in d:
        return [
            ("core", f"{d['core_um2'] / 1e6:.4f} mm2"),
            ("utilisation", f"{d['utilisation'] * 100:.0f}%"),
            ("cells", str(d["cells"])),
            ("setup slack, slow", f"{d['setup_slack_ns']:+.3f} ns"),
            ("setup slack, typical", f"{d['setup_slack_typ_ns']:+.3f} ns"),
            ("hold slack", f"{d['hold_slack_ns']:+.3f} ns"),
            ("Fmax, slow corner", f"{d['fmax_worst_mhz']:.0f} MHz"),
            (
                "DRC / LVS / antenna",
                f"{d['drc_errors']} / {d.get('lvs_errors', d.get('connectivity_errors', 0))}"
                f" / {d['antenna_nets']}",
            ),
        ]
    if r.name == "coverage" and "kinds" in d:
        return [
            (k, f"{v['percent']}% ({v['covered']}/{v['total']})") for k, v in d["kinds"].items()
        ]
    if r.name == "sim" and "tests" in d:
        return [("passed", str(d["passed"])), ("failed", str(d["failed"]))]
    return []


def _test_rows(r: StepResult) -> list[list[str]]:
    return [
        [
            f"{t.get('module', '')}.{t.get('name', '')}",
            t.get("status", ""),
            f"{t.get('sim_time_ns', 0):g}",
            (f"{_loc(t)}: " if t.get("status") == "fail" and _loc(t) else "")
            + t.get("message", ""),
        ]
        for t in r.data.get("tests", [])
    ]


def _coverage_rows(r: StepResult) -> tuple[list[str], list[list[str]]]:
    kinds = list(r.data.get("kinds", {}))
    rows = []
    for f in r.data.get("files", []):
        cells = [f["file"]]
        for k in kinds:
            c, t = f.get(k, [0, 0])
            cells.append(f"{100 * c / t:.0f}% ({c}/{t})" if t else "")
        rows.append(cells)
    return ["file", *kinds], rows


def project_markdown(p: Project, results: Sequence[StepResult]) -> str:
    lines = [f"# Verification of {p.name}", ""]
    if p.description:
        lines += [p.description, ""]
    lines += [md_table(["", ""], [[k, v] for k, v in _project_rows(p)]), ""]
    lines += [
        md_table(["step", "status", "summary"], [[r.name, r.status, r.summary] for r in results]),
        "",
    ]
    for r in results:
        if r.status == "skipped":
            continue
        lines += [f"## {STEP_TITLES.get(r.name, r.name)}", "", f"{r.status}. {r.summary}.", ""]
        kv = _kv(r)
        if kv:
            lines += [md_table(["", ""], [[k, v] for k, v in kv]), ""]
        if r.name == "sim" and r.data.get("tests"):
            lines += [md_table(["test", "status", "sim ns", "message"], _test_rows(r)), ""]
        if r.name == "coverage" and r.data.get("files"):
            head, rows = _coverage_rows(r)
            lines += [md_table(head, rows), ""]
        diags = r.data.get("diagnostics") or []
        if diags:
            lines += [md_table(DIAG_HEAD, _diag_rows(diags)), ""]
        if r.name == "pnr" and r.data.get("gds"):
            lines += [f"GDS: `{r.data['gds']}`", ""]
    return "\n".join(lines).rstrip() + "\n"


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def _kpis(kv: list[tuple[str, str]]) -> str:
    return (
        '<div class="kpis">'
        + "".join(f'<div class="kpi"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in kv)
        + "</div>"
    )


def project_html(p: Project, results: Sequence[StepResult]) -> str:
    status = (
        '<span class="bad">' + esc("Some steps fail.") + "</span>"
        if any(r.status == "fail" for r in results)
        else esc("No step fails.")
    )
    steps = _table(
        ["step", "status", "summary"],
        [[STEP_TITLES.get(r.name, r.name), r.status, r.summary] for r in results],
    )
    project_rows = "".join(
        f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in _project_rows(p)
    )
    sections = []
    for r in results:
        if r.status == "skipped":
            continue
        parts = [f"<h2>{esc(STEP_TITLES.get(r.name, r.name))}</h2>"]
        cls = ' class="bad"' if r.status == "fail" else ""
        parts.append(f"<p{cls}>{esc(r.status.capitalize())}. {esc(r.summary)}.</p>")
        kv = _kv(r)
        if kv:
            parts.append(_kpis(kv))
        if r.name == "sim" and r.data.get("tests"):
            parts.append(_table(["test", "status", "sim ns", "message"], _test_rows(r)))
        if r.name == "coverage" and r.data.get("files"):
            head, rows = _coverage_rows(r)
            parts.append(_table(head, rows))
            unc = r.data.get("uncovered", [])
            if unc:
                parts.append(
                    f"<p>Points the tests never reached ({r.data.get('uncovered_total', len(unc))}):</p>"
                )
                parts.append(
                    _table(
                        ["where", "kind", "point"],
                        [[_loc(u), u["kind"], u["name"]] for u in unc[:50]],
                    )
                )
        diags = r.data.get("diagnostics") or []
        if diags:
            parts.append(_table(DIAG_HEAD, _diag_rows(diags)))
        if r.name == "pnr" and r.data.get("gds"):
            parts.append(f"<p>GDS: <code>{esc(r.data['gds'])}</code></p>")
        sections.append("\n".join(parts))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>ChipLeap verification: {esc(p.name)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{esc(p.name)}</h1>
<p class="lead">{esc(p.description or f"RTL project with top module {p.top}.")} {status}</p>
<table><tbody>{project_rows}</tbody></table>
<h2>Steps</h2>
{steps}
{"".join(sections)}
<footer>Generated by ChipLeap {esc(__version__)}. Every number is measured by the tool named in its
step; the Fmax after synthesis is an estimate at the typical corner, and place and route signs off
at the slow corner (1.08 V, 125 C).</footer>
</div></body></html>
"""
