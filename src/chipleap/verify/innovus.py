"""Cadence Innovus place-and-route scripts for a netlist from Genus or Design Compiler.

ChipLeap writes these next to the synthesis results so the mapped accelerator can go
straight into Innovus on a machine that has it; the open flow (LibreLane) is the one
ChipLeap runs itself. The scripts use Innovus's classic interface: an MMMC file for the
liberty and the constraints, ``init_design`` from the LEF files, a floorplan at the
given utilisation, ``place_opt_design``, clock tree synthesis, routing and post-route
timing, then reports and, when a layer map is given, a GDS.

    innovus -batch -no_gui -files innovus.tcl -log innovus
"""

from __future__ import annotations

from pathlib import Path


def mmmc(liberty: str, sdc: str) -> str:
    return f"""# ChipLeap: one typical view for the accelerator.
create_library_set -name typ -timing [list {liberty}]
create_constraint_mode -name func -sdc_files [list {sdc}]
create_delay_corner -name typ -library_set typ
create_analysis_view -name func_typ -constraint_mode func -delay_corner typ
set_analysis_view -setup [list func_typ] -hold [list func_typ]
"""


def script(
    netlist: str,
    lefs: tuple[str, ...],
    *,
    top: str = "chipleap_accel",
    power: str = "VDD",
    ground: str = "VSS",
    utilisation: float = 0.6,
    margin_um: float = 10.0,
    layer_map: str = "",
) -> str:
    """The Innovus run for ``netlist``: the technology LEF must come first in ``lefs``."""
    if not lefs:
        raise ValueError("Innovus needs the technology LEF and the cell LEFs")
    gds = (
        f"streamOut {top}.gds -mapFile {layer_map} -units 1000 -mode ALL\n"
        if layer_map
        else "# streamOut needs the PDK's layer map: set layer_map in the technology.\n"
    )
    m = f"{margin_um:g}"
    return f"""# ChipLeap: Innovus place and route of the synthesized accelerator.
#   innovus -batch -no_gui -files innovus.tcl -log innovus
set init_lef_file [list {" ".join(lefs)}]
set init_verilog {netlist}
set init_top_cell {top}
set init_pwr_net {power}
set init_gnd_net {ground}
set init_mmmc_file mmmc.tcl
init_design
floorPlan -r 1.0 {utilisation:g} {m} {m} {m} {m}
globalNetConnect {power} -type pgpin -pin {power} -inst * -verbose
globalNetConnect {ground} -type pgpin -pin {ground} -inst * -verbose
globalNetConnect {power} -type tiehi -inst *
globalNetConnect {ground} -type tielo -inst *
setPlaceMode -place_global_place_io_pins true
place_opt_design
ccopt_design
routeDesign
setAnalysisMode -analysisType onChipVariation
timeDesign -postRoute -outDir timing_postroute
timeDesign -postRoute -hold -outDir timing_postroute
report_area > innovus_area.rpt
verifyConnectivity -report innovus_connectivity.rpt
verify_drc -report innovus_drc.rpt
saveDesign {top}.enc
{gds}puts "CHIPLEAP_INNOVUS_DONE"
exit
"""


def write(
    workdir: Path,
    netlist: str,
    sdc: str,
    liberty: str,
    lefs: tuple[str, ...],
    **kwargs: object,
) -> Path:
    """Write ``mmmc.tcl`` and ``innovus.tcl`` into ``workdir``; returns the script."""
    (workdir / "mmmc.tcl").write_text(mmmc(liberty, sdc))
    path = workdir / "innovus.tcl"
    path.write_text(script(netlist, lefs, **kwargs))  # type: ignore[arg-type]
    return path


def ihp_lefs(liberty: Path) -> tuple[str, ...]:
    """The SG13G2 technology and standard-cell LEFs next to its liberty, if present."""
    lef_dir = liberty.parent.parent / "lef"
    tech, cells = lef_dir / "sg13g2_tech.lef", lef_dir / "sg13g2_stdcell.lef"
    return (str(tech), str(cells)) if tech.is_file() and cells.is_file() else ()
