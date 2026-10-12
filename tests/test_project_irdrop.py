"""IR drop: OpenROAD's script and PDNSim's report, the voltage file and the map."""

import xml.etree.ElementTree as ET

import pytest

from chipleap.rtl import irdrop as I
from chipleap.rtl.project import load_project

LOG = """
CHIPLEAP-IR net 0 GROUND VGND
[INFO PSM-0040] All shapes on net VGND are connected.
########## IR report #################
Net              : VGND
Corner           : default
Total power      : 4.37e-04 W
Supply voltage   : 0.00e+00 V
Worstcase voltage: 1.14e-03 V
Average voltage  : 5.49e-04 V
Average IR drop  : 5.49e-04 V
Worstcase IR drop: 1.14e-03 V
Percentage drop  : 0.10 %
######################################
CHIPLEAP-IR net 1 POWER VPWR
Supply voltage   : 1.20e+00 V
Worstcase voltage: 1.20e+00 V
Average IR drop  : 2.65e-04 V
Worstcase IR drop: 6.28e-04 V
Percentage drop  : 0.05 %
CHIPLEAP-IR end
"""

CSV = """Instance,Terminal,Layer,X location,Y location,Voltage
FILLER_0_0,VDD,Metal1,7.4490,18.3010,1.199370
_143_,VDD,Metal1,56.8890,18.3010,1.199500
_150_,VDD,Metal1,20.0,60.0,1.199900
broken,VDD,Metal1,x,y,z
"""


def test_report_and_voltages():
    nets = I.parse_ir(LOG)
    assert [(i, k, n) for i, k, n, _ in nets] == [(0, "GROUND", "VGND"), (1, "POWER", "VPWR")]
    assert nets[1][3] == {
        "supply": 1.2,
        "worst_voltage": 1.2,
        "average": 2.65e-4,
        "worst": 6.28e-4,
        "percent": 0.05,
    }
    pts = I.parse_voltages(CSV, "POWER", 1.2)
    assert len(pts) == 3 and pts[1][0] == "_143_" and pts[1][3] == pytest.approx(0.5e-3)
    net = I.NetDrop("VPWR", "POWER", 1.2, 6.28e-4, 2.65e-4, 0.05, pts)
    # Fill cells draw no current, so they are not among the worst instances.
    assert [i for i, _ in net.worst_instances] == ["_143_", "_150_"]


def test_map_is_svg():
    pts = I.parse_voltages(CSV, "POWER", 1.2)
    svg = I.voltage_map_svg(
        I.NetDrop("VPWR", "POWER", 1.2, 6e-4, 3e-4, 0.05, pts), (0, 0, 104, 123)
    )
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg") and "VPWR: drop up to 0.63 mV" in svg
    assert I._colour(0.0) == I.PALETTE[0] and I._colour(1.0) == I.PALETTE[-1]
    assert I.voltage_map_svg(I.NetDrop("V", "POWER", 1.2, 0, 0, 0, [])) == ""


def test_die_and_corners():
    head = "VERSION 5.8 ;\nUNITS DISTANCE MICRONS 1000 ;\nDIEAREA ( 0 0 ) ( 104365 123085 ) ;\n"
    assert I.die_area(head) == (0.0, 0.0, 104.365, 123.085)
    assert (I.corner_voltage("typ"), I.corner_voltage("slow"), I.corner_voltage("fast")) == (
        1.2,
        1.08,
        1.32,
    )


def test_script_keeps_net_names_as_data(counter_project):
    p = load_project(counter_project)
    files = {"tech_lef": "t.lef", "cell_lefs": ["c.lef"], "liberty": {"slow": "s.lib"}}
    text = I.ir_script(p, files, "slow", "d.def", "x.sdc", None, "gl.vcd", "counter")
    assert "read_def {d.def}" in text and "read_vcd -scope {counter} {gl.vcd}" in text
    assert '$kind eq "POWER" ? 1.08 : 0' in text
    assert 'analyze_power_grid -net $name -voltage_file "ir_$i.csv"' in text


def test_needs_a_routed_design(counter_project):
    r = I.ir_drop(load_project(counter_project))
    assert r.status == "skipped" and "no routed design" in r.summary
