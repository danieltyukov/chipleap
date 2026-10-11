"""Gate-level power: the dump module, OpenROAD's script and report, the clock in a VCD,
and a real run when OpenROAD, cocotb and Icarus 13 are installed."""

import os

import pytest

from chipleap.rtl import power as P
from chipleap.rtl.project import load_project
from chipleap.rtl.sim import cocotb_python
from chipleap.verify.tools import gl_iverilog, openroad

LOG = """
CHIPLEAP-POWER vectorless
Group                    Internal    Switching      Leakage        Total
                            Power        Power        Power        Power (Watts)
------------------------------------------------------------------------
Sequential           3.340752e-04 9.491995e-06 2.533949e-08 3.435925e-04  51.0%
Combinational        1.476452e-04 6.243515e-05 6.869076e-08 2.101490e-04  31.2%
Clock                6.978367e-05 4.945957e-05 1.151636e-07 1.193584e-04  17.7%
Macro                0.000000e+00 0.000000e+00 0.000000e+00 0.000000e+00   0.0%
Pad                  0.000000e+00 0.000000e+00 0.000000e+00 0.000000e+00   0.0%
------------------------------------------------------------------------
Total                5.515042e-04 1.213867e-04 2.091938e-07 6.731001e-04 100.0%
                            81.9%        18.0%         0.0%
CHIPLEAP-POWER vcd
vcd           977
unannotated     0
Group                    Internal    Switching      Leakage        Total
Sequential           2.942836e-04 1.100296e-06 2.676275e-08 2.954107e-04  67.7%
Total                3.782677e-04 5.814536e-05 2.097703e-07 4.366228e-04 100.0%
CHIPLEAP-POWER end
"""

VCD = """$date today $end
$timescale
  1ps
$end
$scope module counter $end
$var wire 1 ! clk_i $end
$var wire 6 " q_o [5:0] $end
$upscope $end
$enddefinitions $end
#0
0!
#5000
1!
#10000
0!
#15000
1!
#20000
0!
#25000
1!
#35000
1!
"""


def test_parse_power_tables():
    r = P.parse_power(LOG)
    assert r["vectorless"]["Total"]["total"] == pytest.approx(6.731001e-4)
    assert r["vectorless"]["Clock"]["switching"] == pytest.approx(4.945957e-05)
    assert r["vcd"]["Total"]["total"] == pytest.approx(4.366228e-4)
    assert (r["annotated"], r["unannotated"]) == (977, 0)


def test_clock_period_in_a_vcd(tmp_path):
    vcd = tmp_path / "x.vcd"
    vcd.write_text(VCD)
    assert P.vcd_clock_period_ns(vcd, "counter", "clk_i") == pytest.approx(10.0)
    assert P.vcd_clock_period_ns(vcd, "tb", "clk_i") is None


def test_dump_module_forces_scan_ports(tmp_path):
    text = P.dump_module("tb.u_dut", tmp_path / "gl.vcd", ["scan_enable_0", "scan_in_0"])
    assert f'$dumpfile("{tmp_path}/gl.vcd");' in text and "$dumpvars(0, tb.u_dut);" in text
    assert "force tb.u_dut.scan_enable_0 = 1'b0;" in text
    with pytest.raises(P.PowerError):
        P.dump_module("tb", tmp_path / 'a"b.vcd', [])


def test_power_script(counter_project):
    p = load_project(counter_project)
    files = {"tech_lef": "t.lef", "cell_lefs": ["c.lef"], "liberty": {"slow": "s.lib"}}
    text = P.power_script(p, files, "slow", "n.v", "x.sdc", "x.spef", "gl.vcd", "counter")
    assert "read_liberty {s.lib}" in text and "read_spef {x.spef}" in text
    assert "set_power_activity -input -activity 0.1" in text
    assert text.index("CHIPLEAP-POWER vectorless") < text.index(
        "read_vcd -scope {counter} {gl.vcd}"
    )
    assert P.simple_sdc(p) == "create_clock -name {clk_i} -period 10 [get_ports {clk_i}]\n"


def test_tests_wrapped_in_a_testbench_need_a_scope(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(
        proj.read_text().replace(
            "tests: {dir: test, seed: 3}", "tests: {dir: test, seed: 3, toplevel: tb}"
        )
    )
    p = load_project(counter_project)
    with pytest.raises((P.PowerError, P.OpenroadUnavailable)):
        P.gate_level_run(
            p, counter_project / "rtl/counter.sv", None, counter_project / "build", False
        )


@pytest.mark.skipif(
    not (openroad().found and gl_iverilog().found and cocotb_python()),
    reason="needs OpenROAD, Icarus 13 and cocotb",
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_IMPL"), reason="set CHIPLEAP_RUN_IMPL=1")
def test_counter_power_from_its_tests(counter_project):
    proj = counter_project / "project.yaml"
    proj.write_text(proj.read_text() + "power: {corners: [typ, fast]}\n")
    test = counter_project / "test/test_counter.py"
    # A netlist has no parameter W: the gate-level run passes it in the environment.
    test.write_text(
        "import os\n"
        + test.read_text().replace(
            "int(dut.W.value)",
            "(int(os.environ['CHIPLEAP_PARAM_W']) if os.environ.get('GATES') else int(dut.W.value))",
        )
    )
    r = P.power(load_project(counter_project))
    assert r.status == "pass", r.summary
    assert r.data["netlist"] == "synthesized"
    assert r.data["gate_level_tests"]["passed"] == 2
    typ, fast = r.data["corners"]
    assert 0 < typ["vcd_mw"] < fast["vcd_mw"]
    assert typ["annotated_pins"] > 0 and typ["unannotated_pins"] == 0
