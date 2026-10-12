"""The chip's layout in LibreLane: one Classic flow over the chip top, the ring's cells
placed where the floorplan puts them.

LibreLane has no pad ring flow, but its Classic flow places any instance from
``MACRO_PLACEMENT_CFG``, takes a die and a core area, a PDN script, routing
obstructions and a pin template, and lets a configuration remove or add steps. That is
enough: the ring's cells are placed instances, the PDN script joins the core to the
ring (``chipleap.chip.pdn``), the obstructions keep signal routes off the ring (Metal1
to Metal5, so the router cannot reach a pin through the ring's rails), and the pins sit
outside the pads' bond sides.

Steps changed: the IO placement is removed (the pins come from the template), Magic's
extraction and overlap check are removed (Magic cannot read the IO cells' abstracts as
LibreLane gives them, and its stream-out needs a PR boundary aispice's GDS has not),
OpenROAD writes a CDL netlist after the last timing step (for LVS), and LibreLane's own
DRC and LVS are off: IHP's decks check the finished chip instead
(``chipleap.chip.checks``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from chipleap.chip.config import ChipConfig
from chipleap.chip.floorplan import BOND_UM, HALO_UM, Floorplan
from chipleap.chip.io import IO, IoCell, chip_lef, lef_path, liberty
from chipleap.chip.macros import Imported
from chipleap.chip.netlist import Binding
from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.verify.pnr import PnrError, run_librelane, signoff

TAG = "chipleap"
IO_VOLTAGES = {"vdd": 1.2, "vss": 0.0, "iovdd": 3.3, "iovss": 0.0}
OBSTRUCTED = ("Metal1", "Metal2", "Metal3", "Metal4", "Metal5")


def write_io(flow: Path, pdk: Path, cells: Mapping[str, IoCell], used: list[str]) -> None:
    """The IO library's views in ``flow/io``: the GDS and CDL as the PDK has them, the LEF
    without the analog pad's bond-side port, and a liberty of the cells the chip uses."""
    io = builddir.work_dir(flow, "io", empty=True)
    ref = pdk / "libs.ref" / IO
    builddir.write_bytes(io, f"{IO}.gds", (ref / "gds" / f"{IO}.gds").read_bytes())
    builddir.write_bytes(io, f"{IO}.cdl", (ref / "cdl" / f"{IO}.cdl").read_bytes())
    builddir.write_text(io, f"{IO}_chip.lef", chip_lef(lef_path(pdk).read_text()))
    builddir.write_text(
        io, "io_cells.lib", liberty("io_cells", [cells[c] for c in used], IO_VOLTAGES)
    )


def obstructions(plan: Floorplan) -> list[list[Any]]:
    """Routing obstructions over the IO ring on Metal1 to Metal5: from 3 um inside its
    outer edge (the routes to the pins outside the bond sides pass there) to 1 um short
    of its inner edge (the core-side pins)."""
    w, h = plan.die
    b, c = float(BOND_UM), plan.pad_depth
    boxes = (
        (b, b + 3, w - b, b + c - 1),
        (b, h - b - c + 1, w - b, h - b - 3),
        (b + 3, b, b + c - 1, h - b),
        (w - b - c + 1, b, w - b - 3, h - b),
    )
    return [[layer, *box] for layer in OBSTRUCTED for box in boxes]


