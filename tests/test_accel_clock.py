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
    added = fast.area_mm2["accelerator logic"] - shared.area_mm2["accelerator logic"]
    assert added == pytest.approx(tech.logic_mm2(ACCEL_CDC_KGE + ACCEL_CDC_PORT_KGE))
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
