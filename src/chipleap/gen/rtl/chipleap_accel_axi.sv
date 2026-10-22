// AXI4 wrapper of the ChipLeap matrix accelerator, for any host.
//
// The accelerator core speaks a register interface and OBI. This wrapper turns them into
// the AMBA interfaces most SoCs use, so the same core works next to an Arm, RISC-V or
// FPGA processing system:
//
//   s_axil_*   AXI4-Lite slave, 32 bit, the register map (one access at a time, OKAY
//              responses; a write and a read arriving together are taken write first)
//   m_axi_ar*  one AXI4 read master per core read port: single-beat INCR reads of
//   m_axi_r*   32 bits, a fixed ID, so responses return in order. Each port keeps up to
//              MAX_OUTSTANDING reads in flight, the same as on OBI, so a port still moves
//              one word per cycle when the interconnect keeps up
//   m_axi_aw*  one AXI4 write master for the results: single-beat writes; a result
//   m_axi_w*   counts as issued once both its address and its data handshakes are done
//   m_axi_b*
//
// Read port 0 together with the write channels forms one complete AXI4 master; ports 1
// and up are read-only masters. All ready signals the wrapper drives towards memory are
// tied high: the core always accepts read data and write responses. Error responses
// (SLVERR, DECERR) are not reported back to software.
//
// SPDX-License-Identifier: MIT

