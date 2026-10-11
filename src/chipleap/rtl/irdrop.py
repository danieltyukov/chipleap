"""Static IR drop on the routed power grid with OpenROAD's PDNSim.

OpenROAD reads the routed DEF, which holds the power grid, with the corner's liberty,
the constraints, the parasitics and the activity of the project's tests (the power
step's VCD of the same netlist, else a new gate-level run, else a vectorless default).
Each supply net the DEF marks as power or ground is set to the corner's voltage or to
zero and analysed; PDNSim writes the voltage at every cell's supply pin. ChipLeap reports
the worst and average drop per net and draws the drop over the die as an SVG, binned so
the picture stays small for any design.

Net names come from the DEF and stay Tcl data: the script loops over the database's
nets and never puts a name into its own text.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chipleap.rtl import builddir
from chipleap.rtl import quote as q
from chipleap.rtl.openroad import (
    Job,
    OpenroadError,
    OpenroadUnavailable,
    failure,
    pdk_views,
    read_limited,
    routed_views,
)
from chipleap.rtl.power import activity
from chipleap.rtl.project import CORNERS, Project
from chipleap.verify.result import StepResult

BINS = 48  # most map cells along the longer side
ROW_UM = 3.78  # IHP SG13G2's standard-cell row
MAP_PX = 360  # the map's longer side
WORST_SHOWN = 5
# The physical-only cells LibreLane names: fill, decap, well tap and row ends.
PHYSICAL_RE = re.compile(r"^(FILLER|DECAP|TAP|PHY|WELLTAP|ENDCAP)_")
# A sequential palette from light to dark, which reads in both light and dark themes.
PALETTE = ("#fde725", "#5ec962", "#21918c", "#3b528b", "#440154")


class IrDropError(RuntimeError):
    pass


@dataclass
class NetDrop:
    name: str
    kind: str  # POWER | GROUND
    supply_v: float
    worst_v: float  # worst drop, V
    average_v: float
    percent: float
    points: list[tuple[str, float, float, float]] = field(default_factory=list)  # inst, x, y, drop

    @property
    def worst_instances(self) -> list[tuple[str, float]]:
        """The logic cells with the worst drop; fill, tap and edge cells draw no current."""
        seen: dict[str, float] = {}
        for inst, _, _, d in self.points:
            if not PHYSICAL_RE.match(inst):
                seen[inst] = max(d, seen.get(inst, 0.0))
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:WORST_SHOWN]


def corner_voltage(corner: str) -> float:
    """The supply of a corner from IHP's liberty name: typ_1p20V_25C is 1.20 V."""
    m = re.search(r"(\d+)p(\d+)V", CORNERS[corner])
    if not m:
        raise IrDropError(f"no voltage in the corner name {CORNERS[corner]}")
    return float(f"{m.group(1)}.{m.group(2)}")


def ir_script(
    project: Project,
    files: dict[str, Any],
    corner: str,
    def_: str,
    sdc: str,
    spef: str | None,
    vcd: str | None,
    scope: str,
) -> str:
    voltage = corner_voltage(corner)
    lines = [
        f"read_lef {q.tcl(files['tech_lef'])}",
        *(f"read_lef {q.tcl(f)}" for f in files["cell_lefs"]),
        f"read_liberty {q.tcl(files['liberty'][corner])}",
        f"read_def {q.tcl(def_)}",
        f"read_sdc {q.tcl(sdc)}",
    ]
    if spef:
        lines.append(f"read_spef {q.tcl(spef)}")
    lines += [
        "set_propagated_clock [all_clocks]",
        f"set_power_activity -input -activity {q.number(float(project.power.input_activity))}",
    ]
    if vcd:
        lines.append(f"read_vcd -scope {q.tcl(scope)} {q.tcl(vcd)}")
    lines += [
        "set i 0",
        "foreach net [[ord::get_db_block] getNets] {",
        "    set kind [$net getSigType]",
        '    if {$kind ne "POWER" && $kind ne "GROUND"} { continue }',
        "    set name [$net getName]",
        '    puts "CHIPLEAP-IR net $i $kind $name"',
        f'    set_pdnsim_net_voltage -net $name -voltage [expr {{$kind eq "POWER" ? {q.number(voltage)} : 0}}]',
        '    analyze_power_grid -net $name -voltage_file "ir_$i.csv"',
        "    incr i",
        "}",
        'puts "CHIPLEAP-IR end"',
    ]
    return "\n".join(lines) + "\n"


