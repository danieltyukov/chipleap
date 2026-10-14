"""The whole SoC as one block: X-HEEP's ``core_v_mini_mcu``, rendered for the design by
X-HEEP's own generator, with the accelerator on its external ports, and the parts X-HEEP
leaves to a technology, written for IHP SG13G2.

- ``chipleap_soc``, the top. The accelerator's read and write masters are X-HEEP's
  external crossbar masters, its registers the external peripheral slave and its
  interrupt the external peripheral interrupt, as the datasheet tells an integrator.
  Every other port of ``core_v_mini_mcu`` that a chip brings out (the pads' signals, the
  external memory's OBI ports, the pad control bus) is a port of the top, with X-HEEP's
  own types; the rest is tied off as a chip without coprocessor, external power domains
  or hardware FIFOs ties it (``TIED`` and ``OPEN``). A port this module does not know
  stops it, so a different X-HEEP cannot be wired by guess.
- ``sram_wrapper``, X-HEEP's bank, in IHP's single-port macros with a bit mask
  (``chipleap.macro.sram.choose``), for each bank size the design has: X-HEEP writes
  bytes, and a macro without ``A_BM`` cannot.
- X-HEEP's clock gates (``tc_clk_gating`` and the CPU's) on IHP's integrated clock gate,
  ``sg13g2_slgcp_1``, the other clock cells as logic; X-HEEP's ``pulp_clock_*`` and
  ``cluster_clock_*`` cells wrap these.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chipleap.gen import banks
from chipleap.macro.sram import Bank, Sram, choose

TOP = "chipleap_soc"
MCU = "core_v_mini_mcu"
MCU_INST = "u_mcu"
ACCEL_INST = "u_accel"
# IHP's integrated clock gate: a latch on GATE (and SCE, the scan enable) while CLK is low.
ICG = "sg13g2_slgcp_1"

# Inputs of core_v_mini_mcu a chip without these parts ties, and what to.
TIED = {
    "hart_id_i": "'0",
    "xheep_instance_id_i": "'0",
    "intr_vector_ext_i": "'0",
    "ext_ao_peripheral_slave_req_i": "'0",
    "hw_fifo_resp_i": "'0",
    "ext_dma_stop_i": "'0",
    "hw_fifo_done_i": "'0",
    "ext_dma_slot_tx_i": "'0",
    "ext_dma_slot_rx_i": "'0",
    # No power switches: each switch acknowledges at once.
    "cpu_subsystem_powergate_switch_ack_ni": "cpu_subsystem_powergate_switch_n",
    "peripheral_subsystem_powergate_switch_ack_ni": "peripheral_subsystem_powergate_switch_n",
    "external_subsystem_powergate_switch_ack_ni": "external_subsystem_powergate_switch_n",
}
# Outputs left open, or read only by a tie above.
OPEN = {
    "ext_ao_peripheral_slave_resp_o": "",
    "hw_fifo_req_o": "",
    "ext_debug_req_o": "",
    "ext_debug_reset_no": "",
    "external_subsystem_powergate_iso_no": "",
    "external_subsystem_rst_no": "",
    "ext_cpu_subsystem_rst_no": "",
    "external_ram_banks_set_retentive_no": "",
    "external_subsystem_clkgate_en_no": "",
    "dma_done_o": "",
    "cpu_subsystem_powergate_switch_no": "cpu_subsystem_powergate_switch_n",
    "peripheral_subsystem_powergate_switch_no": "peripheral_subsystem_powergate_switch_n",
    "external_subsystem_powergate_switch_no": "external_subsystem_powergate_switch_n",
}
# The accelerator's connections.
ACCEL = {
    "ext_xbar_master_req_i": "acc_obi_req",
    "ext_xbar_master_resp_o": "acc_obi_rsp",
    "ext_peripheral_slave_req_o": "acc_reg_req",
    "ext_peripheral_slave_resp_i": "acc_reg_rsp",
    "intr_ext_peripheral_i": "acc_irq",
}
# core_v_mini_mcu's type parameters, as the top writes them.
TYPES = {
    "obi_req_t": "xheep_obi_pkg::xheep_obi_req_t",
    "obi_rsp_t": "xheep_obi_pkg::xheep_obi_rsp_t",
    "reg_req_t": "xheep_reg_pkg::xheep_reg_req_t",
    "reg_rsp_t": "xheep_reg_pkg::xheep_reg_rsp_t",
    "fifo_req_t": "xheep_fifo_pkg::xheep_fifo_req_t",
    "fifo_rsp_t": "xheep_fifo_pkg::xheep_fifo_rsp_t",
}
# Through the top: X-HEEP's own types, only on these ports.
THROUGH_TYPED = {
    "pad_req_o",
    "pad_resp_i",
    "ext_core_instr_req_o",
    "ext_core_instr_resp_i",
    "ext_core_data_req_o",
    "ext_core_data_resp_i",
    "ext_debug_master_req_o",
    "ext_debug_master_resp_i",
    "ext_dma_read_req_o",
    "ext_dma_read_resp_i",
    "ext_dma_write_req_o",
    "ext_dma_write_resp_i",
    "ext_dma_addr_req_o",
    "ext_dma_addr_resp_i",
}

_HEADER_RE = re.compile(rf"\bmodule\s+{MCU}\s*#\s*\((.*?)\)\s*\((.*?)\);", re.S)
_PORT_RE = re.compile(r"^(input|output)\s+(\w+)\s*((?:\[[^\]]+\]\s*)*)([A-Za-z_]\w*)$")
_IF_RE = re.compile(r"^if_xif\.(\w+)\s+([A-Za-z_]\w*)$")
_BANK_RE = re.compile(
    r"\bsram_wrapper\s*#\s*\(\s*\.NumWords\s*\(\s*(\d+)\s*\)[^;]*?\)\s*([A-Za-z_]\w*)\s*\("
)


class SocError(ValueError):
    pass


@dataclass(frozen=True)
class Port:
    name: str
    direction: str  # input, output, or the modport of an if_xif port
    kind: str  # logic, a type parameter's name, or if_xif
    dims: str = ""  # packed dimensions as written, "[31:0]"


def mcu_ports(text: str) -> list[Port]:
    """The ports of the rendered ``core_v_mini_mcu`` (its module header)."""
    m = _HEADER_RE.search(_strip_comments(text))
    if m is None:
        raise SocError(f"no module {MCU} with parameters and ports")
    out = []
    for raw in m.group(2).split(","):
        decl = " ".join(raw.split())
        if not decl:
            continue
        if p := _PORT_RE.match(decl):
            out.append(Port(p[4], p[1], p[2], p[3].replace(" ", "")))
        elif p := _IF_RE.match(decl):
            out.append(Port(p[2], p[1], "if_xif"))
        else:
            raise SocError(f"a port of {MCU} this generator cannot read: {decl[:80]!r}")
    if not out:
        raise SocError(f"{MCU} has no ports")
    return out


def bank_words(memory_subsystem: str) -> list[tuple[str, int]]:
    """Each X-HEEP bank as (``sram_wrapper`` instance, depth in words), from the rendered
    ``memory_subsystem.sv``."""
    return [(inst, int(n)) for n, inst in _BANK_RE.findall(_strip_comments(memory_subsystem))]


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def top_sv(ports: Sequence[Port], accel_ports: int, accel_module: str = banks.TOP) -> str:
    """``chipleap_soc``: ``core_v_mini_mcu`` and the accelerator (``accel_module``, the
    flat ports of ``chipleap_accel``, one OBI master per read port and one for writes)."""
    names = {p.name for p in ports}
    for known in ACCEL:
        if known not in names:
            raise SocError(f"{MCU} has no port {known}, which the accelerator needs")
    head: list[str] = []
    conns: list[str] = []
    for p in ports:
        if p.kind == "if_xif":
            conns.append(f".{p.name}(xif)")
        elif p.name in ACCEL:
            conns.append(f".{p.name}({ACCEL[p.name]})")
        elif p.name in TIED:
            conns.append(f".{p.name}({TIED[p.name]})")
        elif p.name in OPEN:
            conns.append(f".{p.name}({OPEN[p.name]})")
        elif p.kind == "logic" or p.name in THROUGH_TYPED:
            if p.kind != "logic" and p.kind not in TYPES:
                raise SocError(f"port {p.name} of {MCU} has an unknown type {p.kind}")
            kind = "logic" if p.kind == "logic" else TYPES[p.kind]
            head.append(f"    {p.direction} {kind} {p.dims + ' ' if p.dims else ''}{p.name}")
            conns.append(f".{p.name}({p.name})")
        else:
            raise SocError(f"port {p.name} ({p.kind}) of {MCU} has no place in the SoC top")
    n = accel_ports
    lines = [
        "// Written by ChipLeap: X-HEEP with the accelerator on its external ports.",
        f"module {TOP} (",
        ",\n".join(head),
        ");",
        "  // Power switches that acknowledge at once (no switches in this block).",
        "  logic cpu_subsystem_powergate_switch_n, peripheral_subsystem_powergate_switch_n;",
        "  logic [(core_v_mini_mcu_pkg::EXTERNAL_DOMAINS == 0 ? 1 : "
        "core_v_mini_mcu_pkg::EXTERNAL_DOMAINS)-1:0] external_subsystem_powergate_switch_n;",
        "  // The accelerator's masters: its read ports, then its write port.",
        f"  xheep_obi_pkg::xheep_obi_req_t [{n}:0] acc_obi_req;",
        f"  xheep_obi_pkg::xheep_obi_rsp_t [{n}:0] acc_obi_rsp;",
        "  xheep_reg_pkg::xheep_reg_req_t acc_reg_req;",
        "  xheep_reg_pkg::xheep_reg_rsp_t acc_reg_rsp;",
        "  logic acc_irq;",
        "  // No coprocessor on the CORE-V eXtension Interface.",
        "  if_xif xif ();",
        "",
        f"  {MCU} #(",
        f"      .EXT_XBAR_NMASTER({n + 1})",
        f"  ) {MCU_INST} (",
        ",\n".join(f"      {c}" for c in conns),
        "  );",
        "",
        f"  logic [{n - 1}:0] rd_req, rd_gnt, rd_rvalid;",
        f"  logic [{n * 32 - 1}:0] rd_addr, rd_rdata;",
        "  logic wr_req;",
        "  logic [31:0] wr_addr, wr_wdata;",
        "  logic [3:0] wr_be;",
        f"  for (genvar i = 0; i < {n}; i++) begin : g_rd",
        "    assign acc_obi_req[i].req = rd_req[i];",
        "    assign acc_obi_req[i].we = 1'b0;",
        "    assign acc_obi_req[i].be = 4'hF;",
        "    assign acc_obi_req[i].addr = rd_addr[i*32+:32];",
        "    assign acc_obi_req[i].wdata = '0;",
        "    assign rd_gnt[i] = acc_obi_rsp[i].gnt;",
        "    assign rd_rvalid[i] = acc_obi_rsp[i].rvalid;",
        "    assign rd_rdata[i*32+:32] = acc_obi_rsp[i].rdata;",
        "  end",
        f"  assign acc_obi_req[{n}].req = wr_req;",
        f"  assign acc_obi_req[{n}].we = 1'b1;",
        f"  assign acc_obi_req[{n}].be = wr_be;",
        f"  assign acc_obi_req[{n}].addr = wr_addr;",
        f"  assign acc_obi_req[{n}].wdata = wr_wdata;",
        "",
        f"  {accel_module} {ACCEL_INST} (",
        "      .clk_i(clk_i),",
        "      .rst_ni(rst_ni),",
        "      .reg_valid_i(acc_reg_req.valid),",
        "      .reg_write_i(acc_reg_req.write),",
        "      .reg_addr_i(acc_reg_req.addr),",
        "      .reg_wdata_i(acc_reg_req.wdata),",
        "      .reg_wstrb_i(acc_reg_req.wstrb),",
        "      .reg_rdata_o(acc_reg_rsp.rdata),",
        "      .reg_error_o(acc_reg_rsp.error),",
        "      .reg_ready_o(acc_reg_rsp.ready),",
        "      .rd_req_o(rd_req),",
        "      .rd_gnt_i(rd_gnt),",
        "      .rd_addr_o(rd_addr),",
        "      .rd_rvalid_i(rd_rvalid),",
        "      .rd_rdata_i(rd_rdata),",
        "      .wr_req_o(wr_req),",
        f"      .wr_gnt_i(acc_obi_rsp[{n}].gnt),",
        "      .wr_addr_o(wr_addr),",
        "      .wr_wdata_o(wr_wdata),",
        "      .wr_be_o(wr_be),",
        f"      .wr_rvalid_i(acc_obi_rsp[{n}].rvalid),",
        "      .irq_o(acc_irq)",
        "  );",
        "endmodule",
    ]
    for need in ("clk_i", "rst_ni"):
        if need not in names:
            raise SocError(f"{MCU} has no port {need}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class BankPlan:
    """X-HEEP's banks of one depth as IHP macros."""

    words: int
    bank: Bank
    count: int  # banks of this depth in the design

    @property
    def block(self) -> str:
        return f"g_{self.words}x32"


