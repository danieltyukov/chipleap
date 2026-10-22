"""The accelerator on a clock of its own: the design field, the generated files, and the
bundle's steps that read them."""

import pytest

from chipleap.gen.bundle import load_design, write_bundle
from chipleap.gen.rtl import write_rtl
from chipleap.hw import Accel, Design
from chipleap.hw.design import DesignError
from chipleap.verify import consistency, crossings

SMOLLM2 = Design(
    name="own-clock",
    freq_mhz=90.0,
    banks=1,
    bus="onetoM",
    ext_mem="octal-psram",
    ext_channels=2,
    accel=Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2),
    accel_mhz=90.0,
)


def test_the_field_needs_an_accelerator_on_xheep_and_is_left_out_when_unused():
    SMOLLM2.validate()
    for bad in (
        SMOLLM2.with_(accel_mhz=-1.0),
        SMOLLM2.with_(accel=None),
        SMOLLM2.with_(platform="standalone"),
    ):
        with pytest.raises(DesignError, match="accel_mhz"):
            bad.validate()
    assert SMOLLM2.to_dict()["accel_mhz"] == 90.0
    assert Design.from_dict(SMOLLM2.to_dict()) == SMOLLM2
    # A design on X-HEEP's clock writes the same design.yaml as before the field.
    shared = SMOLLM2.with_(accel_mhz=0.0)
    assert "accel_mhz" not in shared.to_dict()
    assert Design.from_dict(shared.to_dict()) == shared
    assert "@ 90 MHz" in SMOLLM2.summary() and "port @" not in shared.summary()


def test_the_wrappers_put_the_accelerator_behind_its_crossings(tmp_path):
    files = {p.name for p in write_rtl(SMOLLM2.accel, "x", tmp_path, own_clock=True)}
    assert {"chipleap_accel_cdc.sv", "tb_chipleap_cdc.cpp"} <= files
    for name in ("chipleap_accel_top.sv", "chipleap_accel_xheep.sv"):
        text = (tmp_path / "rtl" / name).read_text()
        assert "input logic accel_clk_i," in text and "chipleap_accel_cdc #(" in text
    assert "      - chipleap_accel_cdc.sv" in (tmp_path / "rtl" / "chipleap_accel.core").read_text()
    shared = tmp_path / "shared"
    shared.mkdir()
    write_rtl(SMOLLM2.accel, "x", shared)
    assert not (shared / "rtl" / "chipleap_accel_cdc.sv").exists()
    assert "accel_clk_i" not in (shared / "rtl" / "chipleap_accel_xheep.sv").read_text()
    with pytest.raises(ValueError, match="X-HEEP only"):
        write_rtl(SMOLLM2.accel, "x", tmp_path / "s", platform="standalone", own_clock=True)


def test_a_bundle_on_its_own_clock_is_consistent_and_drift_is_found(tmp_path):
    write_bundle(tmp_path, SMOLLM2, weights="int4")
    design, _ = load_design(tmp_path)
    assert design.accel_mhz == 90.0
    assert consistency.check(tmp_path, design) == []
    xheep = tmp_path / "rtl" / "chipleap_accel_xheep.sv"
    xheep.write_text(xheep.read_text().replace("chipleap_accel_cdc #(", "chipleap_accel #("))
    assert consistency.check(tmp_path, design) == [
        "rtl/chipleap_accel_xheep.sv: the accelerator is not behind its clock crossings, "
        "design.yaml says accel_mhz is 90"
    ]
    # The other way round: the top wrapper still has the crossings, a design on X-HEEP's
    # clock does not.
    assert consistency.check(tmp_path, design.with_(accel_mhz=0.0)) == [
        "rtl/chipleap_accel_top.sv: the accelerator is behind clock crossings, design.yaml "
        "says accel_mhz is 0"
    ]


