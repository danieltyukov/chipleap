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
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from chipleap.chip import io
from chipleap.gen import banks
from chipleap.gen.rtl import accel_sv, rtl_params
from chipleap.hw.design import Accel
from chipleap.hw.tech import accel_logic_kge, get_tech
from chipleap.macro import grid, layer, place, sram
from chipleap.rtl import builddir
from chipleap.verify import launch, librelane_pdk, minarea
from chipleap.verify.drc import DrcReportError, DrcResult, pdk_cells, read_lyrdb
from chipleap.verify.synth import _sv2v
from chipleap.verify.tools import (
    librelane,
    librelane_env,
    librelane_ran,
    librelane_version_key,
)

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
    synth_cell_um2: float = 0.0  # LibreLane's synthesis, before placement's repairs
    # Off-grid pin markers inside the PDK's own macros, left out of drc_errors
    # (``chipleap.verify.drc``): rule, cell, the macros that hold it, count.
    pin_markers: list[dict[str, Any]] = field(default_factory=list)

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
        # ChipLeap judges KLayout's DRC from the run's report, with off-grid pin markers
        # inside the PDK's own macros set apart (``klayout_drc``). LibreLane counts those
        # markers as errors, and its verdict would end the run on exit 2, which ChipLeap
        # reads no result from.
        "ERROR_ON_KLAYOUT_DRC": False,
        # ChipLeap judges setup and hold at every corner from the run's metrics, and reports
        # a run that misses them with its slack. LibreLane's checks would end such a run on
        # exit 2: IHP's configuration checks setup at every corner from 22f2a25 on (it was
        # the typical corner only), and LibreLane checks hold at every corner. [""] matches
        # no corner.
        "SETUP_VIOLATION_CORNERS": [""],
        "HOLD_VIOLATION_CORNERS": [""],
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
        # Each read port has an address generator that the DMA's per-transfer steps fan out
        # to. With 16 ports at 45 % the GEMV 1 x 8 engine failed global routing on
        # congestion, while its timing was met; at 35 % it routes.
        "FP_CORE_UTIL": 45 if accel.ports <= 8 else 35,
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


def parse(
    metrics: dict[str, Any], clock_mhz: float, gds: Path | None, klayout: DrcResult | None = None
) -> PnrResult:
    """The result of a run from its metrics; with ``klayout`` (``klayout_drc``), its KLayout
    DRC count is that report's, off-grid pin markers inside the PDK's macros set apart."""
    drc = sum(int(_num(metrics, k)) for k in ("route__drc_errors", "magic__drc_error__count"))
    drc += klayout.total if klayout else int(_num(metrics, "klayout__drc_error__count"))
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
        pin_markers=klayout.markers_data() if klayout else [],
    )


def klayout_report(run: Path) -> Path | None:
    """The report database of a run's last KLayout DRC."""
    steps = sorted(run.glob("*-klayout-drc"), key=lambda p: int(p.name.split("-")[0]))
    report = steps[-1] / "reports" / "drc.klayout.lyrdb" if steps else None
    return report if report is not None and report.is_file() else None


def klayout_drc(
    metrics: dict[str, Any], report: Path | None, held: Mapping[str, tuple[str, ...]]
) -> DrcResult | None:
    """A run's KLayout DRC report read as the chip step reads IHP's (``chipleap.verify.drc``),
    ``held`` naming the cells of the PDK's own macros; None when the run counted no KLayout
    violation. The report must hold what LibreLane counted."""
    counted = int(_num(metrics, "klayout__drc_error__count"))
    if counted == 0:
        return None
    if report is None:
        raise PnrError(f"LibreLane's KLayout DRC counted {counted} violations and left no report")
    result = DrcResult()
    try:
        read_lyrdb(report, result, held)
    except (DrcReportError, OSError) as exc:
        raise PnrError(f"LibreLane's KLayout DRC report: {exc}") from None
    if result.total + result.markers != counted:
        raise PnrError(
            f"LibreLane's KLayout DRC counted {counted} violations, its report holds "
            f"{result.total + result.markers}"
        )
    return result


def parse_run(
    metrics: dict[str, Any],
    clock_mhz: float,
    gds: Path | None,
    run: Path,
    held: Mapping[str, tuple[str, ...]],
) -> PnrResult:
    """``parse`` with the run's own KLayout DRC report (``klayout_drc``)."""
    return parse(metrics, clock_mhz, gds, klayout_drc(metrics, klayout_report(run), held))