def plan_banks(depths: Iterable[int], srams: Iterable[Sram]) -> list[BankPlan]:
    """The macros of each bank depth the design has: 32-bit words with a bit mask, the
    choice the accelerator's banks get too."""
    srams = list(srams)
    counts: dict[int, int] = {}
    for d in depths:
        if d < 2 or d & (d - 1):
            raise SocError(f"an X-HEEP bank of {d} words is not a power of two")
        counts[d] = counts.get(d, 0) + 1
    if not counts:
        raise SocError("the design has no X-HEEP memory bank")
    return [BankPlan(d, choose(srams, d, 32, bit_mask=True), n) for d, n in sorted(counts.items())]


BANK_PREFIX = "bank"


def sram_wrapper_sv(plans: Sequence[BankPlan]) -> str:
    """X-HEEP's ``sram_wrapper`` on IHP's macros: a bank of ``NumWords`` 32-bit words, its
    byte enables as the macros' bit mask, a word read a cycle after its address. Each
    depth is a generate block of its own (``g_<words>x32``); any other stops elaboration."""
    lines = [
        "// Written by ChipLeap: X-HEEP's memory bank in IHP's single-port SRAM macros.",
        "module sram_wrapper #(",
        "    parameter int unsigned NumWords = 32'd1024,",
        "    parameter int unsigned DataWidth = 32'd32,",
        "    parameter int unsigned AddrWidth = (NumWords > 32'd1) ? $clog2(NumWords) : 32'd1",
        ") (",
        "    input logic clk_i,",
        "    input logic rst_ni,",
        "    input logic req_i,",
        "    input logic we_i,",
        "    input logic [AddrWidth-1:0] addr_i,",
        "    input logic [DataWidth-1:0] wdata_i,",
        "    input logic [DataWidth/8-1:0] be_i,",
        "    input logic pwrgate_ni,",
        "    output logic pwrgate_ack_no,",
        "    input logic set_retentive_ni,",
        "    output logic [DataWidth-1:0] rdata_o",
        ");",
        "  // No power switch around the macros: the request is acknowledged at once.",
        "  assign pwrgate_ack_no = pwrgate_ni;",
        "  wire [31:0] bank_wdata = wdata_i;",
        "  wire [31:0] bank_wmask = {{8{be_i[3]}}, {8{be_i[2]}}, {8{be_i[1]}}, {8{be_i[0]}}};",
    ]
    for i, plan in enumerate(plans):
        aba = plan.words.bit_length() - 1
        cond = f"NumWords == {plan.words} && DataWidth == 32"
        lines.append(f"  {'if' if i == 0 else 'else if'} ({cond}) begin : {plan.block}")
        body: list[str] = []
        sig = ("req_i & ~we_i", "req_i & we_i", f"addr_i[{aba - 1}:0]", "rdata_o")
        banks.bank_macros(body, BANK_PREFIX, plan.bank, aba, 32, sig)
        lines += ["  " + ln if ln else ln for ln in "\n".join(body).splitlines()]
        lines.append("  end")
    lines += [
        "  else begin : g_unsupported",
        '    $error("ChipLeap wrote no IHP bank of this size");',
        "  end",
        "endmodule",
    ]
    return "\n".join(lines) + "\n"


