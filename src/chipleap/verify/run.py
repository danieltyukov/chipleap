"""`chipleap verify`: check a design bundle with open-source tools.

Every step is optional and skipped with a reason when its tool is missing:

* lint:  Verilator --lint-only -Wall on the generated accelerator
* sim:   randomized matrix multiplies against a golden model, cycles against the model
* synth: Yosys on IHP SG13G2, logic area against the model's estimate, fmax estimate
* xheep: X-HEEP's own generator loads, validates and renders the configuration
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chipleap.gen.rtl import rtl_params
from chipleap.gen.xheep import EXT_LARGE, xheep_config
from chipleap.hw.design import Design
from chipleap.hw.tech import accel_bank_kge, accel_logic_kge, get_tech
from chipleap.model.evaluate import buffer_mm2, soc_limit_mhz
from chipleap.rtl import builddir
from chipleap.verify import backends, pnr, sim, synth, xheep
from chipleap.verify.remote import Remote
from chipleap.verify.result import StepResult
from chipleap.verify.tools import clean_env, verilator

if TYPE_CHECKING:
    from chipleap.verify.soc import SocResult


def consistency(design: Design, bundle: Path) -> StepResult:
    from chipleap.verify.consistency import check

    problems = check(bundle, design)
    if problems:
        return StepResult(
            "consist",
            "fail",
            f"{len(problems)} files disagree with design.yaml",
            {"problems": problems},
        )
    agree = (
        "design.yaml, AXI4 wrapper, driver and register map agree"
        if design.platform == "standalone"
        else "design.yaml, RTL wrappers, driver, register map and X-HEEP configuration agree"
    )
    return StepResult("consist", "pass", agree, {})


def lint(design: Design, bundle: Path) -> StepResult:
    tool = verilator()
    if design.accel is None:
        return StepResult("lint", "skipped", "no accelerator in this design")
    if not tool.found:
        return StepResult("lint", "skipped", "Verilator is not installed")
    rtl = bundle / "rtl"
    # The standalone platform's AXI4 wrapper is linted together with the core it wraps.
    axi = design.platform == "standalone"
    files = [rtl / "chipleap_accel.sv"] + ([rtl / "chipleap_accel_axi.sv"] if axi else [])
    cmd = [
        str(tool.path),
        "--lint-only",
        "-Wall",
        "--top-module",
        "chipleap_accel_axi" if axi else "chipleap_accel",
        *rtl_params(design.accel).verilator_args(),
        *(str(f) for f in files),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=False, env=clean_env())
    msgs = [ln for ln in (out.stdout + out.stderr).splitlines() if ln.startswith("%")]
    if out.returncode == 0 and not msgs:
        return StepResult("lint", "pass", "Verilator -Wall reports no warnings", {"warnings": 0})
    return StepResult("lint", "fail", f"{len(msgs)} lint messages", {"messages": msgs[:40]})


def simulate(design: Design, cases: int = 0, seed: int = 0) -> StepResult:
    if design.accel is None:
        return StepResult("sim", "skipped", "no accelerator in this design")
    if not verilator().found:
        return StepResult("sim", "skipped", "Verilator is not installed")
    try:
        todo = sim.default_cases(design.accel, seed=seed)
        if cases:
            todo = todo[:cases]
        results = sim.run(design.accel, todo, platform=design.platform)
    except sim.SimError as exc:
        return StepResult("sim", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    via = " through the AXI4 wrapper" if design.platform == "standalone" else ""
    return sim_step(results, via=via)


def sim_step(results: list[sim.CaseResult], simulator: str = "", via: str = "") -> StepResult:
    """The simulation step's verdict from case results, whichever simulator ran them."""
    fails = [r for r in results if not r.ok]
    clean = [abs(r.error) for r in results if r.case.stall_pct == 0]
    stalled = [abs(r.error) for r in results if r.case.stall_pct]
    data: dict[str, Any] = {
        "cases": len(results),
        "failures": len(fails),
        "cycle_error_no_stall_max": max(clean) if clean else None,
        "cycle_error_with_stalls_mean": statistics.mean(stalled) if stalled else None,
        "results": [
            {
                "m": r.case.m,
                "k": r.case.k,
                "n": r.case.n,
                "fmt": r.case.fmt,
                "stall_pct": r.case.stall_pct,
                "latency": r.case.latency,
                "ok": r.ok,
                "cycles": r.cycles,
                "predicted": round(r.predicted, 1),
                "tile": [r.tm, r.tn, r.tk],
                "x_resident": r.x_resident,
            }
            for r in results
        ],
    }
    who = f"{simulator}: " if simulator else ""
    if simulator:
        data["simulator"] = simulator
    if fails:
        return StepResult("sim", "fail", f"{who}{len(fails)} of {len(results)} cases wrong", data)
    msg = (
        f"{who}{len(results)} randomized jobs bit-exact{via}; cycle model error "
        f"{(max(clean) if clean else 0) * 100:.1f}% max without stalls, "
        f"{(statistics.mean(stalled) if stalled else 0) * 100:.1f}% mean with random stalls"
    )
    return StepResult("sim", "pass", msg, data)


