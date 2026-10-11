"""Accelerator RTL, driver and testbench sources for a design point."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from chipleap.hw.design import Accel
from chipleap.model.accel import ACC_ROWS
from chipleap.rtl import builddir

DATAFLOW_ID = {"gemv": 0, "ws": 1, "os": 2}
# Default base address of the accelerator's registers: X-HEEP's external peripheral
# region (base peripheral domain at 0x20000000, ext_peripheral at offset 0x70000).
DEFAULT_REG_BASE = 0x20070000


@dataclass(frozen=True)
class RtlParams:
    dataflow: int
    rows: int
    cols: int
    wbits: int
    ports: int
    abuf_words: int
    wbuf_words: int
    double_buf: int
    acc_rows: int = ACC_ROWS

    def verilator_args(self) -> list[str]:
        return [
            f"-GDATAFLOW={self.dataflow}",
            f"-GROWS={self.rows}",
            f"-GCOLS={self.cols}",
            f"-GWBITS={self.wbits}",
            f"-GPORTS={self.ports}",
            f"-GABUF_WORDS={self.abuf_words}",
            f"-GWBUF_WORDS={self.wbuf_words}",
            f"-GDOUBLE_BUF={self.double_buf}",
            f"-GACC_ROWS={self.acc_rows}",
        ]

    def sv_overrides(self) -> str:
        return (
            f"    .DATAFLOW({self.dataflow}),\n"
            f"    .ROWS({self.rows}),\n"
            f"    .COLS({self.cols}),\n"
            f"    .WBITS({self.wbits}),\n"
            f"    .PORTS({self.ports}),\n"
            f"    .ABUF_WORDS({self.abuf_words}),\n"
            f"    .WBUF_WORDS({self.wbuf_words}),\n"
            f"    .DOUBLE_BUF(1'b{self.double_buf}),\n"
            f"    .ACC_ROWS({self.acc_rows})"
        )


def rtl_params(accel: Accel) -> RtlParams:
    return RtlParams(
        dataflow=DATAFLOW_ID[accel.dataflow],
        rows=accel.rows,
        cols=accel.cols,
        wbits=accel.w_bits,
        ports=accel.ports,
        abuf_words=accel.abuf_kib * 256,
        wbuf_words=accel.wbuf_kib * 256,
        double_buf=1 if accel.double_buffer else 0,
    )


def _source(sub: str, name: str) -> str:
    return resources.files("chipleap.gen").joinpath(sub).joinpath(name).read_text()


def accel_sv() -> str:
    return _source("rtl", "chipleap_accel.sv")


def axi_sv() -> str:
    return _source("rtl", "chipleap_accel_axi.sv")


def testbench_cpp() -> str:
    return _source("tb", "tb_chipleap.cpp")


def testbench_axi_cpp() -> str:
    return _source("tb", "tb_chipleap_axi.cpp")


def driver_header(
    accel: Accel, name: str, base: int = DEFAULT_REG_BASE, platform: str = "xheep"
) -> str:
    p = rtl_params(accel)
    if platform == "standalone":
        host = "any host that maps its AXI4-Lite port into memory"
        # The integrator places the register window, so there is no default to guess.
        base_block = (
            "#ifndef CHIPLEAP_BASE\n"
            '#error "Define CHIPLEAP_BASE: the address the interconnect gives the AXI4-Lite '
            'register port"\n'
            "#endif"
        )
    else:
        host = "X-HEEP firmware"
        base_block = f"#ifndef CHIPLEAP_BASE\n#define CHIPLEAP_BASE {base:#010x}u\n#endif"
    return _source("sw", "chipleap_driver.h.tpl").format(
        name=name,
        host=host,
        base_block=base_block,
        dataflow_id=p.dataflow,
        rows=p.rows,
        cols=p.cols,
        wbits=p.wbits,
        ports=p.ports,
        abuf_bytes=accel.abuf_kib * 1024,
        wbuf_bytes=accel.wbuf_kib * 1024,
        double_buf=p.double_buf,
        acc_rows=p.acc_rows,
    )


def write_rtl(
    accel: Accel,
    name: str,
    out: Path,
    base: int = DEFAULT_REG_BASE,
    platform: str = "xheep",
) -> list[Path]:
    """Write the accelerator, its wrappers, driver and testbench into ``out``.

    ``xheep`` adds the X-HEEP adapter (register interface and OBI); ``standalone`` adds
    the AXI4 wrapper and its testbench instead, for any host. Nothing is written through
    a symlink in ``out`` (``chipleap.rtl.builddir.write_text``).
    """
    p = rtl_params(accel)
    files: dict[Path, str] = {out / "rtl" / "chipleap_accel.sv": accel_sv()}
    if platform == "standalone":
        files |= {
            out / "rtl" / "chipleap_accel_axi.sv": axi_sv(),
            out / "rtl" / "chipleap_accel_axi_top.sv": _axi_top_wrapper(p, name),
            out / "rtl" / "chipleap_accel.core": _fusesoc_core(name, platform),
            out / "sw" / "chipleap_driver.h": driver_header(accel, name, base, platform),
            out / "tb" / "tb_chipleap_axi.cpp": testbench_axi_cpp(),
        }
    else:
        files |= {
            out / "rtl" / "chipleap_accel_top.sv": _top_wrapper(p, name),
            out / "rtl" / "chipleap_accel_xheep.sv": _xheep_wrapper(p, name),
            out / "rtl" / "chipleap_accel.core": _fusesoc_core(name, platform),
            out / "sw" / "chipleap_driver.h": driver_header(accel, name, base, platform),
            out / "tb" / "tb_chipleap.cpp": testbench_cpp(),
        }
    for path, text in files.items():
        builddir.write_text(out, path.relative_to(out), text)
    return list(files)


def _top_wrapper(p: RtlParams, name: str) -> str:
    return f"""// Parameterised instance of the ChipLeap accelerator for design "{name}".