def bank_instances(plan: BankPlan, wrapper_path: str) -> list[tuple[str, str]]:
    """The macros of one bank, as (instance path, cell), under ``wrapper_path``."""
    return [
        (f"{wrapper_path}.{plan.block}.{BANK_PREFIX}_m{k}", plan.bank.macro.cell)
        for k in range(plan.bank.count)
    ]


def clock_cells_sv() -> str:
    """X-HEEP's clock cells for IHP: every clock gate on ``sg13g2_slgcp_1``, the rest as
    logic, which synthesis maps to standard cells. A ``tc_clk_gating`` its user marks
    not functional (``IS_FUNCTIONAL`` 0, the GPIO input stages' power-saving gates) passes
    the clock through, as the generic cell's contract allows: a gate fewer under which
    the clock tree must be balanced."""
    gate = "{ICG} i_icg (.GATE({en}), .SCE({te}), .CLK(clk_i), .GCLK(clk_o));"
    return (
        "\n".join(
            [
                "// Written by ChipLeap: X-HEEP's clock cells on IHP SG13G2's standard cells.",
                "module tc_clk_gating #(",
                "    parameter bit IS_FUNCTIONAL = 1'b1",
                ") (",
                "    input  logic clk_i,",
                "    input  logic en_i,",
                "    input  logic test_en_i,",
                "    output logic clk_o",
                ");",
                "  if (IS_FUNCTIONAL) begin : g_gate",
                "    " + gate.format(ICG=ICG, en="en_i", te="test_en_i"),
                "  end else begin : g_through",
                "    assign clk_o = clk_i;",
                "  end",
                "endmodule",
                "",
                "module cve2_clock_gate (",
                "    input  logic clk_i,",
                "    input  logic en_i,",
                "    input  logic scan_cg_en_i,",
                "    output logic clk_o",
                ");",
                "  " + gate.format(ICG=ICG, en="en_i", te="scan_cg_en_i"),
                "endmodule",
                "",
                *_logic_clock_cells(),
            ]
        )
        + "\n"
    )