def synthesize(design: Design, workdir: Path | None = None) -> StepResult:
    if design.accel is None:
        return StepResult("synth", "skipped", "no accelerator in this design")
    try:
        r = synth.synthesize(design.accel, workdir=workdir)
    except synth.SynthError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not installed" in msg or "not found" in msg else "fail"
        return StepResult("synth", status, msg, {"error": str(exc)})
    # Compare cell area with cell area: the model's floorplan figure also divides by the
    # placement utilisation, which synthesis does not see.
    ihp = get_tech("ihp-sg13g2")
    a = design.accel
    model = accel_logic_kge(a.dataflow, a.rows, a.cols, a.ports) * 1000 * ihp.ge_um2 / 1e6
    err = (model - r.logic_mm2) / r.logic_mm2 if r.logic_mm2 else 0.0
    data = {
        "liberty": r.liberty,
        "logic_mm2": round(r.logic_mm2, 5),
        "model_logic_mm2": round(model, 5),
        "model_error": round(err, 4),
        "cells": r.cells,
        "flops": r.flops,
        "critical_path_ns": round(r.delay_ns, 3),
        "fmax_mhz_estimate": round(r.fmax_mhz, 1),
        "runs": [{"buffer_words": w, "area_um2": a} for w, a in r.runs],
    }
    msg = (
        f"logic cell area {r.logic_mm2:.4f} mm2 on IHP SG13G2 (model {model:.4f} mm2, "
        f"{err * 100:+.0f}%), critical path {r.delay_ns:.2f} ns, about {r.fmax_mhz:.0f} MHz"
    )
    return StepResult("synth", "pass", msg, data)


