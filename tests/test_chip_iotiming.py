"""The chip timed at its ports, through its pads: the budget, the constraints, the IO
liberty's arcs and the reports. OpenSTA itself runs in the gated chip test."""

from __future__ import annotations

import json
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
    sdc = iotiming.signoff_sdc(b, {"clk": 10.0}, iotiming.budget(IoBudget(), 10.0), 0.25)
    assert "create_clock -name clk -period 10 [get_ports pad_clk]" in sdc
    assert "set_clock_uncertainty 0.25 [all_clocks]" in sdc
    assert "set_input_delay 2 -clock clk [get_ports {{pad_a}}]" in sdc
    assert "set_input_transition 1 [get_ports {{pad_a} {pad_clk}}]" in sdc
    assert "set_output_delay 2 -clock clk [get_ports {{pad_y}}]" in sdc
    assert "set_load 5 [get_ports {{pad_y}}]" in sdc
    assert "p2c" not in sdc and "c2p" not in sdc


LIB = """
/* IHP's own layout, cut down: a comment { with a brace } is not a group. */
library (io) {
  cell (sg13g2_IOPadIn) {
    pin (pad) { direction : input; }
    pin (p2c) {
      direction : output;
      timing () {
        related_pin : "pad"; timing_type : combinational;
        cell_rise (t) { index_1 ("0.12, 1.2, 3.5"); index_2 ("0.024, 0.24"); \\
          values ("1, 2", "3, 4", "5, 6"); }
        cell_fall (t) { index_1 ("0.12, 1.2, 3.5"); index_2 ("0.024, 0.24"); }
      }
    }
  }
  cell (sg13g2_IOPadOut4mA) {
    pin (c2p) { direction : input; }
    pin (pad) {
      direction : output;
      internal_power () { related_pin : "c2p"; rise_power (p) { values ("1"); } }
    }
  }
}
"""


def test_a_pad_without_an_arc_is_reported_not_timed():
    assert iotiming.missing_arcs(LIB, ["sg13g2_IOPadIn"]) == {}
    # The output pad's pin has internal power related to c2p, but no timing group.
    assert iotiming.missing_arcs(LIB, ["sg13g2_IOPadIn", "sg13g2_IOPadOut4mA"]) == {
        "sg13g2_IOPadOut4mA": "sg13g2_IOPadOut4mA has no c2p to pad arc"
    }
    # A cell the library does not hold at all has no arc either.
    assert iotiming.missing_arcs("library (io) { }", ["sg13g2_IOPadIn"]) == {
        "sg13g2_IOPadIn": "sg13g2_IOPadIn is not in the IO liberty"
    }
    # Nor does a pad cell whose arc ChipLeap does not know.
    assert iotiming.missing_arcs(LIB, ["sg13g2_IOPadOut16mA"]) == {
        "sg13g2_IOPadOut16mA": "sg13g2_IOPadOut16mA has no pad arc ChipLeap knows"
    }


def test_an_arc_needs_a_delay_for_both_edges():
    rise_only = LIB.replace(
        'cell_fall (t) { index_1 ("0.12, 1.2, 3.5"); index_2 ("0.024, 0.24"); }', ""
    )
    assert iotiming.missing_arcs(rise_only, ["sg13g2_IOPadIn"]) == {
        "sg13g2_IOPadIn": "sg13g2_IOPadIn's pad to p2c arc has no cell_fall table"
    }
    other = LIB.replace('related_pin : "pad"', 'related_pin : "padres"')
    assert iotiming.missing_arcs(other, ["sg13g2_IOPadIn"]) == {
        "sg13g2_IOPadIn": "sg13g2_IOPadIn has no pad to p2c arc"
    }


def test_a_budget_outside_the_pads_characterisation_is_a_warning():
    numbers = iotiming.budget(IoBudget(input_transition_ns=1.0), 10.0)
    assert iotiming.characterised(LIB, numbers, ["sg13g2_IOPadIn"]) == []
    numbers = iotiming.budget(IoBudget(input_transition_ns=5.0), 10.0)
    assert iotiming.characterised(LIB, numbers, ["sg13g2_IOPadIn"]) == [
        "chip.io.input_transition_ns 5 ns is outside the 0.12 to 3.5 ns sg13g2_IOPadIn is "
        "characterised for; its delay is extrapolated"
    ]


SIGNOFF = (("typ", 1.2, 25.0), ("slow", 1.08, 125.0), ("fast", 1.32, -40.0))