module chipleap_accel_axi #(
    parameter int unsigned DATAFLOW = 0,
    parameter int unsigned ROWS = 8,
    parameter int unsigned COLS = 8,
    parameter int unsigned WBITS = 8,
    parameter int unsigned PORTS = 2,
    parameter int unsigned ABUF_WORDS = 2048,
    parameter int unsigned WBUF_WORDS = 1024,
    parameter int unsigned DOUBLE_BUF = 1,
    parameter int unsigned ACC_ROWS = 8,
    parameter int unsigned MAX_OUTSTANDING = 4,
    parameter int unsigned AXI_ID_WIDTH = 1
) (
    input logic clk_i,
    input logic rst_ni,

    // AXI4-Lite slave: registers
    input  logic [31:0] s_axil_awaddr_i,
    input  logic [ 2:0] s_axil_awprot_i,
    input  logic        s_axil_awvalid_i,
    output logic        s_axil_awready_o,
    input  logic [31:0] s_axil_wdata_i,
    input  logic [ 3:0] s_axil_wstrb_i,
    input  logic        s_axil_wvalid_i,
    output logic        s_axil_wready_o,
    output logic [ 1:0] s_axil_bresp_o,
    output logic        s_axil_bvalid_o,
    input  logic        s_axil_bready_i,
    input  logic [31:0] s_axil_araddr_i,
    input  logic [ 2:0] s_axil_arprot_i,
    input  logic        s_axil_arvalid_i,
    output logic        s_axil_arready_o,
    output logic [31:0] s_axil_rdata_o,
    output logic [ 1:0] s_axil_rresp_o,
    output logic        s_axil_rvalid_o,
    input  logic        s_axil_rready_i,

    // AXI4 read masters, one per read port
    output logic [PORTS-1:0][AXI_ID_WIDTH-1:0] m_axi_arid_o,
    output logic [PORTS-1:0][            31:0] m_axi_araddr_o,
    output logic [PORTS-1:0][             7:0] m_axi_arlen_o,
    output logic [PORTS-1:0][             2:0] m_axi_arsize_o,
    output logic [PORTS-1:0][             1:0] m_axi_arburst_o,
    output logic [PORTS-1:0]                   m_axi_arlock_o,
    output logic [PORTS-1:0][             3:0] m_axi_arcache_o,
    output logic [PORTS-1:0][             2:0] m_axi_arprot_o,
    output logic [PORTS-1:0][             3:0] m_axi_arqos_o,
    output logic [PORTS-1:0]                   m_axi_arvalid_o,
    input  logic [PORTS-1:0]                   m_axi_arready_i,
    input  logic [PORTS-1:0][AXI_ID_WIDTH-1:0] m_axi_rid_i,
    input  logic [PORTS-1:0][            31:0] m_axi_rdata_i,
    input  logic [PORTS-1:0][             1:0] m_axi_rresp_i,
    input  logic [PORTS-1:0]                   m_axi_rlast_i,
    input  logic [PORTS-1:0]                   m_axi_rvalid_i,
    output logic [PORTS-1:0]                   m_axi_rready_o,

    // AXI4 write master: results
    output logic [AXI_ID_WIDTH-1:0] m_axi_awid_o,
    output logic [            31:0] m_axi_awaddr_o,
    output logic [             7:0] m_axi_awlen_o,
    output logic [             2:0] m_axi_awsize_o,
    output logic [             1:0] m_axi_awburst_o,
    output logic                    m_axi_awlock_o,
    output logic [             3:0] m_axi_awcache_o,
    output logic [             2:0] m_axi_awprot_o,
    output logic [             3:0] m_axi_awqos_o,
    output logic                    m_axi_awvalid_o,
    input  logic                    m_axi_awready_i,
    output logic [            31:0] m_axi_wdata_o,
    output logic [             3:0] m_axi_wstrb_o,
    output logic                    m_axi_wlast_o,
    output logic                    m_axi_wvalid_o,
    input  logic                    m_axi_wready_i,
    input  logic [AXI_ID_WIDTH-1:0] m_axi_bid_i,
    input  logic [             1:0] m_axi_bresp_i,
    input  logic                    m_axi_bvalid_i,
    output logic                    m_axi_bready_o,

    output logic irq_o
);

  // ---------------------------------------------------------------------------
  // AXI4-Lite slave to the core's register interface
  // ---------------------------------------------------------------------------
  logic        reg_valid, reg_write, reg_error, reg_ready;
  logic [31:0] reg_addr, reg_rdata;
  logic bvalid_q, rvalid_q, berr_q, rerr_q;
  logic [31:0] rdata_q;

  // The core answers in the cycle it is asked, so an access completes on its handshake.
  wire wr_fire = s_axil_awvalid_i && s_axil_wvalid_i && !bvalid_q;
  wire rd_fire = s_axil_arvalid_i && !rvalid_q && !wr_fire;

  assign s_axil_awready_o = wr_fire;
  assign s_axil_wready_o = wr_fire;
  assign s_axil_arready_o = rd_fire;
  assign reg_valid = wr_fire || rd_fire;
  assign reg_write = wr_fire;
  assign reg_addr = wr_fire ? s_axil_awaddr_i : s_axil_araddr_i;

  assign s_axil_bvalid_o = bvalid_q;
  assign s_axil_bresp_o = berr_q ? 2'b10 : 2'b00;
  assign s_axil_rvalid_o = rvalid_q;
  assign s_axil_rdata_o = rdata_q;
  assign s_axil_rresp_o = rerr_q ? 2'b10 : 2'b00;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      bvalid_q <= 1'b0;
      berr_q <= 1'b0;
      rvalid_q <= 1'b0;
      rerr_q <= 1'b0;
      rdata_q <= '0;
    end else begin
      if (wr_fire) begin
        bvalid_q <= 1'b1;
        berr_q <= reg_error || !reg_ready;
      end else if (s_axil_bready_i) begin
        bvalid_q <= 1'b0;
      end
      if (rd_fire) begin
        rvalid_q <= 1'b1;
        rerr_q <= reg_error || !reg_ready;
        rdata_q <= reg_rdata;
      end else if (s_axil_rready_i) begin
        rvalid_q <= 1'b0;
      end
    end
  end

  // ---------------------------------------------------------------------------
  // OBI read ports to AXI4 read masters
  // ---------------------------------------------------------------------------
  logic [PORTS-1:0] rd_req, rd_gnt, rd_rvalid;
  logic [PORTS-1:0][31:0] rd_addr, rd_rdata;

  for (genvar p = 0; p < int'(PORTS); p++) begin : g_rd
    assign m_axi_arid_o[p] = '0;
    assign m_axi_araddr_o[p] = rd_addr[p];
    assign m_axi_arlen_o[p] = 8'd0;  // one beat
    assign m_axi_arsize_o[p] = 3'd2;  // 4 bytes
    assign m_axi_arburst_o[p] = 2'b01;  // INCR
    assign m_axi_arlock_o[p] = 1'b0;
    assign m_axi_arcache_o[p] = 4'b0011;  // normal, non-cacheable, bufferable
    assign m_axi_arprot_o[p] = 3'b000;
    assign m_axi_arqos_o[p] = 4'd0;
    // The core holds its request and address until granted, as AXI requires of VALID.
    assign m_axi_arvalid_o[p] = rd_req[p];
    assign rd_gnt[p] = m_axi_arready_i[p];
    assign rd_rvalid[p] = m_axi_rvalid_i[p];
    assign rd_rdata[p] = m_axi_rdata_i[p];
    assign m_axi_rready_o[p] = 1'b1;
  end

  // ---------------------------------------------------------------------------
  // OBI write port to the AXI4 write master
  // ---------------------------------------------------------------------------
  logic wr_req, wr_gnt, wr_rvalid;
  logic [31:0] wr_addr, wr_wdata;
  logic [3:0] wr_be;
  logic aw_done_q, w_done_q;

  wire aw_hs = m_axi_awvalid_o && m_axi_awready_i;
  wire w_hs = m_axi_wvalid_o && m_axi_wready_i;
  // A result is granted once its address and its data have both been accepted, in
  // either order or in the same cycle.
  assign wr_gnt = wr_req && (aw_done_q || aw_hs) && (w_done_q || w_hs);

  assign m_axi_awid_o = '0;
  assign m_axi_awaddr_o = wr_addr;
  assign m_axi_awlen_o = 8'd0;
  assign m_axi_awsize_o = 3'd2;
  assign m_axi_awburst_o = 2'b01;
  assign m_axi_awlock_o = 1'b0;
  assign m_axi_awcache_o = 4'b0011;
  assign m_axi_awprot_o = 3'b000;
  assign m_axi_awqos_o = 4'd0;
  assign m_axi_awvalid_o = wr_req && !aw_done_q;
  assign m_axi_wdata_o = wr_wdata;
  assign m_axi_wstrb_o = wr_be;
  assign m_axi_wlast_o = 1'b1;
  assign m_axi_wvalid_o = wr_req && !w_done_q;
  assign m_axi_bready_o = 1'b1;
  assign wr_rvalid = m_axi_bvalid_i;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      aw_done_q <= 1'b0;
      w_done_q <= 1'b0;
    end else if (wr_gnt) begin
      aw_done_q <= 1'b0;
      w_done_q <= 1'b0;
    end else begin
      if (aw_hs) aw_done_q <= 1'b1;
      if (w_hs) w_done_q <= 1'b1;
    end
  end

  // ---------------------------------------------------------------------------
  // The accelerator core
  // ---------------------------------------------------------------------------
  /* verilator lint_off PINCONNECTEMPTY */
  chipleap_accel #(
      .DATAFLOW(DATAFLOW),
      .ROWS(ROWS),
      .COLS(COLS),
      .WBITS(WBITS),
      .PORTS(PORTS),
      .ABUF_WORDS(ABUF_WORDS),
      .WBUF_WORDS(WBUF_WORDS),
      .DOUBLE_BUF(DOUBLE_BUF),
      .ACC_ROWS(ACC_ROWS),
      .MAX_OUTSTANDING(MAX_OUTSTANDING)
  ) u_core (
      .clk_i,
      .rst_ni,
      .reg_valid_i(reg_valid),
      .reg_write_i(reg_write),
      .reg_addr_i(reg_addr),
      .reg_wdata_i(s_axil_wdata_i),
      .reg_wstrb_i(s_axil_wstrb_i),
      .reg_rdata_o(reg_rdata),
      .reg_error_o(reg_error),
      .reg_ready_o(reg_ready),
      .rd_req_o(rd_req),
      .rd_gnt_i(rd_gnt),
      .rd_addr_o(rd_addr),
      .rd_rvalid_i(rd_rvalid),
      .rd_rdata_i(rd_rdata),
      .wr_req_o(wr_req),
      .wr_gnt_i(wr_gnt),
      .wr_addr_o(wr_addr),
      .wr_wdata_o(wr_wdata),
      .wr_be_o(wr_be),
      .wr_rvalid_i(wr_rvalid),
      // The host's interconnect reaches all of memory: no direct path.
      .ext_req_o(),
      .ext_gnt_i('0),
      .ext_addr_o(),
      .ext_rvalid_i('0),
      .ext_rdata_i('0),
      .irq_o
  );
  /* verilator lint_on PINCONNECTEMPTY */

  // Inputs AXI defines but this wrapper does not need: protection bits of register
  // accesses, response IDs (one ID, in order), response codes and RLAST (single beats).
  logic unused;
  assign unused = ^s_axil_awprot_i ^ ^s_axil_arprot_i ^ ^m_axi_rid_i ^ ^m_axi_rresp_i ^
                  ^m_axi_rlast_i ^ ^m_axi_bid_i ^ ^m_axi_bresp_i;

endmodule