def config(
    chip: ChipConfig,
    plan: Floorplan,
    binding: Binding,
    imported: Imported,
    verilog: Mapping[str, Any],
    clock_period: float,
) -> dict[str, Any]:
    """The LibreLane configuration; the files it names are in the flow directory."""
    macros: dict[str, Any] = {}
    for m in plan.macros:
        view = imported.views[m.cell]
        rel = Path("macros") / _first_instance(imported, m.cell) / "views"
        entry = macros.setdefault(
            m.cell,
            {
                "gds": [f"dir::{rel / view.gds.name}"],
                "lef": [f"dir::{rel / view.lef.name}"],
                "lib": {"*": [f"dir::{rel / view.lib.name}"]},
                "spice": [f"dir::{rel / view.spice.name}"],
                "instances": {},
            },
        )
        entry["instances"][m.inst] = {"location": [m.x, m.y], "orientation": m.orient}
    first_clock = next(iter(binding.clocks), None)
    cfg: dict[str, Any] = {
        "meta": {
            "version": 2,
            "flow": "Classic",
            "substituting_steps": {
                "OpenROAD.IOPlacement": None,
                "Magic.SpiceExtraction": None,
                "Checker.IllegalOverlap": None,
                "+OpenROAD.STAPostPNR": "OpenROAD.WriteCDL",
            },
        },
        "DESIGN_NAME": chip.name,
        **verilog,
        "VERILOG_FILES": [f"dir::src/{chip.name}.v", *verilog.get("VERILOG_FILES", [])],
        "CLOCK_PORT": None,
        "CLOCK_PERIOD": clock_period,
        "PNR_SDC_FILE": "dir::chip.sdc",
        "SIGNOFF_SDC_FILE": "dir::chip.sdc",
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, *plan.die],
        "CORE_AREA": list(plan.core),
        "PL_TARGET_DENSITY_PCT": 50,
        "MACRO_PLACEMENT_CFG": "dir::macro_placement.cfg",
        "FP_DEF_TEMPLATE": "dir::pins.def",
        "EXTRA_LEFS": [f"dir::io/{IO}_chip.lef"],
        "EXTRA_GDS": [f"dir::io/{IO}.gds"],
        "EXTRA_LIBS": ["dir::io/io_cells.lib"],
        # OpenROAD's CDL writer needs a subcircuit for every master, the macros' too.
        "EXTRA_CDLS": [
            f"dir::io/{IO}.cdl",
            *(
                f"dir::macros/{_first_instance(imported, c)}/views/{v.spice.name}"
                for c, v in sorted(imported.views.items())
            ),
        ],
        "VDD_NETS": ["VDD"],
        "GND_NETS": ["VSS"],
        "PDN_CFG": "dir::pdn.tcl",
        "FP_MACRO_HORIZONTAL_HALO": HALO_UM,
        "FP_MACRO_VERTICAL_HALO": HALO_UM,
        "RUN_MAGIC_STREAMOUT": False,
        "PRIMARY_GDSII_STREAMOUT_TOOL": "klayout",
        "RUN_LINTER": False,
        "RUN_KLAYOUT_DRC": False,
        "RUN_LVS": False,
        # Signals route up to Metal5; TopMetal1 and TopMetal2 carry power. The pins at the
        # ring's edge crowd global routing; detailed routing resolves it, and its DRC count
        # must be zero.
        "RT_MAX_LAYER": "Metal5",
        "GRT_ALLOW_CONGESTION": True,
        "ROUTING_OBSTRUCTIONS": obstructions(plan),
    }
    if first_clock is not None:
        cfg["CLOCK_NET"] = binding.core_nets[first_clock]
    if macros:
        cfg["MACROS"] = macros
    for k, v in signoff().items():
        if k != "KLAYOUT_DRC_THREADS":
            cfg.setdefault(k, v)
    # LibreLane hands these values to Tcl; refuse any a Tcl word could not hold as text.
    for value in _strings(cfg):
        try:
            q.plain(value)
        except q.QuoteError as exc:
            raise PnrError(f"LibreLane configuration: {exc}") from exc
    return cfg


def _first_instance(imported: Imported, cell: str) -> str:
    return next(i for i, c in imported.cells.items() if c == cell)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def run(flow: Path, cfg: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    """Run LibreLane in ``flow``; the metrics and the run directory."""
    run_dir = flow / "runs" / TAG
    try:
        metrics, gds = run_librelane(flow, cfg, TAG)
    except PnrError as exc:
        # LibreLane's own summary of what stopped it is in the run's error log.
        log = run_dir / "error.log"
        first = ""
        if log.is_file() and log.stat().st_size < (1 << 20):
            first = next(
                (ln.strip() for ln in log.read_text(errors="replace").splitlines() if ln.strip()),
                "",
            )
        if first:
            raise PnrError(f"LibreLane stopped: {first[:300]}\n{exc}") from None
        raise
    if gds is None:
        raise PnrError("LibreLane finished without a GDS")
    return metrics, run_dir


def outputs(run_dir: Path, name: str) -> dict[str, Path]:
    """The run's files the checks read: the GDS, the DEF, OpenROAD's CDL netlist and
    LibreLane's resolved configuration."""
    final = run_dir / "final"
    cdl = sorted(run_dir.glob("*-openroad-writecdl/*.cdl"))
    found = {
        "gds": final / "gds" / f"{name}.gds",
        "def": final / "def" / f"{name}.def",
        "resolved": run_dir / "resolved.json",
    }
    if cdl:
        found["cdl"] = cdl[-1]
    missing = [k for k, p in found.items() if not p.is_file()] + ([] if cdl else ["cdl"])
    if missing:
        raise PnrError(f"LibreLane's run has no {', '.join(missing)}")
    if found["gds"].resolve().parent != (final / "gds").resolve():
        raise PnrError("LibreLane's GDS is not in its run directory")
    return found


def std_cell_cdl(resolved: Path) -> Path:
    """The standard cells' CDL that LibreLane used, from its resolved configuration: a
    file of the PDK LibreLane installed, never one in the project."""
    data = json.loads(resolved.read_text())
    value = data.get("CELL_CDLS")
    paths = value if isinstance(value, list) else [value]
    for p in paths:
        if isinstance(p, str) and p.endswith("/libs.ref/sg13g2_stdcell/cdl/sg13g2_stdcell.cdl"):
            path = Path(p)
            if path.is_file():
                return path
    raise PnrError("LibreLane's resolved configuration names no standard-cell CDL")
