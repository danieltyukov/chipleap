// The same input used straight away.
module async_in_bad (
  input  logic clk_i, input logic rst_ni,
  input  logic rx_i,
  output logic q_o
);
  always_ff @(posedge clk_i or negedge rst_ni) if (!rst_ni) q_o <= 1'b0; else q_o <= rx_i & !q_o;
endmodule
