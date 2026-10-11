// A synchronous FIFO with its properties under `ifdef FORMAL (read -formal defines it).
module fifo #(
  parameter int unsigned W = 8,
  parameter int unsigned AW = 2
) (
  input  logic         clk_i,
  input  logic         rst_ni,
  input  logic         push_i,
  input  logic         pop_i,
  input  logic [W-1:0] data_i,
  output logic [W-1:0] data_o,
  output logic         full_o,
  output logic         empty_o
);
  localparam int unsigned DEPTH = 1 << AW;
  logic [W-1:0] mem [DEPTH];
  logic [AW-1:0] wptr, rptr;
  logic [AW:0] count;
  wire do_push = push_i && !full_o;
  wire do_pop = pop_i && !empty_o;
  assign full_o = count == DEPTH;
  assign empty_o = count == 0;
  assign data_o = mem[rptr];
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      wptr <= '0;
      rptr <= '0;
      count <= '0;
    end else begin
      if (do_push) wptr <= wptr + 1'b1;
      if (do_pop) rptr <= rptr + 1'b1;
      count <= count + (do_push ? 1 : 0) - (do_pop ? 1 : 0);
    end
  end
  always_ff @(posedge clk_i) if (do_push) mem[wptr] <= data_i;
`ifdef FORMAL
  logic past_valid = 1'b0;
  always_ff @(posedge clk_i) past_valid <= 1'b1;
  always_comb if (!past_valid) assume (!rst_ni);
  always_comb begin
    never_over: assert (count <= DEPTH);
    pointers_agree: assert (wptr == AW'(rptr + count[AW-1:0]));
  end
  always_ff @(posedge clk_i) begin
    if (past_valid && $past(rst_ni) && rst_ni && $past(do_push) && !$past(do_pop)) begin
      grows_on_push: assert (count == $past(count) + 1);
    end
  end
  always_comb fills_up: cover (full_o);
`endif
endmodule
