"""Gate-level simulation: the project's cocotb tests on its netlist.

By default the routed netlist of the LibreLane run in ``build/pnr`` (``final/nl``) runs
with the technology's cell models and the SDF of the same run at one corner
(``final/sdf/<corner>``, the typical one unless ``gate_sim.corner`` names another), so the
tests see the delays of the routed design. ``gate_sim.netlist: synth`` runs ChipLeap's
synthesized netlist with zero delay instead, which needs no routed run.

IHP's cell models need Icarus 13: Icarus 12 elaborates them, but every flip-flop stays X
and every test fails the same way. ``scripts/install_iverilog13.sh`` installs it beside the
system one and ``CHIPLEAP_IVERILOG`` selects it; this step checks the version.

The netlist is copied into ``build/gls`` with ``initial $sdf_annotate("<sdf>");`` added to
its top module, so the design under test is still the netlist's top with the same ports,
and the cell models and the SDF are copied there too. Icarus runs with ``-gspecify`` and
``-T`` for the minimum, typical or maximum delays. The tests run with ``GATES=yes`` in the
environment (the convention of Tiny Tapeout's gate-level tests, for a test that must skip
what only RTL has) and with ``CHIPLEAP_PARAMETERS``, as every simulation does, since a
netlist has no parameters left to read.

The tests are the project's code, so this step runs them as the simulation step does: only
for projects you trust.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from pathlib import Path
from typing import Any

from chipleap.rtl import quote as q
from chipleap.rtl.pnr import routed_run
from chipleap.rtl.project import Project
from chipleap.rtl.sim import SimError, SimUnavailable, run_tests, sim_data, summarize
from chipleap.rtl.synth import SynthError, liberty_for, synthesize
from chipleap.verify.result import StepResult
from chipleap.verify.tools import Tool, iverilog

DEFAULT_CORNER = "nom_typ_1p20V_25C"
INSTALL_HINT = "scripts/install_iverilog13.sh, then CHIPLEAP_IVERILOG"


class GlsUnavailable(RuntimeError):
    pass


_COND_RE = re.compile(r"\(COND ([^\n(][^\n]*)\n")


def normalize_sdf(text: str) -> str:
    """OpenSTA writes conditions as ``(COND A0 == 1'b1 & A1 == 1'b0``; Icarus reads only
    the parenthesized form the cell models use, ``(COND (A0 == 1'b1 && A1 == 1'b0)``, and
    stops reading the file at the first one it cannot parse, leaving later cells without
    delays."""

    def fix(m: re.Match[str]) -> str:
        expr = re.sub(r"\s\|\s", " || ", re.sub(r"\s&\s", " && ", m.group(1).strip()))
        return f"(COND ({expr})\n"

    return _COND_RE.sub(fix, text)


def sdf_messages(log: str) -> dict[str, Any]:
    """What Icarus said about the SDF: lines it could not read (the delays after them are
    lost), and interconnects it could not place (Icarus cannot annotate a wire that ends
    at a port of the top module; their delays stay zero)."""
    lines = [ln for ln in log.splitlines() if "SDF ERROR" in ln]
    broken = sorted(
        {ln.split("SDF ERROR: ", 1)[-1] for ln in lines if re.search(r"syntax|malformed", ln, re.I)}
    )
    unplaced = sum("intermodpath" in ln for ln in lines)
    return {"unreadable": broken[:5], "unplaced_interconnects": unplaced}


def icarus_major(tool: Tool) -> int:
    m = re.match(r"(\d+)", tool.version or "")
    return int(m.group(1)) if tool.found and m else 0


def cell_models(project: Project) -> list[Path]:
    """The Verilog models of the technology's cells, found beside its liberty."""
    lib = liberty_for(project.tech)
    if project.tech != "ihp-sg13g2" or lib is None:
        raise GlsUnavailable(
            f"gate-level simulation needs the cell models of {project.tech}; "
            "ChipLeap finds them for IHP SG13G2"
        )
    found = sorted((lib.resolve().parent.parent / "verilog").glob("*.v"))
    if not found:
        raise GlsUnavailable(f"no cell models beside {lib}")
    return found


def annotated(text: str, top: str, sdf: Path) -> str:
    """The netlist with an SDF annotation in the module ``top``."""
    m = re.search(rf"\bmodule\s+{re.escape(top)}\b", text)
    end = re.compile(r"\bendmodule\b").search(text, m.end()) if m else None
    if m is None or end is None:
        raise ValueError(f"no module {top} in the netlist")
    line = f'initial $sdf_annotate("{q.plain(sdf)}");\n'
    return text[: end.start()] + line + text[end.start() :]


def _netlist(project: Project) -> tuple[Path, Path | None, str]:
    """The netlist to simulate, its SDF (None without delays) and its corner."""
    cfg = project.gate_sim
    if cfg.netlist == "synth":
        try:
            return synthesize(project).netlist, None, ""
        except SynthError as exc:
            raise GlsUnavailable(f"synthesis failed: {exc}") from exc
    run = routed_run(project)
    if run is None:
        raise GlsUnavailable(
            "no routed run in build/pnr: run chipleap verify --steps pnr first, or set "
            "gate_sim.netlist: synth"
        )
    corner = cfg.corner or DEFAULT_CORNER
    sdf = run / "final" / "sdf" / corner / f"{project.top}__{corner}.sdf"
    if not sdf.is_file():
        have = sorted(p.name for p in (run / "final" / "sdf").glob("*") if p.is_dir())
        raise GlsUnavailable(f"no SDF for corner {corner}; the run has {', '.join(have) or 'none'}")
    return run / "final" / "nl" / f"{project.top}.nl.v", sdf, corner


def prepare(project: Project, workdir: Path) -> tuple[Project, tuple[str, ...], dict[str, Any]]:
    """The project as the simulator sees it at gate level (cell models and the netlist
    copy as its sources, no parameters), the simulator options, and what was used."""
    netlist, sdf, corner = _netlist(project)
    models = workdir / "cells"
    models.mkdir(parents=True)
    sources = []
    for m in cell_models(project):  # the PDK's, trusted; copied so the run reads its own
        shutil.copy2(m, models / m.name)
        sources.append(models / m.name)
    text = project.read(netlist).decode(errors="replace")
    args: tuple[str, ...] = ()
    if sdf is not None:
        local_sdf = workdir / sdf.name
        local_sdf.write_text(normalize_sdf(project.read(sdf).decode(errors="replace")))
        text = annotated(text, project.top, local_sdf)
        args = ("-gspecify", "-ginterconnect", f"-T{project.gate_sim.delays}")
    copy = workdir / f"{project.top}.gls.v"
    copy.write_text(text)
    gate = dataclasses.replace(project, sources=(*sources, copy), include_dirs=(), parameters={})
    used = {
        "netlist": project.rel(netlist),
        "sdf": project.rel(sdf) if sdf else None,
        "corner": corner or None,
        "delays": project.gate_sim.delays if sdf else None,
    }
    return gate, args, used


def gls(project: Project, test_filter: str = "") -> StepResult:
    """The gate-level step: the project's tests on its netlist."""
    if project.tests.dir is None:
        return StepResult("gls", "skipped", "the project has no tests (add a tests section)")
    tool = iverilog()
    if icarus_major(tool) < 13:
        found = f"Icarus {tool.version}" if tool.found else "no Icarus"
        return StepResult(
            "gls",
            "skipped",
            f"gate-level simulation of IHP cells needs Icarus 13 or newer, found {found}: "
            f"{INSTALL_HINT}",
        )
    workdir = (project.build_dir / "gls").resolve()
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        gate, args, used = prepare(project, workdir)
    except GlsUnavailable as exc:
        return StepResult("gls", "skipped", str(exc))
    env = {"GATES": "yes", "CHIPLEAP_PARAMETERS": json.dumps(project.parameters, sort_keys=True)}
    try:
        run = run_tests(
            gate,
            "icarus",
            test_filter=test_filter,
            workdir=workdir / "sim",
            extra_args=args,
            env=env,
        )
    except SimUnavailable as exc:
        return StepResult("gls", "skipped", str(exc).splitlines()[0])
    except SimError as exc:
        return StepResult("gls", "fail", str(exc).splitlines()[0], {"error": str(exc), **used})
    status, summary = summarize(run)
    data = {**sim_data(run), **used}
    if used["sdf"]:
        test_log = workdir / "sim" / "test.log"
        msgs = sdf_messages(test_log.read_text(errors="replace") if test_log.is_file() else "")
        data["sdf_messages"] = msgs
        if msgs["unreadable"]:
            first = msgs["unreadable"][0].replace(str(workdir) + "/", "")
            return StepResult(
                "gls",
                "fail",
                f"Icarus could not read the SDF, so delays are missing: {first}",
                data,
            )
    if used["sdf"]:
        what = f"routed netlist with SDF at {used['corner']} ({used['delays']} delays)"
    else:
        what = "synthesized netlist, zero delay"
    return StepResult("gls", status, f"{what}: {summary}", data)
