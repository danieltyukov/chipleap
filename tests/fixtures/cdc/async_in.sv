// An input no clock samples safely, through a two-flop synchronizer.
module async_in (
  input  logic clk_i, input logic rst_ni,
  input  logic rx_i,
  output logic q_o
);
  logic meta_q, sync_q;
  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      meta_q <= 1'b0;
      sync_q <= 1'b0;
      q_o <= 1'b0;
    end else begin
      meta_q <= rx_i;
      sync_q <= meta_q;
      q_o <= sync_q & !q_o;
    end
  end
endmodule
