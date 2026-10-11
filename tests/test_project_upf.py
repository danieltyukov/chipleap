"""Power intent: the UPF ChipLeap writes, the domains of a hierarchical netlist, the
signals between them; a real run when OpenROAD, Yosys and the PDK are installed."""

import os
import shutil
import textwrap
from pathlib import Path

import pytest

from chipleap.rtl import upf as U
from chipleap.rtl.netlist import parse_netlist
from chipleap.rtl.project import PowerDomain, UpfConfig, load_project
from chipleap.verify.tools import ihp_liberty, openroad, yosys

LEF = """
MACRO sg13g2_inv_1
  CLASS CORE ;
  SIZE 1.44 BY 3.78 ;
  PIN Y
    DIRECTION OUTPUT ;
  END Y
  PIN A
    DIRECTION INPUT ;
  END A
END sg13g2_inv_1
MACRO sg13g2_dfrbpq_1
  SIZE 12.48 BY 3.78 ;
  PIN Q
    DIRECTION OUTPUT ;
  END Q
  PIN D
    DIRECTION INPUT ;
  END D
END sg13g2_dfrbpq_1
"""

HIER = r"""
module \$paramod\sub\W=1 (clk, d, q);
  input clk; input d; output q;
  wire n;
  sg13g2_inv_1 _1_ (.A(d), .Y(n));
  sg13g2_dfrbpq_1 _2_ (.CLK(clk), .D(n), .Q(q));
endmodule
module top (clk, a, y);
  input clk; input a; output y;
  wire m;
  \$paramod\sub\W=1  u_a (.clk(clk), .d(a), .q(m));
  \$paramod\sub\W=1  u_b (.clk(clk), .d(m), .q(y));
endmodule
"""

DOMAINS = (PowerDomain("PD_TOP"), PowerDomain("PD_A", ("u_a",), None, 1.2))


def test_write_upf():
    text = U.write_upf(UpfConfig(DOMAINS), {"PD_A": (6.0, 8.0, 40.5, 90.0)})
    assert text.splitlines() == [
        "create_power_domain {PD_TOP} -elements {.}",
        "create_power_domain {PD_A} -elements {{u_a}}",
        "set_domain_voltage -domain {PD_A} -voltage 1.2",
        "set_domain_area {PD_A} -area {6 8 40.5 90}",
    ]


def test_cells_by_domain_and_crossings():
    lib = U.read_lef(LEF)
    assert lib.sizes["sg13g2_inv_1"] == (1.44, 3.78) and lib.outputs["sg13g2_dfrbpq_1"] == {"Q"}
    flat = U.flatten(parse_netlist(HIER), "top", DOMAINS)
    assert sorted((path, dom) for path, _, dom, _ in flat.cells) == [
        ("u_a/_1_", "PD_A"),
        ("u_a/_2_", "PD_A"),
        ("u_b/_1_", "PD_TOP"),
        ("u_b/_2_", "PD_TOP"),
    ]
    cross = U.crossings(flat, lib, DOMAINS)
    assert [(c["from"], c["to"], c["nets"]) for c in cross] == [
        ("PD_A", "PD_TOP", 1),
        ("PD_TOP", "PD_A", 2),
    ]
    assert not any(c["needs_level_shifters"] for c in cross)  # PD_TOP has no voltage
    both = (PowerDomain("PD_TOP", (), None, 1.5), DOMAINS[1])
    assert all(c["needs_level_shifters"] for c in U.crossings(flat, lib, both))
    with pytest.raises(U.UpfError, match="u_x is not an instance"):
        U.flatten(parse_netlist(HIER), "top", (DOMAINS[0], PowerDomain("PD_X", ("u_x",))))


def test_placement_report():
    log = "CHIPLEAP-UPF placed 93 0 PD_RX\nCHIPLEAP-UPF placed 1 2 PD_B\nCHIPLEAP-UPF end\n"
    assert U.parse_placed(log) == {"PD_RX": (93, 0), "PD_B": (1, 2)}


def test_step_needs_a_section(counter_project):
    r = U.upf(load_project(counter_project))
    assert r.status == "skipped" and "no upf section" in r.summary


TOP = """\
module pair (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic       en_i,
  output logic [5:0] a_o,
  output logic [5:0] b_o
);
  counter #(.W(6)) u_a (.clk_i, .rst_ni, .en_i, .q_o(a_o));
  counter #(.W(6)) u_b (.clk_i, .rst_ni, .en_i(a_o[0]), .q_o(b_o));
endmodule
"""


@pytest.mark.skipif(
    not (openroad().found and yosys().found and ihp_liberty()), reason="needs OpenROAD, Yosys, PDK"
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_IMPL"), reason="set CHIPLEAP_RUN_IMPL=1")
def test_a_domain_lands_in_its_area(counter_project):
    (counter_project / "rtl/pair.sv").write_text(TOP)
    (counter_project / "project.yaml").write_text(
        textwrap.dedent(
            """\
            name: pair
            top: pair
            sources: [rtl/counter.sv, rtl/pair.sv]
            clocks: [{port: clk_i, period_ns: 10}]
            resets: [{port: rst_ni}]
            upf:
              domains:
                - {name: PD_TOP}
                - {name: PD_B, elements: [u_b]}
            """
        )
    )
    r = U.upf(load_project(counter_project))
    assert r.status == "pass", r.summary
    b = r.data["domains"][1]
    assert b["cells"] > 0 and b["placed_inside"] == b["cells"] and b["placed_outside"] == 0
    assert "create_power_domain PD_B" in r.data["openroad_upf"]


def test_placement_check_findings():
    log = "[WARNING DPL-0004] Placed in rows check failed (19).\nCHIPLEAP-UPF check_placement DPL-0033\n"
    assert U.placement_check(log) == "Placed in rows check failed (19)."
    assert U.placement_check("[WARNING DPL-0004] x\nCHIPLEAP-UPF end\n") == ""


FIXTURE = Path(__file__).parent / "fixtures" / "eco_timing"


@pytest.mark.skipif(
    not (openroad().found and yosys().found and ihp_liberty()), reason="needs OpenROAD, Yosys, PDK"
)
@pytest.mark.skipif(not os.environ.get("CHIPLEAP_RUN_IMPL"), reason="set CHIPLEAP_RUN_IMPL=1")
def test_two_voltages_need_level_shifters(tmp_path):
    """The fixture's multiplier runs at 1.08 V beside a 1.2 V top: OpenROAD places both
    domains, and the step fails for the level shifters IHP's library does not have."""
    root = tmp_path / "eco_timing"
    shutil.copytree(FIXTURE, root, ignore=shutil.ignore_patterns("build"))
    r = U.upf(load_project(root))
    assert r.status == "fail" and "level shifter" in r.summary, r.summary
    assert any(c["needs_level_shifters"] for c in r.data["crossings"])
