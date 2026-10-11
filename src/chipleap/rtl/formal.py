"""Formal proofs of a project's properties with SymbiYosys.

The properties are the project's own: immediate assertions, assumptions and covers in the
RTL (usually under `ifdef FORMAL, which ``read -formal`` defines) or in the files of
``formal.sources``, often a wrapper top that instantiates the design. Open Yosys reads
immediate assertions with $past, $rose, $fell, $stable and $changed; it rejects concurrent
SVA (``assert property (@(posedge clk) a |-> b)``) and ignores ``bind``, so properties
that need either belong to a commercial tool.

Each check of ``formal.checks`` gets a .sby file ChipLeap writes from checked values: the
project's sources, include directories, defines and parameters, every path absolute and
quoted (``chipleap.rtl.quote``), and the check's mode, depth and engine from a fixed list.
SymbiYosys runs under the confinement of ``chipleap.rtl.confine``. A .sby file the project
names itself (``formal.sby``) runs as it is: it can hold Python and Yosys commands that run
programs, so it is the project's code, like its tests.

A check passes, fails (a counterexample from reset, or a cover never reached), is unknown
(induction failed with no counterexample from reset: the property may hold but needs a
stronger invariant or more depth) or errs. Each counterexample comes back as the failed
property, its source line and step, the VCD and the values of the top module's signals
at every step, so an assistant can tell a wrong assertion from a design bug.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Any

from chipleap.rtl import confine
from chipleap.rtl import quote as q
from chipleap.rtl.project import FormalCheck, Project, ProjectError
from chipleap.rtl.vcd import read_trace
from chipleap.verify.result import StepResult
from chipleap.verify.tools import sby

INSTALL_HINT = "scripts/install_formal_tools.sh, then CHIPLEAP_SBY"
# "summary:   failed assertion fifo.p at /x/fifo.sv:40.5-40.63 in step 2"
_FAILED_RE = re.compile(r"failed assertion (\S+) at (.+?):(\d+)\.\d+-\d+\.\d+ (?:in )?step (\d+)")
_REACHED_RE = re.compile(
    r"reached cover statement (\S+) at (.+?):(\d+)\.\d+-\d+\.\d+ in step (\d+)"
)
_UNREACHED_RE = re.compile(r"Unreached cover statement at (.+?):(\d+)\.\d+-\d+\.\d+ \((\S+)\)")
# SymbiYosys 0.33 lists them under "summary: unreached cover statements:" instead.
_UNREACHED_ITEM_RE = re.compile(r"summary:\s+(\S+) at (.+?):(\d+)\.\d+-\d+\.\d+\s*$")
_TRACE_RE = re.compile(r"summary: (?:(?:counterexample|cover) )?trace(?: \[(\w+)\])?: (\S+)")
_DONE_RE = re.compile(r"DONE \((\w+), rc=\d+\)")
_STATUS = {"PASS": "pass", "FAIL": "fail", "UNKNOWN": "unknown"}


def sby_text(project: Project, check: FormalCheck, files: list[Path]) -> str:
    """The .sby file for one check. Raises ValueError (QuoteError) for a value no script
    may hold; the project file already refused it, so this is the second layer."""
    top = q.plain(check.top or project.top)
    opts = [*(f"-I {q.yosys(d)}" for d in project.include_dirs)]
    opts += [f"-D {q.yosys(d)}" for d in project.define_args()]
    read = " ".join(["read -formal", *opts, *(q.yosys(f) for f in files)])
    script = [read]
    if project.parameters:
        sets = " ".join(f"-set {q.plain(k)} {int(v)}" for k, v in project.parameters.items())
        script.append(f"chparam {sets} {q.plain(project.top)}")
    script.append(f"prep -top {top}")
    return "\n".join(
        [
            "[options]",
            f"mode {q.plain(check.mode)}",
            f"depth {int(check.depth)}",
            "",
            "[engines]",
            q.plain(check.engine),
            "",
            "[script]",
            *script,
            "",
        ]
    )


def _location(project: Project, file: str, line: str) -> tuple[str, int]:
    return project.rel(file), int(line)


def _property(name: str) -> str:
    """A property's label (``fifo.never_over``); "" for one Yosys named itself."""
    return "" if "$" in name else name


