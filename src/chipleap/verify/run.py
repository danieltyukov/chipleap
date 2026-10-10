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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.gen.rtl import rtl_params
from chipleap.gen.xheep import EXT_LARGE, xheep_config
from chipleap.hw.design import Design
from chipleap.hw.tech import accel_logic_kge, get_tech
from chipleap.verify import sim, synth, xheep
from chipleap.verify.tools import verilator


@dataclass
class StepResult:
    name: str
    status: str  # pass | fail | skipped
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


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
    out = subprocess.run(cmd, capture_output=True, text=True, check=False)
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
    fails = [r for r in results if not r.ok]
    clean = [abs(r.error) for r in results if r.case.stall_pct == 0]
    stalled = [abs(r.error) for r in results if r.case.stall_pct]
    data = {
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
    if fails:
        return StepResult("sim", "fail", f"{len(fails)} of {len(results)} cases wrong", data)
    via = " through the AXI4 wrapper" if design.platform == "standalone" else ""
    msg = (
        f"{len(results)} randomized jobs bit-exact{via}; cycle model error "
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


def verify_bundle(
    bundle: Path,
    *,
    steps: tuple[str, ...] = ("consistency", "lint", "sim", "synth", "xheep"),
    xheep_dir: Path | None = None,
    fetch_xheep: bool = False,
    cases: int = 0,
    seed: int = 0,
) -> list[StepResult]:
    from chipleap.gen.bundle import load_design

    design, _ = load_design(bundle)
    results: list[StepResult] = []
    for step in steps:
        if step == "consistency":
            results.append(consistency(design, bundle))
        elif step == "lint":
            results.append(lint(design, bundle))
        elif step == "sim":
            results.append(simulate(design, cases, seed))
        elif step == "synth":
            results.append(synthesize(design, bundle / "verify" / "synth"))
        elif step == "xheep":
            results.append(check_xheep(bundle, design, xheep_dir, fetch_xheep))
        else:
            raise ValueError(f"unknown verification step {step!r}")
    out = bundle / "verify"
    out.mkdir(parents=True, exist_ok=True)
    (out / "verify.json").write_text(
        json.dumps(
            [{"step": r.name, "status": r.status, "summary": r.summary, **r.data} for r in results],
            indent=2,
        )
    )
    lines = [f"# Verification of {design.name}", ""]
    lines += [f"- **{r.name}**: {r.status}. {r.summary}." for r in results]
    (out / "VERIFY.md").write_text("\n".join(lines) + "\n")
    return results
