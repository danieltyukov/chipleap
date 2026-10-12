// A register of clk_a sampled straight into clk_b: an unsynchronized crossing.
module unsync (
  input  logic clk_a, input logic clk_b, input logic rst_ni,
  input  logic d_i,
  output logic q_o
);
  logic a_q;
  always_ff @(posedge clk_a or negedge rst_ni) if (!rst_ni) a_q <= 1'b0; else a_q <= d_i;
  always_ff @(posedge clk_b or negedge rst_ni) if (!rst_ni) q_o <= 1'b0; else q_o <= a_q;
endmodule
