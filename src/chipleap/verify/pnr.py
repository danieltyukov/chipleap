"""Placement and routing of the generated accelerator with LibreLane on IHP SG13G2.

LibreLane runs the whole open flow: Yosys, OpenROAD for floorplan, placement, clock tree,
routing and timing repair, KLayout with IHP's rule deck for design-rule checks, Netgen
for layout versus schematic, and OpenSTA for timing at the design's clock. The result is a GDS that
passes the checks a foundry shuttle asks for.

The activation and weight buffers are SRAM macros in silicon. When the build can have
them without a change to its schedule (``chipleap.gen.banks``), the IHP PDK ChipLeap
uses is installed and the macros' rows leave a core that routes, the layout has them:
IHP's own macros at the design's buffer sizes, in rows above and below the logic
(``chipleap.macro.place``), each with its halo and the macro power grid
(``chipleap.macro.grid``), timed at every corner from their liberty and checked by LVS as
boxes with their pins. Otherwise the layout holds the accelerator's logic with 16-word
flop buffers, and the result says why.
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
from chipleap.gen.rtl import accel_sv, cdc_sv, rtl_params
from chipleap.hw.design import Accel
from chipleap.macro import grid, layer, place, sram
from chipleap.rtl import builddir
from chipleap.verify import antenna, launch, minarea
from chipleap.verify.drc import (
    DrcReportError,
    DrcResult,
    describe_markers,
    pdk_cells,
    read_lyrdb,
)
from chipleap.verify.synth import _sv2v
from chipleap.verify.tools import librelane, librelane_env, librelane_pdk_args, librelane_ran

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
    max_slew_violations: int = 0  # pins over their slew limit, at any corner
    max_cap_violations: int = 0  # nets over their capacitance limit, at any corner
    max_fanout_violations: int = 0  # drivers over their fanout limit, at any corner
    # Off-grid pin markers inside the PDK's own macros, left out of drc_errors
    # (``chipleap.verify.drc``): rule, cell, the macros that hold it, count.
    pin_markers: list[dict[str, Any]] = field(default_factory=list)
    # IHP's antenna deck on the GDS (``chipleap.verify.antenna``), or why it did not run
    # (``ihp_antenna_skipped``); neither when the flow does not run it.
    ihp_antenna: DrcResult | None = None
    ihp_antenna_skipped: str = ""
    # With two clocks: each clock's period, and the worst slack of each path group (each
    # clock, and "path delay" for the crossings) at each corner, for setup and for hold
    # (``group_slacks``).
    clocks: dict[str, float] = field(default_factory=dict)
    groups: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.drc_errors == 0 and self.lvs_errors == 0

    @property
    def checks_clean(self) -> bool:
        """DRC and LVS clean, antenna clean under OpenROAD's check and IHP's deck, and no
        pin or net over its slew or capacitance limit at any corner. A driver over its
        fanout limit does not count: fanout is a constraint for the repairs, and the slew
        and capacitance limits carry what it costs (a diode the router adds is a load to
        the count and switches nothing)."""
        ihp = self.ihp_antenna
        return (
            self.clean
            and self.antenna_nets == 0
            and (ihp is None or ihp.total == 0)
            and self.max_slew_violations == 0
            and self.max_cap_violations == 0
        )

    def checks(self) -> str:
        """The checks for a step's summary: DRC, LVS and OpenROAD's antenna counts, IHP's
        antenna deck's violations with their rules and places, or why it did not run, the
        slew and capacitance violations, the drivers over their fanout limit (reported
        only), and the pin markers set apart."""
        ihp = self.ihp_antenna
        if self.checks_clean:
            text = "DRC, LVS and antenna clean"
            if ihp is not None:
                text += ", IHP's antenna deck included"
            text += ", no slew or capacitance violations"
        else:
            text = (
                f"{self.drc_errors} DRC, {self.lvs_errors} LVS and {self.antenna_nets} "
                f"antenna violations, {self.max_slew_violations} slew and "
                f"{self.max_cap_violations} capacitance violations"
            )
        if ihp is not None and ihp.total:
            n = ihp.total
            text += (
                f"; IHP's antenna deck: {n} violation{'s' if n != 1 else ''}, "
                f"{antenna.describe(antenna.summary(ihp), ihp.top)}"
            )
        elif ihp is None and self.ihp_antenna_skipped:
            text += f"; IHP's antenna deck not run: {self.ihp_antenna_skipped}"
        if n := self.max_fanout_violations:
            text += f"; {n} driver{'s' if n != 1 else ''} over the fanout limit (reported only)"
        if self.pin_markers:
            text += f"; {describe_markers(self.pin_markers)}"
        return text

    def ihp_antenna_data(self) -> dict[str, Any]:
        """IHP's antenna deck for a step's result (``chipleap.verify.antenna.summary``), or
        why it did not run; empty when the flow does not run it."""
        if self.ihp_antenna is not None:
            return {"ihp_antenna": antenna.summary(self.ihp_antenna)}
        return (
            {"ihp_antenna": {"skipped": self.ihp_antenna_skipped}}
            if self.ihp_antenna_skipped
            else {}
        )

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


def _violations(metrics: dict[str, Any], prefix: str) -> int:
    """The largest of a violation count overall and at each corner."""
    vals = [
        v
        for k, v in metrics.items()
        if (k == prefix or k.startswith(prefix + "__corner:"))
        and isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(v)
    ]
    return int(max(vals, default=0))


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
        max_slew_violations=_violations(metrics, "design__max_slew_violation__count"),
        max_cap_violations=_violations(metrics, "design__max_cap_violation__count"),
        max_fanout_violations=_violations(metrics, "design__max_fanout_violation__count"),
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


ANTENNA_DIR = "antenna"  # beside the run: IHP's antenna deck's report and log


def with_ihp_antenna(
    r: PnrResult, pdk: Path | None, top: str, work: Path, name: str = ANTENNA_DIR
) -> PnrResult:
    """``r`` with IHP's antenna deck run on its GDS (``chipleap.verify.antenna``), its
    report in ``work/name`` (emptied first), or with why it could not run here. A run of
    the deck that did not finish fails the step."""
    why = "the run left no GDS" if r.gds is None else antenna.unavailable(pdk)
    if why or pdk is None or r.gds is None:
        return replace(r, ihp_antenna_skipped=why)
    out = builddir.work_dir(work, name, empty=True)
    limit = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", "10800"))
    try:
        return replace(r, ihp_antenna=antenna.check(pdk, r.gds, top, out, limit))
    except antenna.AntennaError as exc:
        raise PnrError(str(exc)) from None


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


def run_librelane(
    workdir: Path,
    cfg: dict[str, Any],
    tag: str = "chipleap",
    *,
    to: str | None = None,
    resume_from: str | None = None,
    timeout_s: float = 10800,
    late_antenna_repair: bool = False,
) -> tuple[dict[str, Any], Path | None]:
    """Write ``cfg`` to ``workdir/config.json`` and run LibreLane on it in ``workdir``.
    Returns the final metrics and the GDS.

    LibreLane runs in two parts, in one run directory: up to detailed routing, then the
    rest. Between them, routed shapes under a layer's minimum area get a patch
    (``chipleap.verify.minarea``): the router leaves one now and then, and it would fail
    signoff. The patched layout goes back into LibreLane's database through
    ``OpenROAD.DEFtoODB`` and the rest of the flow runs on it, signoff included.

    With ``to`` (a step before detailed routing) the run stops after that step and
    returns no metrics; with ``resume_from`` it goes on in the same run from that step,
    with the state the run's last step left and the configuration given now.
    ``CHIPLEAP_PNR_TIMEOUT`` (seconds) bounds the run, ``timeout_s`` when it is unset.

    With ``late_antenna_repair``, antennas are repaired again after the timing repair
    (``with_late_antenna_repair``). The accelerator, an RTL project and a chip ask for it
    (an ECO adds it to its own step list). The SoC step does not yet: the runs its
    results stand on were routed without it, and it turns it on once a SoC run with it
    has been checked."""
    tool = librelane()
    if not tool.found or tool.path is None:
        raise PnrError("LibreLane is not installed")
    try:
        pdk_args = librelane_pdk_args()
        env = librelane_env()
    except ValueError as exc:
        raise PnrError(str(exc)) from None
    if late_antenna_repair:
        cfg = with_late_antenna_repair(cfg)
    (workdir / "config.json").write_text(json.dumps(cfg, indent=2))
    base = [tool.path, *pdk_args, "--run-tag", tag]
    first = ["--from", resume_from] if resume_from else ["--overwrite"]
    limit = float(os.environ.get("CHIPLEAP_PNR_TIMEOUT", timeout_s))
    start = time.monotonic()
    run = workdir / "runs" / tag
    if to is not None:
        log = workdir / f"librelane-{_step_dir(to)}.log"
        code = _librelane(
            [*base, *first, "--to", to, "config.json"], workdir, log, limit, limit, env
        )
        if code != 0 or step_state(run, to) is None:
            raise PnrError(f"LibreLane stopped before {to} (exit {code}):\n{_tail(log)}")
        return {}, None
    route_log = workdir / "librelane-route.log"
    code = _librelane(
        [*base, *first, "--to", DETAILED_ROUTING, "config.json"],
        workdir,
        route_log,
        limit,
        limit,
        env,
    )
    try:
        librelane_ran(run)
    except ValueError as exc:
        raise PnrError(str(exc)) from None
    routed = step_state(run, DETAILED_ROUTING)
    if routed is None:
        stopped = last_step(run)
        where = (
            f"stopped in {stopped}, before detailed routing finished"
            if stopped
            else "did not finish detailed routing"
        )
        raise PnrError(f"LibreLane {where} (exit {code}):\n{_tail(route_log)}")
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


def _step_dir(step_id: str) -> str:
    return step_id.replace(".", "-").lower()


_RUNNING_RE = re.compile(r"^Running '([^']+)' at ", re.MULTILINE)


def last_step(run: Path) -> str:
    """The step LibreLane started last in ``run``, from its ``flow.log``, which every
    invocation in the run appends to; empty when the log names none."""
    try:
        found = _RUNNING_RE.findall((run / "flow.log").read_text(errors="replace"))
    except OSError:
        return ""
    return found[-1] if found else ""


def step_state(run: Path, step_id: str) -> Path | None:
    """The state a step of the run left, by its directory (``45-openroad-detailedrouting``)."""
    name = _step_dir(step_id)
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


# LibreLane repairs antennas after global routing (jumpers, and diodes at the gates of
# nets over the PDK's ratios) and then repairs timing, whose buffers are new gates on nets
# routed again; nothing checks those nets until the detailed router's own repair. In the
# SmolLM2 accelerator's layout a hold buffer's input (a dlygate4sd3_1) was left on a Metal2
# island at a cumulative ratio of 312.72 against 200, out of reach of the router's diodes,
# which joined only through Metal3. So every layout ChipLeap routes with LibreLane but the
# SoC's (``run_librelane``) repairs antennas once more after the timing repair: in that
# layout the second repair found 35 nets over the ratios and put 17 diodes on them, the
# routed design was clean under OpenROAD's antenna check and IHP's rule deck, and its slack
# at the slow corner moved by +0.004 ns. Only a repair's diodes last: LibreLane's step
# routes globally again after repair_antennas, over the jumpers it inserted (40 there),
# and the detailed router follows that route. The second repair put no diode on a clock
# net there; the detailed router's own repair put 10 on one (see docs/verification.md).
TIMING_REPAIR = "OpenROAD.ResizerTimingPostGRT"
ANTENNA_REPAIR = "OpenROAD.RepairAntennas"


def with_late_antenna_repair(cfg: dict[str, Any]) -> dict[str, Any]:
    """``cfg`` with LibreLane's antenna repair once more after the timing repair after
    global routing: after that step in a flow given as a step list, else as a substitution.
    Unchanged when the configuration turns antenna repair off, or the timing repair (a
    Classic flow's) or a step list has none."""
    if cfg.get("RUN_ANTENNA_REPAIR") is False:
        return cfg
    meta = dict(cfg.get("meta") or {"version": 2, "flow": "Classic"})
    flow = meta.get("flow")
    if isinstance(flow, list):
        if TIMING_REPAIR not in flow:
            return cfg
        i = flow.index(TIMING_REPAIR)
        if flow[i + 1 : i + 2] != [ANTENNA_REPAIR]:
            meta["flow"] = [*flow[: i + 1], ANTENNA_REPAIR, *flow[i + 1 :]]
    elif cfg.get("RUN_POST_GRT_RESIZER_TIMING"):
        meta["substituting_steps"] = {
            **(meta.get("substituting_steps") or {}),
            f"+{TIMING_REPAIR}": ANTENNA_REPAIR,
        }
    else:
        return cfg
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
# IHP's SRAMs allow a slew of 0.595 ns at their inputs at the slow corner, a quarter of the
# standard cells' 2.5 ns, and OpenROAD's repair_design holds a net to its driver's limit
# and the design's, not to its loads': in the macro-backed GEMV 1 x 4 build, an address bit
# driven 190 um from an o21ai_1 was estimated at 0.54 ns at the macro's pin, nothing
# repaired it (not with a 50 % slew margin either), and routed it reached 0.80 ns, its
# wire 45 to 60 % more capacitive than estimated. So a macro-backed accelerator is placed
# and routed against a design-wide limit of MACRO_PNR_SLEW_NS (PNR_SDC), under which a
# routed slew stays within the macros', and signed off against LibreLane's own constraints
# (SIGNOFF_SDC), with every cell's own limit: in that build the repair added about 400
# buffers, 1.6 % of its cells, and took the address pins to 0.09 ns estimated.
MACRO_PNR_SLEW_NS = 0.3
PNR_SDC = "pnr.sdc"
SIGNOFF_SDC = "signoff.sdc"


def macro_sdc(pnr: bool) -> str:
    """The constraints of a macro-backed accelerator: LibreLane's own (base.sdc), and for
    placement and routing the design-wide slew limit ``MACRO_PNR_SLEW_NS``."""
    lines = ["# LibreLane's own constraints.", "source $::env(SCRIPTS_DIR)/base.sdc"]
    if pnr:
        lines += [
            "# A slew target for repair under the SRAM macros' input limit (0.595 ns at the",
            "# slow corner), since repair holds nets to their drivers' limits, not their loads'.",
            f"set_max_transition {MACRO_PNR_SLEW_NS} [current_design]",
        ]
    return "\n".join(lines) + "\n"


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
        "PNR_SDC_FILE": f"dir::{PNR_SDC}",
        "SIGNOFF_SDC_FILE": f"dir::{SIGNOFF_SDC}",
        "FP_MACRO_HORIZONTAL_HALO": place.HALO_UM,
        "FP_MACRO_VERTICAL_HALO": place.HALO_UM,
        **signoff(),
    }


