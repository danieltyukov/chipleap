"""Place and route with LibreLane: configuration, metrics, and a full run on request."""

import math
import os

import pytest

from chipleap.hw import Accel
from chipleap.hw.tech import accel_bank_kge, accel_logic_kge, get_tech
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


# The buffers as SRAM macros


SMOLLM2 = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)


def stand_in_pdk(tmp_path):
    from chip_fakes import sram_1p

    pdk = tmp_path / "ihp-sg13g2"
    sram_1p(pdk, 64, 64, (784.48, 64.36))
    sram_1p(pdk, 256, 64, (784.48, 118.78))
    return pdk


def test_the_layout_says_why_it_keeps_flop_buffers(tmp_path):
    assert "2 DMA read ports" in pnr.sram_reason(Accel("gemv", 4, 8, 8, ports=2), tmp_path)
    assert "CHIPLEAP_IHP_PDK" in pnr.sram_reason(SMOLLM2, None)
    assert "no single-port SRAM" in pnr.sram_reason(SMOLLM2, tmp_path)
    assert pnr.sram_reason(SMOLLM2, stand_in_pdk(tmp_path)) is None


def test_a_floorplan_too_flat_to_route_keeps_flop_buffers(tmp_path):
    """The GEMV 1 x 16's eight 784 um macros make rows of four and a core 7.4 times as wide
    as tall, which did not route; the 2 x 8's rows of three (3.7 to 1) did."""
    pdk = stand_in_pdk(tmp_path)
    reason = pnr.sram_reason(Accel("gemv", 1, 16, 8, ports=1, abuf_kib=2, wbuf_kib=2), pdk)
    assert reason is not None and "8 macros" in reason and "(7.4 to 1), too flat" in reason
    assert pnr.sram_reason(Accel("gemv", 2, 8, 8, ports=1, abuf_kib=2, wbuf_kib=4), pdk) is None
    # A single buffer halves the 1 x 16's macros, two to a row.
    one = Accel("gemv", 1, 16, 8, ports=1, abuf_kib=1, wbuf_kib=1, double_buffer=False)
    assert pnr.sram_reason(one, pdk) is None


