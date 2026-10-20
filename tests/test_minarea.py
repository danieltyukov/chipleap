"""Routed shapes under the minimum metal area, and the patches that fix them
(chipleap.verify.minarea)."""

import json

import pytest

from chipleap.verify import minarea, pnr

# The parts of IHP SG13G2's tech LEF the check reads.
TECH = """
MANUFACTURINGGRID 0.005 ;
LAYER Metal1
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  WIDTH 0.16 ;
  AREA 0.09 ;
END Metal1
LAYER Via1
  TYPE CUT ;
END Via1
LAYER Metal2
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  WIDTH 0.20 ;
  AREA 0.144 ;
END Metal2
LAYER Via2
  TYPE CUT ;
END Via2
LAYER Metal3
  TYPE ROUTING ;
  DIRECTION HORIZONTAL ;
  WIDTH 0.20 ;
  AREA 0.144 ;
END Metal3
LAYER Via3
  TYPE CUT ;
END Via3
LAYER Metal4
  TYPE ROUTING ;
  DIRECTION VERTICAL ;
  WIDTH 0.20 ;
  AREA 0.144 ;
END Metal4
Via Via3_XY DEFAULT
  LAYER Metal3 ;
    RECT -0.145 -0.105 0.145 0.105 ;
  LAYER Via3 ;
    RECT -0.095 -0.095 0.095 0.095 ;
  LAYER Metal4 ;
    RECT -0.1 -0.145 0.1 0.145 ;
END Via3_XY
Via Via1_XY DEFAULT
  LAYER Metal1 ;
    RECT -0.145 -0.105 0.145 0.105 ;
  LAYER Via1 ;
    RECT -0.095 -0.095 0.095 0.095 ;
  LAYER Metal2 ;
    RECT -0.1 -0.145 0.1 0.145 ;
END Via1_XY
Via Via2_YX DEFAULT
  LAYER Metal2 ;
    RECT -0.105 -0.145 0.105 0.145 ;
  LAYER Via2 ;
    RECT -0.095 -0.095 0.095 0.095 ;
  LAYER Metal3 ;
    RECT -0.145 -0.1 0.145 0.1 ;
END Via2_YX
"""

# Net net14185 of the routed GEMV 4 x 8 engine with two ports, as the detailed router left
# it: a Metal2 stub from a Via1 up to a Via2, jogged by 5 nm, with a patch that lies on the
# stub's own metal. KLayout measured the shape at 0.1315 um2 (rule M2.d).
STUB = """
      + ROUTED Metal3 ( 876480 1140300 ) ( 882240 * )
      NEW Metal2 ( 876480 1140300 ) ( 876485 * )
      NEW Metal2 ( 876485 1139945 ) ( * 1140300 )
      NEW Metal2 ( 876480 1139945 ) ( 876485 * )
      NEW Metal2 ( 876480 1139945 ) ( * 1139955 )
      NEW Metal2 ( 876480 1140300 ) Via2_YX
      NEW Metal1 ( 876480 1139955 ) Via1_XY
      NEW Metal2 ( 876480 1139955 ) RECT ( -100 0 100 200 )"""


def _def(nets: str, pins: str = "", special: str = "") -> str:
    return f"""VERSION 5.8 ;
DESIGN chipleap_accel ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 1216085 1234805 ) ;
PINS 1 ;
{pins}
END PINS
SPECIALNETS 1 ;
{special}
END SPECIALNETS
NETS 2 ;
{nets}
END NETS
END DESIGN
"""


def _net(name: str, routing: str) -> str:
    return f"    - {name} ( u1 A ) ( u2 X ) + USE SIGNAL{routing} ;"


def _layout(text: str) -> minarea.Layout:
    tech = minarea.read_tech(TECH)
    return minarea.read_layout(text, tech, {})