def parse_log(text: str, project: Project, cwd: Path, name: str) -> dict[str, Any]:
    """Status, failures, induction failures and covers from a SymbiYosys log. Trace paths
    in the log are relative to ``cwd``, where SymbiYosys ran; results name them relative
    to the project."""
    done = _DONE_RE.findall(text)
    status = _STATUS.get(done[-1], "error") if done else "error"
    out: dict[str, Any] = {"status": status, "failures": [], "induction": [], "covers": []}
    kind, trace = "", ""
    unreached = False  # inside the summary's list of unreached covers
    for line in text.splitlines():
        if "summary: unreached cover statements:" in line:
            unreached = True
            continue
        m = _UNREACHED_ITEM_RE.search(line) if unreached else None
        if m:
            file, ln = _location(project, m.group(2), m.group(3))
            out["covers"].append(
                {"property": _property(m.group(1)), "file": file, "line": ln, "reached": False}
            )
            continue
        unreached = False
        m = _TRACE_RE.search(line)
        if m:
            kind, trace = m.group(1) or "basecase", project.rel(cwd / m.group(2))
            continue
        m = _FAILED_RE.search(line)
        if m:
            file, ln = _location(project, m.group(2), m.group(3))
            entry = {
                "property": _property(m.group(1)),
                "file": file,
                "line": ln,
                "step": int(m.group(4)),
                "trace": trace,
            }
            out["induction" if kind == "induction" else "failures"].append(entry)
            continue
        m = _REACHED_RE.search(line)
        if m and "summary:" in line:
            file, ln = _location(project, m.group(2), m.group(3))
            out["covers"].append(
                {
                    "property": _property(m.group(1)),
                    "file": file,
                    "line": ln,
                    "reached": True,
                    "step": int(m.group(4)),
                    "trace": trace,
                }
            )
            continue
        m = _UNREACHED_RE.search(line)
        if m:
            file, ln = _location(project, m.group(1), m.group(2))
            out["covers"].append(
                {"property": _property(m.group(3)), "file": file, "line": ln, "reached": False}
            )
    out["covers"].sort(key=lambda c: (c["file"], c["line"]))
    if status == "error":
        errors = [ln for ln in text.splitlines() if "ERROR" in ln and "DONE" not in ln]
        first = errors[0] if errors else (text.strip().splitlines() or ["no output"])[-1]
        first = re.sub(r"^SBY \S+ \[\S+\] ", "", first)
        out["error"] = first.replace(str(project.root.resolve()) + "/", "")
    return out


def _run(cmd: list[str], cwd: Path, project: Project, workdir: Path) -> str:
    """Run SymbiYosys confined; returns its output. A timeout is an error in the output."""
    timeout = float(os.environ.get("CHIPLEAP_FORMAL_TIMEOUT", "1800"))
    with confine.confined(cmd, [*project.roots, workdir]) as (argv, env):
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # the solvers are its children
            proc.wait()
            return f"ERROR: timed out after {timeout:.0f} s (CHIPLEAP_FORMAL_TIMEOUT)\n"
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    return out or ""


def _with_traces(result: dict[str, Any], project: Project) -> dict[str, Any]:
    """Each counterexample's values, read from its VCD in the build directory."""
    for f in [*result["failures"], *result["induction"], *result["covers"]]:
        path = project.root / f.get("trace", "") if f.get("trace") else None
        if path is None or not path.is_file():
            continue
        try:
            f["values"] = read_trace(project.read(path).decode(errors="replace"))
        except ProjectError:
            continue
    return result


