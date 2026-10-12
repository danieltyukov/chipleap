// The combinational multiplier, a module of its own so a power domain can name it.
module mul (
  input  logic [11:0] a_i,
  input  logic [11:0] b_i,
  output logic [23:0] p_o
);
  assign p_o = a_i * b_i;
endmodule
