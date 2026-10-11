"""Placement and routing of the generated accelerator with LibreLane on IHP SG13G2.

LibreLane runs the whole open flow: Yosys, OpenROAD for floorplan, placement, clock tree,
routing and timing repair, KLayout with IHP's rule deck for design-rule checks, Netgen
for layout versus schematic, and OpenSTA for timing at the design's clock. The result is a GDS that
passes the checks a foundry shuttle asks for.

As in synthesis, the activation and weight buffers are SRAM macros in silicon. Placing
them as flip-flop arrays would measure the wrong thing, so the layout holds the
accelerator's logic with 16-word buffers; the macros' area comes from their liberty files.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.gen.rtl import accel_sv, rtl_params
from chipleap.hw.design import Accel
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
        "PL_RESIZER_SETUP_SLACK_MARGIN": 0.8,
        "GRT_RESIZER_SETUP_SLACK_MARGIN": 1.6,
        # IHP's KLayout rule deck is the slowest step; let it use several cores.
        "KLAYOUT_DRC_THREADS": max(1, min(8, (os.cpu_count() or 2) // 2)),
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


def place_and_route(accel: Accel, clock_mhz: float, workdir: Path) -> PnrResult:
    tool = librelane()
    if not tool.found or tool.path is None:
        raise PnrError("LibreLane is not installed")
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "src"
    src.mkdir(exist_ok=True)
    (src / "chipleap_accel.sv").write_text(accel_sv())
    _sv2v(src / "chipleap_accel.sv", src / "chipleap_accel.v")
    (workdir / "config.json").write_text(json.dumps(config(accel, clock_mhz), indent=2))
    tag = "chipleap"
    cmd = [tool.path, "--pdk", "ihp-sg13g2", "--run-tag", tag, "--overwrite", "config.json"]
    log = workdir / "librelane.log"
    with log.open("w") as fh:
        proc = subprocess.Popen(
            cmd, cwd=workdir, stdout=fh, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            proc.wait(timeout=float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "10800")))
        except BaseException:
            # SIGTERM first: a containerised LibreLane forwards it and stops its container,
            # which a SIGKILL to the client would leave running.
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    final = workdir / "runs" / tag / "final"
    metrics_file = final / "metrics.json"
    if not metrics_file.is_file():
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-25:])
        raise PnrError(f"LibreLane did not finish (exit {proc.returncode}):\n{tail}")
    gds = next(iter(sorted((final / "gds").glob("*.gds"))), None)
    return parse(json.loads(metrics_file.read_text()), clock_mhz, gds)
