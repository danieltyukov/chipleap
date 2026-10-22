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