def parse_ir(log: str) -> list[tuple[int, str, str, dict[str, float]]]:
    """Per analysed net: its index, kind, name and PDNSim's report (supply, worst voltage,
    average and worst drop in V, percentage)."""
    out: list[tuple[int, str, str, dict[str, float]]] = []
    keys = {
        "Supply voltage": "supply",
        "Worstcase voltage": "worst_voltage",
        "Average IR drop": "average",
        "Worstcase IR drop": "worst",
        "Percentage drop": "percent",
    }
    for line in log.splitlines():
        m = re.match(r"\s*CHIPLEAP-IR net (\d+) (POWER|GROUND) (\S+)\s*$", line)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3), {}))
            continue
        m = re.match(r"\s*([A-Za-z ]+?)\s*:\s*([-+.\deE]+)\s*(V|%)", line)
        if m and out and m.group(1) in keys:
            try:
                out[-1][3][keys[m.group(1)]] = float(m.group(2))
            except ValueError:
                continue
    return out


def parse_voltages(text: str, kind: str, supply: float) -> list[tuple[str, float, float, float]]:
    """PDNSim's voltage file as (instance, x, y, drop in V)."""
    rows = csv.reader(io.StringIO(text))
    header = next(rows, None)
    if header is None or len(header) < 6:
        return []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            x, y, v = float(row[3]), float(row[4]), float(row[5])
        except ValueError:
            continue
        drop = supply - v if kind == "POWER" else v
        out.append((row[0], x, y, max(drop, 0.0)))
    return out


def _colour(t: float) -> str:
    """The palette at ``t`` in [0, 1], interpolated."""
    t = min(max(t, 0.0), 1.0) * (len(PALETTE) - 1)
    i = min(int(t), len(PALETTE) - 2)
    a, b = PALETTE[i], PALETTE[i + 1]
    f = t - i
    rgb = [round(int(a[k : k + 2], 16) * (1 - f) + int(b[k : k + 2], 16) * f) for k in (1, 3, 5)]
    return "#" + "".join(f"{c:02x}" for c in rgb)