def test_finds_the_stub_the_router_left_under_the_minimum():
    tech = minarea.read_tech(TECH)
    assert tech.layers["Metal2"] == minarea.Layer("Metal2", 200, 0.144, True)
    assert not tech.layers["Metal3"].vertical and tech.grid == 5
    (shape,) = minarea.check(_layout(_def(_net("net14185", STUB))))
    # The same area as KLayout's: the patch adds only 0.000275 um2 to the stub.
    assert (shape.net, shape.layer) == ("net14185", "Metal2")
    assert shape.area_um2 == pytest.approx(0.13145)
    assert shape.bbox == (876375, 1139810, 876585, 1140445)


def test_a_long_enough_wire_and_a_pin_are_not_flagged():
    wire = "\n      + ROUTED Metal2 ( 500000 500000 ) ( * 501000 )"
    stub = "\n      + ROUTED Metal2 ( 300000 200 ) ( * 400 )"
    pin = (
        "    - out + NET out + DIRECTION OUTPUT + USE SIGNAL\n"
        "      + PORT + LAYER Metal2 ( -100 -400 ) ( 100 400 ) + PLACED ( 300000 200 ) N ;"
    )
    nets = "\n".join([_net("long", wire), _net("out", stub)])
    assert minarea.check(_layout(_def(nets, pins=pin))) == []


def test_the_patch_extends_the_free_end_and_reaches_the_minimum():
    layout = _layout(_def(_net("net14185", STUB)))
    (patch,) = minarea.repair(layout)
    # The top end is the Via2's enclosure, 0.21 um wide: 90 nm more brings the shape to
    # 0.15035 um2, the minimum and a margin.
    assert patch.rect == (876375, 1140445, 876585, 1140535)
    assert patch.statement() == "NEW Metal2 ( 876375 1140445 ) RECT ( 0 0 210 90 )"
    patched = minarea.apply(_def(_net("net14185", STUB)), [patch])
    assert minarea.check(_layout(patched)) == []


def test_the_patch_keeps_clear_of_other_nets():
    # Another net's wire 0.25 um above the stub's top: too close for a patch there, so the
    # patch goes to the bottom end instead.
    above = "\n      + ROUTED Metal2 ( 876480 1140695 ) ( * 1145000 )"
    nets = "\n".join([_net("net14185", STUB), _net("other", above)])
    (patch,) = minarea.repair(_layout(_def(nets)))
    assert patch.rect[3] == 1139810  # ends at the stub's bottom
    assert patch.rect[1] < 1139810


def test_no_room_at_either_end_is_an_error():
    above = "\n      + ROUTED Metal2 ( 876480 1140695 ) ( * 1145000 )"
    below = "\n      + ROUTED Metal2 ( 876480 1130000 ) ( * 1139560 )"
    nets = "\n".join([_net("net14185", STUB), _net("up", above), _net("down", below)])
    with pytest.raises(minarea.MinAreaError, match=r"Metal2 of net net14185, 0\.131450 um2"):
        minarea.repair(_layout(_def(nets)))


def test_power_stripes_and_cells_with_metal_block_patches():
    stripe = (
        "    - VSS ( * VSS ) + USE GROUND\n"
        "      + ROUTED Metal2 2000 + SHAPE STRIPE ( 876480 1142000 ) ( 876480 1150000 )"
    )
    layout = minarea.read_layout(
        _def(_net("net14185", STUB), special=stripe + " ;"), minarea.read_tech(TECH), {}
    )
    # The stripe is 2 um wide, so a patch keeps 0.24 um (plus a margin) from it; its end,
    # half its width past its last point, is 0.555 um above the stub, so the patch fits.
    (patch,) = minarea.repair(layout)
    assert patch.rect[1] == 1140445
    masters = {
        "sram": minarea.Master(
            2000, 2000, frozenset({"Metal2"}), obs=(("Metal2", (0, 0, 2000, 2000)),)
        )
    }
    comps = "COMPONENTS 1 ;\n    - u_ram sram + FIXED ( 875000 1140600 ) N ;\nEND COMPONENTS\n"
    text = _def(_net("net14185", STUB)).replace("PINS 1 ;", comps + "PINS 1 ;")
    (patch,) = minarea.repair(minarea.read_layout(text, minarea.read_tech(TECH), masters))
    # The macro's obstruction over the top end sends the patch to the bottom.
    assert patch.rect[3] == 1139810


