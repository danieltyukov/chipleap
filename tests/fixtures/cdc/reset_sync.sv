// A reset synchronizer: asserted at once, released two clk_i edges later.
module reset_sync (
  input  logic clk_i, input logic rst_ni,
  input  logic d_i,
  output logic q_o
);
  logic r1_q, r2_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      r1_q <= 1'b0;
      r2_q <= 1'b0;
    end else begin
      r1_q <= 1'b1;
      r2_q <= r1_q;
    end
  end
  always_ff @(posedge clk_i or negedge r2_q) if (!r2_q) q_o <= 1'b0; else q_o <= d_i;
endmodule
