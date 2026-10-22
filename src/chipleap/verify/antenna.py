"""IHP's own antenna rules on a routed layout.

LibreLane's KLayout DRC runs IHP's main rule deck, and the chip step runs IHP's
``run_drc.py`` with its density and extra decks; neither runs the antenna deck
(``rule_decks/antenna.drc``), which ``run_drc.py`` leaves out unless asked. OpenROAD's
antenna check, which LibreLane runs, reads the ratios in the PDK's LEF and counts the
metal differently: in the SmolLM2 accelerator's layout it flagged a hold buffer's input
that IHP's deck passed, and IHP's deck flagged a Metal5 wire at a cumulative ratio of
200.15 against 200 that it passed. So place and route, an ECO and the chip step also run
IHP's antenna deck (``run_drc.py --antenna_only``) on the finished GDS and fail on its
violations, each named with its rule and where it is.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from chipleap.chip.run import ToolError, ihp_runner, python_with_klayout
from chipleap.verify.drc import DrcReportError, DrcResult, read_lyrdb

DECK = Path("libs.tech") / "klayout" / "tech" / "drc" / "rule_decks" / "antenna.drc"


class AntennaError(RuntimeError):
    pass


def threads() -> int:
    """KLayout's threads for the deck: half the CPUs this process may use, at most 8, as
    for LibreLane's KLayout DRC. The deck takes one per CPU of the machine without it."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cpus = os.cpu_count() or 2
    return max(1, min(8, cpus // 2))


def unavailable(pdk: Path | None) -> str:
    """Why IHP's antenna deck cannot run here, or empty when it can."""
    if pdk is None:
        return "no IHP PDK (CHIPLEAP_IHP_PDK)"
    if not (pdk / DECK).is_file():
        return f"the PDK at {pdk} has no antenna deck"
    if python_with_klayout() is None:
        return "no Python with KLayout's module (CHIPLEAP_KLAYOUT_PYTHON)"
    return ""


def check(pdk: Path, gds: Path, top: str, out_dir: Path, timeout: float) -> DrcResult:
    """IHP's antenna deck on ``gds`` (top cell ``top``); its report and log go to
    ``out_dir``. A run that did not finish, or whose exit code disagrees with its report,
    is an error, never a result."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        code, log = ihp_runner(
            pdk,
            "drc",
            out_dir,
            {"layout.gds": gds},
            lambda d: [
                "--path=layout.gds",
                f"--topcell={top}",
                f"--run_dir={d}",
                "--antenna_only",
                f"--density_thr={threads()}",
            ],
            (),
            timeout,
        )
    except ToolError as exc:
        raise AntennaError(f"IHP's antenna deck: {exc}") from None
    report = out_dir / f"layout_{top}_antenna.lyrdb"
    if code not in (0, 1) or not report.is_file():
        last = next((ln for ln in reversed(log.splitlines()) if ln.strip()), "")
        raise AntennaError(f"IHP's antenna deck did not finish (exit {code}): {last}")
    result = DrcResult(top=top)
    try:
        read_lyrdb(report, result)
    except (DrcReportError, OSError) as exc:
        raise AntennaError(f"IHP's antenna deck: {exc}") from None
    if (code == 1) != (result.total > 0):
        raise AntennaError(
            f"IHP's antenna deck exited {code} with {result.total} violations in its report"
        )
    return result


def summary(result: DrcResult) -> dict[str, Any]:
    """The deck's result as data: its violations, by rule, and where the first of each
    rule are (the centre of the marker, in um in its cell)."""
    return {
        "violations": result.total,
        "rules": dict(sorted(result.violations.items())),
        "places": {
            rule: [{"cell": c, "x_um": round(x, 3), "y_um": round(y, 3)} for c, x, y in at]
            for rule, at in sorted(result.places.items())
            if at
        },
    }


def describe(data: Mapping[str, Any], top: str, rules: int = 3, places: int = 2) -> str:
    """The violations of ``summary``'s ``data`` for a step's summary: the first ``rules``
    rules, each at its first ``places`` locations, in um, and in which cell when it is not
    the layout's ``top``."""
    parts = []
    found: Mapping[str, list[dict[str, Any]]] = data.get("places", {})
    for rule, n in list(data.get("rules", {}).items())[:rules]:
        where = [
            f"({p['x_um']:.2f}, {p['y_um']:.2f}) um"
            + (f" in {p['cell']}" if p["cell"] and p["cell"] != top else "")
            for p in found.get(rule, [])[:places]
        ]
        text = f"{rule} at {', '.join(where)}" if where else f"{rule} ({n})"
        if where and n > len(where):
            text += f" and {n - len(where)} more"
        parts.append(text)
    left = len(data.get("rules", {})) - rules
    if left > 0:
        parts.append(f"{left} more rule{'s' if left != 1 else ''}")
    return "; ".join(parts)