# IHP's 4 mA output pad as its LEF has it, the parts the check reads: the bond-side pin
# on every metal, the core-side pin, and obstructions over the whole cell.
PAD_LEF = """
MACRO sg13g2_IOPadOut4mA
    CLASS PAD OUTPUT ;
    ORIGIN 0.000 0.000 ;
    SIZE 80.000 BY 180.000 ;
  PIN c2p
    DIRECTION INPUT ;
    PORT
      LAYER Metal2 ;
        RECT 38.145 167.000 41.855 180.000 ;
    END
  END c2p
  PIN pad
    DIRECTION INOUT ;
    PORT
      LAYER Metal3 ;
        RECT 5.000 0.000 75.000 3.000 ;
      LAYER Metal4 ;
        RECT 5.000 0.000 75.000 3.000 ;
    END
  END pad
  OBS
    LAYER Metal3 ;
      RECT 0.000 0.000 80.000 178.000 ;
    LAYER Metal4 ;
      RECT 0.000 0.000 80.000 178.000 ;
  END
END sg13g2_IOPadOut4mA
"""
PAD = (
    "COMPONENTS 1 ;\n    - pad_w0 sg13g2_IOPadOut4mA + FIXED ( 70000 260000 ) E ;\nEND COMPONENTS\n"
)

# The UART chip's route from a chip pin to that pad's bond-side pin, as the router left
# it: a Metal3 wire from the pin, a Via3 and a Metal4 stub that ends 80 nm inside the pad,
# on its pin. The stub alone is 0.134 um2.
CHIP_PIN = (
    "    - pad_o + NET pad_o + DIRECTION INOUT + USE SIGNAL\n"
    "      + PORT + LAYER Metal3 ( -1000 -1000 ) ( 1000 1000 ) + FIXED ( 68500 300000 ) N ;"
)
TO_PAD = """
      + ROUTED Metal3 ( 69120 299040 0 ) ( 69600 * )
      NEW Metal4 ( 69600 299040 ) ( 70080 * 0 )
      NEW Metal3 ( 69600 299040 ) Via3_XY"""


def _with(
    components: str, nets: str, masters: dict[str, minarea.Master], pins: str = ""
) -> minarea.Layout:
    text = _def(nets, pins=pins).replace("PINS 1 ;", components + "PINS 1 ;")
    return minarea.read_layout(text, minarea.read_tech(TECH), masters)


def test_the_lef_gives_each_pin_its_ports_and_the_cell_its_obstructions():
    pad = minarea.read_masters(PAD_LEF)["sg13g2_IOPadOut4mA"]
    assert (pad.width, pad.height) == (80000, 180000)
    assert pad.pins["pad"] == (
        ("Metal3", (5000, 0, 75000, 3000)),
        ("Metal4", (5000, 0, 75000, 3000)),
    )
    assert pad.pins["c2p"] == (("Metal2", (38145, 167000, 41855, 180000)),)
    assert ("Metal4", (0, 0, 80000, 178000)) in pad.obs
    assert pad.layers == {"Metal2", "Metal3", "Metal4"}


def test_lef_shapes_are_taken_from_the_cells_origin_and_through_a_mask():
    lef = """
MACRO m
  ORIGIN 1 2 ;
  SIZE 10 BY 10 ;
  PIN a
    PORT
      LAYER Metal2 ;
        RECT MASK 1 0 0 1 1 ;
    END
  END a
  OBS
    LAYER Metal3 ;
      RECT -1 -2 9 8 ;
  END
END m
"""
    m = minarea.read_masters(lef)["m"]
    assert m.pins["a"] == (("Metal2", (1000, 2000, 2000, 3000)),)
    assert m.obs == (("Metal3", (0, 0, 10000, 10000)),)


