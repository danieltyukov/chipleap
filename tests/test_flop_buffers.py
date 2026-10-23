"""accel.buffers: flops keeps a build's buffers in flip-flops where the banks could take it,
in the layout and in the model alike."""

from types import SimpleNamespace

import pytest

from chipleap.gen import banks
from chipleap.hw import Accel, Design
from chipleap.hw.design import DesignError
from chipleap.hw.tech import get_tech
from chipleap.model import evaluate as E

GEMV = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2)
FLOPS = Accel("gemv", 1, 8, 8, ports=1, abuf_kib=2, wbuf_kib=2, buffers="flops")


def test_the_field_is_checked_and_left_out_of_a_design_that_does_not_set_it():
    with pytest.raises(DesignError, match=r"accel\.buffers"):
        Accel(buffers="sram").validate()
    auto = Design(accel=GEMV, bus="onetoM")
    assert "buffers" not in auto.to_dict()["accel"]
    flops = auto.with_(accel=FLOPS)
    assert flops.to_dict()["accel"]["buffers"] == "flops"
    assert Design.from_dict(flops.to_dict()) == flops
    assert ", flop buffers" in flops.summary() and "flop buffers" not in auto.summary()


def test_the_banks_refuse_it_with_the_reason():
    assert banks.unsupported(GEMV) is None
    assert banks.unsupported(FLOPS) == (
        "the design keeps its buffers in flip-flops (accel.buffers: flops)"
    )


def test_the_model_prices_and_times_it_as_a_build_without_banks():
    ihp = get_tech("ihp-sg13g2")
    assert E.bank_read_ns(GEMV, ihp) > 0 and E.bank_read_ns(FLOPS, ihp) == 0.0
    assert E.accel_kge(FLOPS, ihp) < E.accel_kge(GEMV, ihp)  # no banks' interface
    assert E.buffer_mm2(FLOPS, ihp) != E.buffer_mm2(GEMV, ihp)
    assert E.floorplan_reason(FLOPS, ihp) is None  # not a floorplan that failed to route


def test_place_and_route_lays_it_out_with_flop_buffers(tmp_path, monkeypatch):
    from chipleap.verify import pnr
    from test_pnr import stand_in_pdk

    seen = {}

    def run(workdir, cfg, tag="chipleap", *, late_antenna_repair=False):
        seen["cfg"] = cfg
        return {"timing__setup__ws": 1.0, "timing__hold__ws": 0.1}, None

    monkeypatch.setattr(pnr, "librelane", lambda: SimpleNamespace(found=True, path="ll"))
    monkeypatch.setattr(pnr, "_sv2v", lambda a, b: b.write_text(a.read_text()))
    monkeypatch.setattr(pnr, "run_librelane", run)
    monkeypatch.setattr(pnr, "with_ihp_antenna", lambda r, *a: r)
    r = pnr.place_and_route(FLOPS, 100.0, tmp_path / "pnr", pdk=stand_in_pdk(tmp_path))
    assert seen["cfg"]["DESIGN_NAME"] == "chipleap_accel" and not r.macros
    assert r.buffers == (
        "16-word flop buffers: the design keeps its buffers in flip-flops (accel.buffers: flops)"
    )