def test_ihps_io_library_has_the_pads_arcs_at_every_corner():
    pdk = io.pdk_dir()
    if pdk is None:
        pytest.skip("IHP-Open-PDK is not installed")
    io_dir = pdk / "libs.ref" / "sg13g2_io" / "lib"
    for process, volts, temp in SIGNOFF:
        lib = iotiming.io_library(io_dir, process, volts, temp)
        assert iotiming.missing_arcs(lib.read_text(), iotiming.PAD_ARCS) == {}, lib.name
        numbers = iotiming.budget(IoBudget(), 10.0)
        assert iotiming.characterised(lib.read_text(), numbers, iotiming.PAD_ARCS) == []


def fake_signoff(tmp_path):
    root = tmp_path / "ciel"
    (root / "lib").mkdir(parents=True)
    io_dir = tmp_path / "io"
    io_dir.mkdir()
    libs = {}
    for process, name in (("typ", "1p20V_25C"), ("slow", "1p08V_125C"), ("fast", "1p32V_m40C")):
        f = root / "lib" / f"{process}.lib"
        f.write_text("library (x) { }")
        libs[f"*_{process}_{name}"] = [str(f)]
    for name in (
        "typ_1p2V_3p3V_25C",
        "typ_1p5V_3p3V_25C",
        "slow_1p08V_3p0V_125C",
        "slow_1p35V_3p0V_125C",
        "fast_1p32V_3p6V_m40C",
        "fast_1p65V_3p6V_m40C",
    ):
        (io_dir / f"sg13g2_io_{name}.lib").write_text("library (io) { }")
    resolved = {
        "PDK_ROOT": str(root),
        "STA_CORNERS": [
            "nom_fast_1p32V_m40C",
            "nom_slow_1p08V_125C",
            "max_slow_1p08V_125C",
            "nom_typ_1p20V_25C",
        ],
        "LIB": libs,
        "CLOCK_UNCERTAINTY_CONSTRAINT": 0.25,
    }
    return resolved, io_dir


def test_the_corners_come_from_librelanes_signoff(tmp_path):
    resolved, io_dir = fake_signoff(tmp_path)
    found = iotiming.corners(resolved, io_dir)
    # One per process, a max_ (parasitics) corner timed as its nom_ one; the IO liberty
    # at the core's supply and temperature, not the 1.5 V core's.
    assert [(c.name, c.process, c.io_lib.name) for c in found] == [
        ("nom_typ_1p20V_25C", "typ", "sg13g2_io_typ_1p2V_3p3V_25C.lib"),
        ("nom_slow_1p08V_125C", "slow", "sg13g2_io_slow_1p08V_3p0V_125C.lib"),
        ("nom_fast_1p32V_m40C", "fast", "sg13g2_io_fast_1p32V_3p6V_m40C.lib"),
    ]
    assert found[1].std_libs == (tmp_path / "ciel" / "lib" / "slow.lib",)
    assert iotiming.uncertainty(resolved) == 0.25


def test_a_corner_chipleap_cannot_match_is_an_error(tmp_path):
    resolved, io_dir = fake_signoff(tmp_path)
    for corners, message in (
        (["nom_typ_1p20V_25C"], "no slow corner"),
        (["nom_ss_1p08V_125C"], "has no IO liberty ChipLeap knows"),
        (["nom_slow_1p50V_125C"], "no liberty for nom_slow_1p50V_125C"),
    ):
        with pytest.raises(iotiming.IoTimingError, match=message):
            iotiming.corners({**resolved, "STA_CORNERS": corners}, io_dir)
    libs = {**resolved["LIB"], "*_slow_1p50V_125C": [resolved["LIB"]["*_slow_1p08V_125C"][0]]}
    with pytest.raises(iotiming.IoTimingError, match=r"no slow liberty at 1\.5 V and 125 C"):
        iotiming.corners({**resolved, "LIB": libs, "STA_CORNERS": ["nom_slow_1p50V_125C"]}, io_dir)
    with pytest.raises(iotiming.IoTimingError, match="no clock uncertainty"):
        iotiming.uncertainty({"CLOCK_UNCERTAINTY_CONSTRAINT": "0.25"})


def test_the_standard_cells_must_be_librelanes_pdk(tmp_path):
    """A liberty LibreLane names outside the PDK it installed (one in the project, say)
    is refused, not read."""
    resolved, io_dir = fake_signoff(tmp_path)
    elsewhere = tmp_path / "project" / "slow.lib"
    elsewhere.parent.mkdir()
    elsewhere.write_text("library (x) { }")
    for path in (str(elsewhere), str(tmp_path / "ciel" / ".." / "project" / "slow.lib")):
        libs = {**resolved["LIB"], "*_slow_1p08V_125C": [path]}
        with pytest.raises(iotiming.IoTimingError, match="outside its PDK"):
            iotiming.corners({**resolved, "LIB": libs}, io_dir)
    link = tmp_path / "ciel" / "lib" / "link.lib"
    link.symlink_to(elsewhere)
    libs = {**resolved["LIB"], "*_slow_1p08V_125C": [str(link)]}
    with pytest.raises(iotiming.IoTimingError, match="outside its PDK"):
        iotiming.corners({**resolved, "LIB": libs}, io_dir)
    with pytest.raises(iotiming.IoTimingError, match="names no PDK"):
        iotiming.corners({**resolved, "PDK_ROOT": "relative"}, io_dir)


