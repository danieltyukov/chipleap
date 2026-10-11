"""Place and route of the accelerator with Cadence Innovus, here or on a design server.

The commercial counterpart of the LibreLane step (``chipleap.verify.pnr``), with the same
inputs and the same result: Genus maps the accelerator with 16-word buffers (in silicon
they are SRAM macros) to the technology's typical-corner library, then Innovus reads the
LEF files and both corners, makes a floorplan at the target utilisation, a power ring and
stripes on the two top routing layers, places, builds the clock tree, routes, repairs
setup and hold after routing, inserts fillers and checks the result: connectivity, design
rules (from the LEF) and process antennas. Timing is reported at both corners.

The power grid is sized from the technology's own layer rules (read in Innovus with
``dbGet``), so one script serves any PDK; only the aluminium redistribution layer is left
out of the grid. Without a Quantus technology file, Innovus extracts parasitics from the
resistance and capacitance the LEF gives each layer.

    innovus -batch -no_gui -files pnr.tcl -log innovus
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from chipleap.gen.rtl import accel_sv
from chipleap.hw.design import Accel
from chipleap.hw.tech import Technology
from chipleap.verify.csynth import TOP, genus_script, run_params, specialize, stage
from chipleap.verify.pnr import PNR_WORDS, PnrError, PnrResult
from chipleap.verify.remote import Remote, RemoteError, Step, run_steps

TAG = f"w{PNR_WORDS}"
DONE = "CHIPLEAP_INNOVUS_DONE"


def mmmc(typ: str, slow: str, sdc: str) -> str:
    """Analysis views: setup and hold at the typical corner, and at the slow one if given."""
    lines = [
        "# ChipLeap: analysis views for the accelerator.",
        f"create_library_set -name typ -timing [list {typ}]",
        f"create_constraint_mode -name func -sdc_files [list {sdc}]",
        "create_delay_corner -name typ -library_set typ",
        "create_analysis_view -name func_typ -constraint_mode func -delay_corner typ",
    ]
    views = "func_typ"
    if slow:
        lines += [
            f"create_library_set -name slow -timing [list {slow}]",
            "create_delay_corner -name slow -library_set slow",
            "create_analysis_view -name func_slow -constraint_mode func -delay_corner slow",
        ]
        views = "func_typ func_slow"
    lines.append(f"set_analysis_view -setup [list {views}] -hold [list {views}]")
    return "\n".join(lines) + "\n"


def script(
    netlist: str,
    lefs: tuple[str, ...],
    *,
    node_nm: int,
    utilisation: float = 0.6,
    slow: bool = True,
    power: str = "VDD",
    ground: str = "VSS",
    top: str = TOP,
) -> str:
    """The Innovus run: floorplan to checks, with the measurements written to a file."""
    if not lefs:
        raise PnrError("Innovus needs the technology LEF and the cell LEFs")
    views = ["func_typ", "func_slow"] if slow else ["func_typ"]
    timing = "\n".join(
        f"report_timing -late -view {v} -max_paths 1 > setup_{v}.rpt\n"
        f"report_timing -early -view {v} -max_paths 1 > hold_{v}.rpt"
        for v in views
    )
    return f"""# ChipLeap: Innovus place and route of the synthesized {top}.
#   innovus -batch -no_gui -files pnr.tcl -log innovus
set init_lef_file [list {" ".join(lefs)}]
set init_verilog {netlist}
set init_top_cell {top}
set init_pwr_net {power}
set init_gnd_net {ground}
set init_mmmc_file mmmc.tcl
init_design
setMultiCpuUsage -localCpu 4
# Libraries without a plain set-and-reset flip-flop (IHP SG13G2) leave Genus a scan flop
# for registers that reset to 1. There is no scan chain to stitch in this block, so the
# placer's "scan chains not defined" notice says nothing about the result.
suppressMessage IMPSP-9099
setDesignMode -process {node_nm}

# The two top routing layers carry the power grid, the aluminium redistribution layer
# (AP, RDL) aside; widths and spacings follow each layer's own rules.
set use {{}}
foreach l [dbGet [dbGet -p head.layers.type routing].name] {{
    if {{![regexp -nocase {{^(AP|RDL|PAD|LB)$}} $l]}} {{ lappend use $l }}
}}
set top [lindex $use end]
set below [lindex $use end-1]
if {{[dbGet [dbGet -p head.layers.name $top].direction] eq "Horizontal"}} {{
    set h $top; set v $below
}} else {{
    set h $below; set v $top
}}
proc rule {{layer attr}} {{ return [lindex [dbGet [dbGet -p head.layers.name $layer].$attr] 0] }}
set w [expr {{max(2.0, 4 * max([rule $h minWidth], [rule $v minWidth]))}}]
set s [expr {{max(1.0, 3 * max([rule $h minSpacing], [rule $v minSpacing]))}}]
set margin [expr {{2 * $w + 2 * $s + 4.0}}]
floorPlan -r 1.0 {utilisation:g} $margin $margin $margin $margin

