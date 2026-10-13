// ChipLeap matrix accelerator for X-HEEP.
//
// Computes Y[M][N] (int32) = X[M][K] (int8) * W[N][K]^T, with W packed as int8, int4 or
// int2 into little-endian 32-bit words. The job is cut into tiles of TM x TN x TK and
// walked in m0, n0, k0 order:
//
//   loader   reads the X and W slices of the next tile into a buffer slot (all read ports)
//   core     computes on a full slot and accumulates into the output tile
//   store    writes the finished output tile through the write port
//
// With DOUBLE_BUF the buffers hold two slots, so the loader fills one while the core
// works on the other. When the whole TM x K block of X fits in the activation buffer
// (cfg_xres), X is loaded once per m0 and stays resident while every n0 reuses it.
//
// The schedule is the one chipleap.model.accel assumes, and the C driver's
// chipleap_plan() picks TM and TK the same way the model does.
//
// Register map (byte offsets):
//   0x00 CTRL    W   bit 0 start
//   0x04 STATUS  R   bit 0 busy, bit 1 done
//   0x08 X_ADDR  RW  0x0C W_ADDR RW  0x10 Y_ADDR RW
//   0x14 M       RW  0x18 K (multiple of the K alignment) RW  0x1C N RW
//   0x20 TM      RW  0x24 TK RW
//   0x28 FMT     RW  weight bits: 8, 4 or 2 (at most WBITS)
//   0x2C XRES    RW  bit 0: X block resident
//   0x30 CYCLES  R   cycles of the last job
//   0x34 INFO    R   {ROWS[31:24], COLS[23:16], WBITS[15:12], PORTS[11:4], DATAFLOW[1:0]}
//   0x38 IRQ_EN  RW  bit 0
//
// With SRAM_BUF the buffers are single-port SRAM banks outside the core (gemv with one
// read port only): the activation buffer a bank per slot, the weight buffer a bank per
// slot and lane, each word COLS bytes. The core presents a bank's address in the cycle it
// would read the flop array, and the bank's word, a cycle later, takes the place of the
// operand registers, so the schedule is the same cycle for cycle. chipleap_accel below
// is the core with its buffers inside; ChipLeap's layout top puts the macros beside it.
//
// SPDX-License-Identifier: MIT