def test_a_stub_that_ends_on_a_pads_pin_is_that_pins_metal():
    masters = minarea.read_masters(PAD_LEF)
    net = "    - pad_o ( PIN pad_o ) ( pad_w0 pad ) + USE SIGNAL" + TO_PAD + " ;"
    layout = _with(PAD, net, masters, CHIP_PIN)
    # The pad turned east: its pin lies 70 um along the die's west edge, 3 um deep.
    assert (70000, 265000, 73000, 335000) in layout.nets[("pad_o", "Metal4")]
    assert minarea.check(layout) == []
    assert minarea.repair(layout) == []


def test_a_stub_that_ends_on_a_macros_pin_is_that_pins_metal():
    lef = """
MACRO blk
  SIZE 20 BY 20 ;
  PIN a
    PORT
      LAYER Metal4 ;
        RECT 0 9.9 0.5 10.1 ;
    END
  END a
  OBS
    LAYER Metal4 ;
      RECT 0.6 0 20 20 ;
  END
END blk
"""
    comps = "COMPONENTS 1 ;\n    - u_blk blk + FIXED ( 100000 100000 ) N ;\nEND COMPONENTS\n"
    stub = "\n      + ROUTED Metal4 ( 99700 110000 ) ( 100200 * 0 )"
    joined = "    - n ( u_blk a ) + USE SIGNAL" + stub + " ;"
    # 0.12 um2 alone, 0.18 um2 with the 0.5 um of pin it ends on.
    assert minarea.check(_with(comps, joined, minarea.read_masters(lef))) == []
    alone = "    - n ( u2 X ) + USE SIGNAL" + stub + " ;"
    (shape,) = minarea.check(_with(comps, alone, minarea.read_masters(lef)))
    assert shape.area_um2 == pytest.approx(0.12)


def test_a_stub_short_of_the_minimum_beside_an_obstruction_still_fails():
    # Another net's stub that stops 0.1 um short of the pad: the pad's obstruction leaves
    # no room at its east end, another net's wire none at its west end.
    stub = "\n      + ROUTED Metal4 ( 69300 299040 ) ( 69900 * 0 )"
    west = "\n      + ROUTED Metal4 ( 68000 298000 ) ( * 300000 )"
    nets = "\n".join(
        [
            "    - x ( u1 A ) + USE SIGNAL" + stub + " ;",
            "    - y ( u2 A ) + USE SIGNAL" + west + " ;",
        ]
    )
    layout = _with(PAD, nets, minarea.read_masters(PAD_LEF))
    (shape,) = minarea.check(layout)
    assert (shape.net, shape.layer) == ("x", "Metal4") and shape.area_um2 == pytest.approx(0.14)
    with pytest.raises(minarea.MinAreaError, match=r"Metal4 of net x, 0\.140000 um2"):
        minarea.repair(layout)


def test_patches_go_back_into_the_net_they_fix():
    nets = "\n".join(
        [_net("a", "\n      + ROUTED Metal3 ( 1000 1000 ) ( 9000 * )"), _net("net14185", STUB)]
    )
    (patch,) = minarea.repair(_layout(_def(nets)))
    out = minarea.apply(_def(nets), [patch])
    a_item, stub_item = out.split("NETS 2 ;")[1].split(";")[:2]
    assert "RECT ( 0 0 210 90 )" in stub_item and "RECT" not in a_item
    with pytest.raises(minarea.MinAreaError, match="not found"):
        minarea.apply(_def(_net("a", "\n      + ROUTED Metal3 ( 0 0 ) ( 9000 * )")), [patch])


def test_lefs_come_from_librelanes_resolved_configuration():
    resolved = {
        "TECH_LEFS": {"nom_*": "/pdk/tech.lef", "min_*": "/pdk/tech.lef"},
        "CELL_LEFS": ["/pdk/cells.lef"],
        "MACROS": {"RM": {"lef": ["/pdk/rm.lef"], "gds": ["/pdk/rm.gds"]}},
    }
    assert minarea.lefs_of(resolved) == ("/pdk/tech.lef", ["/pdk/cells.lef", "/pdk/rm.lef"])
    with pytest.raises(minarea.MinAreaError, match="no tech LEF"):
        minarea.lefs_of({})