globalNetConnect {power} -type pgpin -pin {power} -inst * -verbose
globalNetConnect {ground} -type pgpin -pin {ground} -inst * -verbose
globalNetConnect {power} -type tiehi -inst *
globalNetConnect {ground} -type tielo -inst *
addRing -nets {{{power} {ground}}} -type core_rings -follow core \\
    -layer [list top $h bottom $h left $v right $v] -width $w -spacing $s -offset 2.0
addStripe -nets {{{power} {ground}}} -layer $v -direction vertical -width $w -spacing $s \\
    -set_to_set_distance [expr {{20 * $w + 2 * $s}}] -start_from left -start_offset 20
sroute -connect {{corePin}} -nets {{{power} {ground}}} -allowJogging 1 -allowLayerChange 1

setPlaceMode -place_global_place_io_pins true -place_global_ignore_scan true
place_opt_design

# Clock-tree cells: some libraries' buffers are not picked up automatically, so name them,
# clock buffers and inverters first (CKBD4, CKND4), plain ones otherwise (buf_4, inv_4),
# never a dont_use cell and never a clock NAND (CKND2D1) for an inverter.
set ckbuf {{}}
set ckinv {{}}
set buf {{}}
set inv {{}}
foreach c [dbGet head.libCells] {{
    if {{[dbGet $c.dontUse] == 1}} {{ continue }}
    set n [dbGet $c.name]
    if {{[regexp -nocase {{^CKBD[0-9]+[A-Z]*$}} $n]}} {{ lappend ckbuf $n }}
    if {{[regexp -nocase {{^CKND[0-9]+[A-Z]*$}} $n]}} {{ lappend ckinv $n }}
    if {{[regexp -nocase {{(^BUFFD[0-9]+|_buf_[0-9]+)}} $n]}} {{ lappend buf $n }}
    if {{[regexp -nocase {{(^INVD[0-9]+|_inv_[0-9]+)}} $n]}} {{ lappend inv $n }}
}}
if {{[llength $ckbuf] == 0}} {{ set ckbuf $buf }}
if {{[llength $ckinv] == 0}} {{ set ckinv $inv }}
if {{[llength $ckbuf] > 0}} {{ set_ccopt_property buffer_cells $ckbuf }}
if {{[llength $ckinv] > 0}} {{ set_ccopt_property inverter_cells $ckinv }}
ccopt_design
routeDesign
setAnalysisMode -analysisType onChipVariation
optDesign -postRoute
optDesign -postRoute -hold

# Fillers keep the wells continuous; whatever the library calls them (-e: an empty list,
# not 0x0, when nothing matches).
set fills [dbGet -e [dbGet -e -p head.libCells.name *FILL*].name]
if {{[llength $fills] == 0}} {{ set fills [dbGet -e [dbGet -e -p head.libCells.name *fill*].name] }}
if {{[llength $fills] > 0}} {{
    addFiller -cell $fills -prefix CHIPLEAP_FILL
    ecoRoute -target
}}

{timing}
verifyConnectivity -report connectivity.rpt
verify_drc -report drc.rpt
verifyProcessAntenna -report antenna.rpt

