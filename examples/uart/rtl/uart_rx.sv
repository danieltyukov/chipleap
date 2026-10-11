// UART receiver for the transmitter's frame. The line comes from another clock domain, so
// it passes through two flip-flops first. A frame starts when the line falls; the start
// bit is checked in its middle (a shorter pulse is a glitch, not a frame) and every later
// bit is sampled in its middle too. A low stop bit is a framing error.
module uart_rx #(
  parameter int unsigned CLKS_PER_BIT = 16
) (
  input  logic       clk_i,
  input  logic       rst_ni,
  input  logic       rx_i,
  output logic [7:0] data_o,
  output logic       valid_o,  // high for one cycle per frame
  output logic       error_o  // with valid_o: the stop bit was low
);
  localparam int unsigned CW = $clog2(CLKS_PER_BIT);
  localparam logic [CW-1:0] LAST = CW'(CLKS_PER_BIT - 1);
  localparam logic [CW-1:0] HALF = CW'(CLKS_PER_BIT / 2 - 1);

  typedef enum logic [1:0] {
    Idle,
    Start,
    Data,
    Stop
  } state_e;

  logic   [1:0] sync_q;
  state_e        state_q;
  logic [CW-1:0] tick_q;
  logic [2:0]    bit_q;
  logic [7:0]    shift_q;
  logic          rx;

  assign rx = sync_q[1];

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      sync_q  <= 2'b11;
      state_q <= Idle;
      tick_q  <= '0;
      bit_q   <= '0;
      shift_q <= '0;
      data_o  <= '0;
      valid_o <= 1'b0;
      error_o <= 1'b0;
    end else begin
      sync_q  <= {sync_q[0], rx_i};
      valid_o <= 1'b0;
      case (state_q)
        Idle: begin
          tick_q <= '0;
          if (!rx) state_q <= Start;
        end
        Start: begin
          if (tick_q == HALF) begin
            tick_q  <= '0;
            bit_q   <= '0;
            // A start bit that is gone at its middle was a glitch.
            if (rx) state_q <= Idle;
            else state_q <= Data;
          end else begin
            tick_q <= tick_q + CW'(1);
          end
        end
        Data: begin
          if (tick_q == LAST) begin
            tick_q  <= '0;
            shift_q <= {rx, shift_q[7:1]};
            bit_q   <= bit_q + 3'd1;
            if (bit_q == 3'd7) state_q <= Stop;
          end else begin
            tick_q <= tick_q + CW'(1);
          end
        end
        Stop: begin
          if (tick_q == LAST) begin
            tick_q  <= '0;
            data_o  <= shift_q;
            valid_o <= 1'b1;
            error_o <= !rx;
            state_q <= Idle;
          end else begin
            tick_q <= tick_q + CW'(1);
          end
        end
        default: state_q <= Idle;
      endcase
    end
  end
endmodule