def place_route(design: Design, workdir: Path) -> StepResult:
    """LibreLane on IHP SG13G2 at the design's clock (capped at what the node allows)."""
    if design.accel is None:
        return StepResult("pnr", "skipped", "no accelerator in this design")
    ihp = get_tech("ihp-sg13g2")
    clock = min(design.freq_mhz, ihp.max_freq_mhz) if design.tech == ihp.key else ihp.max_freq_mhz
    try:
        r = pnr.place_and_route(design.accel, clock, workdir)
    except pnr.PnrError as exc:
        msg = str(exc).splitlines()[0]
        return StepResult(
            "pnr", "skipped" if "not installed" in msg else "fail", msg, {"error": str(exc)}
        )
    a = design.accel
    kge = accel_logic_kge(a.dataflow, a.rows, a.cols, a.ports)
    if r.macros:
        # The layout's buffers are macros, and its logic has their interface.
        kge += accel_bank_kge(a.rows, a.cols, a.double_buffer)
    model_cell = kge * 1000 * ihp.ge_um2
    model_buffers = buffer_mm2(a, ihp) * 1e6
    data = {
        "clock_mhz": clock,
        "die_um2": round(r.die_um2),
        "core_um2": round(r.core_um2),
        "cell_um2": round(r.cell_um2),
        "utilisation": round(r.utilisation, 3),
        "cells": r.cells,
        "setup_slack_typ_ns": round(r.setup_slack_typ_ns, 3),
        "setup_slack_ns": round(r.setup_slack_ns, 3),
        "hold_slack_ns": round(r.hold_slack_ns, 3),
        "fmax_mhz": round(r.fmax_mhz, 1),
        "fmax_worst_mhz": round(r.fmax_worst_mhz, 1),
        "drc_errors": r.drc_errors,
        "lvs_errors": r.lvs_errors,
        "antenna_nets": r.antenna_nets,
        **r.ihp_antenna_data(),
        "max_slew_violations": r.max_slew_violations,
        "max_cap_violations": r.max_cap_violations,
        "max_fanout_violations": r.max_fanout_violations,
        **({"pin_markers_in_pdk_macros": r.pin_markers} if r.pin_markers else {}),
        "power_mw": round(r.power_mw, 3),
        "model_cell_um2": round(model_cell),
        "buffers": r.buffers,
        "buffer_words": rtl_params(a).abuf_words if r.macros else pnr.PNR_WORDS,
        "gds": str(r.gds) if r.gds else None,
    }
    # The block as it is in silicon, standard cells and macros, against the model's logic
    # and buffers.
    measured = r.cell_um2 + r.macro_um2
    modelled = model_cell + model_buffers
    if r.macros:
        data |= {
            "macros": [{"instance": i, "cell": c} for i, c in r.macros],
            "macro_um2": round(r.macro_um2),
            "model_buffer_um2": round(model_buffers),
            "measured_um2": round(measured),
            "model_um2": round(modelled),
            "model_area_error": round((modelled - measured) / measured, 4),
        }
    # The model checked part by part: its macros against the layout's, its logic against
    # LibreLane's synthesis, and what placement and timing repair add after synthesis,
    # which the model leaves out.
    if r.synth_cell_um2 > 0:
        data |= {
            "synth_cell_um2": round(r.synth_cell_um2),
            "model_cell_error": round((model_cell - r.synth_cell_um2) / r.synth_cell_um2, 4),
            "pnr_cell_growth": round(r.cell_um2 / r.synth_cell_um2 - 1, 4),
        }
    # Signed off where the model's clock limit for the node is measured: setup at the slow
    # corner (1.08 V, 125 C), hold at every corner. The typical corner is reported beside it.
    # Antenna violations fail the step, as they do a project's and a chip's, and so do slew
    # and capacitance violations at any corner, as they do a project's; fanout is reported.
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    clean = r.checks_clean
    checks = r.checks()
    hold = f"; hold missed by {-r.hold_slack_ns:.3f} ns" if r.hold_slack_ns < 0 else ""
    msg = (
        f"placed and routed on IHP SG13G2: {r.core_um2 / 1e6:.3f} mm2 core at "
        f"{r.utilisation * 100:.0f}% utilisation, {r.cells} cells; {clock:g} MHz "
        f"{'met' if r.setup_slack_ns >= 0 else 'missed'} at the slow corner (about "
        f"{r.fmax_worst_mhz:.0f} MHz, and about {r.fmax_mhz:.0f} MHz at the typical one){hold}; "
        f"{checks}"
    )
    if r.macros:
        msg += (
            f"; buffers in {r.buffers.removeprefix('SRAM macros: ')}: macros "
            f"{r.macro_um2 / 1e6:.3f} mm2, the model {model_buffers / 1e6:.3f} mm2"
        )
        if r.synth_cell_um2 > 0:
            msg += (
                f"; standard cells: the model {model_cell / 1e6:.3f} mm2, synthesis "
                f"{r.synth_cell_um2 / 1e6:.3f} mm2 "
                f"({(model_cell - r.synth_cell_um2) / r.synth_cell_um2 * 100:+.1f}%), and "
                f"place and route adds {(r.cell_um2 / r.synth_cell_um2 - 1) * 100:.0f}% "
                f"({r.cell_um2 / 1e6:.3f} mm2)"
            )
    elif r.buffers:
        msg += f"; {r.buffers}"
    return StepResult("pnr", "pass" if met and clean else "fail", msg, data)


