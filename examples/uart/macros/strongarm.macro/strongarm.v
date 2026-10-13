// strongarm: Verilog model written by aispice 0.2.0. The block is analog, so
// a digital simulator sees only its ports.
(* blackbox *)
module strongarm (
`ifdef USE_POWER_PINS
    inout vdd,
    inout vss,
`endif
    input clk,
    input inm,
    input inp,
    output outn,
    output outp
);
endmodule
