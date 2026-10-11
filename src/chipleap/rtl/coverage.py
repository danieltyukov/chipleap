"""Code coverage of a project's design sources from its cocotb tests, with Verilator.

The tests run once more on Verilator built with ``--coverage``, which counts every line
block, branch, expression term and signal toggle (each bit rising and falling). The
counts come back in ``coverage.dat``; this module reads it, keeps the points in the
project's design sources (not testbench-only files) and summarises them per kind and per
file, with the points the tests never reached. Coverage is reported, not judged: the
step passes when the run produced counts.

Line coverage here is Verilator's ``line`` points: one per straight-line block of
statements. Branches are counted separately, as Verilator's own report does.

Closing coverage is a loop an assistant can run: the step keeps its summary in
``build/coverage/summary.json`` and reports how many points the latest tests newly reached
since the run before; ``gaps`` reads that summary back with the source lines around each
point never reached and the project's test functions, so a test that reaches them can be
written without reading the tree.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl.project import Project, ProjectError
from chipleap.rtl.sim import SimError, SimUnavailable, choose, run_tests, summarize
from chipleap.verify.remote import Remote
from chipleap.verify.result import StepResult

KIND_ORDER = ("line", "branch", "toggle", "expr", "fsm_state", "fsm_arc", "user")
UNCOVERED_LIMIT = 200
SUMMARY = "summary.json"
# A cocotb test function: the decorator, any others, then the coroutine.
_TEST_RE = re.compile(r"@cocotb\.test\b[^\n]*\n(?:[ \t]*@[^\n]*\n)*[ \t]*async\s+def\s+(\w+)")


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


def _point_key(d: dict[str, Any]) -> tuple[str, str, int, str, str]:
    return (d["kind"], d["file"], int(d["line"]), d["name"], d.get("hier", ""))


def delta(before: list[dict[str, Any]] | None, now: list[dict[str, Any]]) -> dict[str, int] | None:
    """How many points the latest run reached that the one before did not, and the other
    way round; None when there was no run before."""
    if before is None:
        return None
    old, new = {_point_key(d) for d in before}, {_point_key(d) for d in now}
    return {"newly_covered": len(old - new), "newly_uncovered": len(new - old)}


def _summary_path(project: Project) -> Path:
    return project.build_dir / "coverage" / SUMMARY


def _last(project: Project) -> dict[str, Any] | None:
    path = _summary_path(project)
    if not path.is_file():
        return None
    try:
        data = json.loads(project.read(path))
    except (ProjectError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def test_functions(project: Project) -> dict[str, list[str]]:
    """The cocotb tests in each of the project's test modules."""
    out: dict[str, list[str]] = {}
    if project.tests.dir is None:
        return out
    for m in project.tests.modules:
        path = project.tests.dir / f"{m.replace('.', '/')}.py"
        try:
            text = project.read(path).decode(errors="replace")
        except ProjectError:
            continue
        out[m] = _TEST_RE.findall(text)
    return out


def gaps(
    project: Project, file: str = "", kinds: str = "", limit: int = 50, context: int = 2
) -> dict[str, Any]:
    """The points the last coverage run never reached, grouped by source line, each with
    the lines around it; and the project's test functions. ``kinds`` is a comma-separated
    list (line, branch, toggle ...). Raises ValueError before any run."""
    last = _last(project)
    if last is None:
        raise ValueError("no coverage results yet: run coverage first (coverage_rtl)")
    wanted = {k.strip() for k in kinds.split(",") if k.strip()}
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for d in last.get("uncovered", []):
        if (file and d["file"] != file) or (wanted and d["kind"] not in wanted):
            continue
        groups.setdefault((d["file"], int(d["line"])), []).append(d)
    sources: dict[str, list[str] | None] = {}
    context = max(0, min(int(context), 20))
    out = []
    for (f, line), points in sorted(groups.items())[: max(0, limit)]:
        if f not in sources:
            try:
                sources[f] = project.read(project.root / f).decode(errors="replace").splitlines()
            except (ProjectError, OSError):
                sources[f] = None  # never outside the project
        text = sources[f]
        start = max(1, line - context)
        out.append(
            {
                "file": f,
                "line": line,
                "points": [
                    {"kind": p["kind"], "name": p["name"]}
                    for p in sorted(points, key=lambda p: (_key(p["kind"]), p["name"]))
                ],
                "source": {"start": start, "lines": text[start - 1 : line + context]}
                if text is not None
                else None,
            }
        )
    tests = {
        "dir": project.rel(project.tests.dir) if project.tests.dir else None,
        "modules": test_functions(project),
    }
    return {
        "totals": last.get("kinds", {}),
        "stale": last.get("inputs_sha256") != project.inputs_digest(),
        "gaps": out,
        "gaps_total": len(groups),
        "tests": tests,
    }


def coverage(project: Project, remote: Remote | None = None) -> StepResult:
    """Run the tests on Verilator with coverage and summarise line and toggle coverage."""
    if project.tests.dir is None:
        return StepResult("coverage", "skipped", "the project has no tests (add a tests section)")
    if remote is not None:
        return StepResult("coverage", "skipped", "coverage runs on this machine's Verilator")
    last = _last(project)  # the run below starts from an empty directory
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
    every = [p.to_dict() for p in s.uncovered]
    change = delta(last.get("uncovered") if last else None, every)
    if change is not None:
        data.update(change)
    (dat.parent / SUMMARY).write_text(
        json.dumps(
            {
                "inputs_sha256": project.inputs_digest(),
                "kinds": data["kinds"],
                "uncovered": every,
            },
            indent=1,
        )
    )
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
        + (f", {change['newly_covered']} newly reached since the last run" if change else "")
    )
    return StepResult("coverage", "pass", msg, data)
