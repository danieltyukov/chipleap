// The SoC's memory ports: X-HEEP's external slave ports and the ChipLeap accelerator's
// direct read ports, on one pipelined OBI port per accelerator read port.
//
// In X-HEEP an external master such as the accelerator reaches only the system crossbar:
// X-HEEP's external slave window is served by one OBI port per X-HEEP master (the CPU, the
// debug module, the DMA's ports), split off before that crossbar. The accelerator's
// direct read ports (chipleap_accel with EXT_DIRECT) therefore go to the memory
// controller here, beside X-HEEP:
//
//   X-HEEP's NX ports --- round robin (clk_i) ---+
//                                                 +--- round robin --- memory port 0
//   accelerator's direct port 0 -----------------+
//   accelerator's direct ports 1 .. PORTS-1 --------------------------- memory ports 1 ..
//
// Every port follows OBI: a request holds its address phase until it is granted, the
// response comes in a later cycle, and a port may have several requests in flight,
// answered in order. The memory controller (not part of ChipLeap) accepts outstanding
// requests on each memory port and answers them in order; docs/xheep.md has the contract.
//
// With CDC the memory ports run on accel_clk_i, the accelerator's own clock, with the
// accelerator's direct ports, so the stream from memory crosses no clock. X-HEEP's side
// crosses into that clock instead, through an asynchronous FIFO for its requests and one
// for its responses (chipleap_cdc_obi from chipleap_accel_cdc.sv), XCDC_DEPTH in flight.
// Without CDC everything runs on clk_i and accel_clk_i is unused.
//
// SPDX-License-Identifier: MIT

/* verilator lint_off DECLFILENAME */  // the arbiter, then chipleap_mem

