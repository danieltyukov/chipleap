// A 1 KiB scratchpad: one of IHP's SRAM macros behind a request port with byte enables.
// A read's word is on rdata_o the cycle after its request, with rvalid_o, and stays there
// until the next read.
// SPDX-License-Identifier: MIT

module scratchpad (
    input  logic        clk_i,
    input  logic        rst_ni,
    input  logic        req_i,
    input  logic        we_i,
    input  logic [ 7:0] addr_i,
    input  logic [31:0] wdata_i,
    input  logic [ 3:0] be_i,
    output logic [31:0] rdata_o,
    output logic        rvalid_o
);
  // The macro writes each bit whose mask bit is 1: a byte enable is eight of them.
  logic [31:0] bm;
  always_comb begin
    for (int b = 0; b < 4; b++) bm[8*b+:8] = {8{be_i[b]}};
  end

  RM_IHPSG13_1P_256x32_c2_bm_bist u_mem (
      .A_CLK(clk_i),
      .A_MEN(req_i),
      .A_WEN(req_i & we_i),
      .A_REN(req_i & ~we_i),
      .A_ADDR(addr_i),
      .A_DIN(wdata_i),
      .A_DLY(1'b1),
      .A_DOUT(rdata_o),
      .A_BM(bm),
      // The built-in self test is not used.
      .A_BIST_CLK(1'b0),
      .A_BIST_EN(1'b0),
      .A_BIST_MEN(1'b0),
      .A_BIST_WEN(1'b0),
      .A_BIST_REN(1'b0),
      .A_BIST_ADDR(8'd0),
      .A_BIST_DIN(32'd0),
      .A_BIST_BM(32'd0)
  );

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) rvalid_o <= 1'b0;
    else rvalid_o <= req_i & ~we_i;
  end
endmodule
