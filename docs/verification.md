# Verification

`chipleap verify DIR` checks a design bundle with open-source tools. Each step is skipped, with the reason, when its tool is missing, so a laptop with only Python still gets the consistency check and a full EDA setup gets everything.

```text
$ chipleap verify out
consist PASS     design.yaml, RTL wrappers, driver, register map and X-HEEP configuration agree
lint    PASS     Verilator -Wall reports no warnings
sim     PASS     21 randomized jobs bit-exact; cycle model error 0.0% max without stalls, ...
synth   PASS     logic cell area ... mm2 on IHP SG13G2 (model ... mm2), critical path ... ns, about ... MHz
xheep   PASS     X-HEEP validated the configuration (... banks, ... interleaved) and rendered 27 templates
```

Results go to `DIR/verify/verify.json` and `DIR/verify/VERIFY.md`. Choose steps with `--steps consistency,lint,sim,synth,xheep`; place and route (`pnr`) takes many minutes, so it runs only when named, or with `--steps all`.

A bundle may come from someone else, so ChipLeap never writes through a symlink in it. `DIR` itself (like the directory you give `explore` and `generate` with `-o`) may not be a symlink, and neither may `verify/` or a step's directory in it (`verify/synth`, `verify/pnr`, `verify/genus` and so on): the run stops with an error that names it. A symlink further inside a step's directory is removed before the step writes there, and nothing it points at is touched. On Windows a directory junction, or any other reparse point, counts as a symlink. A directory that `-o` creates gets a `.chipleap` file that says ChipLeap made it, and ChipLeap never empties an output directory without one.

This page is about generated bundles. The same command verifies RTL you wrote, given a `project.yaml`: lint with Verilator and slang, your cocotb tests on Icarus or Verilator, coverage, formal proofs of your properties with SymbiYosys, clock and reset domain crossing checks, synthesis, equivalence of the RTL and its netlists, place and route with the signoff settings below, and your tests again on the routed netlist with SDF delays. [projects.md](projects.md) describes it.

## Consistency

A bundle gets edited by hand once it leaves ChipLeap. This step re-derives every generated value from `design.yaml` and reports each file that disagrees:

- the RTL wrappers' parameters (`chipleap_accel_top.sv`, `chipleap_accel_xheep.sv`),
- the driver's build constants and register offsets (`sw/chipleap_driver.h`),
- the register decode in `rtl/chipleap_accel.sv` and the reggen description `rtl/chipleap_accel.hjson`,
- the CPU, bus and banks in the X-HEEP configuration.

## Lint

`verilator --lint-only -Wall` on the accelerator with the design's parameters. The test suite lints all three dataflows over a spread of shapes, including 1 x 1 arrays, and expects no warnings at all.

## Simulation and the cycle cross-check

The bundle's Verilator testbench (`tb/tb_chipleap.cpp`) drives the accelerator through its register interface, exactly as firmware would: it includes the generated driver and calls `chipleap_plan`, `chipleap_start` and `chipleap_busy`. A memory model sits on the OBI ports. It grants each request unless a random stall is drawn, and answers a fixed number of cycles after the grant.

Each case fills memory with random int8 activations and int8, int4 or int2 weights packed as the hardware expects, runs one job, compares every output with a golden matrix multiply and reports the cycle count from the `CYCLES` register. The default cases cover decode-shaped GEMV, small GEMMs, edge tiles in every dimension, K tiling with non-resident activations, every weight format the PEs accept, and two cases with 30 % random stalls and three-cycle latency.

For each case, `simulate_job` in `chipleap.model.accel` predicts the cycle count from the same tile plan. On this release:

- every case on all three dataflows is bit-exact;
- without memory stalls the prediction equals the simulated count exactly;
- with random stalls it is within a few percent (the model uses the average grant rate, and waits for the slowest read port, since each port reads its own share of a transfer).

