// Simulator-neutral testbench for the ChipLeap accelerator.
//
// Runs the cases in a stimulus file written by stim_gen.cpp (+stim=FILE, default
// stim.txt): for each case it loads X and W into a memory model, replays the generated
// driver's register writes, polls STATUS until the job is done, reads the CYCLES
// register, compares every output word with the golden result and prints one JSON line,
// the same fields as the Verilator testbench (tb_chipleap.cpp).
//
// The memory model grants each OBI request unless a stall is drawn and answers a fixed
// number of cycles after the grant. Stalls come from a fixed linear congruential
// generator, so every simulator sees the same pattern for the same seed. The design's
// parameters come from tb_config.svh, which ChipLeap writes next to this file.
//
// Plain SystemVerilog with an associative array and queues, so Xcelium, VCS, Questa
// and, with --timing, Verilator run it as is.
//
// SPDX-License-Identifier: MIT

`timescale 1ns / 1ps

module tb_chipleap;
  `include "tb_config.svh"

  localparam logic [31:0] REG_STATUS = 32'h04;
  localparam logic [31:0] REG_CYCLES = 32'h30;
  localparam int unsigned MAX_CYCLES = 20000000;

  logic clk = 1'b0;
  logic rst_n = 1'b0;
  always #5 clk = ~clk;

  logic        reg_valid = 1'b0;
  logic        reg_write = 1'b0;
  logic [31:0] reg_addr = '0;
  logic [31:0] reg_wdata = '0;
  logic [ 3:0] reg_wstrb = '0;
  logic [31:0] reg_rdata;
  logic        reg_error;
  logic        reg_ready;

  logic [PORTS-1:0]       rd_req;
  logic [PORTS-1:0]       rd_gnt = '0;
  logic [PORTS-1:0][31:0] rd_addr;
  logic [PORTS-1:0]       rd_rvalid = '0;
  logic [PORTS-1:0][31:0] rd_rdata = '0;
  logic                   wr_req;
  logic                   wr_gnt = 1'b0;
  logic [31:0]            wr_addr;
  logic [31:0]            wr_wdata;
  logic [ 3:0]            wr_be;
  logic                   wr_rvalid = 1'b0;
  logic                   irq;

  chipleap_accel #(
      .DATAFLOW(DATAFLOW),
      .ROWS(ROWS),
      .COLS(COLS),
      .WBITS(WBITS),
      .PORTS(PORTS),
      .ABUF_WORDS(ABUF_WORDS),
      .WBUF_WORDS(WBUF_WORDS),
      .DOUBLE_BUF(DOUBLE_BUF),
      .ACC_ROWS(ACC_ROWS)
  ) dut (
      .clk_i(clk),
      .rst_ni(rst_n),
      .reg_valid_i(reg_valid),
      .reg_write_i(reg_write),
      .reg_addr_i(reg_addr),
      .reg_wdata_i(reg_wdata),
      .reg_wstrb_i(reg_wstrb),
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
      .ext_req_o(),
      .ext_gnt_i('0),
      .ext_addr_o(),
      .ext_rvalid_i('0),
      .ext_rdata_i('0),
      .irq_o(irq)
  );

  // ---------------------------------------------------------------------------
  // Memory model
  // ---------------------------------------------------------------------------
  logic [31:0] mem[int unsigned];
  int unsigned cycle = 0;
  int unsigned stall_pct = 0;
  int unsigned latency = 1;
  int unsigned lcg = 1;

  // Pending responses per port as ring buffers: plain arrays behave the same in every
  // simulator, where arrays of queues do not. The accelerator keeps at most
  // MAX_OUTSTANDING (4) requests in flight per port, far below the depth.
  localparam int unsigned QD = 64;
  int unsigned rq_ready[PORTS][QD];
  logic [31:0] rq_addr[PORTS][QD];
  int unsigned rq_head[PORTS];
  int unsigned rq_count[PORTS];
  int unsigned wq_ready[QD];
  int unsigned wq_head = 0;
  int unsigned wq_count = 0;
  logic [PORTS-1:0] take_r;
  logic [31:0] raddr[PORTS];
  logic take_w;
  logic [31:0] waddr, wdata;

  function automatic logic [31:0] mem_rd(input logic [31:0] a);
    return mem.exists(a) ? mem[a] : 32'hA5A5A5A5;
  endfunction

  function automatic logic grant();
    if (stall_pct == 0) return 1'b1;
    lcg = lcg * 32'd1103515245 + 32'd12345;
    return ((lcg >> 16) % 100) >= stall_pct;
  endfunction

  // Responses and grants for the request state of this cycle, set while the clock is low.
  always @(negedge clk) begin
    for (int p = 0; p < PORTS; p++) begin
      take_r[p] = rd_req[p] && grant();
      rd_gnt[p] = take_r[p];
      raddr[p] = rd_addr[p];
      if (rq_count[p] > 0 && rq_ready[p][rq_head[p]] <= cycle) begin
        rd_rvalid[p] = 1'b1;
        rd_rdata[p] = mem_rd(rq_addr[p][rq_head[p]]);
      end else begin
        rd_rvalid[p] = 1'b0;
      end
    end
    take_w = wr_req && grant();
    wr_gnt = take_w;
    waddr = wr_addr;
    wdata = wr_wdata;
    wr_rvalid = wq_count > 0 && wq_ready[wq_head] <= cycle;
  end

  // What the rising edge accepted.
  always @(posedge clk) begin
    for (int p = 0; p < PORTS; p++) begin
      if (rd_rvalid[p]) begin
        rq_head[p] = (rq_head[p] + 1) % QD;
        rq_count[p] = rq_count[p] - 1;
      end
      if (take_r[p]) begin
        rq_ready[p][(rq_head[p] + rq_count[p]) % QD] = cycle + latency;
        rq_addr[p][(rq_head[p] + rq_count[p]) % QD] = raddr[p];
        rq_count[p] = rq_count[p] + 1;
      end
    end
    if (wr_rvalid) begin
      wq_head = (wq_head + 1) % QD;
      wq_count = wq_count - 1;
    end
    if (take_w) begin
      mem[waddr] = wdata;
      wq_ready[(wq_head + wq_count) % QD] = cycle + latency;
      wq_count = wq_count + 1;
    end
    cycle++;
  end

  // ---------------------------------------------------------------------------
  // Register access, one cycle each, as the Verilator testbench does
  // ---------------------------------------------------------------------------
  task automatic reg_wr(input logic [31:0] off, input logic [31:0] val);
    @(negedge clk);
    reg_valid = 1'b1;
    reg_write = 1'b1;
    reg_addr  = off;
    reg_wdata = val;
    reg_wstrb = 4'hF;
    @(negedge clk);
    reg_valid = 1'b0;
    reg_write = 1'b0;
  endtask

  task automatic reg_rd(input logic [31:0] off, output logic [31:0] val);
    @(negedge clk);
    reg_valid = 1'b1;
    reg_write = 1'b0;
    reg_addr  = off;
    #1 val = reg_rdata;
    @(negedge clk);
    reg_valid = 1'b0;
  endtask

  // ---------------------------------------------------------------------------
  // Cases
  // ---------------------------------------------------------------------------
  string stim;
  integer fd;
  int unsigned m, k, n, fmt, stall, lat, seed, tm, tn, tk, xres, nw, nm, ne, yaddr;
  logic [31:0] off[$], val[$], eaddr[$], edata[$];
  logic [31:0] a, d, status, cycles;
  int unsigned failures = 0;
  int unsigned bad, t0, wall;

  initial begin
    if (!$value$plusargs("stim=%s", stim)) stim = "stim.txt";
    fd = $fopen(stim, "r");
    if (fd == 0) begin
      $display("{\"error\":\"cannot open %s\"}", stim);
      $finish;
    end
    repeat (4) @(negedge clk);
    rst_n = 1'b1;
    @(negedge clk);

    while ($fscanf(fd, "%h", m) == 1) begin
      bad = 0;
      void'($fscanf(fd, "%h %h %h %h %h %h", k, n, fmt, stall, lat, seed));
      void'($fscanf(fd, "%h %h %h %h %h %h %h %h", tm, tn, tk, xres, nw, nm, ne, yaddr));
      off.delete();
      val.delete();
      eaddr.delete();
      edata.delete();
      for (int i = 0; i < nw; i++) begin
        void'($fscanf(fd, "%h %h", a, d));
        off.push_back(a);
        val.push_back(d);
      end
      mem.delete();
      for (int i = 0; i < nm; i++) begin
        void'($fscanf(fd, "%h %h", a, d));
        mem[a] = d;
      end
      for (int i = 0; i < ne; i++) begin
        void'($fscanf(fd, "%h %h", a, d));
        eaddr.push_back(a);
        edata.push_back(d);
      end
      stall_pct = stall;
      latency = lat < 1 ? 1 : lat;
      lcg = seed | 1;

      for (int i = 0; i < nw; i++) reg_wr(off[i], val[i]);
      t0 = cycle;
      status = 32'h1;
      while (status[0] && cycle - t0 < MAX_CYCLES) reg_rd(REG_STATUS, status);
      wall = cycle - t0;
      reg_rd(REG_CYCLES, cycles);

      for (int i = 0; i < ne; i++) begin
        if (mem_rd(eaddr[i]) !== edata[i]) begin
          if (bad < 3)
            $display("# mismatch at %h: got %h expected %h", eaddr[i], mem_rd(eaddr[i]), edata[i]);
          bad++;
        end
      end
      if (status[0]) bad = bad + 1;
      if (bad != 0) failures++;
      $display(
          "{\"m\":%0d,\"k\":%0d,\"n\":%0d,\"fmt\":%0d,\"stall\":%0d,\"latency\":%0d,\"tm\":%0d,\"tn\":%0d,\"tk\":%0d,\"x_resident\":%0d,\"cycles\":%0d,\"wall\":%0d,\"mismatches\":%0d,\"ok\":%s}",
          m, k, n, fmt, stall, latency, tm, tn, tk, xres, cycles, wall, bad,
          bad != 0 ? "false" : "true");
    end
    $fclose(fd);
    $display("# done, %0d failing cases", failures);
    $finish;
  end
endmodule
