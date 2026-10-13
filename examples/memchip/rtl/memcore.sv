// SPDX-License-Identifier: MIT
// A 256-byte memory behind a request port, in one of IHP's SRAM macros.
module memcore (
    input  logic       clk_i,
    input  logic       rst_ni,
    input  logic       req_i,
    input  logic       we_i,
    input  logic [7:0] addr_i,
    input  logic [7:0] wdata_i,
    output logic [7:0] rdata_o,
    output logic       rvalid_o
);
  RM_IHPSG13_1P_256x8_c3_bm_bist u_mem (
      .A_CLK(clk_i),
      .A_MEN(req_i),
      .A_WEN(req_i & we_i),
      .A_REN(req_i & ~we_i),
      .A_ADDR(addr_i),
      .A_DIN(wdata_i),
      .A_DLY(1'b1),
      .A_DOUT(rdata_o),
      .A_BM(8'hff),
      .A_BIST_CLK(1'b0),
      .A_BIST_EN(1'b0),
      .A_BIST_MEN(1'b0),
      .A_BIST_WEN(1'b0),
      .A_BIST_REN(1'b0),
      .A_BIST_ADDR(8'd0),
      .A_BIST_DIN(8'd0),
      .A_BIST_BM(8'd0)
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) rvalid_o <= 1'b0;
    else rvalid_o <= req_i & ~we_i;
  end
endmodule