The closed form the explorer uses (`job_timing`) is within 0.1 % on average of `simulate_job` over random jobs; jobs of up to 256 tiles use `simulate_job` directly. Together this ties the explorer's numbers to the RTL.

Verilator models are cached by parameter hash in `~/.cache/chipleap` (`CHIPLEAP_CACHE` moves it), so re-running costs seconds.

## Synthesis

The accelerator's logic is synthesized with Yosys against the IHP SG13G2 standard cells (typical corner, 1.2 V). The buffers become SRAM macros in silicon, and their area comes from the macro liberty files instead. So the design is synthesized twice with tiny buffers (8 and 16 words) and the area is extrapolated to zero buffer words. ABC maps the logic with Yosys's fast script, then buffers high-fanout nets and sizes gates, as placement and routing would; its static timing of that netlist plus the flop's clock-to-Q and setup estimate the clock. Without buffering, a control signal that drives a few hundred flops looks like a 5 ns gate and the estimate is several times too slow. ABC sizes for a 5 ns target, the 100 MHz the node allows with room for the slow corner and routing. On this release the accelerators synthesize to 191 to 271 MHz at the typical corner; what they meet after routing at the slow corner is in the next section. A 1 x 8 GEMV engine takes about 20 s; `CHIPLEAP_ABC_FULL=1` selects Yosys's default mapping script, which saves a few percent of area and takes many minutes on multiplier-heavy logic. Each run's Yosys log is kept in the work directory.

