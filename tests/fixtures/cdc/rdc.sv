// Two asynchronous resets: a_q, reset by rst_a_ni, feeds q_o, reset by rst_b_ni. Asserting
// rst_a_ni alone changes a_q while q_o keeps sampling it.
module rdc (
  input  logic clk_i, input logic rst_a_ni, input logic rst_b_ni,
  input  logic d_i,
  output logic q_o
);
  logic a_q;
  always_ff @(posedge clk_i or negedge rst_a_ni) if (!rst_a_ni) a_q <= 1'b0; else a_q <= d_i;
  always_ff @(posedge clk_i or negedge rst_b_ni) if (!rst_b_ni) q_o <= 1'b0; else q_o <= a_q;
endmodule