DETAILED_ROUTING = "OpenROAD.DetailedRouting"
AFTER_ROUTING = "Odb.RemoveRoutingObstructions"  # the Classic flow's next step
DEF_TO_ODB = "OpenROAD.DEFtoODB"
MIN_AREA_DIR = "minarea"
# Beside config.json: LibreLane resolves dir:: paths and puts runs/ beside the file.
MIN_AREA_CONFIG = "config.minarea.json"
# LibreLane writes final/ at the end of each invocation, the first part's too. Before the
# second part starts, the first part's final/ goes to ROUTED_FINAL beside it (never
# deleted) and SECOND_PART marks the start, so a run's result is only ever read from a
# final/ the second part wrote: a second part that stops, or ends on an error, leaves none.
ROUTED_FINAL = "final-routed"
SECOND_PART = "chipleap-second-part"
# No timing analysis gives a worst slack this large; the first part's metrics held
# -1.3e16 ns at the corners its timing had not reached.
MAX_SLACK_NS = 1e6
_SLACK_RE = re.compile(r"timing__(setup|hold)\w*__w(n)?s(__.*)?")


def check_metrics(metrics: Mapping[str, Any], clock: Any = None) -> None:
    """Refuse metrics whose worst slacks are not a timing analysis's: not a number, minus
    infinity, or a finite slack beyond ``MAX_SLACK_NS``. Plus infinity is a group with no
    constrained path, such as register to register in a design whose flip-flops all
    face its ports or a macro; but a design whose worst setup slack is plus infinity had
    nothing timed at all, against ``clock`` (LibreLane's ``CLOCK_PORT``): a clock that
    names no port, or constraints that were not read."""
    setup = metrics.get("timing__setup__ws")
    if isinstance(setup, float) and setup == math.inf:
        ports = clock if isinstance(clock, list) else [clock] if clock else []
        named = " against clock " + ", ".join(str(p) for p in ports) if ports else ""
        raise PnrError(
            f"no path was timed{named}: LibreLane's worst setup slack is plus infinity, so "
            "the clock names no port of the design or its constraints were not read"
        )
    for k, v in metrics.items():
        if not _SLACK_RE.fullmatch(k) or isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if v == math.inf:
            continue
        if not math.isfinite(v) or abs(v) > MAX_SLACK_NS:
            raise PnrError(
                f"LibreLane's metrics give {k} = {v:g} ns, which no timing analysis gives; "
                "the run's results are not read"
            )


def begin_second_part(run: Path) -> Path:
    """Set the first part's final/ aside (``ROUTED_FINAL``) and mark the second part's
    start (``SECOND_PART``, on the run's own file system); returns the marker."""
    final = run / "final"
    if final.is_symlink() or (final.exists() and not final.is_dir()):
        raise PnrError(f"LibreLane's {final} is not a directory")
    if final.is_dir():
        aside = run / ROUTED_FINAL
        if aside.exists() or aside.is_symlink():
            raise PnrError(f"{aside} is there already, in a run LibreLane's first part made")
        final.rename(aside)
    try:
        return builddir.write_text(run, SECOND_PART, "")
    except (builddir.BuildDirError, OSError) as exc:
        raise PnrError(f"cannot mark the second part of the run: {exc}") from None


def second_part_result(
    run: Path, code: int, marker: Path, clock: Any = None
) -> tuple[dict[str, Any], Path | None] | None:
    """The metrics and GDS of the second part (``begin_second_part``), or None when it did
    not finish: LibreLane exited with an error or wrote no final/. A final/ older than the
    marker, or metrics no timing analysis gives (``check_metrics``, against ``clock``),
    raise ``PnrError``."""
    final = run / "final"
    metrics_file = final / "metrics.json"
    if code != 0 or final.is_symlink() or not metrics_file.is_file():
        return None
    if metrics_file.stat().st_mtime < marker.stat().st_mtime:
        raise PnrError(f"{metrics_file} is older than the second part of the run")
    try:
        metrics = json.loads(metrics_file.read_text())
    except (OSError, ValueError) as exc:
        raise PnrError(f"LibreLane's {metrics_file.name}: {exc}") from None
    if not isinstance(metrics, dict):
        raise PnrError(f"LibreLane's {metrics_file.name} is not a mapping")
    check_metrics(metrics, clock)
    return metrics, next(iter(sorted((final / "gds").glob("*.gds"))), None)


# LibreLane 3.0.14 hands KLAYOUT_DRC_THREADS to IHP's rule deck as ``thr``, which the deck
# at 22f2a25 does not read: it reads ``threads`` and otherwise runs on every CPU the process
# may use. KLAYOUT_DRC_DEFINES, which 3.0.0.dev44 does not know, passes ``threads`` too.
DRC_DEFINES_FROM = (3, 0, 14, 2, 0)
# LibreLane 3.0.0.dev44 places for timing by default (PL_TIME_DRIVEN); 3.0.14 calls it
# PL_TIMING_DRIVEN, which 3.0.0.dev44 does not know, and leaves it off. Without it a 12 x 12
# multiplier routed at 5.4 ns with no timing repair missed the slow corner by 1.608 ns, where
# 3.0.0.dev44 missed by 0.624 and 3.0.14 with it by 0.314.
TIMING_DRIVEN_FROM = (3, 0, 14, 2, 0)
TIMING_DRIVEN_KEYS = ("PL_TIMING_DRIVEN", "PL_TIME_DRIVEN")


