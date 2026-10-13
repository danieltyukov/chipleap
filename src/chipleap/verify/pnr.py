"""Placement and routing of the generated accelerator with LibreLane on IHP SG13G2.

LibreLane runs the whole open flow: Yosys, OpenROAD for floorplan, placement, clock tree,
routing and timing repair, KLayout with IHP's rule deck for design-rule checks, Netgen
for layout versus schematic, and OpenSTA for timing at the design's clock. The result is a GDS that
passes the checks a foundry shuttle asks for.

The activation and weight buffers are SRAM macros in silicon. When the build can have
them without a change to its schedule (``chipleap.gen.banks``) and the IHP PDK ChipLeap
uses is installed, the layout has them: IHP's own macros at the design's buffer sizes, in
rows above and below the logic (``chipleap.macro.place``), each with its halo and the
macro power grid (``chipleap.macro.grid``), timed at every corner from their liberty and
checked by LVS as boxes with their pins. Otherwise the layout holds the accelerator's
logic with 16-word flop buffers, and the result says why.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from chipleap.chip import io
from chipleap.gen import banks
from chipleap.gen.rtl import accel_sv, rtl_params
from chipleap.hw.design import Accel
from chipleap.hw.tech import accel_logic_kge, get_tech
from chipleap.macro import grid, place, sram
from chipleap.verify import launch
from chipleap.verify.synth import _sv2v
from chipleap.verify.tools import librelane

PNR_WORDS = 16


class PnrError(RuntimeError):
    pass


@dataclass(frozen=True)
class PnrResult:
    clock_mhz: float
    die_um2: float
    core_um2: float
    cell_um2: float  # standard cells, including buffers and fill-free utilisation
    utilisation: float
    cells: int
    setup_slack_ns: float  # worst across corners; positive meets the clock
    setup_slack_typ_ns: float  # typical corner (1.2 V, 25 C)
    hold_slack_ns: float  # worst across corners
    drc_errors: int
    lvs_errors: int
    antenna_nets: int
    power_mw: float
    gds: Path | None
    metrics: dict[str, Any] = field(default_factory=dict)
    macros: tuple[tuple[str, str], ...] = ()  # (instance, cell) of each SRAM macro placed
    macro_um2: float = 0.0
    buffers: str = ""  # what the buffers are in the layout, and why when they are flops

    @property
    def clean(self) -> bool:
        return self.drc_errors == 0 and self.lvs_errors == 0

    def _fmax(self, slack: float) -> float:
        period = 1000.0 / self.clock_mhz - slack
        return 1000.0 / period if period > 0 else 0.0

    @property
    def fmax_mhz(self) -> float:
        """The clock the routed logic meets at the typical corner: the period less its slack."""
        return self._fmax(self.setup_slack_typ_ns)

    @property
    def fmax_worst_mhz(self) -> float:
        """The same at the worst corner (slow transistors, 1.08 V, 125 C)."""
        return self._fmax(self.setup_slack_ns)


def signoff(place_margin_ns: float = 0.8, route_margin_ns: float = 1.6) -> dict[str, Any]:
    """LibreLane settings every ChipLeap layout shares: the generated accelerator's and an
    RTL project's (``chipleap.rtl.pnr``)."""
    return {
        # Design rules are checked with IHP's own KLayout deck. Magic's IHP rules are a
        # second, single-threaded opinion that takes longer than the rest of the flow.
        "RUN_MAGIC_DRC": False,
        # Let the detailed router fix antenna violations with diodes, as a shuttle requires.
        "DRT_ANTENNA_REPAIR_ITERS": 5,
        # Optimise where the design is signed off: gate sizing, buffering, the clock tree and
        # the repair after global routing all work against the slow corner (1.08 V, 125 C),
        # instead of LibreLane's default of the typical one. Hold is repaired at every corner.
        "DEFAULT_CORNER": "nom_slow_1p08V_125C",
        "RUN_POST_GRT_RESIZER_TIMING": True,
        # Detailed routing took 0.7 to 1.6 ns of slack away from what global routing
        # estimated, and nothing repairs timing after it, so the repairs aim well above zero.
        "PL_RESIZER_SETUP_SLACK_MARGIN": place_margin_ns,
        "GRT_RESIZER_SETUP_SLACK_MARGIN": route_margin_ns,
        # IHP's KLayout rule deck is the slowest step; let it use several cores.
        "KLAYOUT_DRC_THREADS": max(1, min(8, (os.cpu_count() or 2) // 2)),
    }


def config(accel: Accel, clock_mhz: float) -> dict[str, Any]:
    """The LibreLane configuration for the accelerator at ``clock_mhz``."""
    p = rtl_params(accel)
    params = {
        "DATAFLOW": p.dataflow,
        "ROWS": p.rows,
        "COLS": p.cols,
        "WBITS": p.wbits,
        "PORTS": p.ports,
        "ABUF_WORDS": PNR_WORDS,
        "WBUF_WORDS": PNR_WORDS,
        "DOUBLE_BUF": p.double_buf,
        "ACC_ROWS": p.acc_rows,
    }
    return {
        "DESIGN_NAME": "chipleap_accel",
        "VERILOG_FILES": ["dir::src/chipleap_accel.v"],
        "SYNTH_PARAMETERS": [f"{k}={v}" for k, v in params.items()],
        "CLOCK_PORT": "clk_i",
        "CLOCK_PERIOD": round(1000.0 / clock_mhz, 3),
        "FP_CORE_UTIL": 45,
        "PL_TARGET_DENSITY_PCT": 55,
        **signoff(),
    }


def _num(metrics: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = metrics.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return default


def _worst(metrics: dict[str, Any], prefix: str) -> float:
    """The worst value of a per-corner metric, or its overall value."""
    vals = [
        float(v)
        for k, v in metrics.items()
        if (k == prefix or k.startswith(prefix + "__corner:")) and isinstance(v, (int, float))
    ]
    return min(vals) if vals else 0.0


def _corner(metrics: dict[str, Any], prefix: str, corner: str) -> float | None:
    for k, v in metrics.items():
        if k.startswith(prefix + "__corner:") and corner in k and isinstance(v, (int, float)):
            return float(v)
    return None


def parse(metrics: dict[str, Any], clock_mhz: float, gds: Path | None) -> PnrResult:
    drc = sum(
        int(_num(metrics, k))
        for k in ("route__drc_errors", "klayout__drc_error__count", "magic__drc_error__count")
    )
    return PnrResult(
        clock_mhz=clock_mhz,
        die_um2=_num(metrics, "design__die__area"),
        core_um2=_num(metrics, "design__core__area"),
        cell_um2=_num(metrics, "design__instance__area__stdcell", "design__instance__area"),
        utilisation=_num(
            metrics, "design__instance__utilization__stdcell", "design__instance__utilization"
        ),
        cells=int(_num(metrics, "design__instance__count__stdcell", "design__instance__count")),
        setup_slack_ns=_worst(metrics, "timing__setup__ws"),
        setup_slack_typ_ns=_corner(metrics, "timing__setup__ws", "typ")
        or _worst(metrics, "timing__setup__ws"),
        hold_slack_ns=_worst(metrics, "timing__hold__ws"),
        drc_errors=drc,
        lvs_errors=int(_num(metrics, "design__lvs_error__count")),
        antenna_nets=int(_num(metrics, "antenna__violating__nets")),
        power_mw=_num(metrics, "power__total") * 1000,
        gds=gds,
        metrics=metrics,
    )


def run_librelane(
    workdir: Path, cfg: dict[str, Any], tag: str = "chipleap"
) -> tuple[dict[str, Any], Path | None]:
    """Write ``cfg`` to ``workdir/config.json`` and run LibreLane on it in ``workdir``.
    Returns the final metrics and the GDS."""
    tool = librelane()
    if not tool.found or tool.path is None:
        raise PnrError("LibreLane is not installed")
    (workdir / "config.json").write_text(json.dumps(cfg, indent=2))
    cmd = [tool.path, "--pdk", "ihp-sg13g2", "--run-tag", tag, "--overwrite", "config.json"]
    log = workdir / "librelane.log"
    limit = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "10800"))
    try:
        code = launch.run(cmd, workdir, log, limit)
    except launch.Busy as exc:
        raise PnrError(f"LibreLane did not start: {exc}") from None
    except launch.TimedOut as exc:
        raise PnrError(_timed_out(exc, log)) from None
    final = workdir / "runs" / tag / "final"
    metrics_file = final / "metrics.json"
    if not metrics_file.is_file():
        raise PnrError(f"LibreLane did not finish (exit {code}):\n{_tail(log)}")
    gds = next(iter(sorted((final / "gds").glob("*.gds"))), None)
    return json.loads(metrics_file.read_text()), gds


# A design with hard macros has long nets to the macros' pins (a word written to every
# bank, in rows at the top and the bottom of the core), whose slew and capacitance only
# show once they are routed: LibreLane repairs them after global routing too.
MACRO_REPAIR = {"RUN_POST_GRT_DESIGN_REPAIR": True}


def sram_config(
    accel: Accel,
    clock_mhz: float,
    plan: place.Floorplan,
    macros: dict[str, Any],
) -> dict[str, Any]:
    """The LibreLane configuration for ``accel`` with its buffers in SRAM macros: the
    generated top, the floorplan ChipLeap sized, the macros and their power grid."""
    return {
        **MACRO_REPAIR,
        "DESIGN_NAME": banks.TOP,
        "VERILOG_FILES": ["dir::src/chipleap_accel.v", f"dir::src/{banks.TOP}.v"],
        "CLOCK_PORT": "clk_i",
        "CLOCK_PERIOD": round(1000.0 / clock_mhz, 3),
        "FP_SIZING": "absolute",
        "DIE_AREA": [0, 0, *plan.die],
        "CORE_AREA": list(plan.core),
        "PL_TARGET_DENSITY_PCT": 55,
        "MACROS": macros,
        "PDN_CFG": "dir::pdn.tcl",
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
        **signoff(),
    }


def model_cell_um2(accel: Accel) -> float:
    """The model's standard-cell area for the accelerator's logic on SG13G2."""
    ihp = get_tech("ihp-sg13g2")
    return accel_logic_kge(accel.dataflow, accel.rows, accel.cols, accel.ports) * 1000 * ihp.ge_um2


def sram_reason(accel: Accel, pdk: Path | None) -> str | None:
    """Why the layout of ``accel`` keeps flop buffers, or None when it takes macros."""
    reason = banks.unsupported(accel)
    if reason is None and pdk is None:
        reason = f"no IHP PDK with its SRAM macros ({io.PDK_ENV})"
    if reason is None and pdk is not None and not sram.catalog(pdk):
        reason = f"the PDK at {pdk} has no single-port SRAM macros"
    return reason


def place_and_route(
    accel: Accel, clock_mhz: float, workdir: Path, pdk: Path | None = None
) -> PnrResult:
    if not librelane().found:
        raise PnrError("LibreLane is not installed")
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "src"
    src.mkdir(exist_ok=True)
    (src / "chipleap_accel.sv").write_text(accel_sv())
    _sv2v(src / "chipleap_accel.sv", src / "chipleap_accel.v")
    pdk = pdk or io.pdk_dir()
    reason = sram_reason(accel, pdk)
    if reason is not None or pdk is None:
        metrics, gds = run_librelane(workdir, config(accel, clock_mhz))
        result = parse(metrics, clock_mhz, gds)
        return replace(result, buffers=f"{PNR_WORDS}-word flop buffers: {reason}")
    cfg, plan = sram_flow(accel, clock_mhz, workdir, pdk)
    metrics, gds = run_librelane(workdir, cfg)
    from chipleap.rtl.pnr import check_merged

    check_merged(workdir / "runs" / "chipleap", workdir, banks.TOP)
    result = parse(metrics, clock_mhz, gds)
    counts: dict[str, int] = {}
    for _, cell in plan.instances():
        counts[cell] = counts.get(cell, 0) + 1
    return replace(
        result,
        macros=tuple(plan.instances()),
        macro_um2=plan.area_um2,
        buffers="SRAM macros: " + ", ".join(f"{n} x {c}" for c, n in sorted(counts.items())),
    )


def sram_flow(
    accel: Accel, clock_mhz: float, workdir: Path, pdk: Path
) -> tuple[dict[str, Any], banks.Banks]:
    """The files of a macro-backed run in ``workdir`` (the generated top, the macros'
    views, the power grid) and its LibreLane configuration; the RTL is in ``src``."""
    plan = banks.plan(accel, sram.catalog(pdk))
    views = {s.cell: sram.views(pdk, s.cell, workdir) for s in plan.cells()}
    cells = dict(plan.instances())
    fp = place.rows(
        [
            place.Macro(inst, views[cell].view.width, views[cell].view.height)
            for inst, cell in plan.instances()
        ],
        model_cell_um2(accel),
    )
    (workdir / "src" / f"{banks.TOP}.v").write_text(banks.top_sv(plan))
    placements = [
        grid.Placement(p.instance, views[cells[p.instance]].view, p.x, p.y, p.orient)
        for p in fp.macros
    ]
    (workdir / "pdn.tcl").write_text(grid.core_script(placements, fp.core[1]))
    macros, _ = sram.librelane_macros(
        views, [(p.instance, cells[p.instance], p.x, p.y, p.orient) for p in fp.macros], workdir
    )
    return sram_config(accel, clock_mhz, fp, macros), plan


def _timed_out(exc: launch.TimedOut, log: Path) -> str:
    """What a run stopped at the time limit reports; the first line is the step's summary."""
    stopped = "its processes"
    if exc.containers:
        stopped += f" and {_count(len(exc.containers), 'container')}"
    msg = (
        f"LibreLane timed out after {_span(exc.seconds)} (CHIPLEAP_PNR_TIMEOUT); "
        f"{stopped} were stopped"
    )
    if exc.left:
        msg += f"; docker could not stop {_count(len(exc.left), 'container')}: {' '.join(exc.left)}"
    if exc.unchecked:
        msg += "; docker did not answer, so its containers were not checked"
    return f"{msg}\n{_tail(log)}"


def _tail(log: Path) -> str:
    return "\n".join(log.read_text(errors="replace").splitlines()[-25:])


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _span(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.3g} h"
    return f"{seconds / 60:.3g} min" if seconds >= 60 else f"{seconds:.3g} s"