def _routed_run(tmp_path, nets):
    work = tmp_path / "pnr"
    run = work / "runs" / "chipleap"
    step = run / "45-openroad-detailedrouting"
    step.mkdir(parents=True)
    (tmp_path / "tech.lef").write_text(TECH)
    (step / "chipleap_accel.def").write_text(_def(nets))
    state = {"def": str(step / "chipleap_accel.def"), "odb": str(step / "chipleap_accel.odb")}
    (step / "state_out.json").write_text(json.dumps(state))
    (run / "resolved.json").write_text(
        json.dumps({"TECH_LEFS": {"nom_*": str(tmp_path / "tech.lef")}})
    )
    return work, run


def test_the_flow_resumes_from_the_patched_layout(tmp_path):
    work, run = _routed_run(tmp_path, _net("net14185", STUB))
    cfg = {"DESIGN_NAME": "chipleap_accel", "FP_CORE_UTIL": 45}
    routed = pnr.step_state(run, pnr.DETAILED_ROUTING)
    assert routed == run / "45-openroad-detailedrouting" / "state_out.json"
    rest, state_in = pnr.patch_min_area(work, run, routed, cfg)
    assert rest == "OpenROAD.DEFtoODB" and state_in is not None
    state = json.loads(state_in.read_text())
    assert state["def"] == str(work / "minarea" / "chipleap_accel.def")
    assert "RECT ( 0 0 210 90 )" in (work / "minarea" / "chipleap_accel.def").read_text()
    assert "net14185" in (work / "minarea" / "patches.txt").read_text()
    resumed = json.loads((work / pnr.MIN_AREA_CONFIG).read_text())
    assert resumed["FP_CORE_UTIL"] == 45
    assert resumed["meta"]["substituting_steps"] == {
        "+OpenROAD.DetailedRouting": "OpenROAD.DEFtoODB"
    }


def test_a_clean_layout_resumes_where_routing_stopped(tmp_path):
    work, run = _routed_run(
        tmp_path, _net("ok", "\n      + ROUTED Metal2 ( 5000 5000 ) ( * 7000 )")
    )
    routed = pnr.step_state(run, pnr.DETAILED_ROUTING)
    assert pnr.patch_min_area(work, run, routed, {}) == ("Odb.RemoveRoutingObstructions", None)
    assert not (work / "minarea").exists()


PART1_METRICS = {
    "route__drc_errors": 0,
    "timing__setup__ws": 1.3,
    "timing__setup__ws__corner:nom_typ_1p20V_25C": -13093539370310584,
}


def _fake_librelane(tmp_path, monkeypatch, second, calls):
    """LibreLane in two parts: the first routes and, as LibreLane does at the end of every
    invocation, writes final/ with what it has; ``second(run, log)`` is the rest and
    returns its exit code."""
    tool = tmp_path / "librelane"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(tool))

    def fake(cmd, workdir, log, timeout, limit, env):
        calls.append(cmd[5:])
        run = workdir / "runs" / "chipleap"
        log.write_text("")
        if "--to" not in cmd:
            return second(run, log)
        step = run / "45-openroad-detailedrouting"
        step.mkdir(parents=True)
        (tmp_path / "tech.lef").write_text(TECH)
        (step / "chipleap_accel.def").write_text(_def(_net("net14185", STUB)))
        (step / "state_out.json").write_text(json.dumps({"def": str(step / "chipleap_accel.def")}))
        (run / "resolved.json").write_text(
            json.dumps({"TECH_LEFS": {"nom_*": str(tmp_path / "tech.lef")}})
        )
        (run / "final").mkdir()
        (run / "final" / "metrics.json").write_text(json.dumps(PART1_METRICS))
        return 0

    monkeypatch.setattr(pnr, "_librelane", fake)
    (tmp_path / "pnr").mkdir()
    return tmp_path / "pnr" / "runs" / "chipleap"


