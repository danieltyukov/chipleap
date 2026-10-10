<p align="center">
  <img src="assets/logo.svg" alt="ChipLeap" width="300">
</p>

<p align="center">From workload to RISC-V chip, with every decision explained.<br>An explainable design agent, an X-HEEP SoC generator and RTL verification on open tools.</p>

<p align="center">
  <a href="https://github.com/danieltyukov/chipleap/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/danieltyukov/chipleap/ci.yml?branch=main&label=CI" alt="CI status"></a>
  <a href="https://github.com/danieltyukov/chipleap/releases"><img src="https://img.shields.io/github/v/release/danieltyukov/chipleap" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.10-brightgreen" alt="Python 3.10 or newer">
</p>

<p align="center">
  <a href="https://danieltyukov.github.io/chipleap/">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/">Docs</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

## What it does

ChipLeap works on the step before RTL: deciding what to build. You describe a workload (a language model such as Llama 3.2 1B, or a network of convolutions and matrix multiplies) and what you need from it (tokens per second, latency, area, power, the process nodes and memories you can use). ChipLeap then:

- turns the target into hard requirements: external bandwidth, MAC rate, capacity and CPU work, and which memory interfaces and nodes can deliver them;
- explores the design space with an agent that names the bottleneck, tries every single change, keeps the best and writes down why, plus a seeded global search and a Pareto front;
- decides the memory hierarchy (X-HEEP SRAM banks, interleaving, what lives on chip), the external memory (QSPI flash, octal PSRAM, HyperRAM, LPDDR2 to LPDDR5, channels), the dataflow (weight-streaming GEMV, weight-stationary or output-stationary systolic), the compute array, the weight precision, the RISC-V core, the bus and the clock;
- generates an X-HEEP SoC configuration, the accelerator in SystemVerilog with an X-HEEP adapter, a register map, a C driver, a Verilator testbench and a datasheet;
- verifies them: randomized simulation against a golden model with a cycle cross-check, Yosys synthesis on the IHP SG13G2 open PDK, X-HEEP's own configuration checks, and a check that every generated file still agrees with the design.

It runs on your machine, needs no licence, and works as an MCP server for Claude Code, Claude Desktop, Cursor, VS Code, Codex or any other MCP client.

```text
$ chipleap explore examples/llama-3.2-1b-decode.yaml
Baseline: cv32e20 @ 100 MHz, onetoM bus, 2x32 KiB, no accelerator, hyperram, ihp-sg13g2
  0.0097 token/s, 1.03e+05 ms per step, 854 mJ/token, 8.29 mW, 3.53 mm2
Agent trace:
 1. technology ihp-sg13g2 -> generic-65nm, 200 MHz, lpddr2-x32: a newer node allows faster
    clocks and memory interfaces at lower energy. Result: resolves external data 642 MiB
    exceeds 1 x 64 MiB of HyperRAM (HyperBus x8), throughput 0.0097 -> 0.0194 token/s ...
 2. add a gemv 4x8 accelerator with 2 ports: matrix multiplies dominate and the CPU does
    them one MAC at a time. Result: throughput 0.0194 -> 1.71 token/s, energy 327 -> 86.2
    mJ/token, area 2.92 -> 3.49 mm2, bottleneck moves from compute to accelerator ports.
 ...
 5. external memory lpddr4x-x32 -> lpddr5-x16: a slower interface can be cheaper in area
    and energy per bit. Result: energy 27.9 -> 22.5 mJ/token, area 1.34 -> 0.999 mm2.
 ...
Recommended: cv32e20 @ 800 MHz, NtoM bus, 2x16 KiB + 2x16 KiB interleaved, gemv 4x8 int8,
  2 ports, lpddr5-x16, generic-16nm, weights int4
  6.19 token/s, 162 ms per step, 22.5 mJ/token, 139 mW, 0.973 mm2
  bottleneck: accelerator ports
```

## Quick start

