"""Measure the SoC's clock terms in chipleap/hw/tech.py from a routed SoC.

    python scripts/fit_soc_clock.py RUN_DIR

RUN_DIR is the LibreLane run of ``chipleap verify DIR --steps soc``
(``DIR/verify/soc/runs/chipleap``). From the slow corner's setup paths after routing and
extraction (OpenSTA's report of the run's last timing analysis after place and route, with
the 5 % derate the step's constraints apply), it prints

* SOC_READ_PATH_NS: the period the slowest path from one of X-HEEP's bank macros needs,
  less that macro's read time (``chipleap.macro.sram.IHP_1P_READ_NS``),
* SOC_BUS_PATH_NS: the period the slowest other path of ``clk`` needs, leaving out the
  paths inside the accelerator, which its own limit covers
  (``chipleap.hw.tech.accel_signoff_mhz``),

each with its start, end and slack, and the clock the two give with the run's banks.
Only paths from a rising edge of ``clk`` to the next count. OpenSTA's report lists the
1000 worst endpoints; a term with no path among them is reported as missing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from chipleap.gen.soc import ACCEL_INST, MCU_INST
from chipleap.macro.sram import IHP_1P_READ_NS

CORNER = "nom_slow_1p08V_125C"
_EDGE_RE = re.compile(r"^\s+(-?[\d.]+)\s+(-?[\d.]+)\s+clock (\S+) \((rise|fall) edge\)", re.M)
_SLACK_RE = re.compile(r"^\s+(-?[\d.]+)\s+slack \((?:VIOLATED|MET)\)", re.M)
_NAME_RE = re.compile(r"^(Startpoint|Endpoint): (\S+)", re.M)
_GROUP_RE = re.compile(r"^Path Group: (\S+)", re.M)


@dataclass(frozen=True)
class TimedPath:
    start: str
    end: str
    group: str
    needed_ns: float  # the period at which the path's slack would be zero
    slack_ns: float


def paths(report: str, clock: str = "clk") -> list[TimedPath]:
    """The paths of ``report`` (OpenSTA's ``report_checks`` text) from a rising edge of
    ``clock`` to the next, each with the period it needs: the time between its edges
    less its slack."""
    out = []
    for block in report.split("\nStartpoint: ")[1:]:
        block = "Startpoint: " + block
        names = dict(_NAME_RE.findall(block))
        group = _GROUP_RE.search(block)
        edges = _EDGE_RE.findall(block)
        slack = _SLACK_RE.search(block)
        if not (group and slack and len(edges) >= 2 and "Startpoint" in names):
            continue
        launch, capture = edges[0], edges[-1]
        if (
            group.group(1) != clock
            or launch[2:] != (clock, "rise")
            or capture[2:] != (clock, "rise")
        ):
            continue
        span = float(capture[1]) - float(launch[1])
        s = float(slack.group(1))
        out.append(TimedPath(names["Startpoint"], names.get("Endpoint", ""), clock, span - s, s))
    return out


def _inside(name: str, prefix: str) -> bool:
    return name.startswith(f"{prefix}.") or name.startswith(f"{prefix}/")


def terms(
    found: list[TimedPath], banks: dict[str, str]
) -> tuple[tuple[float, TimedPath] | None, TimedPath | None]:
    """The slowest path from one of ``banks`` (instance: cell) with what it needs after
    that macro's read time, and the slowest other path that is not inside the
    accelerator."""
    read: tuple[float, TimedPath] | None = None
    bus: TimedPath | None = None
    for p in found:
        inst = p.start.split("/")[0]
        if inst in banks:
            rest = p.needed_ns - IHP_1P_READ_NS[banks[inst]]
            if read is None or rest > read[0]:
                read = (rest, p)
        elif not (_inside(p.start, ACCEL_INST) and _inside(p.end, ACCEL_INST)):
            if bus is None or p.needed_ns > bus.needed_ns:
                bus = p
    return read, bus


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run", type=Path, help="the soc step's LibreLane run directory")
    args = ap.parse_args(argv)
    sta = sorted(args.run.glob("*-openroad-stapostpnr"), key=lambda d: int(d.name.split("-")[0]))
    if not sta:
        print(f"{args.run} has no timing analysis after place and route", file=sys.stderr)
        return 1
    report = (sta[-1] / CORNER / "max.rpt").read_text(errors="replace")
    macros = json.loads((args.run / "resolved.json").read_text()).get("MACROS") or {}
    banks = {
        inst: cell
        for cell, entry in macros.items()
        for inst in entry["instances"]
        if _inside(inst, MCU_INST) and cell in IHP_1P_READ_NS
    }
    found = paths(report)
    read, bus = terms(found, banks)
    print(f"{sta[-1].name}/{CORNER}: {len(found)} paths of clk, {len(banks)} bank macros")
    if read is None:
        print("SOC_READ_PATH_NS: no path from a bank among the reported ones")
    else:
        rest, p = read
        print(
            f"SOC_READ_PATH_NS = {rest:.2f}  ({p.needed_ns:.3f} ns from {p.start} to {p.end}, "
            f"slack {p.slack_ns:+.3f}, less {IHP_1P_READ_NS[banks[p.start.split('/')[0]]]} ns)"
        )
    if bus is None:
        print("SOC_BUS_PATH_NS: no other path among the reported ones")
    else:
        print(
            f"SOC_BUS_PATH_NS = {bus.needed_ns:.2f}  (from {bus.start} to {bus.end}, "
            f"slack {bus.slack_ns:+.3f})"
        )
    if read is not None and bus is not None and banks:
        slowest = max(IHP_1P_READ_NS[c] for c in banks.values())
        period = max(read[0] + slowest, bus.needed_ns)
        print(f"the SoC's clock with these banks: {1000 / period:.1f} MHz ({period:.2f} ns)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
