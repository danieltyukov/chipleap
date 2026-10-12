"""Reports for an RTL project's verification: Markdown (VERIFY.md) and self-contained HTML
(report.html), from the step results ``chipleap.rtl.verify`` collects."""

from __future__ import annotations

import base64
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
    "formal": "Formal properties",
    "cdc": "Clock and reset domain crossings",
    "equiv": "Equivalence",
    "gls": "Gate-level simulation",
    "eco": "Engineering change order",
    "dft": "Design for test",
    "power": "Gate-level power",
    "ir_drop": "IR drop",
    "upf": "Power intent (UPF)",
    "chip": "Chip",
}
DIAG_ROWS = 100


def _line(text: str) -> str:
    """The first line of a message: table cells hold one line."""
    return text.strip().splitlines()[0] if text.strip() else ""


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
            _line(d.get("message", "")),
        ]
        for d in diags[:DIAG_ROWS]
    ]


DIAG_HEAD = ["tool", "severity", "where", "code or test", "message"]


def _kv(r: StepResult) -> list[tuple[str, str]]:
    """The headline numbers of a step, for both report formats."""
    d = r.data
    if r.name == "synth" and "area_um2" in d:
        # Yosys reports flip-flops; Genus and Design Compiler report slack at the clock.
        kv = [("cell area", f"{d['area_um2']:.0f} um2"), ("cells", str(d.get("cells", "")))]
        if "flops" in d:
            kv.append(("flip-flops", str(d["flops"])))
        if "slack_ns" in d:
            kv.append(("setup slack", f"{d['slack_ns']:+.3f} ns"))
        kv += [
            ("critical path", f"{d['critical_path_ns']} ns"),
            ("Fmax estimate", f"{d['fmax_mhz_estimate']:.0f} MHz"),
        ]
        return kv
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
    if r.name == "dft" and "chains" in d:
        kv = [
            ("scan chains", str(len(d["chains"]))),
            ("flip-flops scanned", f"{d['scanned']} of {d['flops']}"),
        ]
        if d.get("atpg"):
            a = d["atpg"]
            kv += [
                ("stuck-at coverage", f"{a['coverage_pct']:.1f}%"),
                ("patterns", str(a["patterns"])),
            ]
        for key, label in (("simulation", "scan netlist"), ("simulation_routed", "routed netlist")):
            sim = d.get(key)
            if sim:
                ok = sim["flush"] == "pass" and not sim["errors"]
                kv.append(
                    (
                        f"patterns simulated, {label}",
                        f"{sim['patterns']} {'pass' if ok else 'fail'}",
                    )
                )
        return kv
    if r.name == "power" and "corners" in d:
        kv = []
        for c in d["corners"]:
            value = c["vcd_mw"] if c["vcd_mw"] is not None else c["vectorless_mw"]
            how = "from the tests" if c["vcd_mw"] is not None else "vectorless"
            kv.append((f"{c['corner']} ({how})", f"{value:.3g} mW"))
        if d.get("librelane_mw") is not None:
            kv.append(("LibreLane's estimate", f"{d['librelane_mw']:.3g} mW"))
        return kv
    if r.name == "ir_drop" and "nets" in d:
        return [
            (f"worst on {n['net']}", f"{n['worst_mv']:.3g} mV ({n['worst_pct']:.2g}%)")
            for n in d["nets"]
        ]
    if r.name == "upf" and "domains" in d:
        return [
            ("power domains", str(len(d["domains"]))),
            ("signals between domains", str(sum(c["nets"] for c in d["crossings"]))),
        ]
    if r.name == "eco" and "after" in d:
        b, a = d["before"], d["after"]
        return [
            ("changes", str(len(d["changes"]))),
            ("setup slack, slow", f"{b['setup_slack_ns']:+.3f} to {a['setup_slack_ns']:+.3f} ns"),
            ("hold slack", f"{b['hold_slack_ns']:+.3f} to {a['hold_slack_ns']:+.3f} ns"),
            ("DRC / LVS / antenna", f"{a['drc_errors']} / {a['lvs_errors']} / {a['antenna_nets']}"),
        ]
    if r.name == "coverage" and "kinds" in d:
        return [
            (k, f"{v['percent']}% ({v['covered']}/{v['total']})") for k, v in d["kinds"].items()
        ]
    if r.name in ("sim", "gls") and "tests" in d:
        return [("passed", str(d["passed"])), ("failed", str(d["failed"]))]
    if r.name == "formal" and "checks" in d:
        return [("checks passed", str(d["passed"])), ("checks failed", str(d["failed"]))]
    if r.name == "cdc" and "domains" in d:
        return [
            ("clock domains", str(len(d["domains"]))),
            ("crossings", str(d["crossings"])),
            ("errors", str(d["errors"])),
            ("warnings", str(d["warnings"])),
        ]
    if r.name == "chip" and "outline_um" in d:
        o = d["outline_um"]
        hookup = d["supply_hookup"]
        return [
            ("outline", f"{(o[2] - o[0]) / 1000:.2f} x {(o[3] - o[1]) / 1000:.2f} mm"),
            ("pads", str(d["pads"])),
            ("macros", str(len(d["macros"]))),
            ("LVS (IHP deck)", str(d["lvs"]["result"])),
            ("DRC violations (IHP deck)", str(d["drc"]["violations"])),
            ("routing DRC", str(d["route_drc_errors"])),
            (
                "supply joins to the ring",
                f"VDD {sum(hookup.get('VDD', {}).values())}, VSS {sum(hookup.get('VSS', {}).values())}",
            ),
            ("setup slack, slow, core side of the pads", f"{d['setup_slack_ns']:+.3f} ns"),
            ("hold slack, core side of the pads", f"{d['hold_slack_ns']:+.3f} ns"),
            *_io_kv(d.get("io_timing")),
        ]
    return []


