# Demo: digital on top, from RTL to a checked chip

This walks [examples/uart](../examples/uart) from its RTL to a chip GDS on IHP SG13G2: a UART written by hand, with cocotb tests and a Python reference model, at the core of a 1.52 x 1.52 mm chip with 33 pads of IHP's `sg13g2_io` library. In the core next to the UART sits a StrongARM comparator from [aispice](https://github.com/danieltyukov/aispice), handed over as a [macro package](macro-package.md). Its inputs come from two analog pads and its output drives the UART's receive line. The chip is signed off with IHP's own DRC deck on the whole die and its LVS deck down to the transistors of the standard cells and the comparator.

Every number below is from `chipleap verify examples/uart --steps all` on ChipLeap `main` at `d26974f` (14 Oct 2026), with the comparator's package exported by aispice `master` at `5db63e1`. Every step passed. From an empty build folder the whole run takes about 43 minutes on a laptop. Only open tools ran: Verilator, slang, Icarus, cocotb, SymbiYosys, Yosys, OpenROAD, LibreLane, KLayout, Magic, Netgen and Fault.

aispice's [analog-on-top demo](https://github.com/danieltyukov/aispice/tree/master/examples/analog_on_top) is the other way round: analog blocks and a hardened digital block in a pad ring that aispice assembles.

## What you need

```sh
git clone https://github.com/danieltyukov/chipleap && cd chipleap
pip install -e ".[rtl]"
chipleap doctor
```

`chipleap doctor` lists every tool it found and what each is for. The full run needs these; [docs/projects.md](projects.md) says how to get each:

- Verilator and slang for lint.
- Icarus 13 for simulation at gate level (`CHIPLEAP_IVERILOG`, `CHIPLEAP_GL_IVERILOG`), since Icarus 12 leaves IHP's flip-flops at X.
- SymbiYosys and EQY built for the Yosys on the path (`CHIPLEAP_SBY`, `CHIPLEAP_EQY`), with z3.
- OpenROAD and LibreLane 3.0.0.dev44.
- Fault with Quaigh.
- KLayout.
- A checkout of IHP-Open-PDK.

Every step skips itself, with the reason, when its tool is missing.

## 1. The project

[`project.yaml`](../examples/uart/project.yaml) describes the RTL as ChipLeap reads it:

- the top and its sources;
- `CLKS_PER_BIT` and the 100 MHz clock;
- the asynchronous reset;
- the cocotb tests and the golden model;
- a formal check;
- the asynchronous receive input;
- scan chains, power corners and a power domain for the receiver;
- the `chip` section.

```sh
chipleap project check examples/uart    # the project with every default filled in
```

## 2. The RTL checks

```sh
chipleap verify examples/uart           # lint to equivalence, in about half a minute
```

| Step | Tools | Result |
|---|---|---|
| Lint | Verilator `-Wall`, slang `-Wextra` | no warnings |
| Simulation | cocotb 2 on Icarus 13, against the model | 5 of 5 tests pass |
| Coverage | Verilator | line 83.3 % (10 of 12), branch 100 %, toggle 100 %; the 2 lines never reached are `default:` arms no state reaches |
| Formal | SymbiYosys, `abc pdr` | the transmitter's line in every state, proved for every cycle |
| Clock and reset crossings | ChipLeap's checker on Yosys's netlist | 47 flip-flops on `clk_i`; the receive line crosses through a two-flop synchronizer; 1 warning (the reset is released asynchronously) |
| Synthesis | Yosys and ABC on IHP's standard cells | 221 cells, 51 flip-flops, 0.0041 mm²; critical path 1.01 ns, about 684 MHz at the typical corner |
| Equivalence | Yosys | RTL against synthesis: 74 of 75 points proved, the last (`u_rx.state_q[2]`) equal for 40 cycles from reset; synthesis against the routed netlist: all 969 points proved |

## 3. Down to the layout

```sh
chipleap verify examples/uart --steps all
```

`--steps all` adds place and route and everything after it:

| Step | Tools | Result |
|---|---|---|
| Place and route | LibreLane (OpenROAD, KLayout, Magic, Netgen), signed off at the slow corner | 352 cells on a 0.0105 mm² core at 65 % utilisation; 100 MHz met at 125 C (about 264 MHz at the slow corner, 310 MHz at the typical); DRC, LVS and antenna clean |
| Gate level | the cocotb tests on the routed netlist with SDF delays, Icarus 13 | 5 of 5 tests pass |
| Design for test | OpenROAD scan insertion, Fault with Quaigh | 3 chains of 17 flip-flops; 99.2 % stuck-at coverage (1371 of 1382 faults) with 39 patterns, which pass through the chains of both the scan netlist and the routed one |
| Power | OpenROAD, with the activity of the tests on the routed netlist | 0.345 mW at the typical corner (0.947 mW vectorless); 1166 of 1166 pins annotated |
| IR drop | OpenROAD PDNSim | 0.52 mV on VPWR, 0.33 mV on VGND at worst |
| Power intent | UPF read and placed by OpenROAD | the receiver in a domain of its own (106 cells), 13 signals between the two |

The routed block is `examples/uart/build/pnr/runs/chipleap/final/gds/uart.gds`. The report is `examples/uart/build/verify/report.html`, with `VERIFY.md` and `verify.json` beside it.

## 4. The analog block from aispice

