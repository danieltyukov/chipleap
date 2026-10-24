// The ChipLeap accelerator in a clock domain of its own, with the clock domain crossings
// at its bus interface.
//
// chipleap_accel_cdc has chipleap_accel's ports on the bus side, timed by clk_i (X-HEEP's
// clock), and runs the accelerator on accel_clk_i. Every signal crosses one of three
// standard structures:
//
//   register interface  a two-phase request/acknowledge handshake: the bus side holds the
//                       request in registers and toggles a request bit, which reaches the
//                       accelerator through a two-flop synchronizer; the accelerator side
//                       captures the request when the synchronized bit changes, performs it,
//                       holds the response and toggles an acknowledge bit back the same way.
//                       The bus sees ready when the response is captured on its side.
//   OBI master ports    for each read port and the write port, an asynchronous FIFO for the
//                       requests (accelerator to bus) and one for the responses (bus to
//                       accelerator), with gray-coded pointers through two-flop
//                       synchronizers. The accelerator side grants a request only while
//                       fewer than DEPTH are in flight, so the response FIFO always has room
//                       for what the bus returns (OBI responses cannot be held back).
//   interrupt           a level, registered in the accelerator's clock and then through
//                       a two-flop synchronizer.
//
// rst_ni is asserted asynchronously in both domains and released in each through its own
// reset synchronizer.
//
// The direct read ports to external memory (ext_*, with EXT_DIRECT) cross nothing: they
// stay on accel_clk_i, and the memory ports run on that clock too, with X-HEEP's side
// crossing into it instead (chipleap_mem.sv).
//
// SPDX-License-Identifier: MIT

/* verilator lint_off DECLFILENAME */  // the crossing blocks, then chipleap_accel_cdc

// A bit (or a gray-coded word) through STAGES flip-flops of the destination clock.
module chipleap_cdc_sync #(
    parameter int unsigned WIDTH  = 1,
    parameter int unsigned STAGES = 2
) (
    input  logic             clk_i,
    input  logic             rst_ni,
    input  logic [WIDTH-1:0] d_i,
    output logic [WIDTH-1:0] q_o
);
  logic [STAGES-1:0][WIDTH-1:0] sync_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) sync_q <= '0;
    else begin
      sync_q[0] <= d_i;
      for (int unsigned s = 1; s < STAGES; s++) sync_q[s] <= sync_q[s-1];
    end
  end
  assign q_o = sync_q[STAGES-1];
endmodule

// Asserted with rst_ni at once, released two cycles of clk_i after it.
module chipleap_cdc_reset (
    input  logic clk_i,
    input  logic rst_ni,
    output logic rst_no
);
  logic meta_q, sync_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      meta_q <= 1'b0;
      sync_q <= 1'b0;
    end else begin
      meta_q <= 1'b1;
      sync_q <= meta_q;
    end
  end
  assign rst_no = sync_q;
endmodule

