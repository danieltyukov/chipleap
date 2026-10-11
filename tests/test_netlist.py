"""The structural Verilog reader and the scan chain tracer."""

import pytest

from chipleap.rtl.netlist import (
    Limits,
    NetlistError,
    Nets,
    hierarchy,
    parse_netlist,
    read_netlist,
    trace_chain,
)

# The shape OpenROAD's write_verilog gives a scan-inserted design: non-ANSI ports, escaped
# names with a space before the bit select, assigns at the end.
SCAN = r"""
module top (clk_i,
    d_i,
    q_o,
    scan_enable_0,
    scan_in_0,
    scan_out_0);
 input clk_i;
 input [1:0] d_i;
 output [1:0] q_o;
 input scan_enable_0;
 input scan_in_0;
 output scan_out_0;

 wire _1_;
 wire [1:0] \u_a.q ;

 (* keep *) sg13g2_sdfrbpq_1 _10_ (.Q(\u_a.q [0]),
    .CLK(clk_i),
    .RESET_B(1'b1),
    .SCD(scan_in_0),
    .D(d_i[0]),
    .SCE(scan_enable_0));
 sg13g2_buf_1 hold1 (.A(\u_a.q [0]), .X(_1_));
 sg13g2_sdfrbpq_1 _11_ (.Q(\u_a.q [1]),
    .CLK(clk_i),
    .RESET_B(1'b1),
    .SCD(_1_),
    .D(d_i[1]),
    .SCE(scan_enable_0));
 sg13g2_nand2_1 _12_ (.A(\u_a.q [0]), .B(\u_a.q [1]), .Y());
 assign q_o = {\u_a.q [1], \u_a.q [0]};
 assign scan_out_0 = \u_a.q [1];
endmodule
"""

BUFFERS = {"sg13g2_buf_": ("A", "X")}


def test_parse_ports_instances_and_assigns():
    top = parse_netlist(SCAN).top("top")
    assert top.ports == ["clk_i", "d_i", "q_o", "scan_enable_0", "scan_in_0", "scan_out_0"]
    assert top.directions["q_o"] == "output" and top.bits("d_i") == ["d_i[1]", "d_i[0]"]
    assert top.port_bits("input") == ["clk_i", "d_i[1]", "d_i[0]", "scan_enable_0", "scan_in_0"]
    ff = top.instances[0]
    assert (ff.cell, ff.name) == ("sg13g2_sdfrbpq_1", "_10_")
    assert ff.pins["Q"] == ["u_a.q[0]"] and ff.pins["RESET_B"] == ["1'b1"]
    assert top.instances[3].pins["Y"] == []
    assert top.assigns[0] == (["q_o[1]", "q_o[0]"], ["u_a.q[1]", "u_a.q[0]"])


def test_yosys_style_headers_and_constants():
    text = r"""
module \$paramod\sub\W=8 (input [3:0] a, output y);
  assign y = 1'b0;
endmodule
module m(a, y);
  input [3:0] a;
  output y;
  wire [7:0] w = 8'hA5;
  \$paramod\sub\W=8  u_sub (.a({a[3:2], 2'b01}), .y(y));
endmodule
"""
    nl = parse_netlist(text)
    sub = nl.top("$paramod\\sub\\W=8")
    assert sub.directions == {"a": "input", "y": "output"} and sub.ranges["a"] == (3, 0)
    m = nl.top("m")
    assert m.instances[0].pins["a"] == ["a[3]", "a[2]", "1'b0", "1'b1"]
    assert m.assigns[0][1] == [f"1'b{b}" for b in "10100101"]


def test_trace_through_buffers():
    nets = Nets(parse_netlist(SCAN).top("top"))
    chain = trace_chain(nets, "chain_0", "scan_in_0", "scan_out_0", through=BUFFERS)
    assert chain.cells == ("_10_", "_11_")
    with pytest.raises(NetlistError, match="breaks after _10_"):
        trace_chain(nets, "chain_0", "scan_in_0", "scan_out_0")  # without the buffer


def test_forks_and_breaks_are_errors():
    forked = SCAN.replace(".SCD(_1_)", ".SCD(scan_in_0)")
    nets = Nets(parse_netlist(forked).top("top"))
    with pytest.raises(NetlistError, match="forks after scan_in_0"):
        trace_chain(nets, "chain_0", "scan_in_0", "scan_out_0", through=BUFFERS)
    nets = Nets(parse_netlist(SCAN).top("top"))
    with pytest.raises(NetlistError, match="breaks after scan_in_0"):
        trace_chain(nets, "chain_0", "scan_in_0", "q_o[0]", si_pin="SI")


