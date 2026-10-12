# UART: an RTL project

A UART transmitter and receiver (8N1: a start bit, eight data bits, a stop bit) written by hand, with cocotb tests and a Python reference model. It shows ChipLeap on a design it did not generate.

| Path | Contents |
|---|---|
| `project.yaml` | Top, sources, parameter, clock, reset, technology, tests, golden model; scan chains, power corners and a power domain for the receiver |
| `rtl/` | `uart_tx.sv`, `uart_rx.sv` and the top, `uart.sv` |
| `test/test_uart.py` | cocotb tests: frames out, frames in, a framing error, a glitch, back-to-back loopback |
| `model/uart_model.py` | The reference model the tests compare against |
| `macros/strongarm.macro` | A comparator from aispice, for the chip ([macros/README.md](macros/README.md)) |

```sh
pip install "chipleap[rtl]"            # cocotb; Icarus or Verilator for the simulator
chipleap verify examples/uart          # lint, simulation, coverage, formal, CDC, synthesis, equivalence
chipleap verify examples/uart --steps all   # and place and route, gate level, DFT, power, IR drop, UPF, the chip
chipleap verify examples/uart --steps chip  # the chip alone: pad ring, comparator, DRC and LVS
chipleap eco examples/uart --paths           # the worst paths of the routed design
```

Results go to `examples/uart/build/verify/` (`report.html`, `VERIFY.md`, `verify.json`); the routed GDS to `examples/uart/build/pnr/runs/chipleap/final/gds/uart.gds`, the scan patterns to `examples/uart/build/dft/patterns.json` and `patterns.stil`.

With `--steps all` the UART gets three scan chains of 17 flip-flops, which LibreLane routes with the rest; Fault reaches 99.2 % stuck-at coverage with 39 patterns, which pass through the chains of the scan netlist and of the routed one; the tests pass on the routed netlist with its scan chains and draw 0.35 mW at the typical corner (0.95 mW vectorless); the supply drops by at most 0.5 mV; and OpenROAD places the receiver in a power domain of its own. The steps need OpenROAD, Fault with Quaigh and Icarus 13 ([docs/projects.md](../../docs/projects.md#after-place-and-route)). The tests read `CLKS_PER_BIT` from `CHIPLEAP_PARAMETERS` when they run on a netlist, which has no parameters.

`CLKS_PER_BIT` is 16 so the tests run in a few seconds; set 434 in `project.yaml` for 115200 baud from a 50 MHz clock. Coverage reports the two `default:` arms of the state machines as never reached: every state is used, so they cannot be.

The transmitter states its own properties under `` `ifdef FORMAL `` in `rtl/uart_tx.sv` (the line is high when idle and in the stop bit, low in the start bit), which the formal step proves for every cycle with `abc pdr`. The receive line comes from another chip's clock, so `project.yaml` lists `rx_i` under `cdc.async_inputs`, and the CDC step accepts the receiver's two-flop synchronizer. The tests read `CLKS_PER_BIT` from `CHIPLEAP_PARAMETERS` when they run on a netlist, which has no parameters left, so the same tests run at gate level: `--steps all` ends with them on the routed netlist with SDF delays, after equivalence has shown that netlist equal to the RTL.

The `chip` section makes the UART a whole chip ([docs/chips.md](../../docs/chips.md)): 33 pads of IHP's `sg13g2_io` library around it on a 1.4 mm die, and aispice's StrongARM comparator in the core, its inputs on two analog pads and its output on the receive line, so the UART reads the comparator's decisions. `--steps chip` routes it with LibreLane, adds bond pads, the seal ring and fill, and checks it with IHP's LVS deck (the comparator's transistors included) and DRC deck; the chip is `build/chip/uart_chip.gds` and the bond map `build/chip/uart_chip.bondmap.csv`.