def _logic_clock_cells() -> list[str]:
    """The clock cells that are logic: and, buffer, inverter, multiplexer, xor, or and a
    delay that synthesis drops."""
    out: list[str] = []
    out += [
        "module tc_clk_and2 (input logic clk0_i, input logic clk1_i, output logic clk_o);",
        "  assign clk_o = clk0_i & clk1_i;",
        "endmodule",
        "",
        "module tc_clk_buffer (input logic clk_i, output logic clk_o);",
        "  assign clk_o = clk_i;",
        "endmodule",
        "",
        "module tc_clk_inverter (input logic clk_i, output logic clk_o);",
        "  assign clk_o = ~clk_i;",
        "endmodule",
        "",
        "module tc_clk_mux2 (",
        "    input logic clk0_i, input logic clk1_i, input logic clk_sel_i, output logic clk_o",
        ");",
        "  assign clk_o = clk_sel_i ? clk1_i : clk0_i;",
        "endmodule",
        "",
        "module tc_clk_xor2 (input logic clk0_i, input logic clk1_i, output logic clk_o);",
        "  assign clk_o = clk0_i ^ clk1_i;",
        "endmodule",
        "",
        "module tc_clk_or2 (input logic clk0_i, input logic clk1_i, output logic clk_o);",
        "  assign clk_o = clk0_i | clk1_i;",
        "endmodule",
        "",
        "module tc_clk_delay #(parameter int unsigned Delay = 300) (",
        "    input logic in_i, output logic out_o",
        ");",
        "  assign out_o = in_i;",
        "endmodule",
    ]
    return out


