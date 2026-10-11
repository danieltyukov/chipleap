// A two-flop synchronizer: the first flop of clk_b samples clk_a's register and feeds
// only the second.
module sync2 (
  input  logic clk_a, input logic clk_b, input logic rst_ni,
  input  logic d_i,
  output logic q_o
);
  logic a_q, meta_q, sync_q;
  always_ff @(posedge clk_a or negedge rst_ni) if (!rst_ni) a_q <= 1'b0; else a_q <= d_i;
  always_ff @(posedge clk_b or negedge rst_ni) begin
    if (!rst_ni) begin
      meta_q <= 1'b0;
      sync_q <= 1'b0;
    end else begin
      meta_q <= a_q;
      sync_q <= meta_q;
    end
  end
  always_ff @(posedge clk_b or negedge rst_ni) if (!rst_ni) q_o <= 1'b0; else q_o <= sync_q ^ q_o;
endmodule