// N OBI masters to one OBI slave. Round robin among the masters that request: after a
// grant, the next master in turn has priority. A request the slave has not granted keeps
// its place, so the slave sees req and the address phase stable until its grant, and a
// master that requests waits for at most N - 1 grants to the others. The master of each
// granted request goes into a queue of DEPTH entries that routes the responses back in
// order; while it is full no request goes to the slave. rdata goes to every master, and
// m_rvalid_o says whose response it is.
module chipleap_obi_arb #(
    parameter int unsigned N = 2,
    parameter int unsigned DEPTH = 16  // a power of two
) (
    input  logic                clk_i,
    input  logic                rst_ni,
    input  logic [N-1:0]        m_req_i,
    output logic [N-1:0]        m_gnt_o,
    input  logic [N-1:0]        m_we_i,
    input  logic [N-1:0][ 3:0]  m_be_i,
    input  logic [N-1:0][31:0]  m_addr_i,
    input  logic [N-1:0][31:0]  m_wdata_i,
    output logic [N-1:0]        m_rvalid_o,
    output logic [31:0]         m_rdata_o,
    output logic                s_req_o,
    input  logic                s_gnt_i,
    output logic                s_we_o,
    output logic [ 3:0]         s_be_o,
    output logic [31:0]         s_addr_o,
    output logic [31:0]         s_wdata_o,
    input  logic                s_rvalid_i,
    input  logic [31:0]         s_rdata_i
);
  localparam int unsigned IW = N > 1 ? $clog2(N) : 1;
  localparam int unsigned DW = $clog2(DEPTH);

  logic [IW-1:0] prio_q, sel, held_sel_q;
  logic held_q, any, full, grant;
  logic [IW-1:0] queue[DEPTH];
  logic [DW-1:0] wp_q, rp_q;
  logic [DW:0] count_q;

  // The first master that requests, from the one with priority on; or the one whose
  // request is waiting for its grant.
  always_comb begin
    any = 1'b0;
    sel = prio_q;
    for (int unsigned i = 0; i < N; i++) begin
      logic [IW:0] j;
      j = (IW + 1)'(prio_q) + (IW + 1)'(i);
      if (j >= (IW + 1)'(N)) j = j - (IW + 1)'(N);
      if (!any && m_req_i[IW'(j)]) begin
        any = 1'b1;
        sel = IW'(j);
      end
    end
    if (held_q) sel = held_sel_q;
  end

  assign full = count_q == (DW + 1)'(DEPTH);
  assign s_req_o = any && !full;
  assign grant = s_req_o && s_gnt_i;
  assign s_we_o = m_we_i[sel];
  assign s_be_o = m_be_i[sel];
  assign s_addr_o = m_addr_i[sel];
  assign s_wdata_o = m_wdata_i[sel];
  always_comb begin
    m_gnt_o = '0;
    m_gnt_o[sel] = grant;
    m_rvalid_o = '0;
    m_rvalid_o[queue[rp_q]] = s_rvalid_i;
  end
  assign m_rdata_o = s_rdata_i;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      prio_q <= '0;
      held_q <= 1'b0;
      held_sel_q <= '0;
      wp_q <= '0;
      rp_q <= '0;
      count_q <= '0;
    end else begin
      held_q <= s_req_o && !s_gnt_i;
      held_sel_q <= sel;
      if (grant) begin
        prio_q <= sel == IW'(N - 1) ? '0 : sel + IW'(1);
        wp_q <= wp_q + DW'(1);
      end
      if (s_rvalid_i) rp_q <= rp_q + DW'(1);
      count_q <= count_q + (DW + 1)'(grant) - (DW + 1)'(s_rvalid_i);
    end
  end
  always_ff @(posedge clk_i) if (grant) queue[wp_q] <= sel;
endmodule

module chipleap_mem #(
    parameter int unsigned PORTS = 1,  // the accelerator's direct read ports
    parameter int unsigned NX = 3,  // X-HEEP's external slave ports
    parameter int unsigned CDC = 0,  // 1: memory ports on accel_clk_i, X-HEEP's side crosses
    // requests in flight on memory port 0: the accelerator's port 0's and X-HEEP's
    parameter int unsigned DEPTH = 32,
    parameter int unsigned XDEPTH = 4,  // requests in flight from X-HEEP's side
    /* verilator lint_off UNUSEDPARAM */  // used only with CDC
    parameter int unsigned XCDC_AW = 1  // X-HEEP's crossing holds 2^XCDC_AW requests
    /* verilator lint_on UNUSEDPARAM */
) (
    input logic clk_i,  // X-HEEP's clock
    input logic rst_ni,
    input logic accel_clk_i,  // with CDC: the accelerator's clock and the memory ports'

    // X-HEEP's external slave ports, on clk_i
    input  logic [NX-1:0]       x_req_i,
    output logic [NX-1:0]       x_gnt_o,
    input  logic [NX-1:0]       x_we_i,
    input  logic [NX-1:0][ 3:0] x_be_i,
    input  logic [NX-1:0][31:0] x_addr_i,
    input  logic [NX-1:0][31:0] x_wdata_i,
    output logic [NX-1:0]       x_rvalid_o,
    output logic [NX-1:0][31:0] x_rdata_o,

    // The accelerator's direct read ports, on the memory ports' clock
    input  logic [PORTS-1:0]       a_req_i,
    output logic [PORTS-1:0]       a_gnt_o,
    input  logic [PORTS-1:0][31:0] a_addr_i,
    output logic [PORTS-1:0]       a_rvalid_o,
    output logic [PORTS-1:0][31:0] a_rdata_o,

    // The memory ports
    output logic [PORTS-1:0]       mem_req_o,
    input  logic [PORTS-1:0]       mem_gnt_i,
    output logic [PORTS-1:0]       mem_we_o,
    output logic [PORTS-1:0][ 3:0] mem_be_o,
    output logic [PORTS-1:0][31:0] mem_addr_o,
    output logic [PORTS-1:0][31:0] mem_wdata_o,
    input  logic [PORTS-1:0]       mem_rvalid_i,
    input  logic [PORTS-1:0][31:0] mem_rdata_i
);
  /* verilator lint_on DECLFILENAME */
  logic m_clk, m_rst_n;
  /* verilator lint_off UNUSEDSIGNAL */  // unused with one X-HEEP port and one clock
  logic x_rst_n;
  /* verilator lint_on UNUSEDSIGNAL */

  // X-HEEP's ports to one, on clk_i.
  logic xm_req, xm_gnt, xm_we, xm_rvalid;
  logic [3:0] xm_be;
  logic [31:0] xm_addr, xm_wdata, xm_rdata;
  if (NX > 1) begin : g_xarb
    logic [31:0] rdata;
    chipleap_obi_arb #(
        .N(NX),
        .DEPTH(XDEPTH)
    ) u_xarb (
        .clk_i,
        .rst_ni(x_rst_n),
        .m_req_i(x_req_i),
        .m_gnt_o(x_gnt_o),
        .m_we_i(x_we_i),
        .m_be_i(x_be_i),
        .m_addr_i(x_addr_i),
        .m_wdata_i(x_wdata_i),
        .m_rvalid_o(x_rvalid_o),
        .m_rdata_o(rdata),
        .s_req_o(xm_req),
        .s_gnt_i(xm_gnt),
        .s_we_o(xm_we),
        .s_be_o(xm_be),
        .s_addr_o(xm_addr),
        .s_wdata_o(xm_wdata),
        .s_rvalid_i(xm_rvalid),
        .s_rdata_i(xm_rdata)
    );
    assign x_rdata_o = {NX{rdata}};
  end else begin : g_x1
    assign xm_req = x_req_i[0];
    assign x_gnt_o = xm_gnt;
    assign xm_we = x_we_i[0];
    assign xm_be = x_be_i[0];
    assign xm_addr = x_addr_i[0];
    assign xm_wdata = x_wdata_i[0];
    assign x_rvalid_o = xm_rvalid;
    assign x_rdata_o = xm_rdata;
  end

  // X-HEEP's side on the memory ports' clock.
  logic xs_req, xs_gnt, xs_we, xs_rvalid;
  logic [3:0] xs_be;
  logic [31:0] xs_addr, xs_wdata, xs_rdata;
  if (CDC != 0) begin : g_cdc
    chipleap_cdc_reset u_x_rst (
        .clk_i (clk_i),
        .rst_ni(rst_ni),
        .rst_no(x_rst_n)
    );
    chipleap_cdc_reset u_m_rst (
        .clk_i (accel_clk_i),
        .rst_ni(rst_ni),
        .rst_no(m_rst_n)
    );
    assign m_clk = accel_clk_i;
    chipleap_cdc_obi #(
        .REQW(69),
        .RSPW(32),
        .AW  (XCDC_AW)
    ) u_xcdc (
        .a_clk_i(clk_i),
        .a_rst_ni(x_rst_n),
        .a_req_i(xm_req),
        .a_gnt_o(xm_gnt),
        .a_payload_i({xm_we, xm_be, xm_addr, xm_wdata}),
        .a_rvalid_o(xm_rvalid),
        .a_rdata_o(xm_rdata),
        .b_clk_i(accel_clk_i),
        .b_rst_ni(m_rst_n),
        .b_req_o(xs_req),
        .b_gnt_i(xs_gnt),
        .b_payload_o({xs_we, xs_be, xs_addr, xs_wdata}),
        .b_rvalid_i(xs_rvalid),
        .b_rdata_i(xs_rdata)
    );
  end else begin : g_shared
    assign m_clk = clk_i;
    assign m_rst_n = rst_ni;
    assign x_rst_n = rst_ni;
    assign xs_req = xm_req;
    assign xm_gnt = xs_gnt;
    assign xs_we = xm_we;
    assign xs_be = xm_be;
    assign xs_addr = xm_addr;
    assign xs_wdata = xm_wdata;
    assign xm_rvalid = xs_rvalid;
    assign xm_rdata = xs_rdata;
    /* verilator lint_off UNUSEDSIGNAL */
    wire unused_clk = accel_clk_i;  // only the crossing uses it
    /* verilator lint_on UNUSEDSIGNAL */
  end

  // Memory port 0: X-HEEP's side and the accelerator's port 0.
  logic [1:0] m_gnt, m_rvalid;
  chipleap_obi_arb #(
      .N(2),
      .DEPTH(DEPTH)
  ) u_arb (
      .clk_i(m_clk),
      .rst_ni(m_rst_n),
      .m_req_i({a_req_i[0], xs_req}),
      .m_gnt_o(m_gnt),
      .m_we_i({1'b0, xs_we}),
      .m_be_i({4'hF, xs_be}),
      .m_addr_i({a_addr_i[0], xs_addr}),
      .m_wdata_i({32'h0, xs_wdata}),
      .m_rvalid_o(m_rvalid),
      .m_rdata_o(xs_rdata),
      .s_req_o(mem_req_o[0]),
      .s_gnt_i(mem_gnt_i[0]),
      .s_we_o(mem_we_o[0]),
      .s_be_o(mem_be_o[0]),
      .s_addr_o(mem_addr_o[0]),
      .s_wdata_o(mem_wdata_o[0]),
      .s_rvalid_i(mem_rvalid_i[0]),
      .s_rdata_i(mem_rdata_i[0])
  );
  assign {a_gnt_o[0], xs_gnt} = m_gnt;
  assign {a_rvalid_o[0], xs_rvalid} = m_rvalid;
  assign a_rdata_o[0] = mem_rdata_i[0];

  // The other direct ports have their memory ports to themselves.
  for (genvar p = 1; p < PORTS; p++) begin : g_direct
    assign mem_req_o[p] = a_req_i[p];
    assign a_gnt_o[p] = mem_gnt_i[p];
    assign mem_we_o[p] = 1'b0;
    assign mem_be_o[p] = 4'hF;
    assign mem_addr_o[p] = a_addr_i[p];
    assign mem_wdata_o[p] = '0;
    assign a_rvalid_o[p] = mem_rvalid_i[p];
    assign a_rdata_o[p] = mem_rdata_i[p];
  end
endmodule