CDC_TOP = "chipleap_accel_cdc"  # the accelerator behind its clock crossings
CDC_INSTANCE = "u_accel"  # the accelerator in it
CDC_DEPTH = 8  # words of each crossing FIFO (CDC_AW = 3), and reads in flight per port


def two_clock_sdc(bus_mhz: float, accel_mhz: float, slew_ns: float | None = None) -> str:
    """Constraints for the accelerator behind its crossings: X-HEEP's clock (``clk_i``)
    and the accelerator's (``accel_clk_i``), the ports timed against the first, as
    LibreLane's base SDC would (``chipleap.rtl.pnr.io_lines``), and the two clocks
    asynchronous with each crossing's path bounded.

    In OpenSTA as LibreLane runs it (2.7.0, in both 3.0.0.dev44 and 3.0.14, tried on a
    two-flop crossing), ``set_clock_groups -asynchronous`` between the clocks also removes
    a ``set_max_delay`` on the paths between them (they are reported unconstrained), and
    ``-datapath_only`` is not a flag. So every path from one clock to
    the other gets ``set_max_delay -ignore_clock_latency`` (OpenSTA's datapath-only bound:
    the data path alone, without either clock tree), at the faster clock's period, which
    keeps the bits of a gray-coded pointer within a cycle of each other where they are
    sampled, and ``set_false_path -hold``, since a hold check between unrelated clocks
    times nothing. ``slew_ns`` adds a design-wide slew target (``MACRO_PNR_SLEW_NS`` for a
    macro build's placement and routing)."""
    from chipleap.rtl.pnr import io_lines

    bus = round(1000.0 / bus_mhz, 3)
    acc = round(1000.0 / accel_mhz, 3)
    bound = min(bus, acc)
    lines = [
        "# ChipLeap: the accelerator on its own clock behind its crossings (chipleap_accel_cdc).",
        "# Inputs and outputs are on X-HEEP's side and timed against its clock.",
        f"create_clock [get_ports clk_i] -name clk_i -period {bus:g}",
        f"create_clock [get_ports accel_clk_i] -name accel_clk_i -period {acc:g}",
        "set clock_inputs [list [get_ports clk_i] [get_ports accel_clk_i]]",
        "set data_inputs [all_inputs]",
        "foreach port $clock_inputs {",
        "    set i [lsearch $data_inputs $port]",
        "    if { $i >= 0 } { set data_inputs [lreplace $data_inputs $i $i] }",
        "}",
        *io_lines("clk_i", bus),
        "# The clocks are asynchronous. Each path between them is a crossing the CDC step",
        "# checked; its data path is bounded by the faster clock's period, and it has no hold",
        "# check. Not set_clock_groups: in OpenSTA it drops these bounds.",
        *(
            f"set_max_delay -ignore_clock_latency {bound:g} -from [get_clocks {a}] -to [get_clocks {b}]"
            for a, b in (("clk_i", "accel_clk_i"), ("accel_clk_i", "clk_i"))
        ),
        *(
            f"set_false_path -hold -from [get_clocks {a}] -to [get_clocks {b}]"
            for a, b in (("clk_i", "accel_clk_i"), ("accel_clk_i", "clk_i"))
        ),
    ]
    if slew_ns is not None:
        lines += [
            "# A slew target for repair under the SRAM macros' input limit.",
            f"set_max_transition {slew_ns} [current_design]",
        ]
    return "\n".join(lines) + "\n"