def _finishes(metrics):
    def second(run, log):
        (run / "final").mkdir()
        (run / "final" / "metrics.json").write_text(json.dumps(metrics))
        return 0

    return second


def test_run_librelane_routes_then_finishes(tmp_path, monkeypatch):
    calls = []
    run = _fake_librelane(tmp_path, monkeypatch, _finishes({"timing__setup__ws": 0.5}), calls)
    metrics, gds = pnr.run_librelane(tmp_path / "pnr", {})
    assert metrics == {"timing__setup__ws": 0.5} and gds is None
    assert calls[0] == ["--overwrite", "--to", "OpenROAD.DetailedRouting", "config.json"]
    assert calls[1][:2] == ["--from", "OpenROAD.DEFtoODB"]
    assert calls[1][-1] == pnr.MIN_AREA_CONFIG
    # The first part's final/ is kept beside the run's, not read and not deleted.
    kept = json.loads((run / pnr.ROUTED_FINAL / "metrics.json").read_text())
    assert kept == PART1_METRICS


@pytest.mark.parametrize("code", [2, 0])
def test_a_second_part_that_stops_is_not_read_from_the_first_parts_final(
    tmp_path, monkeypatch, code
):
    """LibreLane wrote final/ after routing; the rest stopped (KLayout's DRC crashed, or a
    checker ended the flow) and wrote none. That is a run that did not finish, never the
    first part's metrics read as a signoff."""
    calls = []
    run = _fake_librelane(tmp_path, monkeypatch, lambda run, log: code, calls)
    with pytest.raises(pnr.PnrError, match=f"LibreLane did not finish \\(exit {code}\\)"):
        pnr.run_librelane(tmp_path / "pnr", {})
    assert not (run / "final").exists() and (run / pnr.ROUTED_FINAL / "metrics.json").is_file()


def test_a_second_part_that_ends_on_an_error_is_not_read(tmp_path, monkeypatch):
    def second(run, log):
        _finishes({"timing__setup__ws": 0.5})(run, log)
        return 2

    _fake_librelane(tmp_path, monkeypatch, second, [])
    with pytest.raises(pnr.PnrError, match=r"did not finish \(exit 2\)"):
        pnr.run_librelane(tmp_path / "pnr", {})


def test_slacks_no_timing_analysis_gives_are_refused(tmp_path, monkeypatch):
    _fake_librelane(tmp_path, monkeypatch, _finishes(PART1_METRICS), [])
    with pytest.raises(pnr.PnrError, match=r"nom_typ_1p20V_25C = -1\.30935e\+16 ns"):
        pnr.run_librelane(tmp_path / "pnr", {})
    pnr.check_metrics({"timing__setup__ws": -31.79, "timing__setup__tns": -2.5e7})
    # No path in a group is a slack of plus infinity: the memory chip's flip-flops all
    # face its ports or its macro, so it has no register-to-register hold path.
    pnr.check_metrics({"timing__hold_r2r__ws__corner:nom_fast_1p32V_m40C": float("inf")})
    for bad in (float("-inf"), float("nan"), 2e6, -1.3e16):
        with pytest.raises(pnr.PnrError, match="no timing analysis gives"):
            pnr.check_metrics({"timing__hold__ws__corner:nom_fast_1p32V_m40C": bad})


def test_a_group_without_paths_is_read(tmp_path, monkeypatch):
    """LibreLane writes a group with no path as Infinity, as for the memory chip's
    register-to-register paths."""
    inf = float("inf")
    metrics = {
        "timing__setup__ws": 6.049,
        "timing__hold__ws": 0.2,
        "timing__setup_r2r__ws": inf,
        "timing__hold_r2r__ws": inf,
        "timing__hold_r2r__ws__corner:nom_fast_1p32V_m40C": inf,
    }
    _fake_librelane(tmp_path, monkeypatch, _finishes(metrics), [])
    assert pnr.run_librelane(tmp_path / "pnr", {"CLOCK_PORT": "clk_i"})[0] == metrics


