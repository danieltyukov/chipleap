"""The chip timed at its ports, through its pads: the budget, the constraints, the IO
liberty's arcs and the reports. OpenSTA itself runs in the gated chip test."""

from __future__ import annotations

from pathlib import Path

import pytest

from chipleap.chip import assemble, io, iotiming
from chipleap.chip.config import ChipConfig, IoBudget, Pad
from chipleap.chip.netlist import Port, bind
from chipleap.rtl.project import ProjectError, load_project

FIXTURES = Path(__file__).parent / "fixtures" / "chip"


def binding():
    pads = {
        "south": (Pad("signal", "clk"), Pad("signal", "a"), Pad("vdd"), Pad("vss")),
        "east": (Pad("signal", "y"), Pad("iovdd"), Pad("iovss")),
        "west": (Pad("analog", "v"),),
    }
    cfg = ChipConfig("c", pads, (), 100.0, None)
    ports = {
        "clk": Port("clk", "input", 1),
        "a": Port("a", "input", 1),
        "y": Port("y", "output", 1),
    }
    return bind(cfg, ports, {}, {}, ("clk",), "core")


def test_the_budget_defaults_to_a_fifth_of_the_period():
    assert iotiming.budget(IoBudget(), 10.0) == {
        "input_delay_ns": 2.0,
        "output_delay_ns": 2.0,
        "output_load_pf": 5.0,
        "input_transition_ns": 1.0,
    }
    b = iotiming.budget(IoBudget(input_delay_ns=0.5, output_load_pf=2.0), 10.0)
    assert b["input_delay_ns"] == 0.5 and b["output_delay_ns"] == 2.0 and b["output_load_pf"] == 2.0


def test_the_ports_are_constrained_at_the_pads():
    b = binding()
    assert iotiming.pad_ports(b) == (["pad_a", "pad_clk"], ["pad_y"])
    sdc = iotiming.signoff_sdc(b, {"clk": 10.0}, iotiming.budget(IoBudget(), 10.0))
    assert "create_clock -name clk -period 10 [get_ports pad_clk]" in sdc
    assert "set_input_delay 2 -clock clk [get_ports {{pad_a}}]" in sdc
    assert "set_input_transition 1 [get_ports {{pad_a} {pad_clk}}]" in sdc
    assert "set_output_delay 2 -clock clk [get_ports {{pad_y}}]" in sdc
    assert "set_load 5 [get_ports {{pad_y}}]" in sdc
    assert "p2c" not in sdc and "c2p" not in sdc


LIB = """
library (io) {
  cell (sg13g2_IOPadIn) {
    pin (pad) { direction : input; }
    pin (p2c) {
      direction : output;
      timing () { related_pin : "pad"; timing_type : combinational; }
    }
  }
  cell (sg13g2_IOPadOut4mA) {
    pin (c2p) { direction : input; }
    pin (pad) { direction : output; }
  }
}
"""


def test_a_pad_without_an_arc_is_reported_not_timed():
    assert iotiming.missing_arcs(LIB, ["sg13g2_IOPadIn"]) == []
    assert iotiming.missing_arcs(LIB, ["sg13g2_IOPadIn", "sg13g2_IOPadOut4mA"]) == [
        "sg13g2_IOPadOut4mA has no c2p to pad arc"
    ]
    # A cell the library does not hold at all has no arc either.
    assert iotiming.missing_arcs("library (io) { }", ["sg13g2_IOPadIn"]) == [
        "sg13g2_IOPadIn has no pad to p2c arc"
    ]


def test_ihps_io_library_has_the_pads_arcs_at_every_corner():
    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("IHP-Open-PDK is not installed")
    for corner in iotiming.IO_CORNERS.values():
        lib = pdk / "libs.ref" / "sg13g2_io" / "lib" / f"sg13g2_io_{corner}.lib"
        assert iotiming.missing_arcs(lib.read_text(), iotiming.PAD_ARCS) == [], corner


def test_the_corners_come_from_librelanes_signoff():
    resolved = {
        "STA_CORNERS": ["nom_fast_1p32V_m40C", "nom_slow_1p08V_125C", "nom_typ_1p20V_25C"],
        "LIB": {
            "nom_fast_1p32V_m40C": ["/pdk/fast.lib"],
            "nom_slow_1p08V_125C": ["/pdk/slow.lib"],
            "nom_typ_1p20V_25C": ["/pdk/typ.lib"],
        },
    }
    assert iotiming.corners(resolved) == {
        "fast": [Path("/pdk/fast.lib")],
        "slow": [Path("/pdk/slow.lib")],
        "typ": [Path("/pdk/typ.lib")],
    }
    with pytest.raises(iotiming.IoTimingError, match="no slow corner"):
        iotiming.corners({"STA_CORNERS": ["nom_typ_1p20V_25C"], "LIB": resolved["LIB"]})