def model_cell_um2(accel: Accel, banked: bool = False, crossings: bool = False) -> float:
    """The model's standard-cell area for the accelerator's logic on SG13G2; ``banked``
    for a macro-backed build, whose logic also has its banks' interface, ``crossings``
    for one on its own clock behind its crossings."""
    um2 = banks.cell_um2(accel, banked)
    if crossings:
        from chipleap.hw.tech import ACCEL_CDC_KGE, ACCEL_CDC_PORT_KGE, get_tech

        kge = ACCEL_CDC_KGE + ACCEL_CDC_PORT_KGE * accel.ports
        um2 += kge * 1000 * get_tech("ihp-sg13g2").ge_um2
    return um2


def with_crossings(
    cfg: dict[str, Any], workdir: Path, bus_mhz: float, accel_mhz: float
) -> dict[str, Any]:
    """``cfg`` for the accelerator behind its crossings (``CDC_TOP``): that top, from
    ``src/chipleap_accel_cdc.v`` in place of ``src/chipleap_accel.v``, both clocks, and
    ``two_clock_sdc`` for placement, routing and signoff, written into ``workdir`` (with
    the macros' slew target for placement and routing when ``cfg`` has macros)."""
    files = [
        "dir::src/chipleap_accel_cdc.v" if f == "dir::src/chipleap_accel.v" else f
        for f in cfg["VERILOG_FILES"]
    ]
    slew = MACRO_PNR_SLEW_NS if cfg.get("MACROS") else None
    (workdir / PNR_SDC).write_text(two_clock_sdc(bus_mhz, accel_mhz, slew))
    (workdir / SIGNOFF_SDC).write_text(two_clock_sdc(bus_mhz, accel_mhz))
    return {
        **cfg,
        "DESIGN_NAME": CDC_TOP,
        "VERILOG_FILES": files,
        "CLOCK_PORT": ["clk_i", "accel_clk_i"],
        # LibreLane reads one period; the faster clock's, the accelerator's for its paths.
        "CLOCK_PERIOD": round(1000.0 / max(bus_mhz, accel_mhz), 3),
        "PNR_SDC_FILE": f"dir::{PNR_SDC}",
        "SIGNOFF_SDC_FILE": f"dir::{SIGNOFF_SDC}",
    }