// SPDX-License-Identifier: MIT

module chipleap_accel_top (
    input logic clk_i,
    input logic rst_ni,
    input logic reg_valid_i,
    input logic reg_write_i,
    input logic [31:0] reg_addr_i,
    input logic [31:0] reg_wdata_i,
    input logic [3:0] reg_wstrb_i,
    output logic [31:0] reg_rdata_o,
    output logic reg_error_o,
    output logic reg_ready_o,
    output logic [{p.ports}-1:0] rd_req_o,
    input logic [{p.ports}-1:0] rd_gnt_i,
    output logic [{p.ports}-1:0][31:0] rd_addr_o,
    input logic [{p.ports}-1:0] rd_rvalid_i,
    input logic [{p.ports}-1:0][31:0] rd_rdata_i,
    output logic wr_req_o,
    input logic wr_gnt_i,
    output logic [31:0] wr_addr_o,
    output logic [31:0] wr_wdata_o,
    output logic [3:0] wr_be_o,
    input logic wr_rvalid_i,
    output logic irq_o
);
  chipleap_accel #(
{p.sv_overrides()}
  ) u_accel (.*);
endmodule
"""


def _xheep_wrapper(p: RtlParams, name: str) -> str:
    return f"""// X-HEEP adapter for the ChipLeap accelerator of design "{name}".
//
// Connect reg_req_i/reg_rsp_o to a slot of X-HEEP's external peripheral bus, the
// {p.ports} read master port(s) and the write master port to ext_xbar_master_req_i /
// ext_xbar_master_resp_o, and irq_o to a free fast interrupt. The type parameters are
// X-HEEP's own: reg_pkg::reg_req_t, reg_pkg::reg_rsp_t, obi_pkg::obi_req_t and
// obi_pkg::obi_resp_t.
//
// SPDX-License-Identifier: MIT

module chipleap_accel_xheep #(
    parameter type reg_req_t = logic,
    parameter type reg_rsp_t = logic,
    parameter type obi_req_t = logic,
    parameter type obi_resp_t = logic
) (
    input logic clk_i,
    input logic rst_ni,
    input reg_req_t reg_req_i,
    output reg_rsp_t reg_rsp_o,
    output obi_req_t [{p.ports}-1:0] rd_req_o,
    input obi_resp_t [{p.ports}-1:0] rd_resp_i,
    output obi_req_t wr_req_o,
    input obi_resp_t wr_resp_i,
    output logic irq_o
);
  logic [{p.ports}-1:0] rreq, rgnt, rvalid;
  logic [{p.ports}-1:0][31:0] raddr, rdata;

  for (genvar i = 0; i < {p.ports}; i++) begin : g_port
    assign rd_req_o[i].req = rreq[i];
    assign rd_req_o[i].we = 1'b0;
    assign rd_req_o[i].be = 4'hF;
    assign rd_req_o[i].addr = raddr[i];
    assign rd_req_o[i].wdata = '0;
    assign rgnt[i] = rd_resp_i[i].gnt;
    assign rvalid[i] = rd_resp_i[i].rvalid;
    assign rdata[i] = rd_resp_i[i].rdata;
  end

  logic wreq;
  logic [31:0] waddr, wdata;
  logic [3:0] wbe;
  assign wr_req_o.req = wreq;
  assign wr_req_o.we = 1'b1;
  assign wr_req_o.be = wbe;
  assign wr_req_o.addr = waddr;
  assign wr_req_o.wdata = wdata;

  chipleap_accel #(
{p.sv_overrides()}
  ) u_accel (
      .clk_i,
      .rst_ni,
      .reg_valid_i(reg_req_i.valid),
      .reg_write_i(reg_req_i.write),
      .reg_addr_i(reg_req_i.addr),
      .reg_wdata_i(reg_req_i.wdata),
      .reg_wstrb_i(reg_req_i.wstrb),
      .reg_rdata_o(reg_rsp_o.rdata),
      .reg_error_o(reg_rsp_o.error),
      .reg_ready_o(reg_rsp_o.ready),
      .rd_req_o(rreq),
      .rd_gnt_i(rgnt),
      .rd_addr_o(raddr),
      .rd_rvalid_i(rvalid),
      .rd_rdata_i(rdata),
      .wr_req_o(wreq),
      .wr_gnt_i(wr_resp_i.gnt),
      .wr_addr_o(waddr),
      .wr_wdata_o(wdata),
      .wr_be_o(wbe),
      .wr_rvalid_i(wr_resp_i.rvalid),
      .irq_o
  );
