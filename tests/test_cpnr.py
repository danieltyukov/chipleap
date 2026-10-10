"""Place and route with Cadence Innovus: scripts, report parsing and the step's wiring."""

import shutil
from pathlib import Path

import pytest

from chipleap.cli import main
from chipleap.hw import Accel, Design
from chipleap.hw.tech import get_tech, tech_from_dict, tech_to_dict
from chipleap.verify import backends, cpnr
from chipleap.verify.pnr import PnrError
from chipleap.verify.remote import Remote

ACCEL = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)

SETUP = """Path 1: MET Setup Check with Pin g_gemv.psum_q_reg[0][15]/CLK
Analysis View: func_typ
Other End Arrival Time          0.000
- Setup                         0.100
+ Phase Shift                  10.000
= Required Time                 9.900
- Arrival Time                  7.400
= Slack Time                    2.500
"""
HOLD = """Path 1: MET Hold Check with Pin fifo_rp_reg[0][0]/CLK
Analysis View: func_slow
  Slack Time                    0.050
"""


def test_mmmc_has_both_corners_when_given():
    text = cpnr.mmmc("typ.lib", "slow.lib", "top.sdc")
    assert "func_typ" in text and "func_slow" in text and "slow.lib" in text
    assert "set_analysis_view -setup [list func_typ func_slow]" in text
    one = cpnr.mmmc("typ.lib", "", "top.sdc")
    assert "func_slow" not in one and "set_analysis_view -setup [list func_typ]" in one


def test_script_covers_the_flow_and_checks():
    s = cpnr.script("net.v", ("tech.lef", "cells.lef"), node_nm=130, utilisation=0.55)
    for needle in (
        "init_design",
        "floorPlan -r 1.0 0.55",
        "addRing",
        "addStripe",
        "sroute",
        "place_opt_design",
        "ccopt_design",
        "routeDesign",
        "optDesign -postRoute -hold",
        "addFiller",
        "verifyConnectivity -report connectivity.rpt",
        "verify_drc -report drc.rpt",
        "verifyProcessAntenna -report antenna.rpt",
        "report_timing -late -view func_slow",
        cpnr.DONE,
    ):
        assert needle in s, needle
    # The grid is sized from the PDK's own rules, so no layer names are hard-coded.
    assert "Metal" not in s and "dbGet -e" in s
    assert "func_slow" not in cpnr.script("net.v", ("t.lef",), node_nm=40, slow=False)
    with pytest.raises(PnrError):
        cpnr.script("net.v", (), node_nm=40)


def test_report_parsing():
    assert cpnr.parse_slack(SETUP) == pytest.approx(2.5)
    assert cpnr.parse_slack(HOLD) == pytest.approx(0.05)
    with pytest.raises(PnrError):
        cpnr.parse_slack("no timing here")
    assert cpnr.parse_violations("#  Command: verify_drc\n\nNo DRC violations were found\n") == 0
    assert cpnr.parse_violations("Begin Summary\n    Found no problems or warnings.\n") == 0
    assert cpnr.parse_violations("No Violations Found") == 0
    assert cpnr.parse_violations("Total Violations : 7 Viols.") == 7
    assert cpnr.parse_violations("Begin Summary\n  Found 3 problems or warnings.\n") == 3
    assert cpnr.parse_metrics("die_um2 10.5\ncells 42\n") == {"die_um2": "10.5", "cells": "42"}


def _write_run(d: Path, slow: bool = True) -> None:
    (d / "innovus.out").write_text(f"...\n{cpnr.DONE}\n")
    (d / "chipleap_pnr.txt").write_text(
        "die_um2 600000\ncore_um2 500000\ncell_um2 300000\ncells 15000\n"
    )
    (d / "setup_func_typ.rpt").write_text(SETUP)
    (d / "hold_func_typ.rpt").write_text(HOLD.replace("0.050", "0.200"))
    if slow:
        (d / "setup_func_slow.rpt").write_text(SETUP.replace("2.500", "-0.500"))
        (d / "hold_func_slow.rpt").write_text(HOLD)
    (d / "drc.rpt").write_text("No DRC violations were found\n")
    (d / "connectivity.rpt").write_text("Found no problems or warnings.\n")
    (d / "antenna.rpt").write_text("No Violations Found\n")


def test_result_shape_matches_the_librelane_step(tmp_path):
    _write_run(tmp_path)
    r = cpnr.result(tmp_path, 100.0, slow=True)
    assert r.core_um2 == 500000 and r.cells == 15000 and r.utilisation == pytest.approx(0.6)
    assert r.setup_slack_typ_ns == pytest.approx(2.5) and r.setup_slack_ns == pytest.approx(-0.5)
    assert r.hold_slack_ns == pytest.approx(0.05) and r.clean and r.antenna_nets == 0
    assert r.fmax_mhz == pytest.approx(1000 / 7.5) and r.fmax_worst_mhz == pytest.approx(
        1000 / 10.5
    )
    (tmp_path / "innovus.out").write_text("**ERROR: something\n")
    (tmp_path / "chipleap_pnr.txt").unlink()
    with pytest.raises(PnrError, match="did not finish"):
        cpnr.result(tmp_path, 100.0, slow=True)


def test_slow_corner_library():
    ihp = get_tech("ihp-sg13g2")
    lib = "/nowhere/sg13g2_stdcell_typ_1p20V_25C.lib"
    assert cpnr.slow_liberty(ihp, lib) == ""  # only when the file is there
    mine = tech_from_dict({**tech_to_dict(ihp), "key": "x40", "liberty_slow": "/srv/wc.lib"})
    assert cpnr.slow_liberty(mine, "/srv/tc.lib") == "/srv/wc.lib"


def test_place_route_refuses_what_it_cannot_do(tmp_path):
    design = Design(accel=ACCEL)
    with pytest.raises(PnrError, match="no accelerator"):
        backends.place_route_with(Design(accel=None), tmp_path)
    remote_tech = tech_from_dict(
        {**tech_to_dict(get_tech("ihp-sg13g2")), "key": "srv40", "liberty": "/srv/tc.lib"}
        | {"host": "eda-server", "lef": ["/srv/cells.lef"]}
    )
    from chipleap.hw.tech import TECHNOLOGIES

    TECHNOLOGIES["srv40"] = remote_tech
    try:
        with pytest.raises(PnrError, match="--remote eda-server"):
            backends.place_route_with(design, tmp_path, None, "srv40")
        with pytest.raises(PnrError, match="--remote eda-server"):
            backends.place_route_with(design, tmp_path, Remote("other-host"), "srv40")
    finally:
        del TECHNOLOGIES["srv40"]
    if shutil.which("genus") is None:
        with pytest.raises(PnrError, match="not on the path"):
            cpnr.place_and_route(ACCEL, get_tech("ihp-sg13g2"), "x.lib", ("t.lef",), tmp_path, 100)


def test_cli_offers_innovus_and_mcp_checks_it(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["verify", str(tmp_path), "--pnr-tool", "innovus; rm -rf ~"])
    assert "invalid choice" in capsys.readouterr().err
    assert backends.PNR_TOOLS == ("librelane", "innovus")


@pytest.mark.commercial
@pytest.mark.skipif(
    shutil.which("genus") is None or shutil.which("innovus") is None,
    reason="Genus and Innovus not on the path",
)
def test_innovus_places_and_routes_on_ihp(tmp_path):
    r, clock = backends.place_route_with(Design(accel=ACCEL), tmp_path)
    assert r.clean and r.setup_slack_typ_ns >= 0 and clock == 100