def test_a_macro_backed_layout_is_the_banks_top_macros_and_grid(tmp_path, monkeypatch):
    from types import SimpleNamespace

    pdk = stand_in_pdk(tmp_path)
    work = tmp_path / "pnr"
    seen = {}

    def run(workdir, cfg, tag="chipleap", *, late_antenna_repair=False):
        assert late_antenna_repair  # every layout but the SoC's repairs antennas twice
        seen["cfg"] = cfg
        metrics = {
            "design__instance__area__stdcell": 250000.0,
            "timing__setup__ws": 1.0,
            "timing__hold__ws": 0.1,
        }
        return metrics, None

    # Before run_librelane is replaced: importing chipleap.rtl.pnr while it is would bind
    # the stand-in there for every later test.
    monkeypatch.setattr("chipleap.rtl.pnr.check_merged", lambda *a: {})
    monkeypatch.setattr(pnr, "librelane", lambda: SimpleNamespace(found=True, path="ll"))
    monkeypatch.setattr(pnr, "_sv2v", lambda a, b: b.write_text(a.read_text()))
    monkeypatch.setattr(pnr, "run_librelane", run)
    r = pnr.place_and_route(SMOLLM2, 90.0, work, pdk=pdk)
    cfg = seen["cfg"]
    cell = "RM_IHPSG13_1P_256x64_c2_bm_bist"
    assert cfg["DESIGN_NAME"] == "chipleap_accel_sram" and cfg["FP_SIZING"] == "absolute"
    # The macros' long input nets are repaired after global routing too.
    assert cfg["RUN_POST_GRT_DESIGN_REPAIR"] is True
    assert cfg["DESIGN_REPAIR_MAX_WIRE_LENGTH"] == cfg["GRT_DESIGN_REPAIR_MAX_WIRE_LENGTH"] == 300
    # Placed and routed against a slew limit under the macros' own, signed off against
    # LibreLane's constraints and every cell's own limit.
    assert cfg["PNR_SDC_FILE"] == "dir::pnr.sdc" and cfg["SIGNOFF_SDC_FILE"] == "dir::signoff.sdc"
    # The legaliser reaches the band past the macro rows (test_the_legaliser_reaches_...),
    # and hold is repaired with room for detailed routing at the macros' inputs.
    assert cfg["PL_MAX_DISPLACEMENT_Y"] > 100
    assert cfg["GRT_RESIZER_HOLD_SLACK_MARGIN"] == pnr.MACRO_HOLD_MARGIN_NS == 0.15
    pnr_sdc, signoff_sdc = ((work / n).read_text() for n in ("pnr.sdc", "signoff.sdc"))
    assert "source $::env(SCRIPTS_DIR)/base.sdc" in pnr_sdc and "base.sdc" in signoff_sdc
    assert "set_max_transition 0.3 [current_design]" in pnr_sdc
    assert "set_max_transition" not in signoff_sdc
    assert cfg["VERILOG_FILES"] == ["dir::src/chipleap_accel.v", "dir::src/chipleap_accel_sram.v"]
    assert set(cfg["MACROS"]) == {cell}
    entry = cfg["MACROS"][cell]
    assert set(entry["instances"]) == {"abuf_s0_m0", "abuf_s1_m0", "wbuf_s0_l0_m0", "wbuf_s1_l0_m0"}
    assert set(entry["lib"]) == {"*_typ_*", "*_slow_*", "*_fast_*"}
    assert entry["vh"] == [f"dir::macros/{cell}/{cell}_bb.v"]
    # Two macros a row, pins facing the logic between the rows.
    orients = sorted(i["orientation"] for i in entry["instances"].values())
    assert orients == ["FS", "FS", "N", "N"]
    # The band between the rows holds the build's logic with its banks' interface at 45 %,
    # not the logic of the flop-buffer build.
    x0, _, x1, _ = cfg["CORE_AREA"]
    ys = {i["orientation"]: i["location"][1] for i in entry["instances"].values()}
    band = (ys["N"] - ys["FS"] - 118.78) * (x1 - x0)
    banked = pnr.model_cell_um2(SMOLLM2, banked=True)
    assert banked > pnr.model_cell_um2(SMOLLM2)
    assert banked / 0.45 <= band < banked / 0.45 + 30 * (x1 - x0)
    pdn = (work / "pdn.tcl").read_text()
    assert "source $::env(SCRIPTS_DIR)/openroad/common/pdn_cfg.tcl" in pdn
    assert f"define_pdn_grid -macro -cells {{{cell}}}" in pdn
    assert "module chipleap_accel_sram (" in (work / "src" / "chipleap_accel_sram.v").read_text()
    assert len(r.macros) == 4 and r.macro_um2 == pytest.approx(4 * 784.48 * 118.78)
    assert r.buffers == f"SRAM macros: 4 x {cell}" and r.cell_um2 == 250000.0
    # A build the banks cannot take keeps the flop buffers, and says why.
    r = pnr.place_and_route(Accel("gemv", 4, 8, 8, ports=2), 90.0, tmp_path / "flop", pdk=pdk)
    assert seen["cfg"]["DESIGN_NAME"] == "chipleap_accel"
    assert r.buffers.startswith("16-word flop buffers: 2 DMA read ports") and not r.macros


def test_the_floorplan_puts_macros_in_two_rows_under_supply_pairs():
    from chipleap.macro import grid, place

    ms = [place.Macro(f"m{i}", 784.48, 118.78) for i in range(4)]
    fp = place.rows(ms, 287000)
    x0, y0, x1, y1 = fp.core
    assert x0 % 0.48 == pytest.approx(0, abs=1e-6) and y0 % 3.78 == pytest.approx(0, abs=1e-6)
    tops = [p for p in fp.macros if p.orient == "N"]
    bottoms = [p for p in fp.macros if p.orient == "FS"]
    assert len(tops) == len(bottoms) == 2
    assert all(p.y + 118.78 <= y1 - place.HALO_UM for p in tops)
    assert all(p.y >= y0 + place.HALO_UM for p in bottoms)
    # The band between the rows holds the cells at 45 %.
    band = (min(p.y for p in tops) - max(p.y for p in bottoms) - 118.78) * (x1 - x0)
    assert band >= 287000 / 0.45
    for p in fp.macros:
        assert grid.tm2_pairs(y0, p.y, p.y + 118.78)
    # A short macro moves towards the band until a pair is over it.
    fp = place.rows([place.Macro("short", 784.48, 64.36)], 50000)
    (p,) = fp.macros
    assert grid.tm2_pairs(fp.core[1], p.y, p.y + 64.36)
    with pytest.raises(place.PlaceError, match="utilisation"):
        place.rows(ms, 1000, util=1.5)


def test_the_core_ends_at_the_widest_rows_last_halo():
    """No sliver of rows between the last macro's halo and the core's edge: pdngen cannot
    reach the rails of rows a few sites wide (the SoC's six-macro rows left 2.4 um)."""
    from chipleap.macro import place

    ms = [place.Macro(f"b{i}", 416.64, 336.46) for i in range(8)]
    ms += [place.Macro(f"a{i}", 784.48, 118.78) for i in range(4)]
    fp = place.rows(ms, 3.2e6)
    size = {m.instance: m for m in ms}
    right = max(p.x + size[p.instance].width for p in fp.macros) + place.HALO_UM
    assert 0 <= fp.core[2] - right < place.SITE_UM[0]