The comparator arrives as [`examples/uart/macros/strongarm.macro`](../examples/uart/macros), a `macro/v1` package. It holds:

- the GDS;
- a LEF abstract;
- a liberty view with the pins' capacitance;
- a Verilog model;
- the LVS and extracted netlists;
- its specs, measured before and after layout;
- DRC, LVS, extraction and simulation evidence with the hash of every file each check read.

aispice laid it out from its netlist with `aispice layout generate`, checked it, and exported it with `aispice macro export` (aispice `master` at `5db63e1`). [macros/README.md](../examples/uart/macros/README.md) gives the netlist and both commands.

- **Size and capacitance.** The package is 29.1 x 40.8 um, with 101.1 fF extracted.
- **Both decisions after layout.** Its specs include both decisions: inp 20 mV above inm, then 20 mV below. Both hold after layout at tt, ss and ff.
- **Offset.** aispice draws a comparator mirror-symmetric about its axis, so the laid-out latch has an input-referred offset under 0.2 mV at tt and 27 C.

ChipLeap checks a package before it places it. Both tools check with the same rules, so aispice's own check gives the same answer:

```sh
chipleap macro check examples/uart/macros/strongarm.macro --pdk ihp-sg13g2@22f2a25 \
    --pdk-libs ~/IHP-Open-PDK/ihp-sg13g2
#   strongarm.macro: pass, 0 errors, 0 warnings
aispice macro check examples/uart/macros/strongarm.macro --pdk ihp-sg13g2@22f2a25 \
    --pdk-libs ~/IHP-Open-PDK/ihp-sg13g2
#   strongarm.macro: pass, 0 errors, 0 warnings
```

The `chip` section of `project.yaml` places it and joins its pins to the chip:

```yaml
  macros:
    - instance: comp
      package: macros/strongarm.macro
      at_um: [60, 260]
      connect: {clk: clk_i, inp: ain_p, inm: ain_m, outp: rx_i}
```

## 5. The chip

```sh
chipleap chip plan examples/uart             # the floorplan, without running a tool
chipleap verify examples/uart --steps chip   # only the chip
```

```text
uart_chip: die 1400 x 1400 um (1520 x 1520 um with the seal ring), core 500.16,502.74 to 900,899.64 um
33 pads at a 100 um pitch, 80 IO ring cells in all
    1  west   ain_m                sg13g2_IOPadAnalog               bond at 35,900
    2  west   ain_p                sg13g2_IOPadAnalog               bond at 35,800
...
  macro comp (strongarm) at 560.16,763.56 um, N
```

LibreLane routes the core, the comparator and the pad ring together. KLayout then adds the bond pads, IHP's seal ring and fill. ChipLeap checks the result with IHP's decks and times it at its own ports, through the pads:

| | |
|---|---|
| Outline | 1.52 x 1.52 mm with the seal ring, 33 pads |
| DRC, IHP's deck on the whole die (density included) | 0 violations |
| LVS, IHP's deck down to the comparator's transistors | clean |
| Precheck | top cell, layers, cell names, zero-area shapes, PR boundary and outline pass |
| Supplies | VDD reaches the ring at 30 joins, VSS at 20 |
| Setup slack at the ports, slow corner | +0.968 ns (+6.257 ns at the core side of the pads) |
| Hold slack at the ports, worst corner | +0.118 ns (fast corner) |
| Worst path | core to the `rx_data_o[7]` pad: 5.408 ns of it inside the output pad |

The budget at the ports is 2 ns input and output delay, a 5 pF load and a 1 ns input edge. The chip is `examples/uart/build/chip/uart_chip.gds`, with its bond map in `uart_chip.bondmap.csv`.

<p align="center">
  <img src="img/uart-chip.jpg" alt="The UART chip drawn from its GDS: 33 bond pads and IHP's IO cells around a 400 um core, with aispice's comparator in the core's upper left, inside the seal ring" width="560">
  <br><sub>The chip from its GDS before fill: the core with aispice's comparator in its upper left, IHP's IO ring, 33 bond pads and the seal ring.</sub>
</p>

## The SRAM variant

[examples/memchip](../examples/memchip) is the same flow with a hard macro that the core's RTL instantiates, instead of a block placed beside the core. A 256-byte memory core wraps one of IHP's SRAM macros (`RM_IHPSG13_1P_256x8_c3_bm_bist`), inside a ring of 37 pads.

```sh
chipleap verify examples/memchip --steps chip
```

The run recorded in [#18](https://github.com/danieltyukov/chipleap/pull/18) passed in 17 minutes:

- the die is 1.62 mm with the seal ring;
- LVS is clean on IHP's deck, with the SRAM compared as a box;
- DRC is clean on IHP's deck, with the SRAM's 256 off-grid pin markers reported apart;
- timing is met through the pads, +6.51 ns at the slow corner.

## Reproduce it

```sh
git clone https://github.com/danieltyukov/chipleap && cd chipleap
pip install -e ".[rtl]"
chipleap doctor
chipleap project check examples/uart
chipleap macro check examples/uart/macros/strongarm.macro --pdk ihp-sg13g2 --pdk-libs ~/IHP-Open-PDK/ihp-sg13g2
chipleap chip plan examples/uart
chipleap verify examples/uart --steps all
```

LibreLane's flow is bit-reproducible: the same inputs and tools give the same routed design. The numbers above should come back unchanged on another machine with the same tool versions.