def for_librelane(cfg: dict[str, Any], version: str) -> dict[str, Any]:
    """``cfg`` with what LibreLane ``version`` needs told: KLayout's DRC thread count also
    passed as ``threads``, the name IHP's rule deck reads, and global placement driven by
    timing unless ``cfg`` says otherwise, both on a LibreLane that knows the variable."""
    key = librelane_version_key(version)
    out = dict(cfg)
    threads = cfg.get("KLAYOUT_DRC_THREADS")
    if threads is not None and key >= DRC_DEFINES_FROM:
        # LibreLane takes the defines as text.
        out["KLAYOUT_DRC_DEFINES"] = {"threads": str(threads)}
    if key >= TIMING_DRIVEN_FROM and not any(k in cfg for k in TIMING_DRIVEN_KEYS):
        out["PL_TIMING_DRIVEN"] = True
    return out


def run_librelane(
    workdir: Path, cfg: dict[str, Any], tag: str = "chipleap"
) -> tuple[dict[str, Any], Path | None]:
    """Write ``cfg`` to ``workdir/config.json`` and run LibreLane on it in ``workdir``.
    Returns the final metrics and the GDS.

    LibreLane runs in two parts, in one run directory: up to detailed routing, then the
    rest. Between them, routed shapes under a layer's minimum area get a patch
    (``chipleap.verify.minarea``): the router leaves one now and then, and it would fail
    signoff. The patched layout goes back into LibreLane's database through
    ``OpenROAD.DEFtoODB`` and the rest of the flow runs on it, signoff included."""
    tool = librelane()
    if not tool.found or tool.path is None:
        raise PnrError("LibreLane is not installed")
    try:
        pdk_root = librelane_pdk.ensure()
        env = librelane_env()
    except ValueError as exc:
        raise PnrError(str(exc)) from None
    cfg = for_librelane(cfg, tool.version)
    (workdir / "config.json").write_text(json.dumps(cfg, indent=2))
    base = [tool.path, *librelane_pdk.args_for(pdk_root), "--run-tag", tag]
    limit = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "10800"))
    start = time.monotonic()
    route_log = workdir / "librelane-route.log"
    code = _librelane(
        [*base, "--overwrite", "--to", DETAILED_ROUTING, "config.json"],
        workdir,
        route_log,
        limit,
        limit,
        env,
    )
    run = workdir / "runs" / tag
    try:
        librelane_ran(run, pdk_root)
    except ValueError as exc:
        raise PnrError(str(exc)) from None
    routed = step_state(run, DETAILED_ROUTING)
    if routed is None:
        raise PnrError(
            f"LibreLane did not finish detailed routing (exit {code}):\n{_tail(route_log)}"
        )
    rest, state_in = patch_min_area(workdir, run, routed, cfg)
    log = workdir / "librelane.log"
    args = [*base, "--from", rest]
    if state_in is not None:
        args += ["--with-initial-state", str(state_in), MIN_AREA_CONFIG]
    else:
        args.append("config.json")
    marker = begin_second_part(run)
    left = limit - (time.monotonic() - start)
    code = _librelane(args, workdir, log, max(left, 1.0), limit, env)
    result = second_part_result(run, code, marker, cfg.get("CLOCK_PORT"))
    if result is None:
        raise PnrError(f"LibreLane did not finish (exit {code}):\n{_tail(log)}")
    return result


def _librelane(
    cmd: list[str], workdir: Path, log: Path, timeout: float, limit: float, env: dict[str, str]
) -> int:
    try:
        return launch.run(cmd, workdir, log, timeout, env)
    except launch.Busy as exc:
        raise PnrError(f"LibreLane did not start: {exc}") from None
    except launch.TimedOut as exc:
        exc.seconds = limit  # the whole run's limit, not what was left of it
        raise PnrError(_timed_out(exc, log)) from None


def step_state(run: Path, step_id: str) -> Path | None:
    """The state a step of the run left, by its directory (``45-openroad-detailedrouting``)."""
    name = step_id.replace(".", "-").lower()
    dirs = sorted(run.glob(f"*-{name}"), key=lambda p: int(p.name.split("-")[0]))
    state = dirs[-1] / "state_out.json" if dirs else None
    return state if state is not None and state.is_file() else None