def test_the_legaliser_reaches_past_the_macro_rows():
    """LibreLane 3.0.14 puts a port's buffer at its pin; over the SmolLM2 accelerator's
    macro rows the free rows were 161 um away and the legaliser's 100 um left 44 buffers
    unplaced (DPL-0036). A layout with macros lets it reach the band and 50 um into it."""
    from chipleap.macro import place

    ms = [place.Macro(f"m{i}", 784.48, 118.78) for i in range(4)]
    fp = place.rows(ms, 287000)
    tops = [p for p in fp.macros if p.orient == "N"]
    bottoms = [p for p in fp.macros if p.orient == "FS"]
    reach = place.edge_reach_um(fp, ms)
    assert reach == pytest.approx(fp.die[1] - (min(p.y for p in tops) - place.HALO_UM))
    assert reach >= max(p.y for p in bottoms) + 118.78 + place.HALO_UM - 1e-6
    assert 150 < reach < 170
    assert pnr.macro_reach(fp, ms) == {"PL_MAX_DISPLACEMENT_Y": math.ceil(reach + 50)}
    # A macro with rows between it and the core's edges leaves the default.
    x0, y0, _, _ = fp.core
    inner = place.Macro("inner", 100.0, 50.0)
    mid = place.Floorplan(fp.die, fp.core, (place.Placed("inner", x0 + 60, y0 + 200, "N"),))
    assert place.edge_reach_um(mid, [inner]) == 0
    assert pnr.macro_reach(mid, [inner]) == {"PL_MAX_DISPLACEMENT_Y": 100}


@pytest.mark.parametrize(
    "rows",
    [
        [("RM_IHPSG13_1P_1024x64_c2_bm_bist", 784.48, 336.46)] * 4,
        [("RM_IHPSG13_1P_2048x64_c2_bm_bist", 784.48, 626.7)] * 4,
        [("RM_IHPSG13_1P_4096x16_c3_bm_bist", 416.64, 618.3)] * 4,
        # Rows of two depths, as the SoC's: the deeper decides.
        [("RM_IHPSG13_1P_1024x32_c2_bm_bist", 416.64, 336.46)] * 8
        + [("RM_IHPSG13_1P_256x64_c2_bm_bist", 784.48, 118.78)] * 4,
    ],
)
def test_the_legaliser_reaches_the_band_past_the_deepest_row(rows):
    """From a pin on either edge, the legaliser's limit covers the margin, the deepest
    macro row with its halos and a row of the band, for IHP's deepest SRAMs too."""
    from chipleap.macro import place

    ms = [place.Macro(f"m{i}", w, h) for i, (_, w, h) in enumerate(rows)]
    fp = place.rows(ms, 3.2e6)
    size = {m.instance: m for m in ms}
    tops = [p for p in fp.macros if p.orient == "N"]
    bottoms = [p for p in fp.macros if p.orient == "FS"]
    need = (
        max(
            max(fp.die[1] - (p.y - place.HALO_UM) for p in tops),
            max(p.y + size[p.instance].height + place.HALO_UM for p in bottoms),
        )
        + place.SITE_UM[1]
    )
    deepest = max(m.height for m in ms) + 2 * place.HALO_UM + place.MARGIN_UM
    limit = pnr.macro_reach(fp, ms)["PL_MAX_DISPLACEMENT_Y"]
    assert need >= deepest and need <= limit <= need + 51


