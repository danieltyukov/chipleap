"""Lint a project's RTL with Verilator and, when it is installed, slang.

Verilator runs with ``-Wall`` on the project's top, include directories, defines,
parameters and waiver files, as it does on the generated accelerator; any message fails
the step, so warnings a project accepts belong in a waiver file in its repository. slang
is a second, stricter SystemVerilog front end (``-Wextra``); it elaborates the same top
with the same parameters and reports through its JSON diagnostics. Both run from the
project directory with project-relative paths, so their messages name files the way the
project does, and both are confined to the project's files (``chipleap.rtl.confine``).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from chipleap.rtl import confine
from chipleap.rtl.diag import Diagnostic, counts, parse_slang, parse_verilator, relative
from chipleap.rtl.project import Project
from chipleap.verify.result import StepResult
from chipleap.verify.tools import Tool, slang, verilator


def _run(cmd: list[str], project: Project) -> subprocess.CompletedProcess[str]:
    timeout = float(os.environ.get("CHIPLEAP_LINT_TIMEOUT", "600"))
    with confine.confined(cmd, project.roots) as (argv, env):
        return subprocess.run(
            argv,
            cwd=project.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


def verilator_lint(project: Project, tool: Tool) -> list[Diagnostic]:
    rel = project.rel
    cmd = [
        str(tool.path),
        "--lint-only",
        "-Wall",
        "--top-module",
        project.top,
        *(f"-I{rel(d)}" for d in project.include_dirs),
        *(f"-D{d}" for d in project.define_args()),
        *(f"-G{k}={v}" for k, v in project.parameters.items()),
        *(rel(w) for w in project.lint.waivers),
        *(rel(s) for s in project.sources),
    ]
    out = _run(cmd, project)
    diags = parse_verilator(out.stdout + out.stderr)
    if out.returncode != 0 and not diags:
        tail = (out.stdout + out.stderr).strip().splitlines()[-1:] or ["no message"]
        diags = [Diagnostic("verilator", "error", f"Verilator failed: {tail[0]}")]
    return relative(diags, project.root)


def parse_slang_json(text: str) -> list[Diagnostic]:
    """slang's ``--diag-json`` output: a list of {severity, message, optionName,
    location: "file:line:col"}."""
    out = []
    for d in json.loads(text or "[]"):
        sev = str(d.get("severity", "error"))
        if sev == "note":
            continue
        file, line, col = "", 0, 0
        loc = str(d.get("location", ""))
        parts = loc.rsplit(":", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            file, line, col = parts[0], int(parts[1]), int(parts[2])
        out.append(
            Diagnostic(
                "slang",
                "warning" if sev == "warning" else "error",
                str(d.get("message", "")),
                file,
                line,
                col,
                str(d.get("optionName", "")),
            )
        )
    return list(dict.fromkeys(out))


def slang_lint(project: Project, tool: Tool) -> list[Diagnostic]:
    rel = project.rel
    with tempfile.TemporaryDirectory(prefix="chipleap-slang-") as tmp:
        report = Path(tmp) / "diags.json"
        cmd = [
            str(tool.path),
            "-q",
            "--top",
            project.top,
            "-Wextra",
            "--diag-json",
            str(report),
            *(f"-I{rel(d)}" for d in project.include_dirs),
            *(f"-D{d}" for d in project.define_args()),
            *(a for k, v in project.parameters.items() for a in ("-G", f"{k}={v}")),
            *(rel(s) for s in project.sources),
        ]
        out = _run(cmd, project)
        text = report.read_text() if report.is_file() else ""
    try:
        diags = parse_slang_json(text)
    except json.JSONDecodeError:
        diags = parse_slang(out.stdout + out.stderr)
    if out.returncode != 0 and not diags:
        tail = (out.stdout + out.stderr).strip().splitlines()[-1:] or ["no message"]
        diags = [Diagnostic("slang", "error", f"slang failed: {tail[0]}")]
    return relative(diags, project.root)


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def lint(project: Project) -> StepResult:
    """Verilator ``-Wall`` and slang ``-Wextra`` on the project's design sources."""
    v, s = verilator(), slang()
    use_slang = project.lint.slang and s.found
    if not v.found and not use_slang:
        return StepResult("lint", "skipped", "Verilator is not installed")
    diags: list[Diagnostic] = []
    names = []
    tools: dict[str, str] = {}
    if v.found:
        diags += verilator_lint(project, v)
        names.append("Verilator -Wall")
        tools["verilator"] = v.version
    if use_slang:
        diags += slang_lint(project, s)
        names.append("slang -Wextra")
        tools["slang"] = s.version
    note = ""
    if not project.lint.slang:
        tools["slang"] = "off"
    elif not s.found:
        tools["slang"] = "not installed"
        note = "; slang is not installed"
    c = counts(diags)
    data = {
        "tools": tools,
        "errors": c["error"],
        "warnings": c["warning"],
        "diagnostics": [d.to_dict() for d in diags],
    }
    if not diags:
        who = " and ".join(names)
        return StepResult(
            "lint", "pass", f"{who} report{'' if len(names) > 1 else 's'} no warnings{note}", data
        )
    first = diags[0]
    msg = f"{_plural(c['error'], 'error')} and {_plural(c['warning'], 'warning')}; first: {first}"
    return StepResult("lint", "fail", msg, data)