def run_check(
    project: Project, check: FormalCheck, workdir: Path, files: list[Path] | None = None
) -> dict[str, Any]:
    """One check: write its .sby, run SymbiYosys, and return its result. ``files`` stands
    in for the project's sources and formal sources (proposed assertions use it)."""
    tool = sby()
    if tool.path is None:
        raise RuntimeError(f"SymbiYosys (sby) is not installed: {INSTALL_HINT}")
    workdir.mkdir(parents=True, exist_ok=True)
    srcs = files if files is not None else _sources(project)
    config = workdir / f"{check.name}.sby"
    config.write_text(sby_text(project, check, srcs))
    out = _run([tool.path, "-f", "-d", check.name, config.name], workdir, project, workdir)
    (workdir / f"{check.name}.log").write_text(out)
    result = parse_log(out, project, workdir, check.name)
    head = {
        "name": check.name,
        "mode": check.mode,
        "depth": check.depth,
        "engine": check.engine,
        "top": check.top or project.top,
    }
    return {**head, **_with_traces(result, project)}


def _sources(project: Project) -> list[Path]:
    extra = list(project.formal.sources) if project.formal else []
    return [*project.sources, *extra]


def run_sby_file(project: Project, path: Path, workdir: Path) -> dict[str, Any]:
    """A .sby file of the project's, as it is, from its own directory (its [files] are
    relative to it). The work directory is ChipLeap's."""
    tool = sby()
    if tool.path is None:
        raise RuntimeError(f"SymbiYosys (sby) is not installed: {INSTALL_HINT}")
    name = f"sby_{path.stem}"
    dest = workdir / name
    workdir.mkdir(parents=True, exist_ok=True)
    out = _run([tool.path, "-f", "-d", str(dest), str(path)], path.parent, project, workdir)
    (workdir / f"{name}.log").write_text(out)
    result = parse_log(out, project, path.parent, name)
    head = {"name": f"sby:{path.stem}", "mode": "", "depth": 0, "engine": "", "top": ""}
    return {**head, **_with_traces(result, project)}


def prove(project: Project, only: str = "") -> list[dict[str, Any]]:
    """Every check of the project (or the one named ``only``), then its own .sby files."""
    if project.formal is None:
        return []
    workdir = (project.build_dir / "formal").resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    checks = [c for c in project.formal.checks if not only or c.name == only]
    results = [run_check(project, c, workdir) for c in checks]
    for path in project.formal.sby:
        if not only or only == f"sby:{path.stem}":
            results.append(run_sby_file(project, path, workdir))
    if only and not results:
        known = [c.name for c in project.formal.checks] + [
            f"sby:{p.stem}" for p in project.formal.sby
        ]
        raise ValueError(f"no formal check named {only!r}; checks: {', '.join(known)}")
    return results


def _describe(r: dict[str, Any]) -> str:
    """One check's result in a phrase."""
    label = f"{r['name']}" + (f" ({r['mode']} depth {r['depth']})" if r["mode"] else "")
    if r["status"] == "error":
        return f"{label}: error: {r.get('error', '')}"
    covers = r["covers"]
    reached = sum(c["reached"] for c in covers)
    if r["failures"]:
        f = r["failures"][0]
        what = f["property"] or "an assertion"
        return (
            f"{label}: {what} at {f['file']}:{f['line']} fails in step {f['step']} ({f['trace']})"
        )
    if covers and reached < len(covers):
        return f"{label}: {reached} of {len(covers)} covers reached"
    if r["status"] == "unknown":
        why = r["induction"][0] if r["induction"] else None
        where = f"; induction fails at {why['file']}:{why['line']}" if why else ""
        return f"{label}: unknown, no counterexample from reset but no proof{where}"
    if covers:
        return f"{label}: {reached} of {len(covers)} covers reached"
    return f"{label}: every assertion holds"


def formal(project: Project, only: str = "") -> StepResult:
    """The formal step: every check passes, or the first failure with its counterexample."""
    if project.formal is None:
        return StepResult("formal", "skipped", "the project has no formal section")
    if not sby().found:
        return StepResult("formal", "skipped", f"SymbiYosys (sby) is not installed: {INSTALL_HINT}")
    results = prove(project, only)
    bad = [r for r in results if r["status"] != "pass"]
    data = {
        "tool": "SymbiYosys",
        "checks": results,
        "passed": len(results) - len(bad),
        "failed": len(bad),
    }
    if not bad:
        return StepResult("formal", "pass", "; ".join(_describe(r) for r in results), data)
    return StepResult("formal", "fail", "; ".join(_describe(r) for r in bad), data)