/* verilator lint_off DECLFILENAME */  // the core, then chipleap_accel at the end of the file
module chipleap_accel_core #(
    parameter int unsigned DATAFLOW = 0,  // 0 gemv, 1 weight stationary, 2 output stationary
    parameter int unsigned ROWS = 8,
    parameter int unsigned COLS = 8,
    parameter int unsigned WBITS = 8,
    parameter int unsigned PORTS = 2,
    parameter int unsigned ABUF_WORDS = 2048,
    parameter int unsigned WBUF_WORDS = 1024,
    parameter int unsigned DOUBLE_BUF = 1,
    parameter int unsigned ACC_ROWS = 8,
    parameter int unsigned MAX_OUTSTANDING = 4,
    parameter int unsigned SRAM_BUF = 0,
    // The banks' shape, from the parameters above; not set on their own.
    parameter int unsigned BANK_BITS = 32 * (COLS >= 4 ? COLS / 4 : 1),
    parameter int unsigned NAB = DOUBLE_BUF != 0 ? 2 : 1,
    parameter int unsigned NWB = NAB * ROWS,
    parameter int unsigned ABA = ABUF_WORDS / NAB / (BANK_BITS / 32) > 1 ?
                                 $clog2(ABUF_WORDS / NAB / (BANK_BITS / 32)) : 1,
    parameter int unsigned WBA = WBUF_WORDS / NWB / (BANK_BITS / 32) > 1 ?
                                 $clog2(WBUF_WORDS / NWB / (BANK_BITS / 32)) : 1
) (
    input logic clk_i,
    input logic rst_ni,

    // Register interface (PULP register_interface semantics, single-cycle ready)
    input  logic        reg_valid_i,
    input  logic        reg_write_i,
    input  logic [31:0] reg_addr_i,
    input  logic [31:0] reg_wdata_i,
    input  logic [ 3:0] reg_wstrb_i,
    output logic [31:0] reg_rdata_o,
    output logic        reg_error_o,
    output logic        reg_ready_o,

    // OBI read masters
    output logic [PORTS-1:0]       rd_req_o,
    input  logic [PORTS-1:0]       rd_gnt_i,
    output logic [PORTS-1:0][31:0] rd_addr_o,
    input  logic [PORTS-1:0]       rd_rvalid_i,
    input  logic [PORTS-1:0][31:0] rd_rdata_i,

    // OBI write master
    output logic        wr_req_o,
    input  logic        wr_gnt_i,
    output logic [31:0] wr_addr_o,
    output logic [31:0] wr_wdata_o,
    output logic [ 3:0] wr_be_o,
    input  logic        wr_rvalid_i,

    // SRAM banks (SRAM_BUF): a read, a write, the word address, and the data and bit mask
    // every write shares; the word read comes back a cycle later.
    output logic [NAB-1:0]            abank_re_o,
    output logic [NAB-1:0]            abank_we_o,
    output logic [NAB-1:0][ABA-1:0]   abank_addr_o,
    output logic [NWB-1:0]            wbank_re_o,
    output logic [NWB-1:0]            wbank_we_o,
    output logic [NWB-1:0][WBA-1:0]   wbank_addr_o,
    output logic [BANK_BITS-1:0]      bank_wdata_o,
    output logic [BANK_BITS-1:0]      bank_wmask_o,
    /* verilator lint_off UNUSEDSIGNAL */  // without SRAM_BUF
    input  logic [NAB-1:0][BANK_BITS-1:0] abank_rdata_i,
    input  logic [NWB-1:0][BANK_BITS-1:0] wbank_rdata_i,
    /* verilator lint_on UNUSEDSIGNAL */

    output logic irq_o
);
  /* verilator lint_on DECLFILENAME */

  localparam int unsigned TN = (DATAFLOW == 0) ? ROWS : COLS;
  localparam int unsigned TM_MAX = (DATAFLOW == 2) ? ROWS : ACC_ROWS;
  localparam int unsigned TMI = TM_MAX > 1 ? $clog2(TM_MAX) : 1;
  localparam int unsigned TNI = TN > 1 ? $clog2(TN) : 1;
  localparam bit DB = DOUBLE_BUF != 0;
  localparam int unsigned A_SLOT = DB ? ABUF_WORDS / 2 : ABUF_WORDS;
  localparam int unsigned W_SLOT = DB ? WBUF_WORDS / 2 : WBUF_WORDS;
  localparam int unsigned NSLOT = DB ? 2 : 1;
  localparam int unsigned OSTW = $clog2(MAX_OUTSTANDING + 1);
  localparam int unsigned AWA = $clog2(ABUF_WORDS);
  localparam int unsigned AWW = $clog2(WBUF_WORDS);
  localparam int unsigned AW = AWA > AWW ? AWA : AWW;
  localparam bit SB = SRAM_BUF != 0;
  localparam int unsigned WPM = BANK_BITS / 32;  // buffer words in a bank word
  localparam int unsigned W_ROW = W_SLOT / ROWS;  // buffer words of a lane's bank

  // ---------------------------------------------------------------------------
  // Registers
  // ---------------------------------------------------------------------------
  logic [31:0] cfg_x, cfg_w, cfg_y, cfg_m, cfg_k, cfg_n, cfg_tm, cfg_tk, cfg_fmt;
  logic cfg_xres, irq_en;
  logic busy_q, done_q;
  logic [31:0] cycles_q;
  logic start;
  logic job_done;

  wire [5:0] reg_word = reg_addr_i[7:2];
  assign reg_ready_o = 1'b1;
  assign reg_error_o = 1'b0;
  assign start = reg_valid_i && reg_write_i && reg_word == 6'h00 && reg_wdata_i[0] && !busy_q;

  always_comb begin
    reg_rdata_o = '0;
    unique case (reg_word)
      6'h01: reg_rdata_o = {30'b0, done_q, busy_q};
      6'h02: reg_rdata_o = cfg_x;
      6'h03: reg_rdata_o = cfg_w;
      6'h04: reg_rdata_o = cfg_y;
      6'h05: reg_rdata_o = cfg_m;
      6'h06: reg_rdata_o = cfg_k;
      6'h07: reg_rdata_o = cfg_n;
      6'h08: reg_rdata_o = cfg_tm;
      6'h09: reg_rdata_o = cfg_tk;
      6'h0A: reg_rdata_o = cfg_fmt;
      6'h0B: reg_rdata_o = {31'b0, cfg_xres};
      6'h0C: reg_rdata_o = cycles_q;
      6'h0D: reg_rdata_o = {8'(ROWS), 8'(COLS), 4'(WBITS), 8'(PORTS), 2'b0, 2'(DATAFLOW)};
      6'h0E: reg_rdata_o = {31'b0, irq_en};
      default: reg_rdata_o = '0;
    endcase
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      cfg_x <= '0;
      cfg_w <= '0;
      cfg_y <= '0;
      cfg_m <= 32'd1;
      cfg_k <= 32'd4;
      cfg_n <= 32'd1;
      cfg_tm <= 32'd1;
      cfg_tk <= 32'd4;
      cfg_fmt <= 32'(WBITS);
      cfg_xres <= 1'b0;
      irq_en <= 1'b0;
      busy_q <= 1'b0;
      done_q <= 1'b0;
      cycles_q <= '0;
    end else begin
      if (reg_valid_i && reg_write_i && !busy_q) begin
        unique case (reg_word)
          6'h02: cfg_x <= reg_wdata_i;
          6'h03: cfg_w <= reg_wdata_i;
          6'h04: cfg_y <= reg_wdata_i;
          6'h05: cfg_m <= reg_wdata_i;
          6'h06: cfg_k <= reg_wdata_i;
          6'h07: cfg_n <= reg_wdata_i;
          6'h08: cfg_tm <= reg_wdata_i;
          6'h09: cfg_tk <= reg_wdata_i;
          6'h0A: cfg_fmt <= reg_wdata_i;
          6'h0B: cfg_xres <= reg_wdata_i[0];
          6'h0E: irq_en <= reg_wdata_i[0];
          default: ;
        endcase
      end
      if (start) begin
        busy_q <= 1'b1;
        done_q <= 1'b0;
        cycles_q <= '0;
      end else if (busy_q) begin
        cycles_q <= cycles_q + 32'd1;
        if (job_done) begin
          busy_q <= 1'b0;
          done_q <= 1'b1;
        end
      end
    end
  end
  assign irq_o = done_q & irq_en;

  // Weight format helpers: elements per word is 32 / fmt.
  logic [1:0] fmt_shift;  // log2(32 / fmt) - 2: 0 for int8, 1 for int4, 2 for int2
  always_comb begin
    unique case (cfg_fmt[3:0])
      4'd4: fmt_shift = 2'd1;
      4'd2: fmt_shift = 2'd2;
      default: fmt_shift = 2'd0;
    endcase
  end
  // bytes = elements * fmt / 8, words = elements * fmt / 32
  function automatic logic [31:0] w_bytes(input logic [31:0] elems);
    return elems >> fmt_shift;
  endfunction
  function automatic logic [31:0] w_words(input logic [31:0] elems);
    return elems >> (fmt_shift + 2);
  endfunction

  // ---------------------------------------------------------------------------
  // Buffers (flop arrays without SRAM_BUF; the banks outside it with)
  // ---------------------------------------------------------------------------
  /* verilator lint_off UNDRIVEN */  // with SRAM_BUF
  /* verilator lint_off UNUSEDSIGNAL */
  logic [31:0] abuf[ABUF_WORDS];
  logic [31:0] wbuf[WBUF_WORDS];
  /* verilator lint_on UNUSEDSIGNAL */
  /* verilator lint_on UNDRIVEN */

  // ---------------------------------------------------------------------------
  // DMA: 2-D copy from memory into a buffer over all read ports
  // ---------------------------------------------------------------------------
  logic        dma_start, dma_to_w, dma_done;
  logic [31:0] dma_rows, dma_wpr, dma_src, dma_src_off, dma_src_stride;
  logic [31:0] dma_dst, dma_dst_stride;

  logic        dma_active;
  // A transfer spends its first cycle in setup: the source address is a base plus an offset,
  // and the last word and row indices are the counts less one, all formed here from
  // registers so that none of that arithmetic is in series with the loader's.
  logic        dma_setup;
  logic [31:0] iss_off, iss_wpr, iss_rows;
  logic        dma_tow_q;
  logic [31:0] iss_row, iss_word, iss_rowaddr, iss_rowdst;
  logic [31:0] iss_sstride, iss_dstride;
  // Index of the last word in a row and of the last row, set when a transfer starts, so the
  // per-word check is an equality on registers rather than an increment and a compare.
  logic [31:0] iss_wlast, iss_rlast;
  logic        iss_all;

  logic [PORTS-1:0] pend_v;
  logic [PORTS-1:0][31:0] pend_addr;
  logic [PORTS-1:0][AW-1:0] pend_dst;
  logic [AW-1:0] fifo[PORTS*MAX_OUTSTANDING];  // per-port response queues, flattened
  logic [PORTS-1:0][$clog2(MAX_OUTSTANDING)-1:0] fifo_wp, fifo_rp;
  logic [PORTS-1:0][OSTW-1:0] outst;

  // Per-cycle allocation of new requests to ports, in port order.
  logic [PORTS-1:0] alloc;
  logic [PORTS-1:0][31:0] alloc_addr;
  logic [PORTS-1:0][AW-1:0] alloc_dst;
  logic [31:0] nx_row, nx_word, nx_rowaddr, nx_rowdst;
  logic nx_all;

  always_comb begin
    logic [31:0] r, w, ra, rd;
    logic all_done;
    r = iss_row;
    w = iss_word;
    ra = iss_rowaddr;
    rd = iss_rowdst;
    all_done = iss_all;
    for (int p = 0; p < PORTS; p++) begin
      logic [OSTW:0] after;
      after = (OSTW + 1)'(outst[p]) + (OSTW + 1)'(pend_v[p] && rd_gnt_i[p])
              - (OSTW + 1)'(rd_rvalid_i[p]);
      alloc[p] = 1'b0;
      alloc_addr[p] = '0;
      alloc_dst[p] = '0;
      if (dma_active && !dma_setup && !all_done && (!pend_v[p] || rd_gnt_i[p]) &&
          after < (OSTW + 1)'(MAX_OUTSTANDING)) begin
        alloc[p] = 1'b1;
        alloc_addr[p] = ra + (w << 2);
        alloc_dst[p] = AW'(rd + w);
        if (w == iss_wlast) begin
          w = '0;
          if (r == iss_rlast) all_done = 1'b1;
          r = r + 32'd1;
          ra = ra + iss_sstride;
          rd = rd + iss_dstride;
        end else begin
          w = w + 32'd1;
        end
      end
    end
    nx_row = r;
    nx_word = w;
    nx_rowaddr = ra;
    nx_rowdst = rd;
    nx_all = all_done;
  end

  assign rd_req_o = pend_v;
  assign rd_addr_o = pend_addr;

  logic any_outstanding;
  always_comb begin
    any_outstanding = 1'b0;
    for (int p = 0; p < PORTS; p++) begin
      if (pend_v[p] || outst[p] != '0) any_outstanding = 1'b1;
    end
  end
  assign dma_done = dma_active && !dma_setup && iss_all && !any_outstanding;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      dma_active <= 1'b0;
      dma_setup <= 1'b0;
      iss_off <= '0;
      iss_wpr <= '0;
      iss_rows <= '0;
      dma_tow_q <= 1'b0;
      iss_row <= '0;
      iss_word <= '0;
      iss_rowaddr <= '0;
      iss_rowdst <= '0;
      iss_sstride <= '0;
      iss_dstride <= '0;
      iss_wlast <= '0;
      iss_rlast <= '0;
      iss_all <= 1'b0;
      pend_v <= '0;
      pend_addr <= '0;
      pend_dst <= '0;
      fifo_wp <= '0;
      fifo_rp <= '0;
      outst <= '0;
    end else begin
      if (dma_start) begin
        dma_active <= 1'b1;
        dma_tow_q <= dma_to_w;
        iss_row <= '0;
        iss_word <= '0;
        dma_setup <= 1'b1;
        iss_rowaddr <= dma_src;
        iss_off <= dma_src_off;
        iss_wpr <= dma_wpr;
        iss_rows <= dma_rows;
        iss_rowdst <= dma_dst;
        iss_sstride <= dma_src_stride;
        iss_dstride <= dma_dst_stride;
        iss_all <= 1'b1;
      end else if (dma_active) begin
        if (dma_setup) begin
          dma_setup <= 1'b0;
          iss_rowaddr <= iss_rowaddr + iss_off;
          iss_wlast <= iss_wpr - 32'd1;
          iss_rlast <= iss_rows - 32'd1;
          iss_all <= (iss_rows == 0) || (iss_wpr == 0);
        end else if (dma_done) begin
          dma_active <= 1'b0;
        end else begin
          iss_row <= nx_row;
          iss_word <= nx_word;
          iss_rowaddr <= nx_rowaddr;
          iss_rowdst <= nx_rowdst;
          iss_all <= nx_all;
        end
      end
      for (int p = 0; p < PORTS; p++) begin
        logic g, v;
        g = pend_v[p] && rd_gnt_i[p];
        v = rd_rvalid_i[p];
        if (g) begin
          fifo[p*MAX_OUTSTANDING+int'(fifo_wp[p])] <= pend_dst[p];
          fifo_wp[p] <= fifo_wp[p] + 1'b1;
        end
        if (v) begin
          fifo_rp[p] <= fifo_rp[p] + 1'b1;
        end
        outst[p] <= outst[p] + OSTW'(g) - OSTW'(v);
        if (alloc[p]) begin
          pend_v[p] <= 1'b1;
          pend_addr[p] <= alloc_addr[p];
          pend_dst[p] <= alloc_dst[p];
        end else if (g) begin
          pend_v[p] <= 1'b0;
        end
      end
    end
  end

  // Buffer writes from read responses; with SRAM_BUF the gemv core sends them to the banks.
  if (!SB) begin : g_buf_write
    always_ff @(posedge clk_i) begin
      for (int p = 0; p < PORTS; p++) begin
        if (rd_rvalid_i[p]) begin
          if (dma_tow_q) wbuf[AWW'(fifo[p*MAX_OUTSTANDING+int'(fifo_rp[p])])] <= rd_rdata_i[p];
          else abuf[AWA'(fifo[p*MAX_OUTSTANDING+int'(fifo_rp[p])])] <= rd_rdata_i[p];
        end
      end
    end
    assign abank_re_o = '0;
    assign abank_we_o = '0;
    assign abank_addr_o = '0;
    assign wbank_re_o = '0;
    assign wbank_we_o = '0;
    assign wbank_addr_o = '0;
    assign bank_wdata_o = '0;
    assign bank_wmask_o = '0;
  end

  // ---------------------------------------------------------------------------
  // Tile descriptors, one per buffer slot
  // ---------------------------------------------------------------------------
  typedef struct packed {
    logic [31:0] tm;
    logic [31:0] tn;
    logic [31:0] tk;
    logic [31:0] k0;
    logic [31:0] ybase;
    logic first_k;
    logic last_k;
  } desc_t;

  desc_t desc[NSLOT];
  logic [NSLOT-1:0] desc_v;
  logic [NSLOT-1:0] desc_set, desc_clr;
  desc_t desc_new;

  // ---------------------------------------------------------------------------
  // Loader
  // ---------------------------------------------------------------------------
  typedef enum logic [2:0] {
    L_IDLE,
    L_NEXT,
    L_X,
    L_XWAIT,
    L_W,
    L_WWAIT,
    L_PUSH,
    L_DONE
  } lstate_t;
  lstate_t ls;

  logic [31:0] l_n0, l_k0;
  logic [31:0] l_xrow, l_wrow, l_yrow;  // byte addresses of X[m0][0], W[n0][0], Y[m0][0]
  logic [31:0] l_tm, l_tn, l_tk;
  logic l_slot;
  logic [31:0] tiles_issued, tiles_done;
  logic [31:0] w_row_bytes, w_tile_bytes, tmK, tmN4;  // per-job strides, set at start
  // Whether the next tile moves along K, N or M. The tile and its position are settled once
  // L_NEXT has run, so these compares happen in L_W, a few cycles before L_PUSH needs them,
  // and the 32-bit add-compare chains are not in series with the address updates.
  logic more_k, more_n, more_m;
  logic core_idle;

  // What remains of M, N and K from the current tile position, kept as registers and updated
  // with the position, so sizing the next tile in L_NEXT is a compare and not a subtract.
  logic [31:0] rem_m, rem_n, rem_k;
  wire first_in_m = (l_n0 == 0) && (l_k0 == 0);
  wire slot_free = !desc_v[l_slot];
  wire pipe_empty = (desc_v == '0) && core_idle && (tiles_done == tiles_issued);

  always_comb begin
    dma_start = 1'b0;
    dma_to_w = 1'b0;
    dma_rows = '0;
    dma_wpr = '0;
    dma_src = '0;
    dma_src_off = '0;
    dma_src_stride = '0;
    dma_dst = '0;
    dma_dst_stride = '0;
    desc_set = '0;
    desc_new = '0;
    if (ls == L_X) begin
      dma_start = 1'b1;
      dma_to_w = 1'b0;
      dma_rows = l_tm;
      dma_src_stride = cfg_k;
      if (cfg_xres) begin
        dma_wpr = cfg_k >> 2;
        dma_src = l_xrow;
        dma_dst = '0;
        dma_dst_stride = cfg_k >> 2;
      end else begin
        dma_wpr = l_tk >> 2;
        dma_src = l_xrow;
        dma_src_off = l_k0;
        dma_dst = 32'(l_slot) * A_SLOT;
        dma_dst_stride = cfg_tk >> 2;
      end
    end else if (ls == L_W) begin
      dma_start = 1'b1;
      dma_to_w = 1'b1;
      dma_rows = l_tn;
      dma_wpr = w_words(l_tk);
      dma_src = l_wrow;
      dma_src_off = w_bytes(l_k0);
      dma_src_stride = w_row_bytes;
      dma_dst = 32'(l_slot) * W_SLOT;
      // In banks, each weight row of the tile has its lane's bank: W_ROW words apart.
      dma_dst_stride = SB ? 32'(W_ROW) : w_words(cfg_tk);
    end else if (ls == L_PUSH) begin
      desc_set[l_slot] = 1'b1;
      desc_new.tm = l_tm;
      desc_new.tn = l_tn;
      desc_new.tk = l_tk;
      desc_new.k0 = l_k0;
      desc_new.ybase = l_yrow + (l_n0 << 2);
      desc_new.first_k = (l_k0 == 0);
      desc_new.last_k = !more_k;
    end
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      ls <= L_IDLE;
      l_n0 <= '0;
      l_k0 <= '0;
      l_xrow <= '0;
      l_wrow <= '0;
      l_yrow <= '0;
      l_tm <= '0;
      l_tn <= '0;
      l_tk <= '0;
      l_slot <= 1'b0;
      tiles_issued <= '0;
      w_row_bytes <= '0;
      w_tile_bytes <= '0;
      tmK <= '0;
      tmN4 <= '0;
      more_k <= 1'b0;
      more_n <= 1'b0;
      more_m <= 1'b0;
      rem_m <= '0;
      rem_n <= '0;
      rem_k <= '0;
    end else begin
      unique case (ls)
        L_IDLE: begin
          if (start) begin
            l_n0 <= '0;
            l_k0 <= '0;
            rem_m <= cfg_m;
            rem_n <= cfg_n;
            rem_k <= cfg_k;
            l_xrow <= cfg_x;
            l_wrow <= cfg_w;
            l_yrow <= cfg_y;
            l_slot <= 1'b0;
            tiles_issued <= '0;
            w_row_bytes <= w_bytes(cfg_k);
            w_tile_bytes <= w_bytes(32'(TN) * cfg_k);
            tmK <= cfg_tm[7:0] * cfg_k;
            tmN4 <= (cfg_tm[7:0] * cfg_n) << 2;
            ls <= L_NEXT;
          end
        end
        L_NEXT: begin
          l_tm <= rem_m < cfg_tm ? rem_m : cfg_tm;
          l_tn <= rem_n < 32'(TN) ? rem_n : 32'(TN);
          l_tk <= rem_k < cfg_tk ? rem_k : cfg_tk;
          if (cfg_xres && first_in_m) begin
            // The resident block of X is overwritten: wait for the core to finish.
            if (pipe_empty) ls <= L_X;
          end else if (slot_free) begin
            ls <= cfg_xres ? L_W : L_X;
          end
        end
        L_X: ls <= L_XWAIT;
        L_XWAIT: if (dma_done) ls <= L_W;
        L_W: begin
          // Another tile follows along an axis when more remains than this tile takes; the
          // tile takes the lesser of the two, so one compare of two registers decides it.
          more_k <= rem_k > cfg_tk;
          more_n <= rem_n > 32'(TN);
          more_m <= rem_m > cfg_tm;
          ls <= L_WWAIT;
        end
        L_WWAIT: if (dma_done) ls <= L_PUSH;
        L_PUSH: begin
          tiles_issued <= tiles_issued + 32'd1;
          if (DB) l_slot <= ~l_slot;
          // Advance k0, then n0, then m0.
          if (more_k) begin
            l_k0 <= l_k0 + l_tk;
            rem_k <= rem_k - l_tk;
            ls <= L_NEXT;
          end else if (more_n) begin
            l_k0 <= '0;
            rem_k <= cfg_k;
            l_n0 <= l_n0 + l_tn;
            rem_n <= rem_n - l_tn;
            l_wrow <= l_wrow + w_tile_bytes;
            ls <= L_NEXT;
          end else if (more_m) begin
            l_k0 <= '0;
            l_n0 <= '0;
            rem_k <= cfg_k;
            rem_n <= cfg_n;
            rem_m <= rem_m - l_tm;
            l_xrow <= l_xrow + tmK;
            l_wrow <= cfg_w;
            l_yrow <= l_yrow + tmN4;
            ls <= L_NEXT;
          end else begin
            ls <= L_DONE;
          end
        end
        L_DONE: if (job_done) ls <= L_IDLE;
        default: ls <= L_IDLE;
      endcase
    end
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      desc_v <= '0;
    end else begin
      for (int s = 0; s < int'(NSLOT); s++) begin
        if (desc_set[s]) begin
          desc_v[s] <= 1'b1;
          desc[s] <= desc_new;
        end else if (desc_clr[s]) begin
          desc_v[s] <= 1'b0;
        end
      end
    end
  end

  // ---------------------------------------------------------------------------
  // Operand access. X element (row, k) of the current slot; W element (row, e) where e
  // indexes the tile's K range.
  // ---------------------------------------------------------------------------
  logic c_slot;
  /* verilator lint_off UNUSEDSIGNAL */  // first_k is unused by the output-stationary core
  desc_t cd;
  /* verilator lint_on UNUSEDSIGNAL */
  assign cd = desc[c_slot];
  wire [31:0] a_base = cfg_xres ? 32'd0 : 32'(c_slot) * A_SLOT;
  wire [31:0] a_stride = cfg_xres ? (cfg_k >> 2) : (cfg_tk >> 2);
  wire [31:0] a_koff = cfg_xres ? cd.k0 : 32'd0;
  wire [31:0] w_base = 32'(c_slot) * W_SLOT;
  wire [31:0] w_stride = w_words(cfg_tk);

  // int8 x int8 product, 16 bits wide: the multiplier stays 8 x 8 in synthesis.
  function automatic logic signed [15:0] mul8(input logic signed [7:0] a,
                                               input logic signed [7:0] b);
    return 16'(a) * 16'(b);
  endfunction

  // X element k of the row that starts at word address rowbase. Callers keep rowbase
  // up to date incrementally, so no multiplier sits in the operand path.
  function automatic logic signed [7:0] x_at(input logic [31:0] rowbase, input logic [31:0] k);
    logic [31:0] word;
    logic [31:0] kk;
    kk = a_koff + k;
    word = abuf[AWA'(rowbase + (kk >> 2))];
    return word[8*kk[1:0]+:8];
  endfunction

  function automatic logic signed [7:0] w_at(input logic [31:0] row, input logic [31:0] e);
    logic [31:0] word;
    logic [31:0] bitpos;
    bitpos = e << (3 - fmt_shift);  // e * fmt
    word = wbuf[AWW'(w_base + row * w_stride + (bitpos >> 5))];
    unique case (fmt_shift)
      2'd1: return 8'(signed'(word[bitpos[4:0]+:4]));
      2'd2: return 8'(signed'(word[bitpos[4:0]+:2]));
      default: return word[bitpos[4:0]+:8];
    endcase
  endfunction

  // ---------------------------------------------------------------------------
  // Core control: wait for a full slot, compute, store the output tile when the
  // last K tile is done, free the slot.
  // ---------------------------------------------------------------------------
  typedef enum logic [2:0] {
    C_IDLE,
    C_RUN,
    C_STORE,
    C_STWAIT,
    C_FREE
  } cstate_t;
  cstate_t cs;
  logic core_start, core_done;
  logic st_start, st_done;

  assign core_idle = (cs == C_IDLE);
  assign core_start = (cs == C_IDLE) && desc_v[c_slot];
  assign st_start = (cs == C_STORE);

  always_comb begin
    desc_clr = '0;
    if (cs == C_FREE) desc_clr[c_slot] = 1'b1;
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      cs <= C_IDLE;
      c_slot <= 1'b0;
      tiles_done <= '0;
    end else begin
      if (start) begin
        c_slot <= 1'b0;
        tiles_done <= '0;
      end
      unique case (cs)
        C_IDLE: if (desc_v[c_slot]) cs <= C_RUN;
        C_RUN: if (core_done) cs <= cd.last_k ? C_STORE : C_FREE;
        C_STORE: cs <= C_STWAIT;
        C_STWAIT: if (st_done) cs <= C_FREE;
        C_FREE: begin
          tiles_done <= tiles_done + 32'd1;
          if (DB) c_slot <= ~c_slot;
          cs <= C_IDLE;
        end
        default: cs <= C_IDLE;
      endcase
    end
  end

  assign job_done = busy_q && (ls == L_DONE) && (cs == C_IDLE) && (desc_v == '0) &&
                    (tiles_done == tiles_issued);

  // Output tile accumulators (row-major, TN per row), read by the store engine.
  logic signed [31:0] acc_out[TM_MAX*TN];

  // ---------------------------------------------------------------------------
  // Compute cores
  // ---------------------------------------------------------------------------
  if (DATAFLOW == 0) begin : g_gemv
    // ROWS lanes, each a COLS-wide dot product per cycle.
    localparam int unsigned L = ROWS;
    localparam int unsigned V = COLS;
    logic running, issuing;
    logic [31:0] gm, gkc, nchunks;
    logic [31:0] g_rowbase;  // word address of activation row gm
    // Three stages: fetch and unpack the operands into xq/wq, multiply and add them into
    // psum_q, accumulate into work/acc. The operand registers cut the path from the buffer
    // read to the adder tree in two, so the engine closes timing at the slow corner.
    logic signed [7:0] xq[V];
    logic signed [7:0] wq[L][V];
    logic ov_q, ofirst_q, olast_q;
    logic [$clog2(ACC_ROWS)-1:0] om_q;
    logic signed [31:0] psum_q[L];
    logic pv_q, pfirst_q, plast_q;
    logic [$clog2(ACC_ROWS)-1:0] pm_q;
    logic signed [31:0] work[L];
    logic signed [31:0] acc[ACC_ROWS*L];

    // Operand fetch, one chunk of V elements per cycle. Each lane's chunk is read as whole
    // words from its weight row and sliced at fixed positions per format, so no per-element
    // address arithmetic or barrel shifter sits in the datapath.
    localparam int unsigned XW = V >= 4 ? V / 4 : 1;  // words holding V int8 activations
    localparam int unsigned WW = V >= 4 ? V / 4 : 1;  // words holding V weights at int8
    wire issue = running && issuing;  // a chunk's operands are registered this cycle
    logic signed [7:0] xv[V];  // the chunk's operands, unpacked
    logic signed [7:0] wv[L][V];
    logic signed [31:0] dot[L];

    if (!SB) begin : g_flop
      logic [31:0] xk;  // K offset of the chunk within the X row
      logic [XW*32-1:0] xwords;
      logic [31:0] wbit;  // bit offset of the chunk within each weight row

      always_comb begin
        xk = a_koff + gkc * V;
        for (int j = 0; j < int'(XW); j++) xwords[32*j+:32] = abuf[AWA'(g_rowbase + (xk >> 2) + 32'(j))];
        for (int v = 0; v < int'(V); v++) begin
          // With V of four or more the chunk starts on a word boundary; smaller chunks sit
          // inside one word at a byte offset.
          if (V >= 4) xv[v] = xwords[8*v+:8];
          else xv[v] = xwords[8*(int'(xk[1:0])+v)+:8];
        end
      end

      always_comb begin
        wbit = (gkc * V) << (3 - fmt_shift);
        for (int l = 0; l < int'(L); l++) begin
          logic [WW*32-1:0] words;
          logic [WW*32-1:0] chunk;
          for (int j = 0; j < int'(WW); j++)
            words[32*j+:32] = wbuf[AWW'(w_base + 32'(l) * w_stride + (wbit >> 5) + 32'(j))];
          // Chunks narrower than a word start inside it; shift them down to bit 0.
          chunk = words >> wbit[4:0];
          for (int v = 0; v < int'(V); v++) begin
            unique case (fmt_shift)
              2'd1: wv[l][v] = 8'(signed'(chunk[4*v+:4]));
              2'd2: wv[l][v] = 8'(signed'(chunk[2*v+:2]));
              default: wv[l][v] = chunk[8*v+:8];
            endcase
          end
        end
      end
    end else begin : g_bank
      // A chunk is COLS bytes at most, at a position aligned to its size (the K
      // alignment), so it is one word of a bank: the X chunk a word of the activation
      // bank its address falls in, each lane's weight chunk a word of that lane's bank at
      // the chunk's offset in the row, sliced from the bit its format puts it at.
      //
      // A bank answers a cycle after its address, and an SRAM macro's clock to output
      // is half the clock period at the slow corner, so each chunk is read a cycle ahead:
      // at core_start for the tile's first chunk, and in each issue cycle for the next.
      // Its word is out of the bank in the cycle that issues it, which registers it in
      // xq and wq as the flop buffers' read is, and the schedule stays the same.
      //
      // Where the next chunk is read is kept in registers, a cycle ahead of its read: the
      // row's next chunk is the last one read plus a chunk, the next row's first is this
      // row's first plus the row stride. Between an issue and the banks' enables and
      // addresses there are then only compares and a select; worked out in the issue
      // cycle, the next chunk's address put two 32-bit adders there, and the routed
      // design missed its clock at the slow corner on that path.
      localparam int unsigned SBI = NAB > 1 ? $clog2(NAB) : 1;
      localparam int unsigned WSI = $clog2(BANK_BITS);
      logic pf;  // a chunk is read this cycle
      logic pf_row;  // the chunk read is the first of its row
      logic [31:0] xa, wbit;  // its activation word, and its first bit in each weight row
      logic [31:0] nx_xa_q, nx_wbit_q;  // the next chunk of the row
      logic [31:0] row_xa_q;  // the first chunk of the next row
      logic [31:0] kc_end_q, m_end_q;  // gkc of a row's last chunk, gm of the tile's last row
      wire [31:0] wstep = 32'(V) << (3 - fmt_shift);  // a chunk's bits in a weight row
      logic [31:0] bw_dst;  // where the DMA's word goes, as a buffer address
      logic [SBI-1:0] xb_q;  // the activation bank read, and the slot of the weight banks
      logic cs_q;
      logic [WSI-1:0] wsel_q;  // the weight chunk's first bit in the bank word

      always_comb begin
        pf = 1'b0;
        pf_row = 1'b0;
        xa = nx_xa_q;
        wbit = nx_wbit_q;
        if (core_start) begin
          pf = 1'b1;
          pf_row = 1'b1;
          xa = a_base + (a_koff >> 2);
          wbit = '0;
        end else if (issue) begin
          // The chunk after this one, as the issue below steps gm and gkc; none after the
          // tile's last.
          if (gkc != kc_end_q) begin
            pf = 1'b1;
          end else if (gm != m_end_q) begin
            pf = 1'b1;
            pf_row = 1'b1;
            xa = row_xa_q;
            wbit = '0;
          end
        end
      end

      always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
          nx_xa_q <= '0;
          nx_wbit_q <= '0;
          row_xa_q <= '0;
          kc_end_q <= '0;
          m_end_q <= '0;
        end else begin
          if (core_start) begin
            kc_end_q <= cd.tk / V - 1;
            m_end_q <= cd.tm - 1;
          end
          // Each next address is summed for every way the read can go and chosen
          // after the adders, so the row-end compares that choose it are not in series
          // with an adder. A chunk is V bytes at a word boundary: V / 4 words on.
          if (pf) begin
            if (core_start) nx_xa_q <= a_base + (a_koff >> 2) + 32'(XW);
            else if (pf_row) nx_xa_q <= row_xa_q + 32'(XW);
            else nx_xa_q <= nx_xa_q + 32'(XW);
            nx_wbit_q <= pf_row ? wstep : nx_wbit_q + wstep;
          end
          if (core_start) row_xa_q <= a_base + (a_koff >> 2) + a_stride;
          else if (pf_row) row_xa_q <= row_xa_q + a_stride;
        end
      end

      always_comb begin
        bw_dst = 32'(fifo[int'(fifo_rp[0])]);
        abank_re_o = '0;
        abank_we_o = '0;
        abank_addr_o = '0;
        wbank_re_o = '0;
        wbank_we_o = '0;
        wbank_addr_o = '0;
        bank_wdata_o = {WPM{rd_rdata_i[0]}};
        bank_wmask_o = BANK_BITS'({32{1'b1}}) << (32 * int'(bw_dst % WPM));
        if (pf) begin
          abank_re_o[int'(xa / A_SLOT)] = 1'b1;
          abank_addr_o[int'(xa / A_SLOT)] = ABA'((xa % A_SLOT) / WPM);
        end
        // The loop outside the condition: as a block of its own, sv2v gives its index a
        // variable that only the read cycle would set, which synthesis calls a latch.
        for (int l = 0; l < int'(L); l++) begin
          if (pf) begin
            wbank_re_o[int'(c_slot) * L + l] = 1'b1;
            wbank_addr_o[int'(c_slot) * L + l] = WBA'((wbit >> 5) / WPM);
          end
        end
        if (rd_rvalid_i[0]) begin
          if (dma_tow_q) begin
            wbank_we_o[int'(bw_dst / W_SLOT) * L + int'(bw_dst % W_SLOT / W_ROW)] = 1'b1;
            wbank_addr_o[int'(bw_dst / W_SLOT) * L + int'(bw_dst % W_SLOT / W_ROW)] =
                WBA'(bw_dst % W_ROW / WPM);
          end else begin
            abank_we_o[int'(bw_dst / A_SLOT)] = 1'b1;
            abank_addr_o[int'(bw_dst / A_SLOT)] = ABA'(bw_dst % A_SLOT / WPM);
          end
        end
      end

      always_ff @(posedge clk_i or negedge rst_ni) begin
        if (!rst_ni) begin
          xb_q <= '0;
          cs_q <= 1'b0;
          wsel_q <= '0;
        end else if (pf) begin
          xb_q <= SBI'(xa / A_SLOT);
          cs_q <= c_slot;
          wsel_q <= WSI'(wbit % BANK_BITS);
        end
      end

      always_comb begin
        logic [BANK_BITS-1:0] xw;
        xw = abank_rdata_i[int'(xb_q)];
        for (int v = 0; v < int'(V); v++) xv[v] = xw[8*v+:8];
        for (int l = 0; l < int'(L); l++) begin
          logic [BANK_BITS-1:0] chunk;
          chunk = wbank_rdata_i[int'(cs_q) * L + l] >> wsel_q;
          for (int v = 0; v < int'(V); v++) begin
            unique case (fmt_shift)
              2'd1: wv[l][v] = 8'(signed'(chunk[4*v+:4]));
              2'd2: wv[l][v] = 8'(signed'(chunk[2*v+:2]));
              default: wv[l][v] = chunk[8*v+:8];
            endcase
          end
        end
      end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) begin
        for (int v = 0; v < int'(V); v++) xq[v] <= '0;
        for (int l = 0; l < int'(L); l++) for (int v = 0; v < int'(V); v++) wq[l][v] <= '0;
      end else if (issue) begin
        xq <= xv;
        wq <= wv;
      end
    end

    // Each lane sums its V products as a balanced tree, log2(V) adders deep rather than a
    // chain of V, and only as wide as the sum can get: V signed 16-bit products need
    // 16 + log2(V) bits. The result is sign-extended once, so no adder carries through
    // 32 bits of copies of the sign.
    localparam int unsigned PW = 16 + (V > 1 ? $clog2(V) : 0);
    always_comb begin
      for (int l = 0; l < int'(L); l++) begin
        logic signed [PW-1:0] t[V];
        for (int v = 0; v < int'(V); v++) t[v] = PW'(mul8(xq[v], wq[l][v]));
        for (int w = int'(V); w > 1; w = (w + 1) / 2) begin
          for (int i = 0; i < w / 2; i++) t[i] = t[2*i] + t[2*i+1];
          if (w % 2 == 1) t[w/2] = t[w-1];
        end
        dot[l] = 32'(t[0]);
      end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) begin
        running <= 1'b0;
        issuing <= 1'b0;
        gm <= '0;
        gkc <= '0;
        g_rowbase <= '0;
        nchunks <= '0;
        ov_q <= 1'b0;
        ofirst_q <= 1'b0;
        olast_q <= 1'b0;
        om_q <= '0;
        pv_q <= 1'b0;
        pfirst_q <= 1'b0;
        plast_q <= 1'b0;
        pm_q <= '0;
        for (int l = 0; l < int'(L); l++) psum_q[l] <= '0;
      end else begin
        ov_q <= 1'b0;
        pv_q <= ov_q;
        if (ov_q) begin
          pfirst_q <= ofirst_q;
          plast_q <= olast_q;
          pm_q <= om_q;
          for (int l = 0; l < int'(L); l++) psum_q[l] <= dot[l];
        end
        if (core_start) begin
          running <= 1'b1;
          issuing <= 1'b1;
          gm <= '0;
          gkc <= '0;
          g_rowbase <= a_base;
          nchunks <= cd.tk / V;
        end else if (running) begin
          if (issuing) begin
            ov_q <= 1'b1;
            ofirst_q <= (gkc == 0);
            olast_q <= (gkc + 1 == nchunks);
            om_q <= gm[$clog2(ACC_ROWS)-1:0];
            if (gkc + 1 == nchunks) begin
              gkc <= '0;
              if (gm + 1 == cd.tm) issuing <= 1'b0;
              else begin
                gm <= gm + 1;
                g_rowbase <= g_rowbase + a_stride;
              end
            end else begin
              gkc <= gkc + 1;
            end
          end else if (!ov_q && !pv_q) begin
            running <= 1'b0;
          end
        end
        if (pv_q) begin
          for (int l = 0; l < int'(L); l++) begin
            logic signed [31:0] base, val;
            base = pfirst_q ? (cd.first_k ? 32'sd0 : acc[int'(pm_q)*L+l]) : work[l];
            val = base + psum_q[l];
            work[l] <= val;
            if (plast_q) acc[int'(pm_q)*L+l] <= val;
          end
        end
      end
    end
    assign core_done = running && !issuing && !ov_q && !pv_q;
    always_comb begin
      for (int i = 0; i < int'(TM_MAX); i++)
        for (int j = 0; j < int'(TN); j++) acc_out[i*TN+j] = acc[i*TN+j];
    end

  end else if (DATAFLOW == 1) begin : g_ws
    // Weight-stationary ROWS x COLS array. Rows run along K, columns along N. Each block
    // of ROWS along K: load ROWS weight rows (one per cycle), then stream the TM
    // activation rows through the skewed array.
    localparam int unsigned R = ROWS;
    localparam int unsigned C = COLS;
    localparam int unsigned RI = R > 1 ? $clog2(R) : 1;
    logic running, loading;
    // The time step only counts through one block: R loads, then at most ACC_ROWS rows plus
    // the array's skew, so it is narrow and its compares are short.
    localparam int unsigned TW = $clog2(ACC_ROWS + R + C + 2);
    logic [31:0] blk, nblk;
    logic [TW-1:0] t, tstream;
    logic [31:0] tbase;  // word address of activation row t while streaming
    // PE registers, row-major (r * C + c)
    logic signed [7:0] wq[R*C];
    logic signed [7:0] aq[R*C];
    logic signed [31:0] pq[R*C];
    logic signed [31:0] acc[ACC_ROWS*C];
    // The activation feed is registered before it enters the array, so the buffer read
    // and its address arithmetic are not in series with the first PE's multiply-add.
    logic signed [7:0] feed[R];
    logic signed [7:0] feed_q[R];
    // Write-back of the bottom row, one cycle behind: which accumulator row each column's
    // result belongs to is worked out and registered first, then added in. The last one
    // lands as the core reports done, a cycle before the store engine reads it.
    localparam int unsigned AI = ACC_ROWS > 1 ? $clog2(ACC_ROWS) : 1;
    logic wb_v[C];
    logic wb_first[C];
    logic [AI-1:0] wb_m[C];
    logic signed [31:0] wb_val[C];
    logic wb2_v[C];
    logic [AI-1:0] wb2_m[C];
    logic signed [31:0] wb2_sum[C];

    always_comb begin
      for (int r = 0; r < int'(R); r++) begin
        logic [31:0] m;
        m = 32'(t) - 32'(r);
        feed[r] = '0;
        if (running && !loading && 32'(t) >= 32'(r) && m < cd.tm)
          feed[r] = x_at(tbase - 32'(r) * a_stride, blk * R + 32'(r));
      end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) for (int r = 0; r < int'(R); r++) feed_q[r] <= '0;
      else for (int r = 0; r < int'(R); r++) feed_q[r] <= feed[r];
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) begin
        running <= 1'b0;
        loading <= 1'b0;
        blk <= '0;
        nblk <= '0;
        t <= '0;
        tstream <= '0;
        tbase <= '0;
        for (int r = 0; r < int'(R); r++)
          for (int c = 0; c < int'(C); c++) begin
            aq[r*C+c] <= '0;
            pq[r*C+c] <= '0;
            wq[r*C+c] <= '0;
          end
        for (int c = 0; c < int'(C); c++) begin
          wb_v[c] <= 1'b0;
          wb_first[c] <= 1'b0;
          wb_m[c] <= '0;
          wb_val[c] <= '0;
          wb2_v[c] <= 1'b0;
          wb2_m[c] <= '0;
          wb2_sum[c] <= '0;
        end
      end else begin
        for (int c = 0; c < int'(C); c++) begin
          wb_v[c] <= 1'b0;
          // Read and add: the accumulator row this result belongs to, or nothing on the first
          // block of the first K tile.
          wb2_v[c] <= wb_v[c];
          wb2_m[c] <= wb_m[c];
          wb2_sum[c] <= wb_first[c] ? wb_val[c] : acc[int'(wb_m[c])*C+c] + wb_val[c];
          // Write.
          if (wb2_v[c]) acc[int'(wb2_m[c])*C+c] <= wb2_sum[c];
        end
        if (core_start) begin
          running <= 1'b1;
          loading <= 1'b1;
          blk <= '0;
          nblk <= cd.tk / R;
          t <= '0;
          tstream <= TW'(cd.tm + 32'(R + C));
        end else if (running) begin
          if (loading) begin
            for (int c = 0; c < int'(C); c++)
              wq[int'(t[RI-1:0])*C+c] <= (32'(c) < cd.tn) ?
                  w_at(32'(c), blk * R + 32'(t)) : 8'sd0;
            if (32'(t) + 1 == 32'(R)) begin
              loading <= 1'b0;
              t <= '0;
              tbase <= a_base;
            end else t <= t + TW'(1);
          end else begin
            for (int r = 0; r < int'(R); r++)
              for (int c = 0; c < int'(C); c++) begin
                logic signed [7:0] ain;
                ain = (c == 0) ? feed_q[r] : aq[r*C+c-1];
                aq[r*C+c] <= ain;
                pq[r*C+c] <= ((r == 0) ? 32'sd0 : pq[(r-1)*C+c]) + 32'(mul8(ain, wq[r*C+c]));
              end
            for (int c = 0; c < int'(C); c++) begin
              logic [31:0] m;
              m = 32'(t) - 32'(R) - 32'(c) - 32'd1;
              wb_v[c] <= 32'(t) >= 32'(R + c + 1) && m < cd.tm;
              wb_first[c] <= cd.first_k && blk == 0;
              wb_m[c] <= m[AI-1:0];
              wb_val[c] <= pq[(R-1)*C+c];
            end
            tbase <= tbase + a_stride;
            if (t + TW'(1) == tstream) begin
              t <= '0;
              if (blk + 1 == nblk) running <= 1'b0;
              else begin
                blk <= blk + 1;
                loading <= 1'b1;
              end
            end else t <= t + TW'(1);
          end
        end
      end
    end
    logic was_running;
    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) was_running <= 1'b0;
      else was_running <= running;
    end
    assign core_done = was_running && !running && cs == C_RUN;
    always_comb begin
      for (int i = 0; i < int'(TM_MAX); i++)
        for (int j = 0; j < int'(TN); j++) acc_out[i*TN+j] = acc[i*TN+j];
    end

  end else begin : g_os
    // Output-stationary ROWS x COLS array: PE (r, c) owns Y[m0 + r][n0 + c]. Activations
    // enter from the left and weights from the top, both skewed by one cycle per hop.
    localparam int unsigned R = ROWS;
    localparam int unsigned C = COLS;
    logic running;
    logic [31:0] t, tend;
    // PE registers, row-major (r * C + c)
    logic signed [7:0] aq[R*C];
    logic signed [7:0] wq[R*C];
    logic signed [31:0] pacc[R*C];
    // Both edge feeds are registered before they enter the array, so the buffer reads, the
    // weight unpacking and their address arithmetic are not in series with a multiply-add.
    logic signed [7:0] afeed[R];
    logic signed [7:0] wfeed[C];
    logic signed [7:0] afeed_q[R];
    logic signed [7:0] wfeed_q[C];

    always_comb begin
      for (int r = 0; r < int'(R); r++) begin
        logic [31:0] k;
        k = t - 32'(r);
        afeed[r] = '0;
        if (running && t >= 32'(r) && k < cd.tk && 32'(r) < cd.tm) afeed[r] = x_at(a_base + 32'(r) * a_stride, k);
      end
      for (int c = 0; c < int'(C); c++) begin
        logic [31:0] k;
        k = t - 32'(c);
        wfeed[c] = '0;
        if (running && t >= 32'(c) && k < cd.tk && 32'(c) < cd.tn) wfeed[c] = w_at(32'(c), k);
      end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) begin
        for (int r = 0; r < int'(R); r++) afeed_q[r] <= '0;
        for (int c = 0; c < int'(C); c++) wfeed_q[c] <= '0;
      end else begin
        for (int r = 0; r < int'(R); r++) afeed_q[r] <= afeed[r];
        for (int c = 0; c < int'(C); c++) wfeed_q[c] <= wfeed[c];
      end
    end

    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) begin
        running <= 1'b0;
        t <= '0;
        tend <= '0;
        for (int r = 0; r < int'(R); r++)
          for (int c = 0; c < int'(C); c++) begin
            aq[r*C+c] <= '0;
            wq[r*C+c] <= '0;
            pacc[r*C+c] <= '0;
          end
      end else begin
        if (core_start) begin
          running <= 1'b1;
          t <= '0;
          tend <= cd.tk + 32'(R + C) - 32'd1;
        end else if (running) begin
          for (int r = 0; r < int'(R); r++)
            for (int c = 0; c < int'(C); c++) begin
              logic signed [7:0] ain, win;
              ain = (c == 0) ? afeed_q[r] : aq[r*C+c-1];
              win = (r == 0) ? wfeed_q[c] : wq[(r-1)*C+c];
              aq[r*C+c] <= ain;
              wq[r*C+c] <= win;
              pacc[r*C+c] <= pacc[r*C+c] + 32'(mul8(ain, win));
            end
          if (t + 1 == tend) running <= 1'b0;
          t <= t + 1;
        end
        if (st_done) begin  // the output tile has been stored
          for (int r = 0; r < int'(R); r++)
            for (int c = 0; c < int'(C); c++) pacc[r*C+c] <= '0;
        end
      end
    end
    logic was_running;
    always_ff @(posedge clk_i or negedge rst_ni) begin
      if (!rst_ni) was_running <= 1'b0;
      else was_running <= running;
    end
    assign core_done = was_running && !running && cs == C_RUN;
    always_comb begin
      for (int i = 0; i < int'(TM_MAX); i++)
        for (int j = 0; j < int'(TN); j++) acc_out[i*TN+j] = pacc[i*TN+j];
    end
  end

  // ---------------------------------------------------------------------------
  // Store engine: write the output tile through the write port
  // ---------------------------------------------------------------------------
  logic st_active, st_issued;
  // Row and column within the output tile: at most TM_MAX x TN, so the counters and their
  // compares are as narrow as that.
  localparam int unsigned STI = $clog2(TM_MAX + 1);
  localparam int unsigned STJ = $clog2(TN + 1);
  logic [STI-1:0] st_i;
  logic [STJ-1:0] st_j;
  logic [31:0] st_rowaddr;
  logic [OSTW+3:0] st_out;
  logic st_pend;
  logic [31:0] st_paddr, st_pdata;

  assign wr_req_o = st_pend;
  assign wr_addr_o = st_paddr;
  assign wr_wdata_o = st_pdata;
  assign wr_be_o = 4'hF;
  assign st_done = st_active && st_issued && !st_pend && st_out == '0;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      st_active <= 1'b0;
      st_issued <= 1'b0;
      st_i <= '0;
      st_j <= '0;
      st_rowaddr <= '0;
      st_out <= '0;
      st_pend <= 1'b0;
      st_paddr <= '0;
      st_pdata <= '0;
    end else begin
      logic g;
      g = st_pend && wr_gnt_i;
      st_out <= st_out + (OSTW + 4)'(g) - (OSTW + 4)'(wr_rvalid_i);
      if (st_start) begin
        st_active <= 1'b1;
        st_issued <= 1'b0;
        st_i <= '0;
        st_j <= '0;
        st_rowaddr <= cd.ybase;
      end else if (st_active) begin
        if (st_done) begin
          st_active <= 1'b0;
        end else if (!st_issued && (!st_pend || g)) begin
          st_pend <= 1'b1;
          st_paddr <= st_rowaddr + (32'(st_j) << 2);
          st_pdata <= acc_out[int'(st_i[TMI-1:0])*TN+int'(st_j[TNI-1:0])];
          if (st_j + STJ'(1) == STJ'(cd.tn)) begin
            st_j <= '0;
            st_rowaddr <= st_rowaddr + (cfg_n << 2);
            if (st_i + STI'(1) == STI'(cd.tm)) st_issued <= 1'b1;
            else st_i <= st_i + STI'(1);
          end else begin
            st_j <= st_j + STJ'(1);
          end
        end else if (g) begin
          st_pend <= 1'b0;
        end
      end
    end
  end

  // Unused register-interface inputs
  logic unused;
  assign unused = ^reg_wstrb_i ^ ^reg_addr_i[31:8] ^ ^reg_addr_i[1:0];

endmodule

// The accelerator with its buffers inside, as X-HEEP, the AXI wrapper and every test bench
// use it.
module chipleap_accel #(
    parameter int unsigned DATAFLOW = 0,
    parameter int unsigned ROWS = 8,
    parameter int unsigned COLS = 8,
    parameter int unsigned WBITS = 8,
    parameter int unsigned PORTS = 2,
    parameter int unsigned ABUF_WORDS = 2048,
    parameter int unsigned WBUF_WORDS = 1024,
    parameter int unsigned DOUBLE_BUF = 1,
    parameter int unsigned ACC_ROWS = 8,
    parameter int unsigned MAX_OUTSTANDING = 4
) (
    input logic clk_i,
    input logic rst_ni,
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
    output logic irq_o
);
  /* verilator lint_off PINCONNECTEMPTY */  // the banks, unused with the buffers inside
  chipleap_accel_core #(
      .DATAFLOW(DATAFLOW),
      .ROWS(ROWS),
      .COLS(COLS),
      .WBITS(WBITS),
      .PORTS(PORTS),
      .ABUF_WORDS(ABUF_WORDS),
      .WBUF_WORDS(WBUF_WORDS),
      .DOUBLE_BUF(DOUBLE_BUF),
      .ACC_ROWS(ACC_ROWS),
      .MAX_OUTSTANDING(MAX_OUTSTANDING),
      .SRAM_BUF(0)
  ) u_core (
      .clk_i,
      .rst_ni,
      .reg_valid_i,
      .reg_write_i,
      .reg_addr_i,
      .reg_wdata_i,
      .reg_wstrb_i,
      .reg_rdata_o,
      .reg_error_o,
      .reg_ready_o,
      .rd_req_o,
      .rd_gnt_i,
      .rd_addr_o,
      .rd_rvalid_i,
      .rd_rdata_i,
      .wr_req_o,
      .wr_gnt_i,
      .wr_addr_o,
      .wr_wdata_o,
      .wr_be_o,
      .wr_rvalid_i,
      .abank_re_o(),
      .abank_we_o(),
      .abank_addr_o(),
      .wbank_re_o(),
      .wbank_we_o(),
      .wbank_addr_o(),
      .bank_wdata_o(),
      .bank_wmask_o(),
      .abank_rdata_i('0),
      .wbank_rdata_i('0),
      .irq_o
  );
  /* verilator lint_on PINCONNECTEMPTY */
endmodule