def test_openstas_report_is_read():
    """A slow-corner log of the UART chip, cut to one path per category."""
    log = (FIXTURES / "io_timing_slow.log").read_text()
    assert iotiming.parse_worst(log) == (0.9706, 0.5974)
    pads = {"pad_west_0/pad", "pad_south_6/p2c"}
    paths = iotiming.parse_categories(log, pads)
    worst = paths["core_to_pad_setup"][0]
    assert worst["startpoint"] == "_262_" and worst["endpoint"] == "pad_rx_data_o_7"
    # The output pad's own arc, not the 0.0181 ns wire into its c2p pin.
    assert worst["slack_ns"] == 0.9706 and worst["pad_delay_ns"] == 5.4083
    reset = paths["pad_to_core_setup"][0]
    assert reset["startpoint"] == "pad_rst_ni" and reset["pad_delay_ns"] == 0.4361
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
    assert "-fields {input_pin}" in text
    # Pad to pad needs both an input and an output pad.
    assert "if {[llength $data_in] > 0 && [llength $data_out] > 0} {" in text
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
    assert "| paths through macros |" not in md
    assert "| core to pad setup | _262_ | pad_y | +0.970 ns | 5.400 ns |" in md


def test_a_run_times_every_corner_and_names_the_pads_it_cannot_time(tmp_path, monkeypatch):
    """The whole run on a stand-in OpenROAD that prints the UART's slow-corner log: the
    IO liberty of the slow corner has no arc for the output pad, so its port is listed
    as not timed and the chip's IO timing fails, whatever the slack says."""
    resolved, io_dir = fake_signoff(tmp_path)
    lefs = tmp_path / "ciel" / "lef"
    lefs.mkdir()
    for f in ("tech.lef", "cells.lef"):
        (lefs / f).write_text("VERSION 5.8 ;\n")
    resolved.update(
        TECH_LEFS={"nom_*": str(lefs / "tech.lef")}, CELL_LEFS=[str(lefs / "cells.lef")]
    )
    arc = 'timing () { related_pin : "c2p"; cell_rise (t) { } cell_fall (t) { } }'
    full = LIB.replace("internal_power () {", arc + " internal_power () {")
    for f in io_dir.iterdir():
        f.write_text(LIB if "_slow_" in f.name else full)
    pdk = tmp_path / "pdk"
    (pdk / "libs.ref" / "sg13g2_io").mkdir(parents=True)
    (pdk / "libs.ref" / "sg13g2_io" / "lib").symlink_to(io_dir)
    run_dir = tmp_path / "run"
    for rel in ("final/nl/c.nl.v", "final/spef/nom/c.nom.spef"):
        (run_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / rel).write_text("x\n")
    (run_dir / "resolved.json").write_text(json.dumps(resolved))
    io_lef = tmp_path / "io.lef"
    io_lef.write_text("VERSION 5.8 ;\n")
    tool = tmp_path / "bin" / "openroad"
    tool.parent.mkdir()
    tool.write_text(f"#!/bin/sh\ncat {FIXTURES / 'io_timing_slow.log'}\n")
    tool.chmod(0o755)
    monkeypatch.setenv("CHIPLEAP_OPENROAD", str(tool))
    work = tmp_path / "work"
    work.mkdir()
    b = binding()
    cells = {b.chip_ports["pad_a"]: "sg13g2_IOPadIn", b.chip_ports["pad_clk"]: "sg13g2_IOPadIn"}
    cells[b.chip_ports["pad_y"]] = "sg13g2_IOPadOut4mA"
    t = iotiming.run(work, run_dir, pdk, "c", b, cells, {"clk": 10.0}, IoBudget(), [], io_lef)
    assert [c.corner for c in t.corners] == ["typ", "slow", "fast"]
    assert t.untimed == ["sg13g2_IOPadOut4mA has no c2p to pad arc (slow corner): pad_y not timed"]
    assert not t.met and t.to_dict()["untimed_pads"] == t.untimed
    sdc = (work / "io_timing_slow" / "chip_io.sdc").read_text()
    assert "create_clock -name clk -period 10 [get_ports pad_clk]" in sdc
    script = (work / "io_timing_slow" / "io_timing_slow.tcl").read_text()
    assert "read_liberty {inputs/io.lib}" in script and "report_checks" in script
    assert "report_checks" not in (work / "io_timing_typ" / "io_timing_typ.tcl").read_text()