@pytest.mark.parametrize(
    "text",
    [
        "module m(a); input a; sg13g2_inv_1 u (.A(a)",
        "module m(a); input a;",
        "module m(a); input a; 42 endmodule",
    ],
)
def test_malformed_netlists_fail_cleanly(text):
    with pytest.raises(NetlistError):
        parse_netlist(text)


def test_size_limit(tmp_path):
    with pytest.raises(NetlistError, match="larger than"):
        parse_netlist(SCAN, Limits(bytes=10))
    # The byte limit holds across the files read together.
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text("module a(); endmodule\n")
    b.write_text("module b(); endmodule\n")
    assert set(read_netlist([a, b]).modules) == {"a", "b"}
    with pytest.raises(NetlistError, match="larger than"):
        read_netlist([a, b], Limits(bytes=30))


@pytest.mark.parametrize(
    "text",
    [
        "module m(a); input [0:2147483647] a; endmodule",
        "module m(input [2147483647:0] a); endmodule",
        "module m(); wire [2147483647:0] w; endmodule",
        "module m(); wire w; sg13g2_inv_1 u (.A(w[0:2147483647])); endmodule",
        "module m(); sg13g2_inv_1 u (.A(2147483647'b0)); endmodule",
        "module m(); sg13g2_inv_1 u (.A(" + "9" * 5000 + ")); endmodule",
        "module m(); sg13g2_inv_1 u (.A(" + "{" * 100 + "1'b0" + "}" * 100 + ")); endmodule",
    ],
)
def test_a_huge_bus_is_refused_before_its_bits_are_made(text):
    with pytest.raises(NetlistError, match="bits wide|too long|too deeply|wider than"):
        parse_netlist(text)


def test_instances_over_the_cap():
    # An instance array declaring 10**6 + 1 instances costs a few bytes of text.
    with pytest.raises(NetlistError, match="more than 1000000 instances"):
        parse_netlist("module m(); sg13g2_inv_1 u [0:1000000] (); endmodule")
    many = "".join(f"sg13g2_inv_1 u{i} ();\n" for i in range(50))
    with pytest.raises(NetlistError, match="more than 40 instances"):
        parse_netlist(f"module m();\n{many}endmodule\n", Limits(instances=40))
    # A hierarchy of 2 x 1000 x 1000 leaf cells holds 2 * 10**6 instances in under 40 kB.
    leaf = "".join(f"sg13g2_inv_1 u{i} ();\n" for i in range(1000))
    mid = "".join(f"leaf l{i} ();\n" for i in range(1000))
    text = (
        f"module leaf();\n{leaf}endmodule\nmodule mid();\n{mid}endmodule\n"
        "module top(); mid a (); mid b (); endmodule\n"
    )
    nl = parse_netlist(text)
    with pytest.raises(NetlistError, match="more than 1000000 instances"):
        for _ in hierarchy(nl, "top"):
            pass


def test_a_module_that_contains_itself_is_refused():
    nl = parse_netlist("module a(); a u (); endmodule")
    with pytest.raises(NetlistError, match=r"module a contains itself \(a > a\)"):
        list(hierarchy(nl, "a"))
    nl = parse_netlist("module a(); b u (); endmodule module b(); a v (); endmodule")
    with pytest.raises(NetlistError, match=r"contains itself \(a > b > a\)"):
        list(hierarchy(nl, "a"))
    deep = "".join(f"module m{i}(); m{i + 1} u (); endmodule\n" for i in range(80))
    with pytest.raises(NetlistError, match="deeper than 64"):
        list(hierarchy(parse_netlist(deep + "module m80(); endmodule\n"), "m0"))
    two = parse_netlist("module a(); b u (); b v (); endmodule module b(); endmodule")
    assert [p for p, _ in hierarchy(two, "a")] == ["", "u", "v"]


def test_a_chain_longer_than_the_limit_is_refused():
    top = parse_netlist(SCAN).top("top")
    with pytest.raises(NetlistError, match="longer than 1 cells"):
        trace_chain(Nets(top), "chain_0", "scan_in_0", "scan_out_0", through=BUFFERS, limit=1)


def test_a_chain_may_end_on_a_functional_output():
    # Without scan_out_0, the last flip-flop's output port is the scan output.
    text = SCAN.replace("    scan_out_0);", "    unused);").replace(" output scan_out_0;\n", "")
    text = text.replace(" assign scan_out_0 = \\u_a.q [1];\n", "")
    top = parse_netlist(text).top("top")
    nets = Nets(top)
    chain = trace_chain(nets, "chain_0", "scan_in_0", top.port_bits("output"), through=BUFFERS)
    assert chain.cells == ("_10_", "_11_") and chain.scan_out == "q_o[1]"