def test_the_cdc_step_has_nothing_to_check_on_xheeps_clock(tmp_path):
    r = crossings.check(SMOLLM2.with_(accel_mhz=0.0), tmp_path)
    assert r.status == "skipped" and "nothing crosses" in r.summary
    assert crossings.check(SMOLLM2.with_(accel=None, accel_mhz=0.0), tmp_path).status == "skipped"


def test_the_crossing_scales_rates_and_lengthens_latencies():
    from chipleap.model.accel import CDC_ACCEL_CYCLES, CDC_BUS_CYCLES, Bandwidth, crossed

    bw = Bandwidth(x=4.0, w_on=4.0, w_ext=2.0, y=4.0, lat_on=2, lat_ext=8)
    fast = crossed(bw, 90 / 35, ports=1)  # the accelerator 2.57 times the bus's clock
    assert fast.x == pytest.approx(4.0 * 35 / 90) and fast.w_ext == pytest.approx(2.0 * 35 / 90)
    assert fast.lat_on == 12 and fast.lat_ext == 27  # ceil(k (L + 1.25) + 3)
    assert fast.lat_on == -(-(90 / 35 * (2 + CDC_BUS_CYCLES) + CDC_ACCEL_CYCLES) // 1)
    # A slower accelerator takes at most a word per port and cycle, however fast the bus.
    slow = crossed(bw, 35 / 90, ports=1)
    assert slow.x == slow.y == slow.w_ext == 4.0
    with pytest.raises(ValueError):
        crossed(bw, 0.0, ports=1)


def _evaluate(design):
    from pathlib import Path

    from chipleap.model.evaluate import evaluate
    from chipleap.spec import load_spec

    spec = load_spec(Path(__file__).parents[1] / "examples" / "smollm2-135m-open-pdk.yaml")
    return evaluate(spec.workload("int4"), design, firmware_kib=spec.firmware_kib)


def test_the_model_times_the_accelerator_at_its_own_clock_and_prices_its_crossings():
    from chipleap.hw.tech import ACCEL_CDC_KGE, ACCEL_CDC_PORT_KGE, get_tech

    shared = _evaluate(SMOLLM2.with_(accel_mhz=0.0))
    same = _evaluate(SMOLLM2.with_(accel_mhz=shared.soc_mhz))
    fast = _evaluate(SMOLLM2)
    assert same.accel_clock_mhz == shared.soc_mhz and shared.accel_clock_mhz == 0.0

    def unit(ev, u):
        return sum(r.time_s for r in ev.ops if r.unit == u)

    # The CPU's work stays at the SoC's clock; the accelerator's pays for its crossings at
    # the same clock, and speeds up at a faster one only as far as the bus lets it.
    assert unit(same, "cpu") == pytest.approx(unit(shared, "cpu"))
    assert unit(fast, "cpu") == pytest.approx(unit(shared, "cpu"))
    assert unit(same, "accel") > unit(shared, "accel") > unit(fast, "accel")
    tech = get_tech("ihp-sg13g2")
    from chipleap.model.evaluate import direct_kge

    added = fast.area_mm2["accelerator logic"] - shared.area_mm2["accelerator logic"]
    # The crossings, and the direct path's on the accelerator's clock (X-HEEP's side
    # crossing, the ports' queues sized for sixteen reads in flight from eight).
    direct = direct_kge(fast.design) - direct_kge(shared.design)
    assert direct > 0
    assert added == pytest.approx(tech.logic_mm2(ACCEL_CDC_KGE + ACCEL_CDC_PORT_KGE + direct))
    from chipleap.report.text import clock_note

    assert "the accelerator runs on its own clock at 90" in clock_note(fast)


def test_the_accelerators_clock_limit_applies_to_its_own_clock():
    # A GEMV 1 x 32, whose adder tree closes timing at about 88 MHz.
    big = SMOLLM2.with_accel(cols=32)
    limit = _evaluate(big).accel_limit_mhz
    assert limit and 85 < limit < 90
    over = _evaluate(big.with_(accel_mhz=95.0))
    assert any("closes timing at about 88 MHz" in v for v in over.violations), over.violations
    # The SoC asks more than the accelerator meets, but the accelerator has its own clock.
    under = _evaluate(big.with_(freq_mhz=95.0, accel_mhz=80.0))
    assert not any("closes timing" in v for v in under.violations), under.violations
    assert any(
        "closes timing" in v for v in _evaluate(big.with_(freq_mhz=95.0, accel_mhz=0.0)).violations
    )


def test_the_two_clock_constraints_bound_the_crossings_without_clock_groups():
    from chipleap.verify.pnr import two_clock_sdc

    sdc = two_clock_sdc(42.3, 90.0)
    assert "create_clock [get_ports clk_i] -name clk_i -period 23.641" in sdc
    assert "create_clock [get_ports accel_clk_i] -name accel_clk_i -period 11.111" in sdc
    # Every path between the clocks is bounded by the faster clock's period, datapath only
    # (OpenSTA's -ignore_clock_latency), with no hold check between them.
    for a, b in (("clk_i", "accel_clk_i"), ("accel_clk_i", "clk_i")):
        assert (
            f"set_max_delay -ignore_clock_latency 11.111 -from [get_clocks {a}] "
            f"-to [get_clocks {b}]" in sdc
        )
        assert f"set_false_path -hold -from [get_clocks {a}] -to [get_clocks {b}]" in sdc
    # Clock groups would take the bound away: OpenSTA reports such a path unconstrained.
    commands = [ln for ln in sdc.splitlines() if not ln.startswith("#")]
    assert not any("set_clock_groups" in ln or "-datapath_only" in ln for ln in commands)
    # The ports on X-HEEP's side, timed against its clock; neither clock is a data input.
    assert "set_input_delay $io_delay -clock [get_clocks clk_i] $data_inputs" in sdc
    assert "set clock_inputs [list [get_ports clk_i] [get_ports accel_clk_i]]" in sdc
    assert "set_max_transition 0.3" not in sdc
    assert "set_max_transition 0.3 [current_design]" in two_clock_sdc(42.3, 90.0, 0.3)
    assert "-ignore_clock_latency 23.641" in two_clock_sdc(42.3, 30.0)  # the bus the faster


def test_a_macro_build_on_its_own_clock_puts_its_macros_inside_the_crossings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from chipleap.verify import pnr
    from test_pnr import stand_in_pdk

    pdk = stand_in_pdk(tmp_path)
    seen = {}

    def run(workdir, cfg, tag="chipleap", *, late_antenna_repair=False):
        seen["cfg"] = cfg
        return {"timing__setup__ws": 1.0, "timing__hold__ws": 0.1}, None

    monkeypatch.setattr(pnr, "librelane", lambda: SimpleNamespace(found=True, path="ll"))
    monkeypatch.setattr(pnr, "_sv2v", lambda a, b: b.write_text(a.read_text()))
    monkeypatch.setattr(pnr, "run_librelane", run)
    monkeypatch.setattr("chipleap.rtl.pnr.check_merged", lambda *a: {})
    work = tmp_path / "pnr"
    r = pnr.place_and_route(SMOLLM2.accel, 42.3, work, pdk=pdk, accel_mhz=90.0)
    cfg = seen["cfg"]
    assert cfg["DESIGN_NAME"] == "chipleap_accel_cdc"
    assert cfg["CLOCK_PORT"] == ["clk_i", "accel_clk_i"] and cfg["CLOCK_PERIOD"] == 11.111
    assert cfg["VERILOG_FILES"] == [
        "dir::src/chipleap_accel_cdc.v",
        "dir::src/chipleap_accel_sram.v",
    ]
    (cell,) = cfg["MACROS"]
    assert set(cfg["MACROS"][cell]["instances"]) == {
        "u_accel.abuf_s0_m0",
        "u_accel.abuf_s1_m0",
        "u_accel.wbuf_s0_l0_m0",
        "u_accel.wbuf_s1_l0_m0",
    }
    pnr_sdc, signoff_sdc = ((work / n).read_text() for n in ("pnr.sdc", "signoff.sdc"))
    assert "set_max_delay -ignore_clock_latency 11.111" in pnr_sdc
    assert "set_max_transition 0.3 [current_design]" in pnr_sdc
    assert "set_max_transition 0.3" not in signoff_sdc and "base.sdc" not in signoff_sdc
    src = (work / "src" / "chipleap_accel_cdc.v").read_text()
    # The macro build behind the crossings takes only the direct path's parameters.
    assert "chipleap_accel_sram #(\n      .EXT_DIRECT(EXT_DIRECT)," in src
    assert "module chipleap_accel_cdc" in src
    assert ".MAX_OUTSTANDING(8)," in (work / "src" / "chipleap_accel_sram.v").read_text()
    assert r.clocks == {"clk_i": 23.641, "accel_clk_i": 11.111}
    # The band between the macro rows holds the crossings too.
    assert pnr.model_cell_um2(SMOLLM2.accel, banked=True, crossings=True) > pnr.model_cell_um2(
        SMOLLM2.accel, banked=True
    )


def test_each_path_groups_worst_slack_is_read_from_the_signoff_reports(tmp_path):
    from chipleap.verify.pnr import group_slacks

    def path(group, slack):
        return (
            f"Startpoint: a (rising edge-triggered flip-flop)\nEndpoint: b\n"
            f"Path Group: {group}\nPath Type: max\n\n   1.0   data arrival time\n"
            f"                                              {slack}   slack "
            f"({'MET' if slack >= 0 else 'VIOLATED'})\n\n"
        )

    run = tmp_path / "run"
    for n in (30, 58):  # an earlier timing step, then the signoff one
        for corner in ("nom_slow_1p08V_125C", "nom_fast_1p32V_m40C"):
            d = run / f"{n}-openroad-stapostpnr" / corner
            d.mkdir(parents=True)
            late = n == 58
            (d / "max.rpt").write_text(
                "report_checks -path_delay max\n"
                + path("accel_clk_i", 0.5 if late else -9.0)
                + path("accel_clk_i", 0.9)
                + path("clk_i", 12.25)
                + path("path delay", 7.5)
            )
            (d / "min.rpt").write_text(path("accel_clk_i", 0.12) + path("clk_i", 0.2))
    got = group_slacks(run)
    assert got["setup"]["nom_slow_1p08V_125C"] == {
        "accel_clk_i": 0.5,
        "clk_i": 12.25,
        "path delay": 7.5,
    }
    assert got["hold"]["nom_fast_1p32V_m40C"] == {"accel_clk_i": 0.12, "clk_i": 0.2}
    from chipleap.verify.run import group_note

    assert group_note(got) == (
        "setup accel_clk_i +0.500 ns, setup clk_i +12.250 ns, setup crossings +7.500 ns, "
        "hold accel_clk_i +0.120 ns, hold clk_i +0.200 ns"
    )
    # The reports list the design's worst thousand paths, not each group's: a clock with
    # none among them is bounded by the largest slack listed.
    rpt = run / "58-openroad-stapostpnr" / "nom_slow_1p08V_125C" / "max.rpt"
    rpt.write_text(path("accel_clk_i", 0.29) + path("accel_clk_i", 1.75) + path("path delay", 4.5))
    got = group_slacks(run, ("clk_i", "accel_clk_i"))
    assert got["setup_above"]["nom_slow_1p08V_125C"] == {"clk_i": 4.5}
    assert "setup_above" not in got or "nom_fast_1p32V_m40C" not in got["setup_above"]
    assert group_note(got).startswith(
        "setup accel_clk_i +0.290 ns, setup crossings +4.500 ns, setup clk_i above +4.500 ns, "
    )