def patch_min_area(
    workdir: Path,
    run: Path,
    routed: Path,
    cfg: dict[str, Any],
    out_dir: str = MIN_AREA_DIR,
    config_name: str = MIN_AREA_CONFIG,
) -> tuple[str, Path | None]:
    """Patch the routed shapes under the minimum area. Returns the step to resume LibreLane
    from and, with patches, the state it starts from; its configuration is then
    ``config_name`` beside the run's, with ``OpenROAD.DEFtoODB`` after detailed routing."""
    state = json.loads(routed.read_text())
    def_path = Path(state.get("def") or "")
    resolved = run / "resolved.json"
    if not def_path.is_file() or not resolved.is_file():
        raise PnrError("LibreLane's routed state has no DEF or resolved configuration")
    try:
        layout = minarea.load(def_path, json.loads(resolved.read_text()))
        patches = minarea.repair(layout)
        if not patches:
            return AFTER_ROUTING, None
        out = workdir / out_dir
        out.mkdir(exist_ok=True)
        patched = out / def_path.name
        patched.write_text(minarea.apply(def_path.read_text(errors="replace"), patches))
    except (minarea.MinAreaError, OSError) as exc:
        raise PnrError(f"minimum metal area after routing: {exc}") from None
    (out / "patches.txt").write_text(
        "".join(f"{minarea.describe(p.shape)}: {p.statement()}\n" for p in patches)
    )
    (workdir / config_name).write_text(json.dumps(with_def_to_odb(cfg), indent=2))
    state_in = out / "state_in.json"
    state_in.write_text(json.dumps({**state, "def": str(patched)}, indent=1))
    return DEF_TO_ODB, state_in


def with_def_to_odb(cfg: dict[str, Any]) -> dict[str, Any]:
    """``cfg`` with a step after detailed routing that reads the (patched) DEF into a new
    database: in the step list of a flow given as one, else as a substitution."""
    meta = dict(cfg.get("meta") or {"version": 2, "flow": "Classic"})
    flow = meta.get("flow")
    if isinstance(flow, list):
        steps = [s for s in flow if s != DEF_TO_ODB]
        if DETAILED_ROUTING not in steps:
            raise PnrError(f"the flow has no {DETAILED_ROUTING} step")
        i = steps.index(DETAILED_ROUTING)
        meta["flow"] = [*steps[: i + 1], DEF_TO_ODB, *steps[i + 1 :]]
    else:
        meta["substituting_steps"] = {
            **(meta.get("substituting_steps") or {}),
            f"+{DETAILED_ROUTING}": DEF_TO_ODB,
        }
    return {**cfg, "meta": meta}


# A design with hard macros has long nets to the macros' pins (a word written to every
# bank, in rows at the top and the bottom of the core), whose slew and capacitance only
# show once they are routed: LibreLane repairs them after global routing too, and buffers
# every net longer than MACRO_WIRE_UM. Without the length cap, repair found the
# SmolLM2 accelerator's write-data nets to its SRAMs (800 um, eight macro pins) too slow
# at the slow corner and changed nothing: resizing a driver cannot fix a wire, and the
# length LibreLane derives (about 5 mm) never splits it.
MACRO_WIRE_UM = 300
MACRO_REPAIR = {
    "RUN_POST_GRT_DESIGN_REPAIR": True,
    "DESIGN_REPAIR_MAX_WIRE_LENGTH": MACRO_WIRE_UM,
    "GRT_DESIGN_REPAIR_MAX_WIRE_LENGTH": MACRO_WIRE_UM,
}


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
    run = workdir / "runs" / "chipleap"
    if reason is not None or pdk is None:
        metrics, gds = run_librelane(workdir, config(accel, clock_mhz))
        result = parse_run(metrics, clock_mhz, gds, run, {})
        return replace(result, buffers=f"{PNR_WORDS}-word flop buffers: {reason}")
    cfg, plan = sram_flow(accel, clock_mhz, workdir, pdk)
    metrics, gds = run_librelane(workdir, cfg)
    from chipleap.rtl.pnr import check_merged

    check_merged(run, workdir, banks.TOP)
    held = pdk_cells(
        ((s.cell, layer.pdk_macro_cells(pdk, s.cell, sram.LIBRARY)) for s in plan.cells()),
        (banks.TOP,),
    )
    result = replace(parse_run(metrics, clock_mhz, gds, run, held), synth_cell_um2=synth_area(run))
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


def synth_area(run: Path) -> float:
    """The standard-cell area of LibreLane's synthesis in ``run`` (0 when the run has
    none): what the model's logic estimate is checked against, before placement and
    timing repair add to it."""
    found = sorted(run.glob("*-yosys-synthesis/state_out.json"))
    if not found:
        return 0.0
    try:
        metrics = json.loads(found[-1].read_text()).get("metrics", {})
    except (OSError, ValueError, AttributeError):
        return 0.0
    return _num(metrics, "design__instance__area") if isinstance(metrics, dict) else 0.0


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