def test_openstas_report_is_read():
    log = (FIXTURES / "io_timing_slow.log").read_text()
    assert iotiming.parse_worst(log) == (0.9706, 0.5974)
    paths = iotiming.parse_categories(log, {"pad_west_0"})
    worst = paths["core_to_pad_setup"][0]
    assert worst["startpoint"] == "_262_" and worst["endpoint"] == "pad_rx_data_o_7"
    assert worst["slack_ns"] == 0.9706 and worst["pad_delay_ns"] == 5.4264
    assert paths["pad_to_pad_setup"] == []
    assert iotiming.parse_worst("CHIPLEAP-WORST setup INF\nCHIPLEAP-WORST hold 1e30\n") == (
        None,
        None,
    )


def test_the_script_reads_everything_and_reports_by_category():
    files = {"lefs": ["a.lef"], "libs": ["a.lib"], "netlist": "n.v", "spef": "s.spef"}
    text = iotiming.script(files, "c", "c.sdc", ["pad_a"], ["pad_y"], True)
    assert "read_spef {s.spef}" in text and "link_design {c}" in text
    for name in ("pad_to_core", "core_to_pad", "pad_to_pad"):
        assert f"CHIPLEAP-PATHS begin {name} max" in text
    assert "report_checks" not in iotiming.script(files, "c", "c.sdc", [], [], False)


def chip_data(**io_timing):
    timing = {
        "corners": [{"corner": "slow", "setup_slack_ns": 0.97, "hold_slack_ns": 0.6}],
        "paths": {},
        "untimed_pads": [],
        "budget": {},
        "met": True,
        **io_timing,
    }
    return {
        "name": "c",
        "outline_um": [-60.0, -60.0, 1460.0, 1460.0],
        "pads": 4,
        "macros": [],
        "route_drc_errors": 0,
        "antenna_nets": 0,
        "lvs": {"result": "pass"},
        "drc": {"violations": 0, "rules": {}},
        "supply_hookup": {"VDD": {"east": 1}, "VSS": {"east": 1}},
        "precheck": [],
        "setup_slack_ns": 6.3,
        "hold_slack_ns": 0.1,
        "io_timing": timing,
    }


def test_the_chip_fails_when_its_ports_miss_timing_or_a_pad_is_not_timed():
    ok, summary = assemble.verdict(chip_data())
    assert ok and "+0.970 ns at the slow corner" in summary
    late = chip_data(corners=[{"corner": "slow", "setup_slack_ns": -0.5, "hold_slack_ns": 0.1}])
    ok, summary = assemble.verdict(late)
    assert not ok and "setup missed at the ports at the slow corner (-0.5 ns)" in summary
    ok, summary = assemble.verdict(
        chip_data(untimed_pads=["sg13g2_IOPadOut4mA has no c2p to pad arc"])
    )
    assert not ok and "not timed: sg13g2_IOPadOut4mA" in summary


def test_the_budget_is_read_from_the_chip_section(counter_project):
    from chip_fakes import COUNTER_CHIP

    root = counter_project
    text = (root / "project.yaml").read_text() + COUNTER_CHIP
    (root / "project.yaml").write_text(
        text + "  io: {input_delay_ns: 1.5, output_delay_ns: 3, output_load_pf: 2}\n"
    )
    p = load_project(root)
    assert p.chip.io == IoBudget(1.5, 3.0, 2.0, 1.0)
    assert p.to_dict()["chip"]["io"] == {
        "input_delay_ns": 1.5,
        "output_delay_ns": 3.0,
        "output_load_pf": 2.0,
        "input_transition_ns": 1.0,
    }
    for bad, message in (
        ("{input_delay_ns: -1}", "from 0 to"),
        ("{output_load_pf: true}", "from 0 to"),
        ("{slew: 1}", "unknown key"),
    ):
        (root / "project.yaml").write_text(text + f"  io: {bad}\n")
        with pytest.raises(ProjectError, match=message):
            load_project(root)


def test_the_report_shows_the_ports_timing(counter_project):
    from chipleap.rtl.report import project_markdown
    from chipleap.verify.result import StepResult

    data = chip_data(
        paths={
            "core_to_pad_setup": [
                {"startpoint": "_262_", "endpoint": "pad_y", "slack_ns": 0.97, "pad_delay_ns": 5.4}
            ]
        }
    )
    data.update(gds="build/chip/c.gds")
    md = project_markdown(load_project(counter_project), [StepResult("chip", "pass", "ok", data)])
    assert "| setup slack, slow, at the ports | +0.970 ns |" in md
    assert "| core to pad setup | _262_ | pad_y | +0.970 ns | 5.400 ns |" in md
