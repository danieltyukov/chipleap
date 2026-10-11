// Logic of clk_a between its registers and the synchronizer: a glitch can be captured.
module comb_sync (
  input  logic clk_a, input logic clk_b, input logic rst_ni,
  input  logic [1:0] d_i,
  output logic q_o
);
  logic [1:0] a_q;
  logic meta_q;
  always_ff @(posedge clk_a or negedge rst_ni) if (!rst_ni) a_q <= '0; else a_q <= d_i;
  always_ff @(posedge clk_b or negedge rst_ni) begin
    if (!rst_ni) begin
      meta_q <= 1'b0;
      q_o <= 1'b0;
    end else begin
      meta_q <= a_q[0] & a_q[1];
      q_o <= meta_q;
    end
  end
endmodule
