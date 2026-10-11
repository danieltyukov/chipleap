// An asynchronous FIFO: pointers cross the clocks gray coded through two-flop synchronizers,
// the memory is written with wclk and read into rclk with the read pointer.
module fifo_gray #(
  parameter int unsigned AW = 2
) (
  input  logic       wclk,
  input  logic       rclk,
  input  logic       rst_ni,
  input  logic       push_i,
  input  logic [7:0] wdata_i,
  output logic       full_o,
  input  logic       pop_i,
  output logic [7:0] rdata_o,
  output logic       empty_o
);
  logic [7:0] mem [1 << AW];
  logic [AW:0] wbin, wptr, rbin, rptr;
  logic [AW:0] rptr_w1, rptr_w2;  // the read pointer in wclk
  logic [AW:0] wptr_r1, wptr_r2;  // the write pointer in rclk
  wire do_push = push_i && !full_o;
  wire do_pop = pop_i && !empty_o;
  wire [AW:0] wbin_next = wbin + {{AW{1'b0}}, do_push};
  wire [AW:0] rbin_next = rbin + {{AW{1'b0}}, do_pop};
  wire [AW:0] wptr_next = (wbin_next >> 1) ^ wbin_next;
  wire [AW:0] rptr_next = (rbin_next >> 1) ^ rbin_next;
  always_ff @(posedge wclk or negedge rst_ni) begin
    if (!rst_ni) begin
      wbin <= '0;
      wptr <= '0;
    end else begin
      wbin <= wbin_next;
      wptr <= wptr_next;
    end
  end
  always_ff @(posedge wclk) if (do_push) mem[wbin[AW-1:0]] <= wdata_i;
  always_ff @(posedge rclk or negedge rst_ni) begin
    if (!rst_ni) begin
      rbin <= '0;
      rptr <= '0;
    end else begin
      rbin <= rbin_next;
      rptr <= rptr_next;
    end
  end
  always_ff @(posedge rclk) if (do_pop) rdata_o <= mem[rbin[AW-1:0]];
  always_ff @(posedge wclk or negedge rst_ni) begin
    if (!rst_ni) begin
      rptr_w1 <= '0;
      rptr_w2 <= '0;
    end else begin
      rptr_w1 <= rptr;
      rptr_w2 <= rptr_w1;
    end
  end
  always_ff @(posedge rclk or negedge rst_ni) begin
    if (!rst_ni) begin
      wptr_r1 <= '0;
      wptr_r2 <= '0;
    end else begin
      wptr_r1 <= wptr;
      wptr_r2 <= wptr_r1;
    end
  end
  assign empty_o = rptr == wptr_r2;
  assign full_o = wptr == {~rptr_w2[AW:AW-1], rptr_w2[AW-2:0]};
endmodule
