// A UART: the transmitter and the receiver, sharing a clock and a bit period.
module uart #(
  parameter int unsigned CLKS_PER_BIT = 16
) (
  input  logic       clk_i,
  input  logic       rst_ni,
  // transmit
  input  logic [7:0] tx_data_i,
  input  logic       tx_valid_i,
  output logic       tx_ready_o,
  output logic       tx_o,
  // receive
  input  logic       rx_i,
  output logic [7:0] rx_data_o,
  output logic       rx_valid_o,
  output logic       rx_error_o
);
  uart_tx #(
    .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_tx (
    .clk_i,
    .rst_ni,
    .data_i (tx_data_i),
    .valid_i(tx_valid_i),
    .ready_o(tx_ready_o),
    .tx_o
  );

  uart_rx #(
    .CLKS_PER_BIT(CLKS_PER_BIT)
  ) u_rx (
    .clk_i,
    .rst_ni,
    .rx_i,
    .data_o (rx_data_o),
    .valid_o(rx_valid_o),
    .error_o(rx_error_o)
  );
endmodule
