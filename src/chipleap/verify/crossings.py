"""The clock domain crossings of an accelerator on a clock of its own.

A design with ``accel_mhz`` runs the accelerator on its own clock behind
``chipleap_accel_cdc`` (``chipleap.gen.rtl``). This step writes that top as an RTL project
(the accelerator and its crossings, with the design's parameters, the bus clock first and
the accelerator's second) and checks it with ChipLeap's CDC and RDC checker
(``chipleap.rtl.cdc``), which must find every crossing synchronized.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from chipleap.gen.rtl import accel_sv, cdc_sv, rtl_params
from chipleap.hw.design import Accel, Design
from chipleap.rtl import builddir
from chipleap.rtl import cdc as checker
from chipleap.rtl.project import ProjectError, load_project
from chipleap.verify.result import StepResult

TOP = "chipleap_accel_cdc"


def write_project(
    accel: Accel, out: Path, bus_mhz: float, accel_mhz: float, cdc_rtl: str | None = None
) -> Path:
    """``out`` (a directory) as an RTL project of ``chipleap_accel_cdc`` for ``accel``: its
    sources, its parameters and the two clocks. ``cdc_rtl`` replaces the crossings' source (tests use
    it to break one)."""
    p = rtl_params(accel)
    params = {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "ABUF_WORDS": p.abuf_words,
        "WBUF_WORDS": p.wbuf_words,
        "DOUBLE_BUF": p.double_buf,
        "ACC_ROWS": p.acc_rows,
    }
    project = {
        "name": "accelerator-crossings",
        "top": TOP,
        "sources": ["rtl/chipleap_accel.sv", "rtl/chipleap_accel_cdc.sv"],
        "parameters": params,
        "clocks": [
            {"port": "clk_i", "period_ns": round(1000.0 / bus_mhz, 3)},
            {"port": "accel_clk_i", "period_ns": round(1000.0 / accel_mhz, 3)},
        ],
        "resets": [{"port": "rst_ni"}],
    }
    builddir.write_text(out, "rtl/chipleap_accel.sv", accel_sv())
    builddir.write_text(
        out, "rtl/chipleap_accel_cdc.sv", cdc_rtl if cdc_rtl is not None else cdc_sv()
    )
    builddir.write_text(out, "project.yaml", yaml.safe_dump(project, sort_keys=False))
    return out


def check(design: Design, workdir: Path) -> StepResult:
    """The bundle's cdc step: skipped when the accelerator shares X-HEEP's clock."""
    if design.accel is None:
        return StepResult("cdc", "skipped", "no accelerator in this design")
    if not design.accel_mhz:
        return StepResult(
            "cdc", "skipped", "the accelerator runs on X-HEEP's clock: nothing crosses"
        )
    root = write_project(design.accel, workdir, design.freq_mhz, design.accel_mhz)
    try:
        project = load_project(root)
    except ProjectError as exc:
        return StepResult("cdc", "fail", f"the crossings' project: {exc}", {"error": str(exc)})
    r = checker.cdc(project)
    return StepResult(r.name, r.status, f"{TOP}: {r.summary}", r.data)