def test_a_design_with_no_path_timed_is_refused(tmp_path, monkeypatch):
    """A clock constraint that names no port of the design times nothing: every worst
    slack is plus infinity, and that is no signoff."""
    inf = float("inf")
    metrics = {
        "timing__setup__ws": inf,
        "timing__hold__ws": inf,
        "timing__setup__ws__corner:nom_slow_1p08V_125C": inf,
        "timing__setup_r2r__ws": inf,
    }
    _fake_librelane(tmp_path, monkeypatch, _finishes(metrics), [])
    with pytest.raises(pnr.PnrError, match="no path was timed against clock clk_typo"):
        pnr.run_librelane(tmp_path / "pnr", {"CLOCK_PORT": "clk_typo"})
    with pytest.raises(pnr.PnrError, match="against clock a, b: "):
        pnr.check_metrics(metrics, ["a", "b"])
    with pytest.raises(pnr.PnrError, match=r"^no path was timed: "):
        pnr.check_metrics(metrics)


def test_a_final_older_than_the_second_part_is_refused(tmp_path):
    import os

    run = tmp_path / "run"
    (run / "final").mkdir(parents=True)
    (run / "final" / "metrics.json").write_text("{}")
    marker = pnr.begin_second_part(run)
    assert not (run / "final").exists() and marker == run / pnr.SECOND_PART
    (run / "final").mkdir()
    (run / "final" / "metrics.json").write_text('{"timing__setup__ws": 0.5}')
    assert pnr.second_part_result(run, 0, marker) == ({"timing__setup__ws": 0.5}, None)
    old = marker.stat().st_mtime - 60
    os.utime(run / "final" / "metrics.json", (old, old))
    with pytest.raises(pnr.PnrError, match="older than the second part"):
        pnr.second_part_result(run, 0, marker)
    # A run whose first part's final/ is already set aside is not one the first part made.
    with pytest.raises(pnr.PnrError, match="is there already"):
        pnr.begin_second_part(run)


def test_run_librelane_stops_after_a_step_then_resumes_through_routing(tmp_path, monkeypatch):
    calls = []
    tool = tmp_path / "librelane"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(tool))

    def fake(cmd, workdir, log, timeout, limit, env):
        calls.append((cmd[5:], log.name))
        run = workdir / "runs" / "chipleap"
        if "Yosys.Synthesis" in cmd:
            step = run / "02-yosys-synthesis"
            step.mkdir(parents=True)
            (step / "state_out.json").write_text("{}")
        elif "OpenROAD.DetailedRouting" in cmd:
            step = run / "45-openroad-detailedrouting"
            step.mkdir(parents=True)
            (tmp_path / "tech.lef").write_text(TECH)
            (step / "chipleap_accel.def").write_text(
                _def(_net("ok", "\n      + ROUTED Metal2 ( 5000 5000 ) ( * 7000 )"))
            )
            (step / "state_out.json").write_text(
                json.dumps({"def": str(step / "chipleap_accel.def")})
            )
            (run / "resolved.json").write_text(
                json.dumps({"TECH_LEFS": {"nom_*": str(tmp_path / "tech.lef")}})
            )
        else:
            (run / "final").mkdir()
            (run / "final" / "metrics.json").write_text('{"timing__setup__ws": 0.5}')
        log.write_text("")
        return 0

    monkeypatch.setattr(pnr, "_librelane", fake)
    work = tmp_path / "pnr"
    work.mkdir()
    assert pnr.run_librelane(work, {}, to="Yosys.Synthesis") == ({}, None)
    metrics, _ = pnr.run_librelane(work, {}, resume_from="Checker.YosysUnmappedCells")
    assert metrics == {"timing__setup__ws": 0.5}
    assert calls == [
        (
            ["--overwrite", "--to", "Yosys.Synthesis", "config.json"],
            "librelane-yosys-synthesis.log",
        ),
        (
            [
                "--from",
                "Checker.YosysUnmappedCells",
                "--to",
                "OpenROAD.DetailedRouting",
                "config.json",
            ],
            "librelane-route.log",
        ),
        (["--from", "Odb.RemoveRoutingObstructions", "config.json"], "librelane.log"),
    ]