_GROUP_RE = re.compile(r"^Path Group: (.+)$", re.MULTILINE)
_SLACK_LINE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?(?:e-?\d+)?)\s+slack \((?:MET|VIOLATED)\)", re.MULTILINE
)


CROSSINGS = "path delay"  # OpenSTA's path group for the set_max_delay of two_clock_sdc


def group_slacks(run: Path, clocks: tuple[str, ...] = ()) -> dict[str, dict[str, dict[str, float]]]:
    """The worst slack of each path group at each corner, for setup and hold, from the
    paths LibreLane's signoff timing reports (the last ``OpenROAD.STAPostPNR``'s
    ``max.rpt`` and ``min.rpt``). Those list the worst paths of the design, a thousand,
    not of each group, so a group with none among them (each of ``clocks`` and the
    crossings for setup, each clock for hold) is entered under ``setup_above`` or
    ``hold_above`` at the report's largest slack, which its worst path exceeds."""
    steps = sorted(run.glob("*-openroad-stapostpnr"), key=lambda p: int(p.name.split("-")[0]))
    out: dict[str, dict[str, dict[str, float]]] = {}
    if not steps:
        return out
    for kind, name in (("setup", "max.rpt"), ("hold", "min.rpt")):
        expected = (*clocks, CROSSINGS) if kind == "setup" and clocks else clocks
        for rpt in sorted(steps[-1].glob(f"*/{name}")):
            corner = rpt.parent.name
            text = rpt.read_text(errors="replace")
            listed: list[float] = []
            for block in text.split("Startpoint: ")[1:]:
                g, s = _GROUP_RE.search(block), _SLACK_LINE_RE.search(block)
                if g is None or s is None:
                    continue
                worst = out.setdefault(kind, {}).setdefault(corner, {})
                group = g.group(1).strip()
                worst[group] = min(worst.get(group, math.inf), float(s.group(1)))
                listed.append(float(s.group(1)))
            seen = out.get(kind, {}).get(corner, {})
            for group in expected:
                if group not in seen and listed:
                    out.setdefault(f"{kind}_above", {}).setdefault(corner, {})[group] = max(listed)
    return out