def test_the_step_compares_cells_and_macros_with_the_model(tmp_path, monkeypatch):
    from dataclasses import replace

    from chipleap.hw.design import Design
    from chipleap.verify import run

    cell = "RM_IHPSG13_1P_256x64_c2_bm_bist"
    result = pnr.parse(
        {
            "design__instance__area__stdcell": 300000.0,
            "design__core__area": 1.1e6,
            "timing__setup__ws": 1.0,
            "timing__hold__ws": 0.1,
        },
        90.0,
        None,
    )
    result = replace(
        result,
        macros=(("abuf_s0_m0", cell), ("wbuf_s0_l0_m0", cell)),
        macro_um2=2 * 784.48 * 118.78,
        buffers=f"SRAM macros: 2 x {cell}",
        synth_cell_um2=250000.0,
    )
    monkeypatch.setattr(run.pnr, "place_and_route", lambda accel, clock, work: result)
    step = run.place_route(Design(accel=SMOLLM2, freq_mhz=90.0), tmp_path)
    d = step.data
    assert d["macros"] == [
        {"instance": "abuf_s0_m0", "cell": cell},
        {"instance": "wbuf_s0_l0_m0", "cell": cell},
    ]
    assert d["measured_um2"] == round(300000 + 2 * 784.48 * 118.78)
    assert d["buffer_words"] == 512 and d["buffers"].startswith("SRAM macros")
    assert d["model_area_error"] == pytest.approx(
        (d["model_um2"] - d["measured_um2"]) / d["measured_um2"], abs=1e-4
    )
    assert f"buffers in 2 x {cell}: macros 0.186 mm2, the model 0.373 mm2" in step.summary
    # The model's macros are the layout's (four here, two in this stand-in result), its
    # logic is checked against synthesis, and what place and route adds is its own line.
    assert d["model_buffer_um2"] == round(4 * 784.48 * 118.78)
    assert d["synth_cell_um2"] == 250000 and d["pnr_cell_growth"] == pytest.approx(0.2)
    assert d["model_cell_error"] == pytest.approx((d["model_cell_um2"] - 250000) / 250000, abs=1e-4)
    # The layout has macros, so the model's logic has the banks' interface.
    kge = accel_logic_kge("gemv", 1, 8, 1) + accel_bank_kge(1, 8, True)
    assert d["model_cell_um2"] == round(kge * 1000 * get_tech("ihp-sg13g2").ge_um2)
    assert "synthesis 0.250 mm2" in step.summary and "place and route adds 20%" in step.summary


def test_an_antenna_violation_fails_the_step(tmp_path, monkeypatch):
    """As in a project's and a chip's place and route: a net over the PDK's antenna ratio
    fails the step, with DRC and LVS clean and timing met."""
    from dataclasses import replace

    from chipleap.hw.design import Design
    from chipleap.verify import run

    metrics = {
        "design__instance__area__stdcell": 300000.0,
        "design__core__area": 1.1e6,
        "timing__setup__ws": 1.0,
        "timing__hold__ws": 0.1,
    }
    clean = pnr.parse(metrics, 90.0, None)
    for antenna, status, words in (
        (0, "pass", "DRC, LVS and antenna clean"),
        (1, "fail", "0 DRC, 0 LVS and 1 antenna violations"),
    ):
        r = replace(clean, antenna_nets=antenna)
        monkeypatch.setattr(run.pnr, "place_and_route", lambda accel, clock, work, r=r: r)
        step = run.place_route(Design(accel=SMOLLM2, freq_mhz=90.0), tmp_path)
        assert step.status == status and words in step.summary and "90 MHz met" in step.summary


def test_slew_and_capacitance_violations_at_any_corner_fail_the_step_and_fanout_is_reported(
    tmp_path, monkeypatch
):
    """A pin over its slew limit or a net over its capacitance limit, at any corner, fails
    the accelerator's place and route as it does a project's. A driver over its fanout
    limit is reported and passes: in the SmolLM2 layout the detailed router's diodes were
    10 of a clock leaf's 12 loads, with its slew and capacitance well within limits."""
    from chipleap.hw.design import Design
    from chipleap.verify import run

    base = {
        "design__instance__area__stdcell": 300000.0,
        "design__core__area": 1.1e6,
        "timing__setup__ws": 1.0,
        "timing__hold__ws": 0.1,
    }
    slew = "design__max_slew_violation__count"
    cap = "design__max_cap_violation__count"
    fanout = "design__max_fanout_violation__count"
    fast = "__corner:nom_fast_1p32V_m40C"
    for extra, status, words in (
        ({slew: 0, cap: 0, fanout: 0}, "pass", "no slew or capacitance violations"),
        ({slew: 2}, "fail", "2 slew and 0 capacitance violations"),
        ({cap: 0, cap + fast: 3}, "fail", "0 slew and 3 capacitance violations"),
        ({slew + fast: 1, slew: True}, "fail", "1 slew and 0 capacitance violations"),
        ({fanout: 0, fanout + fast: 1}, "pass", "1 driver over the fanout limit (reported only)"),
    ):
        r = pnr.parse({**base, **extra}, 90.0, None)
        monkeypatch.setattr(run.pnr, "place_and_route", lambda accel, clock, work, r=r: r)
        step = run.place_route(Design(accel=SMOLLM2, freq_mhz=90.0), tmp_path)
        assert step.status == status and words in step.summary, step.summary
        assert step.data["max_slew_violations"] == r.max_slew_violations
        assert step.data["max_cap_violations"] == r.max_cap_violations
        assert step.data["max_fanout_violations"] == r.max_fanout_violations
    assert pnr.parse({**base, slew: float("nan")}, 90.0, None).max_slew_violations == 0