def corner_slacks(metrics: dict[str, Any]) -> dict[str, dict[str, float]]:
    """The worst setup and hold slack at each corner LibreLane signed off at."""
    out: dict[str, dict[str, float]] = {}
    for key, value in metrics.items():
        for kind in ("setup", "hold"):
            prefix = f"timing__{kind}__ws__corner:"
            if key.startswith(prefix) and isinstance(value, (int, float)):
                out.setdefault(key[len(prefix) :], {})[kind] = round(float(value), 3)
    return dict(sorted(out.items()))


def place_route_soc(
    design: Design,
    bundle: Path,
    checkout: Path | None,
    fetch: bool,
    clock_mhz: float | None = None,
) -> StepResult:
    """The whole SoC, X-HEEP with the accelerator and every memory in IHP's macros, placed
    and routed as one block in LibreLane (``chipleap.verify.soc``), at the design's clock
    or ``clock_mhz``."""
    from chipleap.chip import io
    from chipleap.verify import soc

    if design.platform != "xheep":
        return StepResult("soc", "skipped", "not an X-HEEP design (standalone AXI4 IP)")
    if design.accel is None:
        return StepResult("soc", "skipped", "no accelerator in this design")
    if design.tech != "ihp-sg13g2":
        return StepResult("soc", "skipped", f"the SoC is laid out on IHP SG13G2, not {design.tech}")
    co = xheep.find_checkout(checkout)
    if co is None and fetch:
        try:
            co = xheep.fetch()
        except xheep.XheepError as exc:
            return StepResult("soc", "skipped", str(exc))
    if co is None:
        return StepResult(
            "soc",
            "skipped",
            "no X-HEEP checkout (pass --xheep DIR, set CHIPLEAP_XHEEP_DIR, or use --fetch-xheep)",
        )
    pdk = io.pdk_dir()
    if pdk is None:
        return StepResult("soc", "skipped", f"no IHP PDK with its SRAM macros ({io.PDK_ENV})")
    # As in the xheep step, X-HEEP's generator runs the configuration ChipLeap writes from
    # design.yaml, never the bundle's own file.
    text, _ = _regenerated_xheep(bundle, design)
    start = time.monotonic()
    try:
        s = soc.place_and_route(
            design, text, co, builddir.work_dir(bundle, "verify", "soc"), pdk, clock_mhz
        )
    except (soc.SocError, pnr.PnrError) as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not installed" in msg else "fail"
        return StepResult("soc", status, msg, {"error": str(exc)})
    return soc_step_result(design, s, time.monotonic() - start)