def sram_reason(accel: Accel, pdk: Path | None) -> str | None:
    """Why the layout of ``accel`` keeps flop buffers, or None when it takes macros."""
    reason = banks.unsupported(accel)
    if reason is None and pdk is None:
        reason = f"no IHP PDK with its SRAM macros ({io.PDK_ENV})"
    if reason is None and pdk is not None:
        catalog = sram.catalog(pdk)
        if not catalog:
            reason = f"the PDK at {pdk} has no single-port SRAM macros"
        else:
            reason = banks.layout_reason(accel, catalog)
    return reason


def place_and_route(
    accel: Accel,
    clock_mhz: float,
    workdir: Path,
    pdk: Path | None = None,
    accel_mhz: float = 0.0,
) -> PnrResult:
    """The accelerator through LibreLane at ``clock_mhz``. With ``accel_mhz``, the
    accelerator runs on its own clock behind its crossings (``CDC_TOP``), and
    ``clock_mhz`` is X-HEEP's side's."""
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
    flops = reason is not None or pdk is None
    if accel_mhz:
        # The core and its crossings in one file, converted together.
        inner = "chipleap_accel" if flops else banks.TOP
        (src / "chipleap_accel_cdc.sv").write_text(accel_sv() + "\n" + cdc_sv(inner))
        _sv2v(src / "chipleap_accel_cdc.sv", src / "chipleap_accel_cdc.v")
    clock = accel_mhz or clock_mhz  # the accelerator's, for its paths' Fmax

    def timed(r: PnrResult) -> PnrResult:
        if not accel_mhz:
            return r
        return replace(
            r,
            clocks={
                "clk_i": round(1000.0 / clock_mhz, 3),
                "accel_clk_i": round(1000.0 / accel_mhz, 3),
            },
            groups=group_slacks(run, ("clk_i", "accel_clk_i")),
        )

    if flops:
        cfg = config(accel, clock_mhz)
        if accel_mhz:
            cfg = with_crossings(cfg, workdir, clock_mhz, accel_mhz)
        metrics, gds = run_librelane(workdir, cfg, late_antenna_repair=True)
        result = timed(parse_run(metrics, clock, gds, run, {}))
        result = with_ihp_antenna(result, pdk, cfg["DESIGN_NAME"], workdir)
        return replace(result, buffers=f"{PNR_WORDS}-word flop buffers: {reason}")
    assert pdk is not None
    cfg, plan = sram_flow(accel, clock_mhz, workdir, pdk, accel_mhz)
    top = cfg["DESIGN_NAME"]
    metrics, gds = run_librelane(workdir, cfg, late_antenna_repair=True)
    from chipleap.rtl.pnr import check_merged

    check_merged(run, workdir, top)
    held = pdk_cells(
        ((s.cell, layer.pdk_macro_cells(pdk, s.cell, sram.LIBRARY)) for s in plan.cells()),
        (top,),
    )
    result = replace(parse_run(metrics, clock, gds, run, held), synth_cell_um2=synth_area(run))
    result = with_ihp_antenna(timed(result), pdk, top, workdir)
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
    accel: Accel, clock_mhz: float, workdir: Path, pdk: Path, accel_mhz: float = 0.0
) -> tuple[dict[str, Any], banks.Banks]:
    """The files of a macro-backed run in ``workdir`` (the generated top, the macros'
    views, the power grid) and its LibreLane configuration; the RTL is in ``src``. With
    ``accel_mhz`` the macro build sits behind the crossings (``with_crossings``), its
    macros inside ``CDC_INSTANCE`` and the floorplan sized for the crossings too."""
    plan = banks.plan(accel, sram.catalog(pdk))
    views = {s.cell: sram.views(pdk, s.cell, workdir) for s in plan.cells()}
    prefix = f"{CDC_INSTANCE}." if accel_mhz else ""
    cells = {prefix + inst: cell for inst, cell in plan.instances()}
    fp = place.rows(
        [
            place.Macro(prefix + inst, views[cell].view.width, views[cell].view.height)
            for inst, cell in plan.instances()
        ],
        model_cell_um2(accel, banked=True, crossings=bool(accel_mhz)),
    )
    # Behind the crossings the core keeps as many reads in flight per port as they hold.
    (workdir / "src" / f"{banks.TOP}.v").write_text(
        banks.top_sv(plan, CDC_DEPTH if accel_mhz else None)
    )
    placements = [
        grid.Placement(p.instance, views[cells[p.instance]].view, p.x, p.y, p.orient)
        for p in fp.macros
    ]
    (workdir / "pdn.tcl").write_text(grid.core_script(placements, fp.core[1]))
    (workdir / PNR_SDC).write_text(macro_sdc(pnr=True))
    (workdir / SIGNOFF_SDC).write_text(macro_sdc(pnr=False))
    macros, _ = sram.librelane_macros(
        views, [(p.instance, cells[p.instance], p.x, p.y, p.orient) for p in fp.macros], workdir
    )
    cfg = sram_config(accel, clock_mhz, fp, macros)
    if accel_mhz:
        cfg = with_crossings(cfg, workdir, clock_mhz, accel_mhz)
    return cfg, plan


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