# The clock cell modules ``clock_cells_sv`` defines, which X-HEEP's own copies must not
# define again.
CLOCK_MODULES = (
    "tc_clk_gating",
    "cve2_clock_gate",
    "tc_clk_and2",
    "tc_clk_buffer",
    "tc_clk_inverter",
    "tc_clk_mux2",
    "tc_clk_xor2",
    "tc_clk_or2",
    "tc_clk_delay",
)


# Clocks besides clk_i that clock flip-flops in X-HEEP, each asynchronous to the others,
# as X-HEEP's own synchronizers and CDC FIFOs treat them: (name, port, source, period in
# ns). JTAG and the SPI slave are timed at 20 MHz. X-HEEP's I2S clock is clk_i through
# the I2S's own divider, bypass mux and clock gate, and gets no clock of its own: clk
# reaches its flip-flops through the bypass and the gate, at its fastest (a divider of
# one), and the clock tree is built through the gate as through X-HEEP's other gates. A
# clock created on the gate's output stops clk there and leaves its 337 flip-flops on an
# unbuffered net: the first layout found them 6 ns late.
EXTRA_CLOCKS = (
    ("jtag_tck", "jtag_tck_i", "[get_ports jtag_tck_i]", 50.0),
    ("spi_slave_sck", "spi_slave_sck_i", "[get_ports spi_slave_sck_i]", 50.0),
)