def _slack(v: Any) -> str:
    return "not constrained" if v is None else f"{v:+.3f} ns"


def _io_kv(io: Any) -> list[tuple[str, str]]:
    if not io:
        return []
    by = {c["corner"]: c for c in io["corners"]}
    out = []
    if "slow" in by:
        out.append(("setup slack, slow, at the ports", _slack(by["slow"]["setup_slack_ns"])))
    holds = [c["hold_slack_ns"] for c in io["corners"] if c["hold_slack_ns"] is not None]
    if holds:
        out.append(("hold slack, worst corner, at the ports", f"{min(holds):+.3f} ns"))
    if io["untimed_pads"]:
        out.append(("pads not timed", "; ".join(io["untimed_pads"])))
    return out


def _tables(r: StepResult) -> list[tuple[list[str], list[list[str]]]]:
    """Detail tables of the verification-depth steps, for both report formats."""
    d = r.data
    out: list[tuple[list[str], list[list[str]]]] = []
    if r.name == "chip" and "precheck" in d:
        out.append(
            (
                ["precheck", "result", "detail"],
                [[c["check"], "pass" if c["pass"] else "fail", c["detail"]] for c in d["precheck"]],
            )
        )
        rules = d["drc"]["rules"]
        if rules:
            out.append((["DRC rule", "violations"], [[k, str(v)] for k, v in rules.items()]))
        io = d.get("io_timing")
        if io:
            out.append(
                (
                    ["corner", "setup slack at the ports", "hold slack at the ports"],
                    [
                        [c["corner"], _slack(c["setup_slack_ns"]), _slack(c["hold_slack_ns"])]
                        for c in io["corners"]
                    ],
                )
            )
            rows = []
            for key, paths in io["paths"].items():
                for path in paths[:1]:
                    rows.append(
                        [
                            key.replace("_", " "),
                            path["startpoint"],
                            path["endpoint"],
                            f"{path['slack_ns']:+.3f} ns",
                            f"{path['pad_delay_ns']:.3f} ns",
                        ]
                    )
            if rows:
                out.append(
                    (["worst path, slow corner", "from", "to", "slack", "in the pads"], rows)
                )
    if r.name == "formal":
        out.append(
            (
                ["check", "mode", "depth", "status", "failures", "covers reached"],
                [
                    [
                        c["name"],
                        c["mode"],
                        str(c["depth"] or ""),
                        c["status"],
                        str(len(c["failures"])),
                        f"{sum(x['reached'] for x in c['covers'])}/{len(c['covers'])}"
                        if c["covers"]
                        else "",
                    ]
                    for c in d.get("checks", [])
                ],
            )
        )
        fails = [(c["name"], f) for c in d.get("checks", []) for f in c["failures"]]
        if fails:
            out.append(
                (
                    ["check", "property", "where", "step", "counterexample"],
                    [
                        [name, f["property"], _loc(f), str(f["step"]), f["trace"]]
                        for name, f in fails
                    ],
                )
            )
    if r.name == "cdc":
        findings = d.get("findings", [])
        if findings:
            out.append(
                (
                    ["rule", "severity", "from", "to", "message"],
                    [
                        [
                            f["rule"],
                            f"waived: {f['waived']}" if f.get("waived") else f["severity"],
                            f"{f['from']} ({f['from_domain']})",
                            f"{f['to']} ({f['to_domain']})",
                            _line(f["message"]),
                        ]
                        for f in findings
                    ],
                )
            )
        accepted = d.get("accepted", [])
        if accepted:
            out.append(
                (
                    ["accepted as", "from", "to", "bits"],
                    [
                        [
                            a["rule"],
                            f"{a['from']} ({a['from_domain']})",
                            f"{a['to']} ({a['to_domain']})",
                            str(a.get("bits", "")),
                        ]
                        for a in accepted
                    ],
                )
            )
    if r.name == "equiv":
        out.append(
            (
                ["pair", "status", "points proved", "netlist", "detail"],
                [
                    [
                        x["pair"],
                        x["status"],
                        f"{x['proved']}/{x['points']}" if "points" in x else "",
                        x.get("gate", ""),
                        x.get("reason")
                        or x.get("error")
                        or x.get("trace")
                        or ", ".join(x.get("unproven", [])[:5]),
                    ]
                    for x in d.get("pairs", [])
                ],
            )
        )
    return out