```sh
uv tool install git+https://github.com/danieltyukov/chipleap    # or: pip install git+...
chipleap init llama.yaml             # an example spec
chipleap require llama.yaml          # what the target demands, before any search
chipleap explore llama.yaml -o out   # decisions, Pareto front, SoC bundle in out/
chipleap verify out                  # lint, simulate, synthesize, check with X-HEEP
```

Open `out/report.html` for the decision log, the Pareto front and where the time goes.

## Install

Requires Python 3.10 or newer. The analytical model, the explorer, the generators and the MCP server need nothing else.

`chipleap verify` uses whichever of these are installed, and skips the rest with a reason:

| Tool | Used for |
|---|---|
| [Verilator](https://verilator.org) 5 and a C++ compiler | lint and cycle-accurate simulation of the accelerator |
| [Yosys](https://yosyshq.net/yosys/) and [sv2v](https://github.com/zachjs/sv2v) | logic synthesis (sv2v lets the Yosys in Ubuntu 24.04 read the RTL) |
| [IHP SG13G2](https://github.com/IHP-GmbH/IHP-Open-PDK) open PDK | standard cells for synthesis (`CHIPLEAP_LIBERTY` or `PDK_ROOT`) |
| [X-HEEP](https://github.com/esl-epfl/x-heep) checkout with `hjson` and `mako` | X-HEEP's own validation and template rendering (`--fetch-xheep` clones it) |

`chipleap doctor` shows what it found. From source:

```sh
git clone https://github.com/danieltyukov/chipleap.git
cd chipleap && pip install -e ".[dev,xheep]" && pytest
```

## A spec

```yaml
name: llama-1b-edge
workload:
  llm: {model: llama-3.2-1b, phase: decode, context: 1024, weights: int4, kv: int8}
targets:
  throughput: 5            # tokens per second
constraints:
  area_mm2: 8
  power_mw: 400
  tech: [ihp-sg13g2, generic-65nm, generic-28nm, generic-16nm]
allow:
  weights: [int8, int4]    # let the explorer choose the precision
objective: energy          # energy | area | throughput | edp
```

Twelve LLM presets ship with shapes taken from each model's `config.json` (SmolLM2, Qwen2.5, Qwen3, Gemma 3, TinyLlama, Llama 3.2, Phi-3). Custom networks are lists of `conv2d`, `dwconv2d`, `matmul` and `vector` operators. The full format is in [docs/spec.md](docs/spec.md); [examples/](examples/) has an open-PDK language model, a prefill latency target and an always-on keyword spotter.

## Commands

| Command | What it does |
|---|---|
| `chipleap require SPEC` or `-m MODEL -t N` | Bandwidth, MAC rate, capacity and CPU work a target demands, and which memories and nodes can meet it. |
| `chipleap explore SPEC [-o DIR]` | Agent loop plus seeded global search. Prints the decisions; with `-o` writes the report and the SoC bundle. `-v` shows the diagnosis and the rejected alternatives at every step. |
| `chipleap evaluate SPEC DESIGN` | One design against a spec, with a per-operator breakdown. |
| `chipleap generate DESIGN -o DIR` | The SoC bundle for a design you wrote or edited. |
| `chipleap verify DIR` | Consistency, lint, simulation, synthesis and X-HEEP checks of a bundle. |
| `chipleap run SPEC -o DIR` | Explore, generate and verify in one go. |
| `chipleap workload`, `models`, `catalog`, `doctor` | What a workload needs, the presets, the technology and memory data with sources, the tools found. |
| `chipleap mcp [--log-dir DIR]`, `chipleap replay LOG` | The MCP server, and a re-run of a logged session to check it reproduces. |

## The bundle

```text
out/
  report.html, report.md   decisions, Pareto front, time, area and energy breakdowns
  explore.json             everything above, machine-readable, including the decision log
  design.yaml, spec.yaml   the design point and the requirements it answers
  DATASHEET.md             memory map, registers, interfaces, programming sequence
  xheep/<name>.py          X-HEEP configuration: make mcu-gen X_HEEP_CFG=xheep/<name>.py
  rtl/                     chipleap_accel.sv, X-HEEP adapter, FuseSoC core, reggen hjson
  sw/chipleap_driver.h     firmware driver with the tile planner
  tb/tb_chipleap.cpp       Verilator testbench
  verify/                  VERIFY.md and verify.json after chipleap verify
```

[docs/xheep.md](docs/xheep.md) explains how the accelerator plugs into X-HEEP.

## Use it from your assistant

```sh
claude mcp add chipleap -s user -- chipleap mcp
```

Claude Desktop, Cursor, VS Code and Codex take the usual stdio entry (`"command": "chipleap", "args": ["mcp"]`); [docs/mcp.md](docs/mcp.md) has each. The tools answer architecture questions: `required_resources`, `explore_designs`, `explain_bottleneck` (the effect of every single change, best first), `evaluate_design`, `describe_workload`, `generate_soc`, `verify_soc`, plus `list_models`, `catalog` and `check_tools`. A `design_chip` prompt walks an assistant through the loop and `chipleap://spec-reference` documents the formats. With `--log-dir` every call goes into a JSONL run log that `chipleap replay` re-runs and compares.

Things to ask once it is connected:

- What memory bandwidth does Llama 3.2 1B need for ten tokens per second, and which memories can deliver it?
- Design the smallest chip on the IHP open PDK that runs SmolLM2 135M at two tokens per second.
- Why is this design memory bound, and what is the cheapest change that fixes it?
- Compare int4 and int8 weights for Qwen3 0.6B prefill on 28 nm.

## How good are the numbers

The accelerator model is the tile schedule the generated RTL runs. On this release:

- Simulation: randomized jobs on all three dataflows are bit-exact against a golden model, and the predicted cycle count equals the simulated one exactly without memory stalls, and stays within a few percent with random stalls.
- The fast closed form the explorer uses is within 0.1 % on average of that exact schedule.
- The C driver plans the same tiles as the model on 6,000 random shapes.
- The IHP SG13G2 technology data comes from the PDK: SRAM macro areas within 2 % and read energies within 10 % of the liberty files.
- Synthesis: the accelerator's logic area model is within 4 % on average (10 % at worst) of Yosys on SG13G2 across 11 configurations, and the buffered netlists reach 136 to 198 MHz at the typical corner, above the 100 MHz the model allows on that node.
- X-HEEP's own generator validates the generated configurations and renders all its templates for them.

The other process nodes are scaled from published figures (Horowitz, ISSCC 2014) and say so; external memory energy is good to perhaps a factor of 1.5; CPU cycle counts per kernel are estimates. [docs/model.md](docs/model.md) lists every number and where it comes from, and [docs/verification.md](docs/verification.md) how each check works.

## Where it fits

Agentic EDA products (Cadence ChipStack, Synopsys AgentEngineer, Siemens Fuse) and startups such as ChipAgents, Chipmind and SigmanticAI mostly start from a specification, existing RTL or simulation results, on licensed tools and PDKs. ChipLeap produces the kind of specification they start from. Workload-to-chip services such as Architect Labs and Cognichip are closed. Academic tools (Timeloop, ZigZag, LLMCompass) model accelerators or datacenter systems without generating hardware. ChipLeap's bottleneck-driven loop follows prior work such as Explainable-DSE (Dave et al., ASPLOS 2024). [docs/landscape.md](docs/landscape.md) has the comparison with sources.

## Roadmap

- Requantization and activation functions in the accelerator's output path, so the CPU no longer handles them.
- int4 group scales applied in the PE array (today they are counted in traffic and storage, and applied on the CPU).
- Place and route of the accelerator on IHP SG13G2 with LibreLane, feeding post-route area, timing and power back into the model.
- A full-system X-HEEP simulation running one transformer layer end to end.
- Workload mixes (prefill and decode together) as one spec.

## Contributing

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). Run `pytest` before opening a pull request; the RTL tests run when Verilator is installed.

## License

MIT. Generated X-HEEP configurations follow X-HEEP's Apache-2.0 header. ChipLeap is an independent project, not affiliated with EPFL, IHP or any company named here.
