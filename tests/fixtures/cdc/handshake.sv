// Data handed over with a request: the request toggles in clk_a and is synchronized to
// clk_b, and the data, held still meanwhile, is captured when the synchronized request
// changes.
module handshake (
  input  logic       clk_a, input logic clk_b, input logic rst_ni,
  input  logic       send_i,
  input  logic [7:0] data_i,
  output logic [7:0] data_o
);
  logic       req_q;
  logic [7:0] data_q;
  logic       req_meta_q, req_sync_q, req_seen_q;
  always_ff @(posedge clk_a or negedge rst_ni) begin
    if (!rst_ni) begin
      req_q <= 1'b0;
      data_q <= '0;
    end else if (send_i) begin
      req_q <= !req_q;
      data_q <= data_i;
    end
  end
  always_ff @(posedge clk_b or negedge rst_ni) begin
    if (!rst_ni) begin
      req_meta_q <= 1'b0;
      req_sync_q <= 1'b0;
      req_seen_q <= 1'b0;
      data_o <= '0;
    end else begin
      req_meta_q <= req_q;
      req_sync_q <= req_meta_q;
      req_seen_q <= req_sync_q;
      if (req_sync_q != req_seen_q) data_o <= data_q;
    end
  end
endmodule