def sdc(ports: Sequence[Port], period_ns: float) -> str:
    """Timing constraints for the SoC: clk_i at ``period_ns``, X-HEEP's other clocks
    (``EXTRA_CLOCKS``, those whose port the top has), all asynchronous to one another,
    and LibreLane's base SDC for the rest, inputs and outputs against clk_i."""
    from chipleap.rtl.pnr import io_lines

    names = {p.name for p in ports}
    clocks = [c for c in EXTRA_CLOCKS if c[1] in names]
    lines = [
        f"# Written by ChipLeap: constraints for {TOP}.",
        f"create_clock [get_ports clk_i] -name clk -period {period_ns:g}",
    ]
    for name, _, source, period in clocks:
        lines += [
            f"set src {source}",
            "if { [llength $src] } {",
            f"    create_clock $src -name {name} -period {period or period_ns:g}",
            "}",
        ]
    inputs = ["clk_i", *(port for _, port, _, _ in clocks if port.endswith("_i"))]
    lines += [
        "set clock_inputs {}",
        "set data_inputs [all_inputs]",
        f"foreach name {{{' '.join(inputs)}}} {{",
        "    set port [get_ports $name]",
        "    lappend clock_inputs $port",
        "    set i [lsearch $data_inputs $port]",
        "    if { $i >= 0 } { set data_inputs [lreplace $data_inputs $i $i] }",
        "}",
        *io_lines("clk", period_ns),
        # X-HEEP's I2S sends its word select and data on the falling edge of its own
        # clock, which leaves on i2s_sck_o through the same clock gate: the receiver
        # samples them against that clock, not against clk_i, so the base SDC's budget
        # (a fifth of clk_i's period before the next clk_i edge) does not describe them.
        # They are held only to leaving within the cycle, as i2s_sck_o is.
        "set i2s_out [get_ports -quiet {i2s_ws_o i2s_sd_tx_o}]",
        "if { [llength $i2s_out] } {",
        "    set_output_delay 0 -clock [get_clocks clk] $i2s_out",
        "}",
        "set groups {}",
        f"foreach c {{clk {' '.join(name for name, _, _, _ in clocks)}}} {{",
        "    set k [get_clocks -quiet $c]",
        "    if { [llength $k] } { lappend groups -group $k }",
        "}",
        "set_clock_groups -asynchronous {*}$groups",
    ]
    return "\n".join(lines) + "\n"
