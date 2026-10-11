// A registered multiplier: one long path from the input registers through the multiplier
// to the output register, for the ECO's timing repair and the UPF step's level shifters.
module mac (
  input  logic        clk_i,
  input  logic        rst_ni,
  input  logic [11:0] a_i,
  input  logic [11:0] b_i,
  output logic [23:0] p_o
);
  logic [11:0] a_q, b_q;
  logic [23:0] p;

  mul u_mul (.a_i(a_q), .b_i(b_q), .p_o(p));

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      a_q <= '0;
      b_q <= '0;
      p_o <= '0;
    end else begin
      a_q <= a_i;
      b_q <= b_i;
      p_o <= p;
    end
  end
endmodule