The step reports the measured logic cell area next to the model's estimate for the same design on SG13G2, so the model's error is visible every time. The Yosys in Ubuntu 24.04 (0.33) cannot read every SystemVerilog construct the RTL uses, so the source goes through [sv2v](https://github.com/zachjs/sv2v) when it is installed.

The liberty file is found under `PDK_ROOT`, `~/.volare`, `~/.ciel` or `~/.local/share/pdk`; `CHIPLEAP_LIBERTY` points at it directly.

## Place and route

`--steps pnr` hands the accelerator to [LibreLane](https://github.com/librelane/librelane), the open flow behind the Tiny Tapeout IHP shuttles: Yosys, OpenROAD for floorplan, placement, clock tree, routing and timing repair, OpenSTA at every corner, KLayout with IHP's own rule deck for design rules, and Netgen for layout versus schematic. The clock is the design's own on IHP SG13G2, or the node's 100 MHz limit for designs on other nodes. LibreLane optimises at its typical corner by default; ChipLeap points gate sizing, buffering, the clock tree and the repair after global routing at the slow corner (1.08 V, 125 C) instead, because that is where the design is signed off, and repairs hold at every corner. It also has the repairs leave 0.8 ns (after placement) and 1.6 ns (after global routing) of setup slack, because detailed routing took 0.7 to 1.6 ns of slack away from the global router's estimate in our runs and nothing repairs timing after it.

### SRAM macros

When the IHP PDK ChipLeap uses is installed (`CHIPLEAP_IHP_PDK`, or the one its liberty is in) and the build can take them without a change to its schedule, the buffers in the layout are IHP's own SRAM macros at the design's sizes. A build can take them when it is GEMV, has one DMA read port and COLS of 4, 8 or 16: a chunk of COLS bytes is one word of a COLS-byte bank, each weight lane has a bank of its own, each slot of a double buffer has its own banks, and one DMA port writes at most one word a cycle, which a single-port macro takes. The RTL's `SRAM_BUF` reads each chunk's bank word a cycle before the chunk issues (the tile's first when the tile starts, each next one while the one before it issues), and the issue registers the word in the operand registers as it registers the flop buffers' read, so the schedule is the same. An IHP macro's clock to output is about half the clock period at the slow corner, so the word is never in series with the multipliers, and the address of the next chunk is kept in registers a cycle ahead (the row's next chunk, the next row's first), so no adder sits between an issue and the macros' enables: the RTL tests run each such build against the flop buffers and the model, cycle for cycle, stalls included, with a model of the macros' ports that stops if a bank is ever read and written in one cycle ([the cycle cross-check](#simulation-and-the-cycle-cross-check)). `chipleap.gen.banks` chooses the macros (the width that wastes fewest bits, then the fewest that hold the depth, then area) and writes the top that holds the core and every macro under a fixed name (`abuf_s0_m0`, `wbuf_s1_l0_m0`).

The floorplan is ChipLeap's: a row of macros along the top of the core, standing upright with their pins (all on the bottom edge) facing down, a mirrored row along the bottom with its pins facing up, and the logic in the band between, sized for the model's standard-cell area at 45 % utilisation. Each macro keeps a 10 um halo, sits under a whole VDD and VSS pair of the core's TopMetal2 stripes, and is powered by a macro grid (Metal5 straps across its Metal4 supply stripes, TopMetal1 stripes over them, joined to TopMetal2). A 30 um column of rows stays at the core's left edge: OpenROAD's core is the bounding box of the rows, and without the column the rows cut under full-width macro rows would leave the macros outside the core's power stripes. The macros' slow, typical and fast liberty time them at every corner, and their CDL makes them boxes with pins for LVS. Repair after global routing runs, and wire length is capped at 300 um, so long nets to the macros' pins get buffers (below). The step adds the macros and their area to its result and compares them with the model's buffers, the model's logic with LibreLane's synthesis, and the routed standard cells with that synthesis (`macro_um2`, `model_buffer_um2`, `synth_cell_um2`, `model_cell_error`, `pnr_cell_growth`).

Otherwise the layout keeps 16-word flop buffers, as in synthesis, and the result says which condition failed.

The SmolLM2 example's accelerator (GEMV 1 x 8, one port, 2 + 2 KiB double-buffered) routes this way with four `RM_IHPSG13_1P_256x64_c2_bm_bist` macros: DRC, LVS and antenna clean, no slew, capacitance or fanout violations at any corner, and 90 MHz met at the slow corner with 0.69 ns to spare (about 96 MHz there, 149 MHz at the typical corner; hold +0.10 ns, no negative slack anywhere). The core is 1.066 mm2 at 60 % utilisation, with 27,054 cells. Macro builds get two repairs the flop builds do not need. The DMA's write word goes to every macro, in rows at the top and the bottom of the core, so its nets to the macros' `A_DIN` pins run about 800 um; at the slow corner they broke the macros' slew and capacitance limits (38 slew and 21 capacitance violations with LibreLane's defaults). Repair after global routing alone found the nets and left 51 and 14, because resizing a driver does not shorten its wire and the maximum wire length LibreLane derives (about 5 mm) never splits one. So macro builds cap wire length at 300 um before and after global routing, and the repair buffers those nets.

The step checks the model's area part by part:

| Part | Model | Measured | |
|---|---|---|---|
| SRAM macros | 0.373 mm2 (4 x 256x64) | 0.373 mm2 in the layout | exact |
| Standard cells | 0.272 mm2 | 0.290 mm2 in LibreLane's synthesis | -6.1 % |
| Growth in place and route | not modelled | 0.417 mm2 routed | +44 % over synthesis |

The macros take twice the area of the buffers' bits: each 1 KiB slot of a double buffer is 128 words of 64 bits, the smallest 64-bit macro that holds them has 256 words, and the model prices the macro the layout places ([model.md](model.md#area)). The model's logic estimate is fitted to ChipLeap's own synthesis of flop-buffer builds with the buffers taken out, which gives 0.269 mm2 for this design (+1 %). The macro build adds the logic that writes the macros and reads them a cycle ahead, which the model does not price: with ChipLeap's synthesis script its core is 0.323 mm2, against 0.290 mm2 from LibreLane's, which maps for area where ChipLeap's buffers and sizes for 5 ns. Place and route then adds 44 % to LibreLane's synthesis, mostly buffers from timing and wire-length repair (0.111 mm2) and the clock tree (0.010 mm2); that growth is general to the flow, and the step reports it apart from the model.

The step passes when setup and hold timing are met at every corner and DRC and LVS are clean. It reports the core area, the utilisation, the cell count, the worst setup slack and the clock it implies, power, and the model's cell-area estimate for comparison, and keeps the GDS in `DIR/verify/pnr/runs/chipleap/final/gds/`. `scripts/render_gds.py` draws it with KLayout in the PDK's colours. `--pnr-tool innovus` runs the same step with Cadence Genus and Innovus instead, here or on a design server, against IHP SG13G2 or a technology of your own; [commercial-tools.md](commercial-tools.md#place-and-route) describes it. `CHIPLEAP_LIBRELANE` points at the `librelane` command and `CHIPLEAP_PNR_TIMEOUT` (seconds, default 10800) bounds the run. At the limit ChipLeap stops LibreLane, every process it started and the containers it runs in, and the step fails with the limit in its summary. Ctrl-C, SIGTERM and SIGHUP stop them the same way, and so does the MCP server when a `verify_soc` call is cancelled or the server shuts down. A container counts as the run's only when it carries the label `chipleap.run=<run id>`, and no other container is touched. ChipLeap puts a `docker` first on LibreLane's PATH that adds the label to `docker run`; a wrapper script around `docker run` and LibreLane's own `--dockerized` mode both go through it. A launcher that calls docker by its full path can add `--label chipleap.run=$CHIPLEAP_RUN_ID` itself; LibreLane with `LIBRELANE_CONTAINER_ENGINE` set cannot, and its container is not stopped. One run at a time can use a work directory. LibreLane runs without a terminal, so `--dockerized` needs `--docker-no-tty`.

LibreLane runs in two parts in one run directory: up to detailed routing, then the rest. Between them ChipLeap measures every routed shape on Metal2 to Metal5 against the layer's minimum area (0.144 um2, IHP's rules M2.d to M5.d), merging each net's wires, vias and patches as the rule deck merges metal (`chipleap.verify.minarea`). OpenROAD's detailed router meets the minimum with patches and passes its own result, but on a short Metal2 stub between a Via1 and a Via2 with a jog in it, it now and then leaves the shape under the minimum: one such shape failed signoff in 4 of 12 accelerator runs, at 0.1287 to 0.143975 um2. In two of them the router's patch lay almost wholly on the stub's own metal, so a larger minimum for the router would only grow a patch that adds nothing. Instead each shape under the minimum gets a rectangle at one end along its layer's direction, as wide as that end and long enough to reach 0.15 um2, where no other net's metal, power grid, pin or cell with metal on that layer comes within the spacing rule plus 0.05 um. The patched DEF goes back into LibreLane through `OpenROAD.DEFtoODB`, and the rest of the flow, signoff included, runs on it. A shape with no room at either end fails the step and is named. `minarea/patches.txt` in the work directory lists the patches, and an ECO runs the same way.

These are the designs routed with this flow at 100 MHz, each with 16-word flop buffers, and their setup slack at the slow corner. All are DRC, LVS and antenna clean. In the 16-port design the detailed router left one Metal2 shape under the minimum area (0.13875 um2), which the step patched before signoff, as above.

| Accelerator | Ports | Slow corner | Typical corner | Worst path at the slow corner |
|---|---|---|---|---|
| GEMV 1 x 8 | 1 | +0.89 ns, about 110 MHz | about 160 MHz | the GEMV engine's row counter |
| Weight-stationary 4 x 4 | 1 | +0.67 ns, about 107 MHz | about 166 MHz | the accumulator update |
| Output-stationary 4 x 4 | 1 | +1.03 ns, about 111 MHz | about 166 MHz | a read port's address step |
| GEMV 4 x 8 | 2 | -0.23 ns, about 98 MHz | about 153 MHz | the accumulator row select into the accumulators |
| GEMV 2 x 8 | 4 | +0.37 ns, about 104 MHz | about 157 MHz | unpacking packed weights into the operand registers |
| GEMV 1 x 8 | 8 | -0.09 ns, about 99 MHz | about 156 MHz | unpacking packed weights into the operand registers |
| GEMV 1 x 8 | 16 | -0.41 ns, about 96 MHz | about 149 MHz | the activation read address into an operand register |

The GEMV 4 x 8 design and the GEMV 1 x 8 designs with eight and sixteen ports are routed with the core reading its tile descriptor from a register loaded a cycle ahead. Before that, the core selected the descriptor of its buffer slot after the registers, and that select limited them: GEMV 4 x 8 met about 97 MHz (-0.27 ns) on the select into the accumulators, and GEMV 1 x 8 about 89 MHz with eight ports (-1.18 ns) and with sixteen (-1.25 ns) on the select into an activation operand register. The other rows were routed before the change and were not limited by it. Routing alone moves a period by about half a nanosecond: the GEMV 4 x 8 design, routed once more from the same logic before ChipLeap's core took its `chipleap_accel` wrapper, met +0.31 ns (about 103 MHz) on the dot product.

Designs with more than eight read ports are placed at 35 % core utilisation instead of 45 %: at 45 % the 16-port design failed global routing on congestion; at 35 % global routing finished with no overflow. With the DMA that handed words to its read ports one after another, the GEMV 2 x 8 engine with four ports met about 71 MHz (-4.02 ns) and the GEMV 1 x 8 engine with sixteen about 24 MHz (-31.79 ns, at 45 %).

The model's clock limit for the accelerator on IHP SG13G2 comes from these runs ([model.md](model.md#technology-data)), so designs the explorer recommends are clocked where they close timing at the slow corner.

## After place and route

RTL projects have five more steps, for what follows place and route in an industrial flow ([projects.md](projects.md#after-place-and-route) describes their sections and results). Each writes an OpenROAD script from the project and runs the `openroad` command on it, and reads OpenROAD's reports back; LibreLane does the routing and the signoff.

| Step | Engines | What it reports |
|---|---|---|
| `dft` | OpenROAD `scan_replace` and `execute_dft_plan`, Fault 0.9 with Quaigh, Icarus 13 | scan chains and their order, stuck-at coverage, `patterns.json` and STIL, a flush pattern and ATPG patterns simulated through the chains of the scan netlist and, once it is routed, of the routed one |
| `pnr` | LibreLane | with `dft.insert`, the scan-inserted netlist routed with the usual signoff |
| `eco` | OpenROAD on the routed DEF, LibreLane resumed before detailed routing | the changes, and timing, DRC, LVS and antenna before and after |
| `power` | Icarus 13 with the cell models and the project's cocotb tests, OpenROAD `read_vcd` and `report_power` | power per group at each corner, from the tests' activity and vectorless |
| `ir_drop` | OpenROAD PDNSim (`analyze_power_grid`) | worst and average drop per supply net, the worst cells, a map |
| `upf` | OpenROAD `read_upf`, floorplan and placement on a hierarchical netlist | power domains, their cells inside their areas, the signals between domains |

Two OpenROAD builds take part, the `openroad` command and the one inside LibreLane's container, and they never exchange their databases: DEF, Verilog, SPEF and SDC go between them. Every input is staged into the step's work directory, because a containerised `openroad` sees only that, and OpenROAD runs through the same launcher as LibreLane, so a time limit (`CHIPLEAP_OPENROAD_TIMEOUT`, one hour by default), Ctrl-C or a cancelled MCP call stops it and its container. Gate-level simulation of IHP's cells needs Icarus 13 or newer (`CHIPLEAP_GL_IVERILOG`; Icarus 12 leaves every flip-flop at X). `chipleap doctor` lists `openroad`, `iverilog-gl`, `fault` and `quaigh`.

What the open tools cannot do is said where it matters: no open simulator is power aware, so a UPF changes placement but not simulation; LibreLane routes one supply; IHP SG13G2's standard cells have no level shifters, isolation cells or power switches; ATPG is stuck-at only, without at-speed patterns or compression.

## X-HEEP

Given an X-HEEP checkout (`--xheep DIR`, `CHIPLEAP_XHEEP_DIR`, or `--fetch-xheep` to clone the tested commit into the cache), ChipLeap:

1. loads the generated configuration with X-HEEP's own `load_config`, then runs `build()` and `validate()`, the checks `make mcu-gen` runs;
2. renders all of X-HEEP's Mako templates for it (RTL packages, bus, memory subsystem, linker scripts, headers) into a scratch copy, so the checkout is never modified, and reads back the bank counts from the generated `core_v_mini_mcu_pkg.sv`.

X-HEEP configurations are Python, and X-HEEP's generator executes them. So this step checks the configuration ChipLeap generates from the bundle's `design.yaml`, in a temporary directory, and never runs the bundle's own `xheep/<name>.py`; it says so when that copy differs, and the consistency step reports how. Bundles from elsewhere can be verified without running their code. Design and spec names are limited to letters, digits, `_`, `.` and `-`, because they become file names and appear in the generated RTL, C and YAML.

This needs `hjson` and `mako` in the Python that runs ChipLeap: `pip install "chipleap[xheep]"`.

## The test suite

`pytest` runs everything above that the machine supports, plus model tests:

- the parameter counts of the twelve LLM presets against their model cards;
- the IHP SG13G2 SRAM fits against every macro's liberty data;
- tile plans that always fit the buffers;
- the C driver's `chipleap_plan` against the Python `plan_tiles` on 6,000 random shapes (compiled with the host C compiler), and the driver compiled for RV32IMC;
- explorer determinism, Pareto non-dominance and the example specs;
- the MCP server end to end over an in-process client, and a run-log replay.

Tests that need Verilator are marked `rtl` and skip themselves when it is missing; tests
that need a commercial simulator or synthesis tool on the path are marked `commercial`.
The RTL project tests (`tests/test_project_*.py`) skip themselves without cocotb, Icarus,
Verilator 5.036 or newer (for coverage), slang, Yosys, SymbiYosys and z3, EQY, Icarus 13
(for gate level) or LibreLane, whichever they need. Each verification check has a passing
and a failing case: SymbiYosys proves a FIFO's properties and finds the counterexample in a
broken one; CDC passes two- and three-flop synchronizers, a handshake and a FIFO with gray
pointers and flags a missing synchronizer, logic before one, binary pointers, an
unsynchronized asynchronous input and a reset domain crossing; equivalence proves the
counter's netlist equal to its RTL and catches one changed gate, with Yosys and with EQY;
the UART's cocotb tests pass on its routed netlist with SDF (from a real LibreLane run kept
in `tests/fixtures/uart_routed`, since CI cannot route) and fail with one gate changed.
`tests/test_tool_path.py` plants programs named like the tools in a project and puts `""`
and `.` on `PATH`, and checks that every step runs the real tools or skips.
The real runs of the implementation steps need OpenROAD, Fault, Quaigh and Icarus 13 and
run with `CHIPLEAP_RUN_IMPL=1` (seconds on the fixtures); the ECO tests place and route
first and run with `CHIPLEAP_RUN_PNR=1`.

## Commercial tools

The simulation and synthesis steps also run on Cadence Xcelium, Synopsys VCS or Siemens
Questa and on Cadence Genus or Synopsys Design Compiler, on this machine or on a design
server over ssh (`--sim-tool`, `--synth-tool`, `--remote`), and a foundry PDK can be added
from its liberty files with `chipleap tech add`. [commercial-tools.md](commercial-tools.md)
explains each.
