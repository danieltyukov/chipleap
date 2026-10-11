"""Code coverage of a project's design sources from its cocotb tests, with Verilator.

The tests run once more on Verilator built with ``--coverage``, which counts every line
block, branch, expression term and signal toggle (each bit rising and falling). The
counts come back in ``coverage.dat``; this module reads it, keeps the points in the
project's design sources (not testbench-only files) and summarises them per kind and per
file, with the points the tests never reached. Coverage is reported, not judged: the
step passes when the run produced counts.

Line coverage here is Verilator's ``line`` points: one per straight-line block of
statements. Branches are counted separately, as Verilator's own report does.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl.project import Project
from chipleap.rtl.sim import SimError, SimUnavailable, choose, run_tests, summarize
from chipleap.verify.remote import Remote
from chipleap.verify.result import StepResult

KIND_ORDER = ("line", "branch", "toggle", "expr", "fsm_state", "fsm_arc", "user")
UNCOVERED_LIMIT = 200


@dataclass(frozen=True)
class Point:
    kind: str  # line | branch | toggle | expr | fsm_state | fsm_arc | user
    file: str
    line: int
    name: str  # Verilator's description: block, if, else, or q[3]:0->1 for a toggle
    hier: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_ENTRY = re.compile(r"^C '(.*)' (\d+)\s*$")


def parse_dat(text: str) -> list[Point]:
    """Points from Verilator's ``coverage.dat``: ``C '<key>\\x02<value>\\x01...' count``."""
    out = []
    for raw in text.splitlines():
        m = _ENTRY.match(raw)
        if not m:
            continue
        fields: dict[str, str] = {}
        for kv in m.group(1).split("\x01"):
            k, _, v = kv.partition("\x02")
            if k:
                fields[k] = v
        page = fields.get("page", "")
        kind = fields.get("t") or (page.split("/", 1)[0].removeprefix("v_") if page else "user")
        out.append(
            Point(
                kind=kind,
                file=fields.get("f", ""),
                line=int(fields.get("l", "0") or 0),
                name=fields.get("o", ""),
                hier=fields.get("h", ""),
                count=int(m.group(2)),
            )
        )
    return out


@dataclass
class Summary:
    kinds: dict[str, tuple[int, int]] = field(default_factory=dict)  # covered, total
    files: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)
    uncovered: list[Point] = field(default_factory=list)

    def percent(self, kind: str) -> float | None:
        c, t = self.kinds.get(kind, (0, 0))
        return round(100.0 * c / t, 1) if t else None


def _key(kind: str) -> tuple[int, str]:
    return (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER), kind)


def summarize_points(points: list[Point], project: Project) -> Summary:
    """Per-kind and per-file totals over the project's design sources, file names relative
    to the project."""
    design = {project.rel(s) for s in project.sources}
    s = Summary()
    kinds: dict[str, list[int]] = {}
    files: dict[str, dict[str, list[int]]] = {}
    for p in points:
        rel = project.rel(p.file) if Path(p.file).is_absolute() else p.file
        if rel not in design:
            continue
        hit = 1 if p.count > 0 else 0
        kinds.setdefault(p.kind, [0, 0])
        kinds[p.kind][0] += hit
        kinds[p.kind][1] += 1
        per = files.setdefault(rel, {}).setdefault(p.kind, [0, 0])
        per[0] += hit
        per[1] += 1
        if not hit:
            s.uncovered.append(Point(p.kind, rel, p.line, p.name, p.hier, 0))
    s.kinds = {k: (v[0], v[1]) for k, v in sorted(kinds.items(), key=lambda kv: _key(kv[0]))}
    s.files = {
        f: {k: (v[0], v[1]) for k, v in sorted(d.items(), key=lambda kv: _key(kv[0]))}
        for f, d in sorted(files.items())
    }
    s.uncovered.sort(key=lambda p: (p.file, p.line, _key(p.kind), p.name))
    return s


def coverage_data(s: Summary) -> dict[str, Any]:
    return {
        "kinds": {
            k: {"covered": c, "total": t, "percent": s.percent(k)} for k, (c, t) in s.kinds.items()
        },
        "files": [
            {"file": f, **{k: [c, t] for k, (c, t) in d.items()}} for f, d in s.files.items()
        ],
        "uncovered": [p.to_dict() for p in s.uncovered[:UNCOVERED_LIMIT]],
        "uncovered_total": len(s.uncovered),
    }


def _fmt(s: Summary, kind: str) -> str:
    c, t = s.kinds[kind]
    return f"{kind} {s.percent(kind):.1f}% ({c}/{t})"


def coverage(project: Project, remote: Remote | None = None) -> StepResult:
    """Run the tests on Verilator with coverage and summarise line and toggle coverage."""
    if project.tests.dir is None:
        return StepResult("coverage", "skipped", "the project has no tests (add a tests section)")
    if remote is not None:
        return StepResult("coverage", "skipped", "coverage runs on this machine's Verilator")
    try:
        _, why = choose(project, "verilator")
        if why:
            raise SimUnavailable(why)
        run = run_tests(project, "verilator", coverage=True)
    except SimUnavailable as exc:
        return StepResult("coverage", "skipped", str(exc).splitlines()[0])
    except SimError as exc:
        return StepResult("coverage", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    dat = run.workdir / "coverage.dat" if run.workdir else None
    if run.stage == "build" or dat is None or not dat.is_file():
        status, summary = summarize(run)
        why = summary if status == "fail" else "Verilator wrote no coverage.dat"
        return StepResult(
            "coverage",
            "fail",
            why,
            {"diagnostics": [d.to_dict() for d in run.diagnostics]},
        )
    s = summarize_points(parse_dat(dat.read_text(errors="replace")), project)
    data = {"simulator_version": run.version, **coverage_data(s)}
    if not s.kinds:
        return StepResult("coverage", "fail", "no coverage points in the design sources", data)
    shown = [k for k in ("line", "branch", "toggle") if k in s.kinds]
    tests = f"{len(run.tests)} tests"
    if run.failed:
        tests += f", {len(run.failed)} failing"
    msg = (
        ", ".join(_fmt(s, k) for k in shown)
        + f" over {len(s.files)} file{'' if len(s.files) == 1 else 's'} ({tests}); "
        + f"{len(s.uncovered)} points never reached"
    )
    return StepResult("coverage", "pass", msg, data)