// An asynchronous FIFO of 2^AW words that shows its oldest word: rdata_o is valid whenever
// empty_o is low, and pop_i takes it. full_o is the write side's view, which lags the
// reads; a write while it is high is lost, so a writer that cannot wait must keep fewer
// than 2^AW words in it by other means.
module chipleap_cdc_fifo #(
    parameter int unsigned WIDTH = 32,
    parameter int unsigned AW    = 3
) (
    input  logic             wclk_i,
    input  logic             wrst_ni,
    input  logic             push_i,
    input  logic [WIDTH-1:0] wdata_i,
    output logic             full_o,
    input  logic             rclk_i,
    input  logic             rrst_ni,
    input  logic             pop_i,
    output logic [WIDTH-1:0] rdata_o,
    output logic             empty_o
);
  logic [WIDTH-1:0] mem[1 << AW];
  logic [AW:0] wbin, wptr, rbin, rptr;
  logic [AW:0] rptr_w, wptr_r;  // the read pointer in wclk_i, the write pointer in rclk_i
  wire do_push = push_i && !full_o;
  wire do_pop = pop_i && !empty_o;
  wire [AW:0] wbin_next = wbin + {{AW{1'b0}}, do_push};
  wire [AW:0] rbin_next = rbin + {{AW{1'b0}}, do_pop};
  wire [AW:0] wptr_next = (wbin_next >> 1) ^ wbin_next;
  wire [AW:0] rptr_next = (rbin_next >> 1) ^ rbin_next;

  always_ff @(posedge wclk_i or negedge wrst_ni) begin
    if (!wrst_ni) begin
      wbin <= '0;
      wptr <= '0;
    end else begin
      wbin <= wbin_next;
      wptr <= wptr_next;
    end
  end
  always_ff @(posedge wclk_i) if (do_push) mem[wbin[AW-1:0]] <= wdata_i;

  always_ff @(posedge rclk_i or negedge rrst_ni) begin
    if (!rrst_ni) begin
      rbin <= '0;
      rptr <= '0;
    end else begin
      rbin <= rbin_next;
      rptr <= rptr_next;
    end
  end
  assign rdata_o = mem[rbin[AW-1:0]];

  chipleap_cdc_sync #(
      .WIDTH(AW + 1)
  ) u_rptr_sync (
      .clk_i (wclk_i),
      .rst_ni(wrst_ni),
      .d_i   (rptr),
      .q_o   (rptr_w)
  );
  chipleap_cdc_sync #(
      .WIDTH(AW + 1)
  ) u_wptr_sync (
      .clk_i (rclk_i),
      .rst_ni(rrst_ni),
      .d_i   (wptr),
      .q_o   (wptr_r)
  );
  assign empty_o = rptr == wptr_r;
  if (AW > 1) begin : g_full
    assign full_o = wptr == {~rptr_w[AW:AW-1], rptr_w[AW-2:0]};
  end else begin : g_full1
    assign full_o = wptr == ~rptr_w;
  end
endmodule

// One OBI master port across the clocks: requests of REQW bits (address, and for a write
// the data and byte enables) from the accelerator to the bus, responses of RSPW bits back.
module chipleap_cdc_obi #(
    parameter int unsigned REQW = 32,
    parameter int unsigned RSPW = 32,
    parameter int unsigned AW   = 3
) (
    // the accelerator's side
    input  logic            a_clk_i,
    input  logic            a_rst_ni,
    input  logic            a_req_i,
    output logic            a_gnt_o,
    input  logic [REQW-1:0] a_payload_i,
    output logic            a_rvalid_o,
    output logic [RSPW-1:0] a_rdata_o,
    // the bus's side
    input  logic            b_clk_i,
    input  logic            b_rst_ni,
    output logic            b_req_o,
    input  logic            b_gnt_i,
    output logic [REQW-1:0] b_payload_o,
    input  logic            b_rvalid_i,
    input  logic [RSPW-1:0] b_rdata_i
);
  localparam int unsigned DEPTH = 1 << AW;
  logic req_full, req_empty, rsp_full, rsp_empty;
  // Requests granted on this side and not yet answered here. At most DEPTH, so the
  // responses for them always fit in the response FIFO.
  logic [AW:0] inflight_q;
  assign a_gnt_o = a_req_i && !req_full && inflight_q < (AW + 1)'(DEPTH);
  assign a_rvalid_o = !rsp_empty;
  always_ff @(posedge a_clk_i or negedge a_rst_ni) begin
    if (!a_rst_ni) inflight_q <= '0;
    else inflight_q <= inflight_q + (AW + 1)'(a_gnt_o) - (AW + 1)'(a_rvalid_o);
  end

  chipleap_cdc_fifo #(
      .WIDTH(REQW),
      .AW(AW)
  ) u_req (
      .wclk_i (a_clk_i),
      .wrst_ni(a_rst_ni),
      .push_i (a_gnt_o),
      .wdata_i(a_payload_i),
      .full_o (req_full),
      .rclk_i (b_clk_i),
      .rrst_ni(b_rst_ni),
      .pop_i  (b_gnt_i),
      .rdata_o(b_payload_o),
      .empty_o(req_empty)
  );
  assign b_req_o = !req_empty;

  chipleap_cdc_fifo #(
      .WIDTH(RSPW),
      .AW(AW)
  ) u_rsp (
      .wclk_i (b_clk_i),
      .wrst_ni(b_rst_ni),
      .push_i (b_rvalid_i),
      .wdata_i(b_rdata_i),
      .full_o (rsp_full),
      .rclk_i (a_clk_i),
      .rrst_ni(a_rst_ni),
      .pop_i  (1'b1),
      .rdata_o(a_rdata_o),
      .empty_o(rsp_empty)
  );
  /* verilator lint_off UNUSEDSIGNAL */
  wire unused = rsp_full;  // never full: inflight_q bounds it
  /* verilator lint_on UNUSEDSIGNAL */
endmodule

// The register interface across the clocks: the bus side's valid is held until ready.
module chipleap_cdc_reg (
    // the bus's side
    input  logic        b_clk_i,
    input  logic        b_rst_ni,
    input  logic        b_valid_i,
    input  logic        b_write_i,
    input  logic [31:0] b_addr_i,
    input  logic [31:0] b_wdata_i,
    input  logic [ 3:0] b_wstrb_i,
    output logic [31:0] b_rdata_o,
    output logic        b_error_o,
    output logic        b_ready_o,
    // the accelerator's side
    input  logic        a_clk_i,
    input  logic        a_rst_ni,
    output logic        a_valid_o,
    output logic        a_write_o,
    output logic [31:0] a_addr_o,
    output logic [31:0] a_wdata_o,
    output logic [ 3:0] a_wstrb_o,
    input  logic [31:0] a_rdata_i,
    input  logic        a_error_i,
    input  logic        a_ready_i
);
  // Bus side: the request, held while it crosses; the response, captured on its way back.
  logic        b_busy_q, b_done_q, b_req_q;
  logic [68:0] b_cmd_q;  // write, addr, wdata, wstrb
  logic [32:0] b_rsp_q;  // error, rdata
  logic        b_ack, b_ack_seen_q;
  // Accelerator side.
  logic        a_req, a_req_seen_q, a_go_q, a_ack_q;
  logic [68:0] a_cmd_q;
  logic [32:0] a_rsp_q;

  always_ff @(posedge b_clk_i or negedge b_rst_ni) begin
    if (!b_rst_ni) begin
      b_busy_q <= 1'b0;
      b_done_q <= 1'b0;
      b_req_q <= 1'b0;
      b_cmd_q <= '0;
      b_rsp_q <= '0;
      b_ack_seen_q <= 1'b0;
    end else begin
      b_ack_seen_q <= b_ack;
      if (b_done_q) begin
        b_done_q <= 1'b0;
        b_busy_q <= 1'b0;
      end else if (b_ack != b_ack_seen_q) begin
        b_rsp_q <= a_rsp_q;
        b_done_q <= 1'b1;
      end else if (b_valid_i && !b_busy_q) begin
        b_cmd_q <= {b_write_i, b_addr_i, b_wdata_i, b_wstrb_i};
        b_req_q <= !b_req_q;
        b_busy_q <= 1'b1;
      end
    end
  end
  assign b_ready_o = b_done_q;
  assign {b_error_o, b_rdata_o} = b_rsp_q;

  chipleap_cdc_sync u_req_sync (
      .clk_i (a_clk_i),
      .rst_ni(a_rst_ni),
      .d_i   (b_req_q),
      .q_o   (a_req)
  );
  always_ff @(posedge a_clk_i or negedge a_rst_ni) begin
    if (!a_rst_ni) begin
      a_req_seen_q <= 1'b0;
      a_go_q <= 1'b0;
      a_ack_q <= 1'b0;
      a_cmd_q <= '0;
      a_rsp_q <= '0;
    end else begin
      a_req_seen_q <= a_req;
      if (a_req != a_req_seen_q) begin
        a_cmd_q <= b_cmd_q;
        a_go_q <= 1'b1;
      end else if (a_go_q && a_ready_i) begin
        a_rsp_q <= {a_error_i, a_rdata_i};
        a_ack_q <= !a_ack_q;
        a_go_q <= 1'b0;
      end
    end
  end
  assign a_valid_o = a_go_q;
  assign {a_write_o, a_addr_o, a_wdata_o, a_wstrb_o} = a_cmd_q;

  chipleap_cdc_sync u_ack_sync (
      .clk_i (b_clk_i),
      .rst_ni(b_rst_ni),
      .d_i   (a_ack_q),
      .q_o   (b_ack)
  );
endmodule

module chipleap_accel_cdc #(
    parameter int unsigned DATAFLOW = 0,
    parameter int unsigned ROWS = 8,
    parameter int unsigned COLS = 8,
    parameter int unsigned WBITS = 8,
    parameter int unsigned PORTS = 2,
    parameter int unsigned ABUF_WORDS = 2048,
    parameter int unsigned WBUF_WORDS = 1024,
    parameter int unsigned DOUBLE_BUF = 1,
    parameter int unsigned ACC_ROWS = 8,
    parameter int unsigned CDC_AW = 3,  // each crossing FIFO holds 2^CDC_AW words
    parameter int unsigned EXT_DIRECT = 0,
    parameter logic [31:0] EXT_BASE = 32'hC0000000,
    parameter logic [31:0] EXT_SIZE = 32'h40000000,
    parameter int unsigned EXT_OUTSTANDING = 16
) (
    input logic clk_i,  // the bus's clock
    input logic rst_ni,
    input logic accel_clk_i,  // the accelerator's clock
    input  logic        reg_valid_i,
    input  logic        reg_write_i,
    input  logic [31:0] reg_addr_i,
    input  logic [31:0] reg_wdata_i,
    input  logic [ 3:0] reg_wstrb_i,
    output logic [31:0] reg_rdata_o,
    output logic        reg_error_o,
    output logic        reg_ready_o,
    output logic [PORTS-1:0]       rd_req_o,
    input  logic [PORTS-1:0]       rd_gnt_i,
    output logic [PORTS-1:0][31:0] rd_addr_o,
    input  logic [PORTS-1:0]       rd_rvalid_i,
    input  logic [PORTS-1:0][31:0] rd_rdata_i,
    output logic        wr_req_o,
    input  logic        wr_gnt_i,
    output logic [31:0] wr_addr_o,
    output logic [31:0] wr_wdata_o,
    output logic [ 3:0] wr_be_o,
    input  logic        wr_rvalid_i,
    // on accel_clk_i
    output logic [PORTS-1:0]       ext_req_o,
    input  logic [PORTS-1:0]       ext_gnt_i,
    output logic [PORTS-1:0][31:0] ext_addr_o,
    input  logic [PORTS-1:0]       ext_rvalid_i,
    input  logic [PORTS-1:0][31:0] ext_rdata_i,
    output logic irq_o
);
  /* verilator lint_on DECLFILENAME */
  logic b_rst_n, a_rst_n;
  chipleap_cdc_reset u_b_rst (
      .clk_i (clk_i),
      .rst_ni(rst_ni),
      .rst_no(b_rst_n)
  );
  chipleap_cdc_reset u_a_rst (
      .clk_i (accel_clk_i),
      .rst_ni(rst_ni),
      .rst_no(a_rst_n)
  );

  logic a_reg_valid, a_reg_write, a_reg_error, a_reg_ready;
  logic [31:0] a_reg_addr, a_reg_wdata, a_reg_rdata;
  logic [3:0] a_reg_wstrb;
  logic [PORTS-1:0] a_rreq, a_rgnt, a_rvalid;
  logic [PORTS-1:0][31:0] a_raddr, a_rdata;
  logic a_wreq, a_wgnt, a_wvalid, a_irq;
  logic [31:0] a_waddr, a_wdata;
  logic [3:0] a_wbe;

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
      // As many reads in flight per port as its crossing holds.
      .MAX_OUTSTANDING(1 << CDC_AW),
      .EXT_DIRECT(EXT_DIRECT),
      .EXT_BASE(EXT_BASE),
      .EXT_SIZE(EXT_SIZE),
      .EXT_OUTSTANDING(EXT_OUTSTANDING)
  ) u_accel (
      .clk_i(accel_clk_i),
      .rst_ni(a_rst_n),
      .reg_valid_i(a_reg_valid),
      .reg_write_i(a_reg_write),
      .reg_addr_i(a_reg_addr),
      .reg_wdata_i(a_reg_wdata),
      .reg_wstrb_i(a_reg_wstrb),
      .reg_rdata_o(a_reg_rdata),
      .reg_error_o(a_reg_error),
      .reg_ready_o(a_reg_ready),
      .rd_req_o(a_rreq),
      .rd_gnt_i(a_rgnt),
      .rd_addr_o(a_raddr),
      .rd_rvalid_i(a_rvalid),
      .rd_rdata_i(a_rdata),
      .wr_req_o(a_wreq),
      .wr_gnt_i(a_wgnt),
      .wr_addr_o(a_waddr),
      .wr_wdata_o(a_wdata),
      .wr_be_o(a_wbe),
      .wr_rvalid_i(a_wvalid),
      .ext_req_o,
      .ext_gnt_i,
      .ext_addr_o,
      .ext_rvalid_i,
      .ext_rdata_i,
      .irq_o(a_irq)
  );

  chipleap_cdc_reg u_reg (
      .b_clk_i(clk_i),
      .b_rst_ni(b_rst_n),
      .b_valid_i(reg_valid_i),
      .b_write_i(reg_write_i),
      .b_addr_i(reg_addr_i),
      .b_wdata_i(reg_wdata_i),
      .b_wstrb_i(reg_wstrb_i),
      .b_rdata_o(reg_rdata_o),
      .b_error_o(reg_error_o),
      .b_ready_o(reg_ready_o),
      .a_clk_i(accel_clk_i),
      .a_rst_ni(a_rst_n),
      .a_valid_o(a_reg_valid),
      .a_write_o(a_reg_write),
      .a_addr_o(a_reg_addr),
      .a_wdata_o(a_reg_wdata),
      .a_wstrb_o(a_reg_wstrb),
      .a_rdata_i(a_reg_rdata),
      .a_error_i(a_reg_error),
      .a_ready_i(a_reg_ready)
  );

  for (genvar p = 0; p < PORTS; p++) begin : g_rd
    chipleap_cdc_obi #(
        .REQW(32),
        .RSPW(32),
        .AW  (CDC_AW)
    ) u_rd (
        .a_clk_i(accel_clk_i),
        .a_rst_ni(a_rst_n),
        .a_req_i(a_rreq[p]),
        .a_gnt_o(a_rgnt[p]),
        .a_payload_i(a_raddr[p]),
        .a_rvalid_o(a_rvalid[p]),
        .a_rdata_o(a_rdata[p]),
        .b_clk_i(clk_i),
        .b_rst_ni(b_rst_n),
        .b_req_o(rd_req_o[p]),
        .b_gnt_i(rd_gnt_i[p]),
        .b_payload_o(rd_addr_o[p]),
        .b_rvalid_i(rd_rvalid_i[p]),
        .b_rdata_i(rd_rdata_i[p])
    );
  end

  /* verilator lint_off UNUSEDSIGNAL */
  logic w_rsp;  // a write's response carries no data
  /* verilator lint_on UNUSEDSIGNAL */
  chipleap_cdc_obi #(
      .REQW(68),
      .RSPW(1),
      .AW  (CDC_AW)
  ) u_wr (
      .a_clk_i(accel_clk_i),
      .a_rst_ni(a_rst_n),
      .a_req_i(a_wreq),
      .a_gnt_o(a_wgnt),
      .a_payload_i({a_wbe, a_waddr, a_wdata}),
      .a_rvalid_o(a_wvalid),
      .a_rdata_o(w_rsp),
      .b_clk_i(clk_i),
      .b_rst_ni(b_rst_n),
      .b_req_o(wr_req_o),
      .b_gnt_i(wr_gnt_i),
      .b_payload_o({wr_be_o, wr_addr_o, wr_wdata_o}),
      .b_rvalid_i(wr_rvalid_i),
      .b_rdata_i(1'b0)
  );

  // The interrupt is a gate's output in the accelerator (done and enabled): registered
  // there first, so the synchronizer never captures a glitch.
  logic a_irq_q;
  always_ff @(posedge accel_clk_i or negedge a_rst_n) begin
    if (!a_rst_n) a_irq_q <= 1'b0;
    else a_irq_q <= a_irq;
  end
  chipleap_cdc_sync u_irq (
      .clk_i (clk_i),
      .rst_ni(b_rst_n),
      .d_i   (a_irq_q),
      .q_o   (irq_o)
  );
endmodule
