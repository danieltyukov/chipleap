# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Place and route: at `CHIPLEAP_PNR_TIMEOUT` (3 h by default) or on Ctrl-C, `verify --steps pnr` now stops LibreLane's container as well. It used to stop only the `docker` client, and the container went on routing to the end. A run that times out fails the step with the limit in its summary instead of ending `chipleap verify` with a traceback. Containers the run did not create are left alone. LibreLane no longer inherits ChipLeap's standard input: a containerised run switched the terminal to raw mode through it, so Ctrl-C never reached ChipLeap, and under the MCP server it could read the client's requests.

## [0.3.0] - 2026-10-11

### Changed

- Timing: the accelerator meets 100 MHz on IHP SG13G2 at the slow corner (1.08 V, 125 C) after routing with LibreLane, not only at the typical one: about 109 MHz for the GEMV 1 x 8 engine, 105 and 107 MHz for the 4 x 4 weight- and output-stationary arrays. The GEMV engine registers its operands before the multipliers, the weight- and output-stationary arrays register their edge feeds, the loader decides where the next tile goes a few cycles before it needs to, each DMA read spends its first cycle setting up its address and counts, the weight-stationary array reads an accumulator one cycle before it writes it back, and the loader decides whether another tile follows from the extent still to cover, with one compare. Each stage adds at most one cycle per tile or transfer, and the cycle model follows exactly.
- The model caps the accelerator's clock on IHP SG13G2 at what it closes timing at after routing, at the slow corner: the period of the routed single-port designs, longer for a wider GEMV datapath, and a period that grows by 1.95 ns per port, fitted to routed GEMV engines with two and four ports (about 99 and 71 MHz). Designs above it are infeasible, and the explorer lowers the clock when a change of accelerator needs it. Clock steps now include 75 and 90 MHz.
- Place and route: `verify --steps pnr` signs off setup at the slow corner. LibreLane's repairs aim for 0.8 ns of slack after placement and 1.6 ns after global routing, because detailed routing took up to 1.6 ns away and nothing repairs timing after it.
- Synthesis sizes gates for a 5 ns target (the 100 MHz the node allows, signed off at the slow corner) instead of the shortest path ABC can reach, which had inflated short-path designs by up to a fifth. The accelerator area model is refitted to it over 12 configurations: 2.9 % leave-one-out error on average, 8.4 % at worst.
- ChipLeap is on PyPI: `pip install chipleap`.

## [0.2.0] - 2026-10-10

### Added

- Place and route: `chipleap verify --steps pnr` (or `--steps all`) runs the accelerator through LibreLane on IHP SG13G2, with timing at every corner, KLayout DRC with IHP's rule deck, LVS and antenna repair, and keeps the GDS. The open-PDK example's 1 x 8 GEMV engine routes to a 0.81 mm2 core with clean DRC and LVS, about 120 MHz at the typical corner and 77 MHz at the slow corner. `scripts/render_gds.py` draws a GDS in the PDK's colours.
- A `standalone` platform: the accelerator as AXI4 IP for any host (an Arm, RISC-V or FPGA SoC), with an AXI4-Lite register port, AXI4 read and write masters, a plain C driver and a protocol-checking AXI testbench. Set `platform: standalone` in a spec or pass `--platform standalone`; reports give the IP's area apart from the assumed host system.
- Commercial EDA tools next to the open ones: simulation with Cadence Xcelium, Synopsys VCS or Siemens Questa through one simulator-neutral SystemVerilog testbench driven by the generated C driver, and synthesis with Cadence Genus or Synopsys Design Compiler, with Innovus scripts for the netlist (`--sim-tool`, `--synth-tool`). Runs on this machine or on a design server over ssh (`--remote`, `--remote-setup`); the MCP server cannot choose a host or a setup command.
- `chipleap tech add`, `list` and `remove`: measure a PDK of your own from its liberty files into your config directory, never into a repository, and use it like the built-in technologies.
- README images: the die render, an animation of the agent's decisions with every floorplan to scale, the report, and the routed layout (`scripts/make_readme_visuals.py`).

### Fixed

- Synthesis failed for bundles given by a relative path.
- The hero callouts drifted off their blocks on phones while scrolling, and figures on phones used a display face whose 1 read oddly.
- `chipleap require` now sizes external memory per process node at the rate that node's I/O allows, as the evaluator does. It no longer rules out serial memories that run below their rated speed on the IHP 130 nm pads, such as two octal PSRAM channels for SmolLM2 135M at 2 tokens per second; `required_resources` reports `channels_by_node`.

## [0.1.0] - 2026-10-10

First release.

### Added

- Workloads: twelve decoder-only LLM presets with shapes from their `config.json` (SmolLM2, Qwen2.5, Qwen3, Gemma 3, TinyLlama, Llama 3.2, Phi-3), decode and prefill, grouped-query attention, sliding windows, int8, int4 and int2 weights with group scales, int8 and int4 KV cache; custom graphs with `conv2d`, `dwconv2d`, `matmul` and `vector` operators.
- Hardware model: X-HEEP cores (CV32E20, CV32E40X, CV32E40P, CV32E40PX), buses, continuous and interleaved banks, seven external memories with per-node PHY and I/O limits, four process nodes; IHP SG13G2 data measured from the open PDK and from Yosys synthesis.
- Analytical evaluator with placement, bandwidth, per-operator timing and bottleneck attribution, energy, area and feasibility checks; `chipleap require` for inverse sizing.
- Explorer: an explainable agent that names the bottleneck, evaluates every single change and records its reasoning, plus seeded global sampling and a Pareto front; precision as a decision.
- Generator: X-HEEP Python configuration, accelerator SystemVerilog (GEMV, weight-stationary and output-stationary cores, multi-port DMA, double buffering), X-HEEP adapter, FuseSoC core, reggen register description, C driver with the model's tile planner, Verilator testbench, datasheet.
- Verification: bundle consistency, Verilator lint, randomized simulation against a golden model with a cycle cross-check (exact without memory stalls), Yosys synthesis on IHP SG13G2 through sv2v with buffered static timing (the accelerator area model is fitted to it), X-HEEP validation and template rendering of the configuration regenerated from `design.yaml` (a bundle's own Python is never executed).
- Reports in text, Markdown, JSON and self-contained HTML with the decision log, the Pareto front and breakdowns; a to-scale floorplan sketch.
- MCP server with ten tools, a `design_chip` prompt and a spec reference, run logs and `chipleap replay`.
- Website with a Blender render of a generated die and the agent's decisions animated to scale.

[Unreleased]: https://github.com/danieltyukov/chipleap/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/danieltyukov/chipleap/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/danieltyukov/chipleap/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/danieltyukov/chipleap/releases/tag/v0.1.0
