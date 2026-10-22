"""Write everything for one design point into a directory.

<out>/
  README.md            what is in the bundle and what to run next
  spec.yaml            the requirements the design answers
  design.yaml          the design point (input to `chipleap generate` and `verify`)
  DATASHEET.md         memory map, registers, interfaces, programming sequence
  report.html|md       explorer report with the agent's decisions (after explore)
  explore.json         machine-readable results and the full decision log
  xheep/<name>.py      X-HEEP configuration (make mcu-gen X_HEEP_CFG=...); xheep only
  rtl/                 accelerator SystemVerilog, the X-HEEP adapter or the AXI4 wrapper,
                       FuseSoC core, register description (reggen hjson)
  sw/chipleap_driver.h firmware driver with the tile planner
  tb/                  Verilator testbench (OBI on xheep, AXI4 on standalone)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from chipleap import __version__
from chipleap.explore.engine import Candidate, ExploreResult
from chipleap.gen.datasheet import datasheet, reggen_hjson
from chipleap.gen.rtl import write_rtl
from chipleap.gen.xheep import ext_window, xheep_config
from chipleap.hw.design import Design, DesignError
from chipleap.report.data import explore_dict, explore_markdown
from chipleap.report.html import explore_html
from chipleap.report.text import ip_note
from chipleap.rtl import builddir
from chipleap.spec import Spec


def design_document(design: Design, weights: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {"chipleap": __version__, "design": design.to_dict()}
    if weights:
        doc["weights"] = weights
    return doc


def load_design(path: str | Path) -> tuple[Design, str | None]:
    p = Path(path)
    if p.is_dir():
        p = p / "design.yaml"
    try:
        data = yaml.safe_load(p.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise DesignError(f"{p}: cannot read design: {exc}") from exc
    if not isinstance(data, dict):
        raise DesignError(f"{p}: a design file must be a mapping")
    body = data.get("design", data)
    weights = data.get("weights")
    base = Design().to_dict()
    base.update(body)
    return Design.from_dict(base), (str(weights) if weights else None)


def _readme(c: Candidate | None, design: Design, spec: Spec | None) -> str:
    lines = [f"# {design.name}", "", f"{design.summary()}.", ""]
    if c is not None:
        e = c.evaluation
        u = e.workload.unit
        lines += [
            f"Predicted: {e.throughput:.3g} {u}/s, {e.energy_per_unit_mj:.3g} mJ/{u}, "
            f"{e.power_mw:.3g} mW, {e.total_area:.3g} mm2 (bottleneck: {e.bound}).",
            "",
        ]
        if ip_note(e):
            lines += [ip_note(e), ""]
    lines += [
        "| Path | Contents |",
        "|---|---|",
        "| `design.yaml` | The design point |",
    ]
    if spec:
        lines.append("| `spec.yaml` | The requirements it answers |")
    if c is not None:
        lines += [
            "| `report.html`, `report.md` | Exploration report and decision log |",
            "| `explore.json` | Machine-readable results |",
        ]
    standalone = design.platform == "standalone"
    lines.append(
        "| `DATASHEET.md` | Registers, interfaces, programming, "
        + ("address map |" if standalone else "memory map |")
    )
    if not standalone:
        lines.append(f"| `xheep/{design.name}.py` | X-HEEP configuration |")
    if design.accel and standalone:
        lines += [
            "| `rtl/` | Accelerator RTL, AXI4 wrapper, FuseSoC core, register hjson |",
            "| `sw/chipleap_driver.h` | Driver for any host (define `CHIPLEAP_BASE`) |",
            "| `tb/tb_chipleap_axi.cpp` | Verilator testbench with an AXI4 memory model |",
        ]
    elif design.accel:
        lines += [
            "| `rtl/` | Accelerator RTL, X-HEEP adapter, FuseSoC core, register hjson |",
            "| `sw/chipleap_driver.h` | Firmware driver |",
            "| `tb/tb_chipleap.cpp` | Verilator testbench |",
        ]
    lines += ["", "Next steps:", "", "```sh"]
    if standalone:
        lines += [
            "chipleap verify .            # lint, simulate through AXI4, synthesize",
            "```",
            "",
            "Instantiate `chipleap_accel_axi_top` (rtl/) in your SoC: its AXI4-Lite port on",
            "the host's peripheral interconnect, its AXI4 masters on a port that reaches the",
            "memory holding the weights. docs/platforms.md has the details.",
            "",
        ]
    else:
        lines += [
            "chipleap verify .            # simulate, synthesize, check with X-HEEP's generator",
            "```",
            "",
            "In an X-HEEP checkout:",
            "",
            "```sh",
            f"make mcu-gen X_HEEP_CFG=<path to this bundle>/xheep/{design.name}.py",
            "```",
            "",
        ]
    return "\n".join(lines)


def write_bundle(
    out: Path,
    design: Design,
    *,
    weights: str | None = None,
    spec: Spec | None = None,
    candidate: Candidate | None = None,
    result: ExploreResult | None = None,
) -> list[Path]:
    """Write a design bundle. Returns the files written. ``out`` may not be a symlink, and
    nothing is written through one inside it (``chipleap.rtl.builddir``)."""
    design.validate()  # among other things, the name becomes a file name here
    builddir.output_dir(out)
    written: list[Path] = []

    def put(rel: str, text: str) -> None:
        written.append(builddir.write_text(out, rel, text))

    put("design.yaml", yaml.safe_dump(design_document(design, weights), sort_keys=False))
    if spec is not None:
        put("spec.yaml", yaml.safe_dump(spec.to_dict(), sort_keys=False))
    ext_bytes = 0.0
    metrics = None
    unit = "token"
    workload = ""
    if candidate is not None:
        ev = candidate.evaluation
        ext = ev.workload.storage
        ext_bytes = (1 - ev.placement["weights"]) * ext.weights + (1 - ev.placement["kv"]) * ext.kv
        metrics = ev.metrics()
        unit = ev.workload.unit
        workload = ev.workload.description
    if design.platform != "standalone":
        put(f"xheep/{design.name}.py", xheep_config(design, ext_bytes, spec.name if spec else ""))
    put(
        "DATASHEET.md",
        datasheet(
            design,
            spec_name=spec.name if spec else "",
            ext_bytes=ext_bytes,
            metrics=metrics,
            unit=unit,
            workload=workload,
        ),
    )
    if design.accel is not None:
        written += write_rtl(
            design.accel,
            design.name,
            out,
            platform=design.platform,
            own_clock=bool(design.accel_mhz),
            direct=design.direct_memory,
            window=ext_window(ext_bytes),
        )
        put("rtl/chipleap_accel.hjson", reggen_hjson(design.name))
    if result is not None:
        put("report.html", explore_html(result, result.cloud))
        put("report.md", explore_markdown(result))
        put("explore.json", json.dumps(explore_dict(result), indent=2))
    put("README.md", _readme(candidate, design, spec))
    return written
