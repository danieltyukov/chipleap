# UART: an RTL project

A UART transmitter and receiver (8N1: a start bit, eight data bits, a stop bit) written by hand, with cocotb tests and a Python reference model. It shows ChipLeap on a design it did not generate.

| Path | Contents |
|---|---|
| `project.yaml` | Top, sources, parameter, clock, reset, technology, tests, golden model |
| `rtl/` | `uart_tx.sv`, `uart_rx.sv` and the top, `uart.sv` |
| `test/test_uart.py` | cocotb tests: frames out, frames in, a framing error, a glitch, back-to-back loopback |
| `model/uart_model.py` | The reference model the tests compare against |

```sh
pip install "chipleap[rtl]"            # cocotb; Icarus or Verilator for the simulator
chipleap verify examples/uart          # lint, simulation, coverage, synthesis
chipleap verify examples/uart --steps all   # and place and route with LibreLane
```

Results go to `examples/uart/build/verify/` (`report.html`, `VERIFY.md`, `verify.json`); the routed GDS to `examples/uart/build/pnr/runs/chipleap/final/gds/uart.gds`.

`CLKS_PER_BIT` is 16 so the tests run in a few seconds; set 434 in `project.yaml` for 115200 baud from a 50 MHz clock. Coverage reports the two `default:` arms of the state machines as never reached: every state is used, so they cannot be.