endmodule
"""


def _axi_top_wrapper(p: RtlParams, name: str, id_width: int = 1) -> str:
    n = p.ports
    return f"""// Parameterised instance of the ChipLeap accelerator with AXI4 interfaces, for
// design "{name}". Read port 0 with the write channels is one full AXI4 master; the
// other {n - 1} read port(s) are read-only AXI4 masters. AXI_ID_WIDTH is {id_width}; change it
// here if the interconnect expects wider IDs (the wrapper drives ID 0).
// SPDX-License-Identifier: MIT

module chipleap_accel_axi_top (
    input logic clk_i,
    input logic rst_ni,
    input logic [31:0] s_axil_awaddr_i,
    input logic [2:0] s_axil_awprot_i,
    input logic s_axil_awvalid_i,
    output logic s_axil_awready_o,
    input logic [31:0] s_axil_wdata_i,
    input logic [3:0] s_axil_wstrb_i,
    input logic s_axil_wvalid_i,
    output logic s_axil_wready_o,
    output logic [1:0] s_axil_bresp_o,
    output logic s_axil_bvalid_o,
    input logic s_axil_bready_i,
    input logic [31:0] s_axil_araddr_i,
    input logic [2:0] s_axil_arprot_i,
    input logic s_axil_arvalid_i,
    output logic s_axil_arready_o,
    output logic [31:0] s_axil_rdata_o,
    output logic [1:0] s_axil_rresp_o,
    output logic s_axil_rvalid_o,
    input logic s_axil_rready_i,
    output logic [{n}-1:0][{id_width}-1:0] m_axi_arid_o,
    output logic [{n}-1:0][31:0] m_axi_araddr_o,
    output logic [{n}-1:0][7:0] m_axi_arlen_o,
    output logic [{n}-1:0][2:0] m_axi_arsize_o,
    output logic [{n}-1:0][1:0] m_axi_arburst_o,
    output logic [{n}-1:0] m_axi_arlock_o,
    output logic [{n}-1:0][3:0] m_axi_arcache_o,
    output logic [{n}-1:0][2:0] m_axi_arprot_o,
    output logic [{n}-1:0][3:0] m_axi_arqos_o,
    output logic [{n}-1:0] m_axi_arvalid_o,
    input logic [{n}-1:0] m_axi_arready_i,
    input logic [{n}-1:0][{id_width}-1:0] m_axi_rid_i,
    input logic [{n}-1:0][31:0] m_axi_rdata_i,
    input logic [{n}-1:0][1:0] m_axi_rresp_i,
    input logic [{n}-1:0] m_axi_rlast_i,
    input logic [{n}-1:0] m_axi_rvalid_i,
    output logic [{n}-1:0] m_axi_rready_o,
    output logic [{id_width}-1:0] m_axi_awid_o,
    output logic [31:0] m_axi_awaddr_o,
    output logic [7:0] m_axi_awlen_o,
    output logic [2:0] m_axi_awsize_o,
    output logic [1:0] m_axi_awburst_o,
    output logic m_axi_awlock_o,
    output logic [3:0] m_axi_awcache_o,
    output logic [2:0] m_axi_awprot_o,
    output logic [3:0] m_axi_awqos_o,
    output logic m_axi_awvalid_o,
    input logic m_axi_awready_i,
    output logic [31:0] m_axi_wdata_o,
    output logic [3:0] m_axi_wstrb_o,
    output logic m_axi_wlast_o,
    output logic m_axi_wvalid_o,
    input logic m_axi_wready_i,
    input logic [{id_width}-1:0] m_axi_bid_i,
    input logic [1:0] m_axi_bresp_i,
    input logic m_axi_bvalid_i,
    output logic m_axi_bready_o,
    output logic irq_o
);
  chipleap_accel_axi #(
{p.sv_overrides()},
    .AXI_ID_WIDTH({id_width})
  ) u_accel (.*);
endmodule
"""


def _fusesoc_core(name: str, platform: str = "xheep") -> str:
    if platform == "standalone":
        files = ["chipleap_accel.sv", "chipleap_accel_axi.sv", "chipleap_accel_axi_top.sv"]
        top = "chipleap_accel_axi_top"
        what = "with AXI4 interfaces"
    else:
        files = ["chipleap_accel.sv", "chipleap_accel_top.sv", "chipleap_accel_xheep.sv"]
        top = "chipleap_accel_top"
        what = "for X-HEEP"
    listing = "\n".join(f"      - {f}" for f in files)
    return f"""CAPI=2:
# SPDX-License-Identifier: MIT
name: "chipleap:accel:{name}:0.1.0"
description: "ChipLeap matrix accelerator generated for {name}, {what}"

filesets:
  rtl:
    files:
{listing}
    file_type: systemVerilogSource

targets:
  default:
    filesets:
      - rtl
    toplevel: {top}
"""