def test_run_librelane_fails_when_the_first_part_stops_early(tmp_path, monkeypatch):
    tool = tmp_path / "librelane"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_LIBRELANE", str(tool))

    def fake(cmd, workdir, log, timeout, limit, env):
        log.write_text("synthesis failed\n")
        return 0

    monkeypatch.setattr(pnr, "_librelane", fake)
    (tmp_path / "pnr").mkdir()
    with pytest.raises(pnr.PnrError, match=r"stopped before Yosys\.Synthesis"):
        pnr.run_librelane(tmp_path / "pnr", {}, to="Yosys.Synthesis")


def test_def_to_odb_follows_routing_in_a_step_list_or_as_a_substitution():
    steps = ["OpenROAD.DetailedRouting", "OpenROAD.DEFtoODB", "KLayout.DRC"]
    listed = pnr.with_def_to_odb({"meta": {"version": 2, "flow": steps}})
    assert listed["meta"]["flow"] == steps  # once, after routing
    chip = {"meta": {"version": 2, "flow": "Classic", "substituting_steps": {"A.B": None}}}
    assert pnr.with_def_to_odb(chip)["meta"]["substituting_steps"] == {
        "A.B": None,
        "+OpenROAD.DetailedRouting": "OpenROAD.DEFtoODB",
    }
    assert chip["meta"]["substituting_steps"] == {"A.B": None}  # the original is unchanged
    with pytest.raises(pnr.PnrError, match=r"no OpenROAD\.DetailedRouting"):
        pnr.with_def_to_odb({"meta": {"flow": ["KLayout.DRC"]}})


@pytest.mark.parametrize("finishes", [True, False])
def test_an_ecos_second_part_is_read_only_from_its_own_final(tmp_path, monkeypatch, finishes):
    from types import SimpleNamespace

    from chipleap.rtl import eco

    work = tmp_path / "pnr"
    work.mkdir()
    run = work / "runs" / "eco"

    def fake(cmd, w, log, timeout, env):
        log.write_text("")
        if "--to" not in cmd:
            if finishes:
                _finishes({"timing__setup__ws": 0.2})(run, log)
            return 0 if finishes else 2
        step = run / "45-openroad-detailedrouting"
        step.mkdir(parents=True)
        (tmp_path / "tech.lef").write_text(TECH)
        clean = _net("ok", "\n      + ROUTED Metal2 ( 5000 5000 ) ( * 7000 )")
        (step / "counter.def").write_text(_def(clean))
        (step / "state_out.json").write_text(json.dumps({"def": str(step / "counter.def")}))
        (run / "resolved.json").write_text(
            json.dumps({"TECH_LEFS": {"nom_*": str(tmp_path / "tech.lef")}})
        )
        (run / "final").mkdir()
        (run / "final" / "metrics.json").write_text(json.dumps(PART1_METRICS))
        return 0

    monkeypatch.setattr(eco, "librelane", lambda: SimpleNamespace(found=True, path="ll"))
    monkeypatch.setattr(eco, "librelane_pdk_args", lambda: [])
    monkeypatch.setattr(eco, "librelane_env", lambda: {})
    monkeypatch.setattr(eco, "_launch", fake)
    state = tmp_path / "state_in.json"
    state.write_text("{}")
    if finishes:
        assert eco._run_librelane(work, {}, state) == ({"timing__setup__ws": 0.2}, None)
    else:
        with pytest.raises(eco.EcoError, match=r"did not finish the ECO \(exit 2\)"):
            eco._run_librelane(work, {}, state)
    assert (run / pnr.ROUTED_FINAL / "metrics.json").is_file()