def _test_rows(r: StepResult) -> list[list[str]]:
    return [
        [
            f"{t.get('module', '')}.{t.get('name', '')}",
            t.get("status", ""),
            f"{t.get('sim_time_ns', 0):g}",
            (f"{_loc(t)}: " if t.get("status") == "fail" and _loc(t) else "")
            + _line(t.get("message", "")),
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


def _num(value: Any, fmt: str = ".3g") -> str:
    return "" if value is None else format(value, fmt)


def _impl_tables(r: StepResult) -> list[tuple[str, list[str], list[list[str]]]]:
    """The implementation steps' tables: (caption, head, rows)."""
    d = r.data
    out: list[tuple[str, list[str], list[list[str]]]] = []
    if r.name == "dft" and d.get("chains"):
        out.append(
            (
                "Scan chains",
                ["chain", "flip-flops", "scan in", "scan out", "clock"],
                [
                    [c["name"], str(c["length"]), c["scan_in"], c["scan_out"], c["clock"]]
                    for c in d["chains"]
                ],
            )
        )
        for key in ("simulation", "simulation_routed"):
            sim = d.get(key) or {}
            if sim.get("mismatches"):
                out.append(("Mismatches", ["where"], [[m] for m in sim["mismatches"]]))
    if r.name == "power" and d.get("corners"):
        rows = []
        for c in d["corners"]:
            for g, v in c["groups"].items():
                rows.append([c["corner"], g, _num(v["vectorless_mw"]), _num(v["vcd_mw"])])
            rows.append([c["corner"], "Total", _num(c["vectorless_mw"]), _num(c["vcd_mw"])])
        out.append(
            ("Power by group, mW", ["corner", "group", "vectorless", "from the tests"], rows)
        )
    if r.name == "ir_drop" and d.get("nets"):
        out.append(
            (
                "Supply nets",
                ["net", "worst mV", "average mV", "worst %", "worst cells"],
                [
                    [
                        n["net"],
                        _num(n["worst_mv"]),
                        _num(n["average_mv"]),
                        _num(n["worst_pct"], ".2g"),
                        ", ".join(w["instance"] for w in n["worst_instances"][:3]),
                    ]
                    for n in d["nets"]
                ],
            )
        )
    if r.name == "upf" and d.get("domains"):
        out.append(
            (
                "Power domains",
                ["domain", "elements", "cells", "voltage", "area (um)", "placed inside"],
                [
                    [
                        x["name"],
                        " ".join(x["elements"]),
                        str(x["cells"]),
                        _num(x["voltage"], "g"),
                        " ".join(f"{v:g}" for v in x["area_um"]) if x["area_um"] else "the core",
                        ""
                        if x["placed_inside"] is None
                        else f"{x['placed_inside']} of {x['cells']}",
                    ]
                    for x in d["domains"]
                ],
            )
        )
        if d.get("crossings"):
            out.append(
                (
                    "Signals between domains",
                    ["from", "to", "signals", "level shifters", "such as"],
                    [
                        [
                            c["from"],
                            c["to"],
                            str(c["nets"]),
                            "needed" if c["needs_level_shifters"] else "",
                            ", ".join(c["examples"][:3]),
                        ]
                        for c in d["crossings"]
                    ],
                )
            )
    if r.name == "eco" and d.get("changes"):
        out.append(
            (
                "Changes",
                ["change", "target", "cell", "result"],
                [
                    [
                        next(k for k in ("resize", "buffer", "repair") if k in c),
                        str(c.get("resize") or c.get("buffer") or c.get("repair")),
                        c.get("cell", ""),
                        c.get("result", ""),
                    ]
                    for c in d["changes"]
                ],
            )
        )
    return out


def _maps(p: Project, r: StepResult) -> list[tuple[str, str]]:
    """The IR drop maps: (net, SVG text), read from the project's build directory."""
    if r.name != "ir_drop":
        return []
    out = []
    for n in r.data.get("nets", []):
        rel = n.get("map")
        path = p.root / rel if rel else None
        if (
            path is not None
            and path.is_file()
            and path.resolve().is_relative_to(p.build_dir.resolve())
        ):
            text = path.read_text(errors="replace")
            if text.lstrip().startswith("<svg"):
                out.append((n["net"], text))
    return out


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
        if r.name in ("sim", "gls") and r.data.get("tests"):
            lines += [md_table(["test", "status", "sim ns", "message"], _test_rows(r)), ""]
        if r.name == "coverage" and r.data.get("files"):
            head, rows = _coverage_rows(r)
            lines += [md_table(head, rows), ""]
        for head, rows in _tables(r):
            lines += [md_table(head, rows), ""]
        for caption, head, rows in _impl_tables(r):
            lines += [f"{caption}:", "", md_table(head, rows), ""]
        for net in (
            (n for n in r.data.get("nets", []) if n.get("map")) if r.name == "ir_drop" else []
        ):
            lines += [f"Map of {net['net']}: `{net['map']}`", ""]
        diags = r.data.get("diagnostics") or []
        if diags:
            lines += [md_table(DIAG_HEAD, _diag_rows(diags)), ""]
        if r.name in ("pnr", "chip") and r.data.get("gds"):
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
        if r.name in ("sim", "gls") and r.data.get("tests"):
            parts.append(_table(["test", "status", "sim ns", "message"], _test_rows(r)))
        for head, rows in _tables(r):
            parts.append(_table(head, rows))
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
        for caption, head, rows in _impl_tables(r):
            parts.append(f"<p>{esc(caption)}:</p>")
            parts.append(_table(head, rows))
        for net, svg in _maps(p, r):
            # As an image, so nothing in the file can run in the report, whatever wrote it.
            data = base64.b64encode(svg.encode()).decode()
            parts.append(
                f'<figure><img src="data:image/svg+xml;base64,{data}" alt="IR drop on '
                f'{esc(net)}"><figcaption>{esc(net)}</figcaption></figure>'
            )
        diags = r.data.get("diagnostics") or []
        if diags:
            parts.append(_table(DIAG_HEAD, _diag_rows(diags)))
        if r.name in ("pnr", "chip") and r.data.get("gds"):
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
