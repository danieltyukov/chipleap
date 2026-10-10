"""Verification steps on commercial tools: simulation with Xcelium, VCS or Questa and
synthesis with Genus or Design Compiler, here or on a design server.

The steps report the same things as the open-source ones (``chipleap.verify.run``), so a
bundle verified either way reads the same: bit-exact jobs and the cycle model's error for
simulation, logic cell area against the model and the critical path for synthesis.
"""

from __future__ import annotations

import os
from pathlib import Path

from chipleap.hw.design import Design
from chipleap.hw.tech import accel_logic_kge, get_tech
from chipleap.verify import csynth, hdlsim, innovus, sim
from chipleap.verify.remote import Remote
from chipleap.verify.synth import SMALL_WORDS, SynthError
from chipleap.verify.tools import ihp_liberty

SIM_TOOLS = ("verilator", *hdlsim.SIMULATORS)
SYNTH_TOOLS = ("yosys", *csynth.SYNTH_TOOLS)


def simulate_with(
    design: Design,
    tool: str,
    workdir: Path,
    remote: Remote | None = None,
    cases: int = 0,
    seed: int = 0,
) -> tuple[list[sim.CaseResult] | None, str]:
    """Run the default cases on ``tool``. Returns the results, or None and why not."""
    if design.accel is None:
        return None, "no accelerator in this design"
    simulator = hdlsim.get_simulator(tool)
    todo = sim.default_cases(design.accel, seed=seed)
    if cases:
        todo = todo[:cases]
    try:
        return hdlsim.run(design.accel, todo, tool, workdir, remote), simulator.name
    except sim.SimError as exc:
        raise sim.SimError(f"{simulator.name}: {exc}") from exc


def synth_liberty(tech_key: str) -> tuple[str, str]:
    """The liberty file and the host it lives on ("" for this machine) for a technology."""
    t = get_tech(tech_key)
    if t.liberty:
        return t.liberty, t.host
    if tech_key == "ihp-sg13g2":
        lib = ihp_liberty()
        if lib is None:
            raise SynthError(
                "IHP SG13G2 liberty not found; install the PDK or set CHIPLEAP_LIBERTY"
            )
        return str(lib), ""
    raise SynthError(
        f"technology {tech_key!r} names no liberty file; add one with chipleap tech add"
    )


def synthesize_with(
    design: Design,
    tool: str,
    workdir: Path,
    remote: Remote | None = None,
    tech_key: str = "ihp-sg13g2",
) -> dict[str, object]:
    """Synthesize the accelerator with Genus or Design Compiler against ``tech_key``.

    Returns the step's data: measured and modelled logic area, timing, and the Innovus
    script written for the netlist when the technology's LEF files are known.
    """
    if design.accel is None:
        raise SynthError("no accelerator in this design")
    st = csynth.get_synth_tool(tool)
    tech = get_tech(tech_key)
    lib, host = synth_liberty(tech_key)
    if host and (remote is None or remote.host != host):
        raise SynthError(
            f"the {tech_key} liberty is on {host}; run with --remote {host} and its setup"
        )
    clock = (
        min(design.freq_mhz, tech.max_freq_mhz) if design.tech == tech_key else tech.max_freq_mhz
    )
    timeout = float(os.environ.get("CHIPLEAP_SYNTH_TIMEOUT", "7200"))
    lefs = tech.lef or (innovus.ihp_lefs(Path(lib)) if tech_key == "ihp-sg13g2" else ())
    r = csynth.synthesize(
        design.accel, tool, lib, workdir, clock, remote, tech.db, timeout, tuple(lefs)
    )
    a = design.accel
    model = accel_logic_kge(a.dataflow, a.rows, a.cols, a.ports) * 1000 * tech.ge_um2 / 1e6
    err = (model - r.logic_mm2) / r.logic_mm2 if r.logic_mm2 else 0.0
    data: dict[str, object] = {
        "tool": st.name,
        "technology": tech_key,
        "clock_mhz": clock,
        "logic_mm2": round(r.logic_mm2, 5),
        "model_logic_mm2": round(model, 5),
        "model_error": round(err, 4),
        "cells": r.cells,
        "critical_path_ns": round(r.delay_ns, 3),
        "fmax_mhz_estimate": round(r.fmax_mhz, 1),
        "runs": [{"buffer_words": w, "area_um2": x} for w, x in r.runs],
    }
    if lefs:
        # Same naming as the synthesis scripts: local files sit in the work directory.
        names = tuple(csynth.local_name(p) if Path(p).is_file() else p for p in lefs)
        lib_ref = csynth.local_name(lib) if Path(lib).is_file() else lib
        tag = f"w{SMALL_WORDS[-1]}"
        innovus.write(workdir, f"{csynth.TOP}_{tag}.v", f"{csynth.TOP}_{tag}.sdc", lib_ref, names)
        data["innovus_script"] = str(workdir / "innovus.tcl")
    return data
