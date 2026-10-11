"""``chipleap verify PROJECT``: the steps after architecture, on a user's RTL.

Each step returns a ``StepResult`` as the accelerator's steps do, and skips itself with a
reason when its tool is missing. Results go to ``PROJECT/build/verify``: ``verify.json``,
``VERIFY.md`` and ``report.html``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from chipleap.rtl import commercial
from chipleap.rtl.coverage import coverage
from chipleap.rtl.lint import lint
from chipleap.rtl.pnr import pnr
from chipleap.rtl.project import Project, load_project
from chipleap.rtl.report import project_html, project_markdown
from chipleap.rtl.sim import COMMERCIAL, simulate
from chipleap.rtl.synth import synth
from chipleap.verify.remote import Remote
from chipleap.verify.result import StepResult

PROJECT_STEPS = ("lint", "sim", "coverage", "synth", "pnr")
# Place and route takes minutes, so it runs only when named or with "all".
DEFAULT_STEPS = ("lint", "sim", "coverage", "synth")
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


def run_step(
    project: Project,
    step: str,
    *,
    sim_tool: str = "",
    synth_tool: str = "yosys",
    pnr_tool: str = "librelane",
    remote: Remote | None = None,
) -> StepResult:
    if step == "lint":
        return lint(project)
    if step == "sim":
        tool = "" if sim_tool in ("", "auto") else sim_tool
        # Open simulators always run here; only the commercial ones go to the server.
        return simulate(project, tool, remote if tool in COMMERCIAL else None)
    if step == "coverage":
        return coverage(project)
    if step == "synth":
        if synth_tool != "yosys":
            return commercial.synth_step(project, synth_tool, remote)
        return synth(project)
    if step == "pnr":
        if pnr_tool == "innovus":
            return commercial.pnr_step(project, remote)
        return pnr(project)
    raise ValueError(f"unknown step {step!r}")


def write_reports(project: Project, results: Sequence[StepResult]) -> Path:
    out = project.build_dir / "verify"
    out.mkdir(parents=True, exist_ok=True)
    (out / "verify.json").write_text(
        json.dumps(
            {
                "project": project.to_dict(),
                "steps": [
                    {"step": r.name, "status": r.status, "summary": r.summary, **r.data}
                    for r in results
                ],
            },
            indent=2,
        )
    )
    (out / "VERIFY.md").write_text(project_markdown(project, results))
    (out / "report.html").write_text(project_html(project, results))
    return out


def verify_project(
    path: str | Path,
    *,
    steps: str | Sequence[str] = DEFAULT_STEPS,
    sim_tool: str = "",
    synth_tool: str = "yosys",
    pnr_tool: str = "librelane",
    remote: Remote | None = None,
) -> list[StepResult]:
    """Run ``steps`` on the project at ``path`` and write its reports."""
    project = load_project(path)
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