def soc_step_result(design: Design, s: SocResult, runtime_s: float) -> StepResult:
    """The soc step's result from what the routed SoC measures."""
    r = s.pnr
    clock = r.clock_mhz
    counts: dict[str, int] = {}
    for _, cell in s.macros:
        counts[cell] = counts.get(cell, 0) + 1
    macros = ", ".join(f"{n} x {c}" for c, n in sorted(counts.items()))
    model = s.model_cell_um2 + s.model_macro_um2
    measured = r.cell_um2 + r.macro_um2
    limit = soc_limit_mhz(design, get_tech(design.tech))
    data: dict[str, object] = {
        "clock_mhz": clock,
        "design_clock_mhz": design.freq_mhz,
        "model_soc_limit_mhz": round(limit, 1) if limit else None,
        "runtime_s": round(runtime_s),
        "die_um2": round(r.die_um2),
        "core_um2": round(r.core_um2),
        "cell_um2": round(r.cell_um2),
        "utilisation": round(r.utilisation, 3),
        "cells": r.cells,
        "setup_slack_typ_ns": round(r.setup_slack_typ_ns, 3),
        "setup_slack_ns": round(r.setup_slack_ns, 3),
        "hold_slack_ns": round(r.hold_slack_ns, 3),
        "slack_by_corner_ns": corner_slacks(r.metrics),
        "fmax_mhz": round(r.fmax_mhz, 1),
        "fmax_worst_mhz": round(r.fmax_worst_mhz, 1),
        "drc_errors": r.drc_errors,
        "lvs_errors": r.lvs_errors,
        "antenna_nets": r.antenna_nets,
        **r.ihp_antenna_data(),
        "max_slew_violations": r.max_slew_violations,
        "max_cap_violations": r.max_cap_violations,
        "max_fanout_violations": r.max_fanout_violations,
        **({"pin_markers_in_pdk_macros": r.pin_markers} if r.pin_markers else {}),
        "power_mw": round(r.power_mw, 3),
        "macros": [{"instance": i, "cell": c} for i, c in s.macros],
        "macro_um2": round(r.macro_um2),
        "model_macro_um2": round(s.model_macro_um2),
        "model_cell_um2": round(s.model_cell_um2),
        "synth_cell_um2": round(r.synth_cell_um2),
        "measured_um2": round(measured),
        "model_um2": round(model),
        "gds": str(r.gds) if r.gds else None,
    }
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    checks = r.checks()
    msg = (
        f"X-HEEP with the accelerator, placed and routed on IHP SG13G2: "
        f"{r.core_um2 / 1e6:.3f} mm2 core at {r.utilisation * 100:.0f}% utilisation, "
        f"{r.cells} cells; {clock:g} MHz"
        f"{'' if clock == design.freq_mhz else f' (the design asks {design.freq_mhz:g})'} "
        f"{'met' if met else 'missed'} at the slow "
        f"corner (about {r.fmax_worst_mhz:.0f} MHz, and about {r.fmax_mhz:.0f} MHz at the "
        f"typical one{f'; the model puts the SoC at about {limit:.0f} MHz' if limit else ''}); "
        f"{checks}; macros {macros}: {r.macro_um2 / 1e6:.3f} mm2, the model "
        f"{s.model_macro_um2 / 1e6:.3f} mm2; standard cells: the model "
        f"{s.model_cell_um2 / 1e6:.3f} mm2"
    )
    if r.synth_cell_um2 > 0:
        msg += (
            f", synthesis {r.synth_cell_um2 / 1e6:.3f} mm2 "
            f"({(s.model_cell_um2 - r.synth_cell_um2) / r.synth_cell_um2 * 100:+.1f}%), and "
            f"place and route adds {(r.cell_um2 / r.synth_cell_um2 - 1) * 100:.0f}% "
            f"({r.cell_um2 / 1e6:.3f} mm2)"
        )
    return StepResult("soc", "pass" if met and r.checks_clean else "fail", msg, data)


