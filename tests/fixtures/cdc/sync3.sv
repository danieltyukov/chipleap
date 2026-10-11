// Three flops in a row also synchronize.
module sync3 (
  input  logic clk_a, input logic clk_b, input logic rst_ni,
  input  logic d_i,
  output logic q_o
);
  logic a_q, s1_q, s2_q;
  always_ff @(posedge clk_a or negedge rst_ni) if (!rst_ni) a_q <= 1'b0; else a_q <= d_i;
  always_ff @(posedge clk_b or negedge rst_ni) begin
    if (!rst_ni) {s1_q, s2_q, q_o} <= '0;
    else {s1_q, s2_q, q_o} <= {a_q, s1_q, s2_q};
  end
endmodule