proc box_area {{b}} {{
    lassign [lindex $b 0] x0 y0 x1 y1
    return [expr {{($x1 - $x0) * ($y1 - $y0)}}]
}}
set cells 0
set area 0.0
foreach inst [dbGet top.insts] {{
    set cell [dbGet $inst.cell]
    if {{[dbGet $cell.baseClass] ne "core"}} {{ continue }}
    if {{[regexp -nocase {{fill|decap|tap|endcap}} [dbGet $cell.name]]}} {{ continue }}
    incr cells
    set area [expr {{$area + [dbGet $cell.size_x] * [dbGet $cell.size_y]}}]
}}
set f [open chipleap_pnr.txt w]
puts $f "die_um2 [box_area [dbGet top.fPlan.box]]"
puts $f "core_um2 [box_area [dbGet top.fPlan.coreBox]]"
puts $f "cell_um2 $area"
puts $f "cells $cells"
puts $f "power_layers $h $v"
close $f
saveDesign {top}.enc
puts "{DONE}"
exit
"""


_NUM = r"(-?\d+(?:\.\d+)?)"


def parse_slack(text: str) -> float:
    """The slack of the worst path in an Innovus report_timing, in ns."""
    m = re.search(r"=\s*Slack Time\s+" + _NUM, text) or re.search(r"Slack Time\s+" + _NUM, text)
    if not m:
        raise PnrError("no slack in the Innovus timing report")
    return float(m.group(1))


def parse_violations(text: str) -> int:
    """Violations in an Innovus verify_drc, verifyConnectivity or antenna report."""
    if re.search(
        r"No (DRC )?violations? (were )?found|No Violations Found|Found no problems|0 Viols",
        text,
        re.I,
    ):
        return 0
    m = re.search(r"Total (?:Violations|Viols)\s*:\s*(\d+)", text, re.I) or re.search(
        r"Found\s+(\d+)\s+(?:violations?|problems?)", text, re.I
    )
    if m:
        return int(m.group(1))
    m = re.search(r"Verification Complete\s*:\s*(\d+)\s*Viols", text, re.I)
    if m:
        return int(m.group(1))
    # A report with violation entries but no total: count the entries.
    return len(re.findall(r"^\s*(?:Violation|Viol)\b", text, re.I | re.M))


def parse_metrics(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            out[key] = value.strip()
    return out


def read(workdir: Path, name: str) -> str:
    p = workdir / name
    return p.read_text(errors="replace") if p.is_file() else ""


def result(workdir: Path, clock_mhz: float, slow: bool) -> PnrResult:
    """The run's measurements, in the same shape as the LibreLane step's."""
    if DONE not in read(workdir, "innovus.out") and not (workdir / "chipleap_pnr.txt").is_file():
        tail = (read(workdir, "innovus.out") or read(workdir, "genus.out"))[-1500:]
        raise PnrError(f"Innovus did not finish:\n{tail}")
    m = parse_metrics(read(workdir, "chipleap_pnr.txt"))
    die, core, area = (float(m.get(k, "0")) for k in ("die_um2", "core_um2", "cell_um2"))
    setup_typ = parse_slack(read(workdir, "setup_func_typ.rpt"))
    hold = parse_slack(read(workdir, "hold_func_typ.rpt"))
    setup_worst = setup_typ
    if slow:
        setup_worst = min(setup_typ, parse_slack(read(workdir, "setup_func_slow.rpt")))
        hold = min(hold, parse_slack(read(workdir, "hold_func_slow.rpt")))
    drc = parse_violations(read(workdir, "drc.rpt"))
    conn = parse_violations(read(workdir, "connectivity.rpt"))
    ant = parse_violations(read(workdir, "antenna.rpt"))
    return PnrResult(
        clock_mhz=clock_mhz,
        die_um2=die,
        core_um2=core,
        cell_um2=area,
        utilisation=area / core if core else 0.0,
        cells=int(m.get("cells", "0")),
        setup_slack_ns=setup_worst,
        setup_slack_typ_ns=setup_typ,
        hold_slack_ns=hold,
        drc_errors=drc,
        lvs_errors=conn,  # Innovus checks connectivity against the netlist, not LVS
        antenna_nets=ant,
        power_mw=0.0,
        gds=None,
        metrics={k: v for k, v in m.items()},
    )


def slow_liberty(tech: Technology, typ: str) -> str:
    """The slow-corner library: the technology's own, or IHP's next to its typical one."""
    if tech.liberty_slow:
        return tech.liberty_slow
    candidate = typ.replace("typ_1p20V_25C", "slow_1p08V_125C")
    return candidate if candidate != typ and Path(candidate).is_file() else ""


def place_and_route(
    accel: Accel,
    tech: Technology,
    typ_liberty: str,
    lefs: tuple[str, ...],
    workdir: Path,
    clock_mhz: float,
    remote: Remote | None = None,
    utilisation: float = 0.6,
    timeout: float = 14400,
) -> PnrResult:
    """Genus then Innovus on the accelerator, here or on ``remote``."""
    if remote is None and (shutil.which("genus") is None or shutil.which("innovus") is None):
        raise PnrError(
            "Cadence Genus and Innovus are not on the path; run them on a server with --remote HOST"
        )
    if not lefs:
        raise PnrError(f"technology {tech.key!r} names no LEF files; add them with tech add --lef")
    workdir.mkdir(parents=True, exist_ok=True)
    for old in ("chipleap_pnr.txt", "innovus.out", "genus.out"):
        (workdir / old).unlink(missing_ok=True)
    try:
        typ = stage(typ_liberty, workdir, remote)
        slow_src = slow_liberty(tech, typ_liberty)
        slow = stage(slow_src, workdir, remote) if slow_src else ""
        phys = tuple(stage(p, workdir, remote) for p in lefs)
    except Exception as exc:
        raise PnrError(str(exc)) from exc
    rtl = f"{TOP}_{TAG}.sv"
    (workdir / rtl).write_text(specialize(accel_sv(), run_params(accel, PNR_WORDS)))
    period = 1000.0 / clock_mhz
    (workdir / "synth.tcl").write_text(genus_script(rtl, typ, period, TAG, phys))
    (workdir / "mmmc.tcl").write_text(mmmc(typ, slow, f"{TOP}_{TAG}.sdc"))
    (workdir / "pnr.tcl").write_text(
        script(
            f"{TOP}_{TAG}.v", phys, node_nm=tech.node_nm, utilisation=utilisation, slow=bool(slow)
        )
    )
    steps = [
        Step(("genus", "-batch", "-no_gui", "-files", "synth.tcl", "-log", "genus"), "genus.out"),
        Step(
            ("innovus", "-batch", "-no_gui", "-files", "pnr.tcl", "-log", "innovus"),
            "innovus.out",
        ),
    ]
    try:
        run_steps(steps, workdir, remote, timeout)
    except RemoteError as exc:
        raise PnrError(str(exc)) from exc
    return result(workdir, clock_mhz, bool(slow))
