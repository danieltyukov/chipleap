"""``chipleap verify PROJECT``: the steps after architecture, on a user's RTL.

Each step returns a ``StepResult`` as the accelerator's steps do, and skips itself with a
reason when its tool is missing. Results go to ``PROJECT/build/verify``: ``verify.json``,
``VERIFY.md`` and ``report.html``.
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from pathlib import Path

from chipleap.rtl import builddir, commercial
from chipleap.rtl.cdc import cdc
from chipleap.rtl.coverage import coverage
from chipleap.rtl.equiv import equiv
from chipleap.rtl.formal import formal
from chipleap.rtl.gls import gls
from chipleap.rtl.lint import lint
from chipleap.rtl.pnr import pnr
from chipleap.rtl.project import Project, load_project
from chipleap.rtl.report import project_html, project_markdown
from chipleap.rtl.sim import COMMERCIAL, simulate
from chipleap.rtl.synth import synth
from chipleap.verify.remote import Remote
from chipleap.verify.result import StepResult

# Equivalence and gate-level simulation come after place and route, so they see its netlist.
PROJECT_STEPS = ("lint", "sim", "coverage", "formal", "cdc", "synth", "pnr", "equiv", "gls")
# Place and route takes minutes, so it runs only when named or with "all"; gate-level
# simulation needs its result.
DEFAULT_STEPS = ("lint", "sim", "coverage", "formal", "cdc", "synth", "equiv")
SIM_TOOLS = ("auto", "icarus", "verilator", *COMMERCIAL)
SYNTH_TOOLS = ("yosys", "genus", "dc")
PNR_TOOLS = ("librelane", "innovus")


def steps_from(text: str | Sequence[str]) -> tuple[str, ...]:
    """Step names from ``lint,sim`` or a sequence; ``all`` is every step."""
    items = [s.strip() for s in text.split(",")] if isinstance(text, str) else list(text)
    items = [s for s in items if s]
    if "all" in items:
        return PROJECT_STEPS
    bad = [s for s in items if s not in PROJECT_STEPS]
    if bad:
        raise ValueError(
            f"unknown step {bad[0]!r} for an RTL project; steps: {', '.join(PROJECT_STEPS)}, all"
        )
    return tuple(items) or DEFAULT_STEPS


# What a step can raise when a tool misbehaves: a timeout, a missing program, a truncated
# results file, a server that went away. The step fails; the others and the reports run.
STEP_ERRORS = (
    subprocess.SubprocessError,
    OSError,
    ET.ParseError,
    ValueError,  # JSONDecodeError, QuoteError, UnicodeDecodeError among them
    RuntimeError,  # SimError, SynthError, PnrError, RemoteError
)


def run_step(
    project: Project,
    step: str,
    *,
    sim_tool: str = "",
    synth_tool: str = "yosys",
    pnr_tool: str = "librelane",
    remote: Remote | None = None,
    test_filter: str = "",
) -> StepResult:
    """One step on the project. A tool that fails in an unexpected way fails the step,
    with what it raised, instead of stopping the run."""
    if step not in PROJECT_STEPS:
        raise ValueError(f"unknown step {step!r}")
    try:
        return _run_step(project, step, sim_tool, synth_tool, pnr_tool, remote, test_filter)
    except STEP_ERRORS as exc:
        first = (str(exc).strip().splitlines() or [""])[0]
        msg = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
        return StepResult(step, "fail", msg, {"error": f"{type(exc).__name__}: {exc}"})


def _run_step(
    project: Project,
    step: str,
    sim_tool: str,
    synth_tool: str,
    pnr_tool: str,
    remote: Remote | None,
    test_filter: str,
) -> StepResult:
    if step == "lint":
        return lint(project)
    if step == "sim":
        tool = "" if sim_tool in ("", "auto") else sim_tool
        # Open simulators always run here; only the commercial ones go to the server.
        return simulate(project, tool, remote if tool in COMMERCIAL else None, test_filter)
    if step == "coverage":
        return coverage(project)
    if step == "formal":
        return formal(project)
    if step == "cdc":
        return cdc(project)
    if step == "equiv":
        return equiv(project)
    if step == "gls":
        return gls(project, test_filter)
    if step == "synth":
        if synth_tool != "yosys":
            return commercial.synth_step(project, synth_tool, remote)
        return synth(project)
    if step == "pnr":
        if pnr_tool == "innovus":
            return commercial.pnr_step(project, remote)
        return pnr(project)
    raise AssertionError(step)  # run_step checked the name


def write_reports(project: Project, results: Sequence[StepResult]) -> Path:
    out = project.work_dir("verify")
    builddir.write_text(
        out,
        "verify.json",
        json.dumps(
            {
                "project": project.to_dict(),
                "steps": [
                    {"step": r.name, "status": r.status, "summary": r.summary, **r.data}
                    for r in results
                ],
            },
            indent=2,
        ),
    )
    builddir.write_text(out, "VERIFY.md", project_markdown(project, results))
    builddir.write_text(out, "report.html", project_html(project, results))
    return out


def verify_project(
    path: str | Path,
    *,
    steps: str | Sequence[str] = DEFAULT_STEPS,
    sim_tool: str = "",
    synth_tool: str = "yosys",
    pnr_tool: str = "librelane",
    remote: Remote | None = None,
    allow: Iterable[str | Path] = (),
) -> list[StepResult]:
    """Run ``steps`` on the project at ``path`` and write its reports. ``allow`` names
    directories outside the project its files may come from (the user's choice)."""
    project = load_project(path, allow)
    if sim_tool and sim_tool not in SIM_TOOLS:
        raise ValueError(f"sim tool must be one of {', '.join(SIM_TOOLS)}")
    if synth_tool not in SYNTH_TOOLS:
        raise ValueError(f"synthesis tool must be one of {', '.join(SYNTH_TOOLS)}")
    if pnr_tool not in PNR_TOOLS:
        raise ValueError(f"place-and-route tool must be one of {', '.join(PNR_TOOLS)}")
    results = [
        run_step(
            project,
            s,
            sim_tool=sim_tool,
            synth_tool=synth_tool,
            pnr_tool=pnr_tool,
            remote=remote,
        )
        for s in steps_from(steps)
    ]
    write_reports(project, results)
    return results
