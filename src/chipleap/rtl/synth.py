"""Logic synthesis of a project with Yosys on its technology's standard cells.

The method is the accelerator's (``chipleap.verify.synth``): sv2v first when it is
installed, because the Yosys in Ubuntu 24.04 (0.33) cannot read every SystemVerilog
construct; then Yosys maps the design to the liberty's cells, and one ABC call maps,
buffers high-fanout nets, sizes gates and times the result. ABC aims at half the fastest
clock's period: routing and the slow corner (1.08 V, 125 C) roughly double a synthesized
path on SG13G2, so that is what leaves the clock reachable after place and route. The
critical path plus a flip-flop's clock-to-Q and setup gives an Fmax estimate at the
typical corner; the routed number at the slow corner comes from ``pnr``.

The mapped netlist stays in the work directory (``<top>.synth.v``) for later checks
(equivalence, gate-level simulation). sv2v and Yosys read only the project, the work
directory and the liberty (``chipleap.rtl.confine``): a memory file the RTL names cannot
bring another file into the netlist.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.hw.tech import FLOP_OVERHEAD_NS, get_tech
from chipleap.rtl import confine
from chipleap.rtl import quote as q
from chipleap.rtl.diag import Diagnostic, parse_sv2v, parse_yosys, relative
from chipleap.rtl.project import Project, runs_tools
from chipleap.verify.result import StepResult
from chipleap.verify.tools import ihp_liberty, liberty_settings_problem, sv2v, yosys

DEFAULT_PERIOD_NS = 10.0  # for designs without a clock
_SEQ_CELL = re.compile(r"(?i)(^|_)(s?df|dl|lh|dff|latch)")
# Warnings about the liberty file, not the design (IHP's scan flops, for one).
_LIBERTY_NOISE = re.compile(r"in pin attribute of cell|unsupported expression .* cell")


class SynthError(RuntimeError):
    def __init__(self, message: str, diagnostics: list[Diagnostic] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics or []


class SynthUnavailable(SynthError):
    """No Yosys or no liberty for the technology."""


@dataclass(frozen=True)
class RtlSynthResult:
    area_um2: float
    cells: int
    flops: int  # flip-flops and latches
    delay_ns: float  # ABC's critical path, buffered and sized
    fmax_mhz: float
    target_ns: float
    liberty: Path
    netlist: Path
    diagnostics: list[Diagnostic] = field(default_factory=list)


def no_liberty(tech_key: str) -> str:
    """Why there is no liberty for ``tech_key``, and what to do."""
    if tech_key != "ihp-sg13g2":
        return f"no liberty file for {tech_key}: add it with chipleap tech add --liberty, or use ihp-sg13g2"
    why = liberty_settings_problem()
    return f"no liberty file for {tech_key}: " + (
        why or "install the open PDK or set CHIPLEAP_LIBERTY"
    )


def liberty_for(tech_key: str) -> Path | None:
    """The typical-corner liberty for a technology, if it is on this machine."""
    t = get_tech(tech_key)
    if t.liberty and not t.host:
        return Path(t.liberty) if Path(t.liberty).is_file() else None
    if tech_key == "ihp-sg13g2":
        return ihp_liberty()
    return None


def prepare_verilog(project: Project, workdir: Path) -> tuple[list[Path], bool]:
    """The files a Verilog-2005 front end reads: one sv2v output with includes and
    defines resolved when sv2v is installed and needed, else the sources themselves.
    Returns the files and whether they still need the include directories and defines."""
    needs_sv = any(s.suffix in (".sv", ".svh") for s in project.sources)
    tool = sv2v()
    if not needs_sv or not tool.found:
        return list(project.sources), True
    out = workdir / f"{project.top}.sv2v.v"
    cmd = [
        str(tool.path),
        "--exclude=Always",
        f"--top={project.top}",
        *(f"-I{project.rel(d)}" for d in project.include_dirs),
        *(f"-D{d}" for d in project.define_args()),
        *(project.rel(s) for s in project.sources),
    ]
    with confine.confined(cmd, project.roots) as (argv, env):
        res = subprocess.run(
            argv,
            cwd=project.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    if res.returncode != 0:
        diags = relative(parse_sv2v(res.stderr), project.root)
        raise SynthError(f"sv2v failed: {diags[0] if diags else res.stderr.strip()}", diags)
    out.write_text(res.stdout)
    return [out], False


def read_commands(project: Project, files: list[Path], raw: bool) -> list[str]:
    """The Yosys commands that read the project: sources, include directories, defines,
    parameters and the top."""
    opts: list[str] = []
    if raw:
        opts += [f"-I {q.yosys(i)}" for i in project.include_dirs]
        opts += [f"-D {q.yosys(x)}" for x in project.define_args()]
    reads = [" ".join(["read_verilog -sv", *opts, q.yosys(f)]) for f in files]
    chparam = " ".join(f"-set {q.plain(k)} {int(v)}" for k, v in project.parameters.items())
    top = q.plain(project.top)
    return [
        *reads,
        *([f"chparam {chparam} {top}"] if chparam else []),
        f"hierarchy -check -top {top}",
    ]


def front(project: Project, files: list[Path], raw: bool, flatten: bool = True) -> list[str]:
    """The Yosys commands that read the project and synthesize it to generic gates, before
    flip-flops and cells are mapped to the liberty. Equivalence checking starts from the
    same commands (``chipleap.rtl.equiv``). ``flatten`` off keeps the module hierarchy."""
    synth = f"synth -top {q.plain(project.top)}" + (" -flatten" if flatten else "")
    return [*read_commands(project, files, raw), synth]


def _script(
    project: Project,
    files: list[Path],
    raw: bool,
    liberty: Path,
    workdir: Path,
    target_ns: float,
    flatten: bool = True,
) -> str:
    """The Yosys script. Every path and value from the project is a quoted word
    (``chipleap.rtl.quote``), so none can start a command or end a word early."""
    d = f"{target_ns * 1000:.0f}"
    # Map, buffer, size and time in one ABC call (see chipleap.verify.synth for why). The
    # script goes inline ("+" form, commas for spaces): ABC cannot read a script file
    # whose path holds a space, and a project directory may.
    abc = "+" + ";".join(
        [
            "strash",
            "dretime",
            f"map,-D,{d}",
            "topo",
            "stime,-p",
            "buffer,-p",
            f"upsize,-D,{d}",
            f"dnsize,-D,{d}",
            "topo",
            "stime,-p",
        ]
    )
    lib = q.yosys(liberty)
    netlist = workdir / f"{project.top}.synth.v"
    return "\n".join(
        [
            *front(project, files, raw, flatten),
            f"dfflibmap -liberty {lib}",
            # ChipLeap's own text, no project data: ";" inside quotes stays in the word.
            f'abc -liberty {lib} -script "{abc}"',
            "opt_clean",
            f"stat -liberty {lib}",
            f"write_verilog -noattr {q.yosys(netlist)}",
        ]
    )


@runs_tools
def synthesize(
    project: Project, workdir: Path | None = None, *, flatten: bool = True
) -> RtlSynthResult:
    """Map the project to its technology's cells. ``flatten`` off keeps the module
    hierarchy, which power domains name (``chipleap.rtl.upf``)."""
    tool = yosys()
    if not tool.found or tool.path is None:
        raise SynthUnavailable(tool.missing("Yosys"))
    lib = liberty_for(project.tech)
    if lib is None:
        raise SynthUnavailable(no_liberty(project.tech))
    work = project.work_dir(workdir or "synth")
    files, raw = prepare_verilog(project, work)
    clk = project.fastest_clock
    target = (clk.period_ns if clk else DEFAULT_PERIOD_NS) / 2
    script = work / "synth.ys"
    with tempfile.TemporaryDirectory(prefix="chipleap-lib-") as tmp:
        liberty = lib.resolve()
        if " " in str(liberty):
            # ABC cannot read a liberty file whose path holds a space; give it a copy.
            liberty = Path(tmp) / "cells.lib"
            shutil.copy2(lib, liberty)
        try:
            script.write_text(_script(project, files, raw, liberty, work, target, flatten) + "\n")
        except q.QuoteError as exc:
            raise SynthError(f"cannot write the Yosys script: {exc}") from exc
        cmd = [str(tool.path), "-s", str(script)]
        reads = [*project.roots, work, lib.resolve(), Path(tmp)]
        with confine.confined(cmd, reads) as (argv, env):
            proc = subprocess.Popen(
                argv,
                cwd=project.root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                out, _ = proc.communicate(
                    timeout=float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "1800"))
                )
            except BaseException:
                os.killpg(proc.pid, signal.SIGKILL)  # ABC is a child of Yosys
                proc.wait()
                raise
    log = out or ""
    (work / "yosys.log").write_text(log)
    diags = relative(parse_yosys(log), project.root)
    if proc.returncode != 0:
        errors = [x for x in diags if x.severity == "error"] or [
            Diagnostic("yosys", "error", log.strip().splitlines()[-1] if log.strip() else "failed")
        ]
        raise SynthError(f"Yosys failed: {errors[0]}", errors)
    area = re.findall(
        rf"Chip area for (?:top )?module '\\?{re.escape(project.top)}': ([\d.]+)", log
    )
    if not area:
        raise SynthError("Yosys did not report an area", diags)
    cells = re.findall(r"Number of cells:\s+(\d+)", log)
    # The cell list of the last stat: "     sg13g2_dfrbp_1     12".
    stat = log[log.rfind("Number of cells:") :]
    flops = sum(
        int(n)
        for name, n in re.findall(r"^\s+(\S+)\s+(\d+)\s*$", stat, re.M)
        if _SEQ_CELL.search(name)
    )
    delay = re.findall(r"Delay =\s+([\d.]+) ps", log)
    delay_ns = float(delay[-1]) / 1000 if delay else 0.0
    period = delay_ns + FLOP_OVERHEAD_NS
    return RtlSynthResult(
        area_um2=float(area[-1]),
        cells=int(cells[-1]) if cells else 0,
        flops=flops,
        delay_ns=delay_ns,
        fmax_mhz=1000 / period if period > 0 else 0.0,
        target_ns=target,
        liberty=lib,
        netlist=work / f"{project.top}.synth.v",
        diagnostics=[
            x for x in diags if x.severity != "error" and not _LIBERTY_NOISE.search(x.message)
        ],
    )


def synth_data(project: Project, r: RtlSynthResult) -> dict[str, Any]:
    clk = project.fastest_clock
    return {
        "tool": "Yosys",
        "technology": project.tech,
        "liberty": r.liberty.name,
        "area_um2": round(r.area_um2, 2),
        "cells": r.cells,
        "flops": r.flops,
        "critical_path_ns": round(r.delay_ns, 3),
        "fmax_mhz_estimate": round(r.fmax_mhz, 1),
        "target_ns": round(r.target_ns, 3),
        "clock_mhz": round(clk.mhz, 3) if clk else None,
        "netlist": project.rel(r.netlist),
        "warnings": len(r.diagnostics),
        "diagnostics": [d.to_dict() for d in r.diagnostics],
    }


@runs_tools
def synth(project: Project) -> StepResult:
    """The synthesis step: area, cells and an Fmax estimate on the project's technology."""
    try:
        r = synthesize(project)
    except SynthUnavailable as exc:
        return StepResult("synth", "skipped", str(exc))
    except SynthError as exc:
        return StepResult(
            "synth",
            "fail",
            str(exc).splitlines()[0],
            {"diagnostics": [d.to_dict() for d in exc.diagnostics]},
        )
    tech = get_tech(project.tech).name
    clk = project.fastest_clock
    fits = ""
    if clk:
        verdict = "within" if r.fmax_mhz >= clk.mhz else "short of"
        fits = f", {verdict} the {clk.mhz:g} MHz clock on {clk.port}"
    warn = f"; {len(r.diagnostics)} Yosys warnings" if r.diagnostics else ""
    msg = (
        f"{r.area_um2 / 1e6:.4f} mm2 of standard cells on {tech} ({r.cells} cells, "
        f"{r.flops} flip-flops); critical path {r.delay_ns:.2f} ns, about {r.fmax_mhz:.0f} MHz "
        f"at the typical corner{fits}{warn}"
    )
    return StepResult("synth", "pass", msg, synth_data(project, r))
