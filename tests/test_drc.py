"""KLayout DRC reports, read one way by the chip step and by place and route: off-grid pin
markers inside the PDK's own macros are set apart, everything else counts."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipleap.verify import drc
from chipleap.verify.pnr import PnrError, klayout_drc, parse_run

BLDRV = "RM_IHPSG13_1P_BLDRV"
SRAM = "RM_IHPSG13_1P_256x32_c2_bm_bist"


def lyrdb(path: Path, items: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<report-database><items>"
        + "".join(f"<item><category>'{r}'</category><cell>{c}</cell></item>" for r, c in items)
        + "</items></report-database>"
    )
    return path


def test_drc_reports_are_counted_by_rule(tmp_path):
    db = lyrdb(tmp_path / "a.lyrdb", [("M1.a", "chip"), ("M1.a", "chip"), ("Pad.d1R", "chip")])
    r = drc.DrcResult()
    drc.read_lyrdb(db, r)
    assert r.violations == {"M1.a": 2, "Pad.d1R": 1} and r.total == 3 and r.markers == 0


def test_the_pdks_macro_cells_are_its_gds_tree_and_never_the_designs():
    held = drc.pdk_cells(
        [(SRAM, (SRAM, BLDRV, "RM_IHPSG13_1P_COLDRV13")), ("RM_SMALL", ("RM_SMALL", BLDRV))],
        design=("scratchpad",),
    )
    assert held[BLDRV] == (SRAM, "RM_SMALL") and held[SRAM] == (SRAM,)
    # A PDK macro whose GDS held a cell named like the design's top: never one of its own.
    assert "scratchpad" not in drc.pdk_cells([(SRAM, (SRAM, "scratchpad"))], ("scratchpad",))


def test_off_grid_pin_markers_inside_the_pdks_macros_are_set_apart(tmp_path):
    """IHP's SRAMs draw Metal1 pin markers off the 5 nm grid, in a cell the SRAM is made
    of; KLayout names the cell's variant after a colon. Only that rule family, only
    inside a PDK macro's tree, is set apart; the same rule elsewhere, or any other rule
    inside the macro, counts."""
    held = drc.pdk_cells([(SRAM, (SRAM, BLDRV))])
    db = lyrdb(
        tmp_path / "a.lyrdb",
        [
            ("metal1_pin_Offgrid", f"{BLDRV}:r0"),
            ("metal1_pin_Offgrid", f"{BLDRV}:m90"),
            ("metal2_pin_Offgrid", SRAM),
            ("metal1_pin_Offgrid", "scratchpad"),
            ("metal1_pin_Offgrid", "sg13g2_dfrbp_1"),
            ("M1.a", f"{BLDRV}:r0"),
            ("metal1_drw_Offgrid", BLDRV),
        ],
    )
    r = drc.DrcResult()
    drc.read_lyrdb(db, r, held)
    assert r.pin_markers == {("metal1_pin_Offgrid", BLDRV): 2, ("metal2_pin_Offgrid", SRAM): 1}
    assert r.violations == {"metal1_pin_Offgrid": 2, "M1.a": 1, "metal1_drw_Offgrid": 1}
    assert r.markers_data()[0] == {
        "rule": "metal1_pin_Offgrid",
        "cell": BLDRV,
        "macros": [SRAM],
        "count": 2,
    }
    assert drc.describe_markers(r.markers_data()) == (
        "3 pin markers off the grid inside the PDK's own macros, not counted: "
        f"2 metal1_pin_Offgrid in {BLDRV}, 1 metal2_pin_Offgrid in {SRAM}"
    )
    plain = drc.DrcResult()
    drc.read_lyrdb(db, plain)
    assert plain.total == 7 and not plain.pin_markers


def test_a_report_that_is_no_database_fails(tmp_path):
    bad = tmp_path / "a.lyrdb"
    bad.write_text("<report-database><items><item>")
    with pytest.raises(drc.DrcReportError, match="not a report database"):
        drc.read_lyrdb(bad, drc.DrcResult())


def test_place_and_route_reads_the_runs_klayout_report_the_same_way(tmp_path):
    run = tmp_path / "runs" / "chipleap"
    held = drc.pdk_cells([(SRAM, (SRAM, BLDRV))], ("scratchpad",))
    marker = ("metal1_pin_Offgrid", f"{BLDRV}:r0")
    lyrdb(run / "60-klayout-drc" / "reports" / "drc.klayout.lyrdb", [("M1.a", "scratchpad")])
    report = lyrdb(run / "66-klayout-drc" / "reports" / "drc.klayout.lyrdb", [marker] * 8)
    metrics = {"klayout__drc_error__count": 8, "route__drc_errors": 0}
    # The last KLayout DRC of the run is the one read.
    r = parse_run(metrics, 50.0, None, run, held)
    assert r.drc_errors == 0 and r.clean
    assert r.pin_markers == [
        {"rule": "metal1_pin_Offgrid", "cell": BLDRV, "macros": [SRAM], "count": 8}
    ]
    # The report must hold what LibreLane counted.
    with pytest.raises(PnrError, match="counted 9 violations, its report holds 8"):
        klayout_drc({"klayout__drc_error__count": 9}, report, held)
    with pytest.raises(PnrError, match="left no report"):
        klayout_drc(metrics, None, held)
    # A clean run is not read at all; routing and Magic still count as they did.
    clean = parse_run({"route__drc_errors": 2}, 50.0, None, tmp_path / "none", held)
    assert clean.drc_errors == 2 and not clean.pin_markers
