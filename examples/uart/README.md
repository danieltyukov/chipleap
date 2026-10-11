# UART: an RTL project

A UART transmitter and receiver (8N1: a start bit, eight data bits, a stop bit) written by hand, with cocotb tests and a Python reference model. It shows ChipLeap on a design it did not generate.

| Path | Contents |
|---|---|
| `project.yaml` | Top, sources, parameter, clock, reset, technology, tests, golden model; scan chains, power corners and a power domain for the receiver |
| `rtl/` | `uart_tx.sv`, `uart_rx.sv` and the top, `uart.sv` |
| `test/test_uart.py` | cocotb tests: frames out, frames in, a framing error, a glitch, back-to-back loopback |
| `model/uart_model.py` | The reference model the tests compare against |

```sh
pip install "chipleap[rtl]"            # cocotb; Icarus or Verilator for the simulator
chipleap verify examples/uart          # lint, simulation, coverage, synthesis
chipleap verify examples/uart --steps all   # and place and route, DFT, power, IR drop, UPF
chipleap eco examples/uart --paths           # the worst paths of the routed design
```

Results go to `examples/uart/build/verify/` (`report.html`, `VERIFY.md`, `verify.json`); the routed GDS to `examples/uart/build/pnr/runs/chipleap/final/gds/uart.gds`, the scan patterns to `examples/uart/build/dft/patterns.json` and `patterns.stil`.

With `--steps all` the UART gets three scan chains of 17 flip-flops, which LibreLane routes with the rest; Fault reaches 99.2 % stuck-at coverage with 39 patterns, which pass through the chains of the scan netlist and of the routed one; the tests on the routed netlist draw 0.34 mW at the typical corner (0.92 mW vectorless); the supply drops by at most 0.5 mV; and OpenROAD places the receiver in a power domain of its own. The steps need OpenROAD, Fault with Quaigh and Icarus 13 ([docs/projects.md](../../docs/projects.md#after-place-and-route)). The tests read `CLKS_PER_BIT` from `CHIPLEAP_PARAM_CLKS_PER_BIT` when they run on a netlist, which has no parameters.

`CLKS_PER_BIT` is 16 so the tests run in a few seconds; set 434 in `project.yaml` for 115200 baud from a 50 MHz clock. Coverage reports the two `default:` arms of the state machines as never reached: every state is used, so they cannot be.