def voltage_map_svg(net: NetDrop, die: tuple[float, float, float, float] | None = None) -> str:
    """The drop over the die: each bin coloured by the worst drop in it, with a scale."""
    if not net.points:
        return ""
    xs = [p[1] for p in net.points]
    ys = [p[2] for p in net.points]
    x0, y0, x1, y1 = die or (min(xs), min(ys), max(xs), max(ys))
    w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
    # Supply pins sit on the cell rows, so a bin spans two rows or more and no row of
    # bins falls between them.
    n = min(BINS, max(8, round(max(w, h) / (2 * ROW_UM))))
    nx = n if w >= h else max(1, round(n * w / h))
    ny = n if h > w else max(1, round(n * h / w))
    grid: dict[tuple[int, int], float] = {}
    for _, x, y, d in net.points:
        i = min(nx - 1, max(0, int((x - x0) / w * nx)))
        j = min(ny - 1, max(0, int((y - y0) / h * ny)))
        grid[i, j] = max(d, grid.get((i, j), 0.0))
    worst = max(grid.values()) or 1e-12
    cell = max(4, MAP_PX // max(nx, ny))
    pad, legend = 4, 40
    width, height = nx * cell + 2 * pad, ny * cell + 2 * pad + legend
    title = f"{net.name}: drop up to {worst * 1000:.3g} mV"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" shape-rendering="crispEdges" '
        f'width="{width}" height="{height}" role="img" aria-label="{_attr(title)}">',
        f"<title>{_attr(title)}</title>",
        f'<rect x="{pad}" y="{pad}" width="{nx * cell}" height="{ny * cell}" '
        'fill="none" stroke="#888888" stroke-opacity="0.5"/>',
    ]
    for (i, j), d in sorted(grid.items()):
        # Rows go up the die: the SVG's y grows downwards.
        parts.append(
            f'<rect x="{pad + i * cell}" y="{pad + (ny - 1 - j) * cell}" width="{cell}" '
            f'height="{cell}" fill="{_colour(d / worst)}"/>'
        )
    bar_y = pad + ny * cell + 10
    steps = 20
    bar_w = max(nx * cell - 110, 40)
    for k in range(steps):
        parts.append(
            f'<rect x="{pad + 40 + k * bar_w / steps:.1f}" y="{bar_y}" width="{bar_w / steps + 0.5:.1f}" '
            f'height="8" fill="{_colour(k / (steps - 1))}"/>'
        )
    parts += [
        f'<text x="{pad}" y="{bar_y + 9}" font-size="12" fill="#888888">0 mV</text>',
        f'<text x="{pad + 46 + bar_w}" y="{bar_y + 9}" font-size="12" fill="#888888">'
        f"{worst * 1000:.3g} mV</text>",
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def _attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def die_area(def_text: str) -> tuple[float, float, float, float] | None:
    """The die from a DEF header, in um."""
    units = re.search(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)", def_text)
    die = re.search(
        r"DIEAREA\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)\s+\(\s*(-?\d+)\s+(-?\d+)\s*\)", def_text
    )
    if not units or not die:
        return None
    scale = float(units.group(1))
    x0, y0, x1, y1 = (float(die.group(i)) / scale for i in range(1, 5))
    return x0, y0, x1, y1


@dataclass
class IrResult:
    corner: str
    nets: list[NetDrop]
    activity: str  # what the currents come from
    maps: dict[str, Path]


def analyze(project: Project) -> IrResult:
    cfg = project.ir_drop
    views = routed_views(project)
    pdk = pdk_views(project, views.run)
    work = project.work_dir("ir_drop", empty=True)
    act = activity(project, views.netlist, pdk, True, views.sealed)
    job = Job(work, "pdnsim")
    files = pdk.stage_all(job, (cfg.corner,))
    def_ = job.stage(views.def_, sealed=views.sealed)
    sdc = job.stage(views.sdc, sealed=views.sealed)
    spef = job.stage(views.spef, sealed=views.sealed) if views.spef else None
    vcd = job.stage(act[0], sealed=act[2]) if act else None
    log, code = job.run(
        ir_script(project, files, cfg.corner, def_, sdc, spef, vcd, act[1] if act else "")
    )
    if code != 0 or "CHIPLEAP-IR end" not in log:
        raise failure("OpenROAD IR drop analysis", log, code)
    parsed = parse_ir(log)
    if not parsed:
        raise IrDropError("the routed DEF has no power or ground nets")
    supply = corner_voltage(cfg.corner)
    head = views.read_text(views.def_)[: 1 << 16]
    die = die_area(head)
    nets, maps = [], {}
    for index, kind, name, rep in parsed:
        if "worst" not in rep:
            raise IrDropError(f"PDNSim reported nothing for {name}")
        csv_file = job.output(f"ir_{index}.csv")
        points = (
            parse_voltages(read_limited(csv_file), kind, supply if kind == "POWER" else 0.0)
            if csv_file.is_file()
            else []
        )
        net = NetDrop(
            name=name,
            kind=kind,
            supply_v=rep.get("supply", supply if kind == "POWER" else 0.0),
            worst_v=rep["worst"],
            average_v=rep.get("average", 0.0),
            percent=rep.get("percent", 0.0),
            points=points,
        )
        nets.append(net)
        svg = voltage_map_svg(net, die)
        if svg:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
            maps[name] = builddir.write_text(work, f"{safe}.svg", svg)
    source = (
        "the tests' VCD"
        if act
        else f"a vectorless input activity of {project.power.input_activity:g}"
    )
    return IrResult(cfg.corner, nets, source, maps)


def ir_data(project: Project, r: IrResult) -> dict[str, Any]:
    return {
        "tool": "OpenROAD PDNSim",
        "corner": r.corner,
        "supply_v": corner_voltage(r.corner),
        "activity": r.activity,
        "nets": [
            {
                "net": n.name,
                "kind": n.kind.lower(),
                "worst_mv": round(n.worst_v * 1000, 4),
                "average_mv": round(n.average_v * 1000, 4),
                "worst_pct": round(n.percent, 3),
                "worst_instances": [
                    {"instance": i, "drop_mv": round(d * 1000, 4)} for i, d in n.worst_instances
                ],
                "map": project.rel(r.maps[n.name]) if n.name in r.maps else None,
            }
            for n in r.nets
        ],
        "max_drop_pct": project.ir_drop.max_drop_pct,
    }


def ir_drop(project: Project) -> StepResult:
    """The IR drop step: worst drop per supply net on the routed grid at a corner."""
    try:
        r = analyze(project)
    except OpenroadUnavailable as exc:
        return StepResult("ir_drop", "skipped", str(exc))
    except (IrDropError, OpenroadError, ValueError) as exc:
        return StepResult("ir_drop", "fail", str(exc).splitlines()[0], {"error": str(exc)})
    data = ir_data(project, r)
    limit = project.ir_drop.max_drop_pct
    nets = ", ".join(
        f"{n['net']} {n['worst_mv']:.3g} mV ({n['worst_pct']:.2g}%)" for n in data["nets"]
    )
    worst = max(data["nets"], key=lambda n: n["worst_pct"])
    status = "fail" if limit is not None and worst["worst_pct"] > limit else "pass"
    msg = (
        f"worst IR drop at the {r.corner} corner ({data['supply_v']:g} V), currents from "
        f"{r.activity}: {nets}"
    )
    if limit is not None:
        msg += f"; {'over' if status == 'fail' else 'within'} the {limit:g}% limit"
    return StepResult("ir_drop", status, msg, data)