def _count(n: object, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def check_xheep(bundle: Path, design: Design, checkout: Path | None, fetch: bool) -> StepResult:
    if design.platform != "xheep":
        return StepResult("xheep", "skipped", "not an X-HEEP design (standalone AXI4 IP)")
    co = xheep.find_checkout(checkout)
    if co is None and fetch:
        try:
            co = xheep.fetch()
        except xheep.XheepError as exc:
            return StepResult("xheep", "skipped", str(exc))
    if co is None:
        return StepResult(
            "xheep",
            "skipped",
            "no X-HEEP checkout (pass --xheep DIR, set CHIPLEAP_XHEEP_DIR, or use --fetch-xheep)",
        )
    # An X-HEEP configuration is Python, and X-HEEP's generator executes it. So this step
    # checks the configuration ChipLeap generates from design.yaml, never the bundle's own
    # file, which could have come from anywhere. The consistency step reports drift.
    text, same = _regenerated_xheep(bundle, design)
    with tempfile.TemporaryDirectory(prefix="chipleap-xheep-cfg-") as tmp:
        cfg = Path(tmp) / f"{design.name}.py"
        cfg.write_text(text)
        v = xheep.validate(cfg, co)
        if not v.ok:
            errors = [ln for ln in v.log.splitlines() if "Error" in ln.split(":")[0]]
            reason = errors[-1].split("ERROR:")[-1].split(":", 1)[-1].strip() if errors else ""
            msg = "X-HEEP rejected the configuration" + (f": {reason}" if reason else "")
            return StepResult("xheep", "fail", msg, {"log": v.log})
        r = xheep.render(cfg, co)
    data = {
        "checkout": str(co),
        "validate": v.summary,
        "render": r.summary,
        "bundle_copy_matches": same,
    }
    if not r.ok:
        return StepResult(
            "xheep", "fail", "X-HEEP could not render its templates", {**data, "log": r.log}
        )
    note = "" if same else "; the bundle's own copy differs from design.yaml"
    return StepResult(
        "xheep",
        "pass",
        f"X-HEEP validated the configuration ({_count(v.summary.get('banks'), 'bank')}, "
        f"{v.summary.get('banks_il')} interleaved) and rendered {r.summary['rendered']} "
        f"templates{note}",
        data,
    )


def _regenerated_xheep(bundle: Path, design: Design) -> tuple[str, bool]:
    """The X-HEEP configuration for design.yaml, and whether the bundle's copy matches it
    (comments aside). Only the external window is read from the bundle's copy, as text."""
    own = bundle / "xheep" / f"{design.name}.py"
    old = own.read_text() if own.is_file() else ""
    m = re.search(r'"ext_slaves", start_address=(0x[0-9a-fA-F]+)', old)
    large = m is not None and int(m.group(1), 16) == EXT_LARGE[0]
    text = xheep_config(design, float(EXT_LARGE[1]) if large else 0.0)

    def code(t: str) -> list[str]:
        return [ln for ln in t.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    return text, code(text) == code(old)


def _simulate_on(
    design: Design, tool: str, bundle: Path, remote: Remote | None, cases: int, seed: int
) -> StepResult:
    """Simulation on a commercial simulator (``chipleap.verify.backends``)."""
    try:
        results, name = backends.simulate_with(
            design, tool, builddir.work_dir(bundle, "verify", tool), remote, cases, seed
        )
    except sim.SimError as exc:
        return StepResult("sim", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    if results is None:
        return StepResult("sim", "skipped", name)
    return sim_step(results, name)


def _synthesize_on(
    design: Design, tool: str, tech: str, bundle: Path, remote: Remote | None
) -> StepResult:
    """Synthesis with Genus or Design Compiler (``chipleap.verify.backends``)."""
    try:
        data = backends.synthesize_with(
            design, tool, builddir.work_dir(bundle, "verify", tool), remote, tech
        )
    except synth.SynthError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not on the path" in msg or "no accelerator" in msg else "fail"
        return StepResult("synth", status, msg, {"error": str(exc)})
    msg = (
        f"{data['tool']}: logic cell area {data['logic_mm2']:.4f} mm2 on {tech} (model "
        f"{data['model_logic_mm2']:.4f} mm2, {float(str(data['model_error'])) * 100:+.0f}%), "
        f"critical path {data['critical_path_ns']:.2f} ns at a {data['clock_mhz']:g} MHz "
        "target"
    )
    return StepResult("synth", "pass", msg, data)


def _place_route_on(design: Design, tech: str, bundle: Path, remote: Remote | None) -> StepResult:
    """Place and route with Genus and Innovus (``chipleap.verify.cpnr``)."""
    try:
        r, clock = backends.place_route_with(
            design, builddir.work_dir(bundle, "verify", "innovus"), remote, tech
        )
    except pnr.PnrError as exc:
        msg = str(exc).splitlines()[0]
        status = "skipped" if "not on the path" in msg or "no accelerator" in msg else "fail"
        return StepResult("pnr", status, f"Innovus: {msg}", {"error": str(exc)})
    data = {
        "tool": "Innovus",
        "technology": tech,
        "clock_mhz": clock,
        "die_um2": round(r.die_um2),
        "core_um2": round(r.core_um2),
        "cell_um2": round(r.cell_um2),
        "utilisation": round(r.utilisation, 3),
        "cells": r.cells,
        "setup_slack_typ_ns": round(r.setup_slack_typ_ns, 3),
        "setup_slack_ns": round(r.setup_slack_ns, 3),
        "hold_slack_ns": round(r.hold_slack_ns, 3),
        "fmax_mhz": round(r.fmax_mhz, 1),
        "fmax_worst_mhz": round(r.fmax_worst_mhz, 1),
        "drc_errors": r.drc_errors,
        "connectivity_errors": r.lvs_errors,
        "antenna_nets": r.antenna_nets,
        "buffer_words": pnr.PNR_WORDS,
    }
    # Setup is signed off at the slowest corner given (the slow liberty, when there is one),
    # as in the LibreLane step; hold at every corner.
    met = r.setup_slack_ns >= 0 and r.hold_slack_ns >= 0
    clean = r.clean and r.antenna_nets == 0
    checks = (
        "DRC, connectivity and antenna clean"
        if clean
        else f"{r.drc_errors} DRC, {r.lvs_errors} connectivity and {r.antenna_nets} antenna violations"
    )
    corner = (
        f"at the slow corner (about {r.fmax_worst_mhz:.0f} MHz, and about {r.fmax_mhz:.0f} MHz "
        "at the typical one)"
        if r.setup_slack_ns != r.setup_slack_typ_ns
        else f"at the typical corner (about {r.fmax_mhz:.0f} MHz)"
    )
    msg = (
        f"Innovus: placed and routed on {tech}: {r.core_um2 / 1e6:.3f} mm2 core at "
        f"{r.utilisation * 100:.0f}% utilisation, {r.cells} cells; {clock:g} MHz "
        f"{'met' if met else 'missed'} {corner}; {checks}"
    )
    return StepResult("pnr", "pass" if met and clean else "fail", msg, data)


def verify_bundle(
    bundle: Path,
    *,
    steps: tuple[str, ...] = ("consistency", "lint", "sim", "synth", "xheep"),
    xheep_dir: Path | None = None,
    fetch_xheep: bool = False,
    cases: int = 0,
    seed: int = 0,
    sim_tool: str = "verilator",
    synth_tool: str = "yosys",
    synth_tech: str = "ihp-sg13g2",
    remote: Remote | None = None,
    pnr_tool: str = "librelane",
    soc_clock_mhz: float | None = None,
) -> list[StepResult]:
    from chipleap.gen.bundle import load_design

    design, _ = load_design(bundle)
    # The steps write into the bundle's verify/, so the bundle may not be a symlink, and
    # verify/ and each step's directory in it are reached without following one.
    builddir.output_dir(bundle)
    results: list[StepResult] = []
    for step in steps:
        if step == "consistency":
            results.append(consistency(design, bundle))
        elif step == "lint":
            results.append(lint(design, bundle))
        elif step == "sim" and sim_tool != "verilator":
            results.append(_simulate_on(design, sim_tool, bundle, remote, cases, seed))
        elif step == "sim":
            results.append(simulate(design, cases, seed))
        elif step == "synth" and synth_tool != "yosys":
            results.append(_synthesize_on(design, synth_tool, synth_tech, bundle, remote))
        elif step == "synth":
            results.append(synthesize(design, builddir.work_dir(bundle, "verify", "synth")))
        elif step == "xheep":
            results.append(check_xheep(bundle, design, xheep_dir, fetch_xheep))
        elif step == "pnr" and pnr_tool == "innovus":
            results.append(_place_route_on(design, synth_tech, bundle, remote))
        elif step == "pnr":
            results.append(place_route(design, builddir.work_dir(bundle, "verify", "pnr")))
        elif step == "soc":
            results.append(place_route_soc(design, bundle, xheep_dir, fetch_xheep, soc_clock_mhz))
        else:
            raise ValueError(f"unknown verification step {step!r}")
    out = builddir.work_dir(bundle, "verify")
    builddir.write_text(
        out,
        "verify.json",
        json.dumps(
            [{"step": r.name, "status": r.status, "summary": r.summary, **r.data} for r in results],
            indent=2,
        ),
    )
    lines = [f"# Verification of {design.name}", ""]
    lines += [f"- **{r.name}**: {r.status}. {r.summary}." for r in results]
    builddir.write_text(out, "VERIFY.md", "\n".join(lines) + "\n")
    return results
