// UART transmitter: one start bit, eight data bits (least significant first) and one stop
// bit, no parity. Each bit lasts CLKS_PER_BIT clock cycles (at least 2). A byte is taken
// when valid_i and ready_o are both high; ready_o is low until its stop bit has been sent.
module uart_tx #(
  parameter int unsigned CLKS_PER_BIT = 16
) (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic [7:0] data_i,
  input  logic       valid_i,
  output logic       ready_o,
  output logic       tx_o
);
  localparam int unsigned CW = $clog2(CLKS_PER_BIT);
  localparam logic [CW-1:0] LAST = CW'(CLKS_PER_BIT - 1);

  typedef enum logic [1:0] {
    Idle,
    Start,
    Data,
    Stop
  } state_e;

  state_e        state_q;
  logic [CW-1:0] tick_q;  // clock cycles into the current bit
  logic [2:0]    bit_q;  // the data bit on the line
  logic [7:0]    shift_q;
  logic          bit_done;

  assign bit_done = tick_q == LAST;
  assign ready_o  = state_q == Idle;

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      state_q <= Idle;
      tick_q  <= '0;
      bit_q   <= '0;
      shift_q <= '0;
      tx_o    <= 1'b1;
    end else begin
      tick_q <= (bit_done || state_q == Idle) ? '0 : tick_q + CW'(1);
      case (state_q)
        Idle: begin
          if (valid_i) begin
            shift_q <= data_i;
            state_q <= Start;
            tx_o    <= 1'b0;
          end
        end
        Start: begin
          if (bit_done) begin
            state_q <= Data;
            bit_q   <= '0;
            tx_o    <= shift_q[0];
          end
        end
        Data: begin
          if (bit_done) begin
            shift_q <= {1'b0, shift_q[7:1]};
            if (bit_q == 3'd7) begin
              state_q <= Stop;
              tx_o    <= 1'b1;
            end else begin
              bit_q <= bit_q + 3'd1;
              tx_o  <= shift_q[1];
            end
          end
        end
        Stop: begin
          if (bit_done) state_q <= Idle;
        end
        default: state_q <= Idle;
      endcase
    end
  end
endmodule
