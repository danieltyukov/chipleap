"""Place and route with LibreLane: configuration, metrics, and a full run on request."""

import os

import pytest

from chipleap.hw import Accel
from chipleap.verify import pnr
from chipleap.verify.tools import librelane


def test_config_sets_parameters_and_clock():
    cfg = pnr.config(Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2), 100.0)
    assert cfg["CLOCK_PERIOD"] == 10.0 and cfg["CLOCK_PORT"] == "clk_i"
    params = dict(p.split("=") for p in cfg["SYNTH_PARAMETERS"])
    assert params["ROWS"] == "1" and params["COLS"] == "8" and params["PORTS"] == "1"
    assert params["ABUF_WORDS"] == params["WBUF_WORDS"] == str(pnr.PNR_WORDS)
    # Optimised for the corner it is signed off at.
    assert cfg["DEFAULT_CORNER"].startswith("nom_slow") and cfg["RUN_POST_GRT_RESIZER_TIMING"]


def test_many_ports_get_room_to_route():
    def util(ports):
        return pnr.config(Accel("gemv", 1, 8, 8, ports=ports), 100.0)["FP_CORE_UTIL"]

    assert util(1) == util(8) == 45
    assert util(16) == 35


def test_metrics_take_the_worst_corner():
    metrics = {
        "design__die__area": 640000.0,
        "design__core__area": 600000.0,
        "design__instance__area__stdcell": 300000.0,
        "design__instance__utilization__stdcell": 0.5,
        "design__instance__count__stdcell": 25000,
        "timing__setup__ws": 1.5,
        "timing__setup__ws__corner:nom_typ_1p20V_25C": 1.5,
        "timing__setup__ws__corner:nom_slow_1p08V_125C": -0.4,
        "timing__hold__ws": 0.2,
        "route__drc_errors": 0,
        "magic__drc_error__count": 0,
        "design__lvs_error__count": 0,
        "power__total": 0.004,
    }
    r = pnr.parse(metrics, 100.0, None)
    assert r.setup_slack_ns == -0.4 and r.setup_slack_typ_ns == 1.5 and r.hold_slack_ns == 0.2
    assert r.clean and r.cells == 25000 and r.power_mw == pytest.approx(4.0)
    assert r.fmax_mhz == pytest.approx(1000 / 8.5)
    assert r.fmax_worst_mhz == pytest.approx(1000 / 10.4)


@pytest.mark.pnr
@pytest.mark.skipif(not librelane().found, reason="LibreLane not installed")
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_PNR"), reason="set CHIPLEAP_RUN_PNR=1")
def test_gemv_places_and_routes(tmp_path):
    r = pnr.place_and_route(Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2), 100, tmp_path)
    assert r.clean and r.setup_slack_typ_ns >= 0 and r.gds is not None
