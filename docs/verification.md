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

This page is about generated bundles. The same command verifies RTL you wrote, given a `project.yaml`: lint with Verilator and slang, your cocotb tests on Icarus or Verilator, coverage, synthesis and place and route with the signoff settings below. [projects.md](projects.md) describes it.

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
- with random stalls it is within a few percent (the model uses the average grant rate).

The closed form the explorer uses (`job_timing`) is within 0.1 % on average of `simulate_job` over random jobs; jobs of up to 256 tiles use `simulate_job` directly. Together this ties the explorer's numbers to the RTL.

Verilator models are cached by parameter hash in `~/.cache/chipleap` (`CHIPLEAP_CACHE` moves it), so re-running costs seconds.

## Synthesis

The accelerator's logic is synthesized with Yosys against the IHP SG13G2 standard cells (typical corner, 1.2 V). The buffers become SRAM macros in silicon, and their area comes from the macro liberty files instead. So the design is synthesized twice with tiny buffers (8 and 16 words) and the area is extrapolated to zero buffer words. ABC maps the logic with Yosys's fast script, then buffers high-fanout nets and sizes gates, as placement and routing would; its static timing of that netlist plus the flop's clock-to-Q and setup estimate the clock. Without buffering, a control signal that drives a few hundred flops looks like a 5 ns gate and the estimate is several times too slow. ABC sizes for a 5 ns target, the 100 MHz the node allows with room for the slow corner and routing. On this release the accelerators synthesize to 184 to 266 MHz at the typical corner; what they meet after routing at the slow corner is in the next section. A 1 x 8 GEMV engine takes about 20 s; `CHIPLEAP_ABC_FULL=1` selects Yosys's default mapping script, which saves a few percent of area and takes many minutes on multiplier-heavy logic. Each run's Yosys log is kept in the work directory.

The step reports the measured logic cell area next to the model's estimate for the same design on SG13G2, so the model's error is visible every time. The Yosys in Ubuntu 24.04 (0.33) cannot read every SystemVerilog construct the RTL uses, so the source goes through [sv2v](https://github.com/zachjs/sv2v) when it is installed.

The liberty file is found under `PDK_ROOT`, `~/.volare`, `~/.ciel` or `~/.local/share/pdk`; `CHIPLEAP_LIBERTY` points at it directly.

## Place and route

`--steps pnr` hands the accelerator to [LibreLane](https://github.com/librelane/librelane), the open flow behind the Tiny Tapeout IHP shuttles: Yosys, OpenROAD for floorplan, placement, clock tree, routing and timing repair, OpenSTA at every corner, KLayout with IHP's own rule deck for design rules, and Netgen for layout versus schematic. The clock is the design's own on IHP SG13G2, or the node's 100 MHz limit for designs on other nodes. LibreLane optimises at its typical corner by default; ChipLeap points gate sizing, buffering, the clock tree and the repair after global routing at the slow corner (1.08 V, 125 C) instead, because that is where the design is signed off, and repairs hold at every corner. It also has the repairs leave 0.8 ns (after placement) and 1.6 ns (after global routing) of setup slack, because detailed routing took 0.7 to 1.6 ns of slack away from the global router's estimate in our runs and nothing repairs timing after it. As in synthesis, the buffers are cut to 16 words, because in silicon they are SRAM macros whose area comes from the macro liberty files.

The step passes when setup and hold timing are met at every corner and DRC and LVS are clean. It reports the core area, the utilisation, the cell count, the worst setup slack and the clock it implies, power, and the model's cell-area estimate for comparison, and keeps the GDS in `DIR/verify/pnr/runs/chipleap/final/gds/`. `scripts/render_gds.py` draws it with KLayout in the PDK's colours. `--pnr-tool innovus` runs the same step with Cadence Genus and Innovus instead, here or on a design server, against IHP SG13G2 or a technology of your own; [commercial-tools.md](commercial-tools.md#place-and-route) describes it. `CHIPLEAP_LIBRELANE` points at the `librelane` command and `CHIPLEAP_PNR_TIMEOUT` (seconds, default 10800) bounds the run. At the limit ChipLeap stops LibreLane, every process it started and the containers it runs in, and the step fails with the limit in its summary. Ctrl-C, SIGTERM and SIGHUP stop them the same way, and so does the MCP server when a `verify_soc` call is cancelled or the server shuts down. A container counts as the run's only when it carries the label `chipleap.run=<run id>`, and no other container is touched. ChipLeap puts a `docker` first on LibreLane's PATH that adds the label to `docker run`; a wrapper script around `docker run` and LibreLane's own `--dockerized` mode both go through it. A launcher that calls docker by its full path can add `--label chipleap.run=$CHIPLEAP_RUN_ID` itself; LibreLane with `LIBRELANE_CONTAINER_ENGINE` set cannot, and its container is not stopped. One run at a time can use a work directory. LibreLane runs without a terminal, so `--dockerized` needs `--docker-no-tty`.

These are the designs routed with this flow at 100 MHz, each with 16-word buffers, and their setup slack at the slow corner. All are DRC and LVS clean.

| Accelerator | Ports | Slow corner | Typical corner | Worst path at the slow corner |
|---|---|---|---|---|
| GEMV 1 x 8 | 1 | +0.82 ns, about 109 MHz | about 162 MHz | the weight address counter |
| Weight-stationary 4 x 4 | 1 | +0.47 ns, about 105 MHz | about 160 MHz | the accumulator update |
| Output-stationary 4 x 4 | 1 | +0.62 ns, about 107 MHz | about 161 MHz | unpacking packed weights |
| GEMV 4 x 8 | 2 | -0.13 ns, about 99 MHz | about 149 MHz | the buffer slot select into the accumulators |
| GEMV 2 x 8 | 4 | -4.02 ns, about 71 MHz | about 104 MHz | the DMA handing words to its four ports in turn |

The model's clock limit for the accelerator on IHP SG13G2 comes from these runs ([model.md](model.md#technology-data)), so designs the explorer recommends are clocked where they close timing at the slow corner.

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
Verilator 5.036 or newer (for coverage), slang, Yosys or LibreLane, whichever they need.

## Commercial tools

The simulation and synthesis steps also run on Cadence Xcelium, Synopsys VCS or Siemens
Questa and on Cadence Genus or Synopsys Design Compiler, on this machine or on a design
server over ssh (`--sim-tool`, `--synth-tool`, `--remote`), and a foundry PDK can be added
from its liberty files with `chipleap tech add`. [commercial-tools.md](commercial-tools.md)
explains each.
