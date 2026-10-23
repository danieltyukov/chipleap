// An asynchronous FIFO whose oldest word is read with the read pointer straight into a
// memory of the reading clock, as an accelerator stores a crossing's response in its
// buffer: no flip-flop between the two memories.
module fifo_into_memory #(
  parameter int unsigned AW = 2
) (
  input  logic       wclk,
  input  logic       rclk,
  input  logic       rst_ni,
  input  logic       push_i,
  input  logic [7:0] wdata_i,
  output logic       full_o,
  input  logic [3:0] baddr_i,
  output logic [7:0] bdata_o
);
  logic [7:0] mem [1 << AW];
  logic [7:0] buffer [16];
  logic [AW:0] wbin, wptr, rbin, rptr;
  logic [AW:0] rptr_w1, rptr_w2;
  logic [AW:0] wptr_r1, wptr_r2;
  logic [3:0] bptr;
  wire empty = rptr == wptr_r2;
  wire do_push = push_i && !full_o;
  wire do_pop = !empty;
  wire [AW:0] wbin_next = wbin + {{AW{1'b0}}, do_push};
  wire [AW:0] rbin_next = rbin + {{AW{1'b0}}, do_pop};
  wire [AW:0] wptr_next = (wbin_next >> 1) ^ wbin_next;
  wire [AW:0] rptr_next = (rbin_next >> 1) ^ rbin_next;
  always_ff @(posedge wclk or negedge rst_ni) begin
    if (!rst_ni) begin
      wbin <= '0;
      wptr <= '0;
      rptr_w1 <= '0;
      rptr_w2 <= '0;
    end else begin
      wbin <= wbin_next;
      wptr <= wptr_next;
      rptr_w1 <= rptr;
      rptr_w2 <= rptr_w1;
    end
  end
  always_ff @(posedge wclk) if (do_push) mem[wbin[AW-1:0]] <= wdata_i;
  always_ff @(posedge rclk or negedge rst_ni) begin
    if (!rst_ni) begin
      rbin <= '0;
      rptr <= '0;
      wptr_r1 <= '0;
      wptr_r2 <= '0;
      bptr <= '0;
    end else begin
      rbin <= rbin_next;
      rptr <= rptr_next;
      wptr_r1 <= wptr;
      wptr_r2 <= wptr_r1;
      bptr <= bptr + {3'b0, do_pop};
    end
  end
  always_ff @(posedge rclk) if (do_pop) buffer[bptr] <= mem[rbin[AW-1:0]];
  assign bdata_o = buffer[baddr_i];
  assign full_o = wptr == {~rptr_w2[AW:AW-1], rptr_w2[AW-2:0]};
endmodule
