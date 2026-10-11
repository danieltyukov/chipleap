<p align="center">
  <img src="assets/logo.svg" alt="ChipLeap" width="280">
</p>

<p align="center"><b>Tell it the model and the speed you need. ChipLeap designs the chip,<br>explains every choice, writes the RTL and checks it.</b></p>

<p align="center">
  <a href="https://github.com/danieltyukov/chipleap/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/danieltyukov/chipleap/ci.yml?branch=main&label=CI" alt="CI status"></a>
  <a href="https://pypi.org/project/chipleap/"><img src="https://img.shields.io/pypi/v/chipleap" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.10-brightgreen" alt="Python 3.10 or newer">
</p>

<p align="center">
  <a href="https://danieltyukov.github.io/chipleap/">Website</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/">Docs</a> ·
  <a href="CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <img src="docs/img/die.jpg" alt="A rendered die that ChipLeap designed for SmolLM2 135M on the IHP 130 nm open PDK, with SRAM, a RISC-V core, a GEMV engine and an octal PSRAM interface" width="760">
  <br><sub>A chip ChipLeap designed for SmolLM2 135M on the open IHP 130 nm PDK, rendered to scale from its floorplan.</sub>
</p>

## What it does

You describe a workload (say, Llama 3.2 1B decoding), a target (5 tokens per second) and your limits (area, power, process nodes). ChipLeap works out the rest:

1. **Sizes the problem.** The bandwidth, compute and memory the target needs, and which memories and nodes can deliver them.
2. **Designs the architecture.** An agent finds the bottleneck, tries every single change, keeps the best one and writes down why. It chooses the on-chip memory, the external memory, the dataflow, the array size, the precision, the CPU, the clock and the node.
3. **Generates the hardware.** Accelerator RTL in SystemVerilog, an X-HEEP RISC-V SoC configuration or AXI4 IP for any host, a C driver, a testbench and a datasheet.
4. **Checks it.** Simulation against a golden model, synthesis, place and route, and the SoC generator's own checks, on open-source tools or the commercial ones you already have.

<p align="center">
  <img src="docs/img/decisions.gif" alt="The agent's decisions for Llama 3.2 1B: each step's floorplan drawn to the same scale, from a bare microcontroller to a 16 nm design with a GEMV engine and LPDDR5" width="760">
  <br><sub>The agent's decisions for Llama 3.2 1B at 5 tokens per second. Every floorplan is drawn to the same scale.</sub>
</p>

## Quick start

```sh
pip install chipleap                # or: uv tool install chipleap
chipleap init llama.yaml            # an example spec to edit
chipleap explore llama.yaml -o out  # decisions, Pareto front and the hardware in out/
chipleap verify out                 # simulate, synthesize and check it
```

Open `out/report.html` to see each decision, the Pareto front and where the time goes.

<p align="center"><img src="docs/img/report.png" alt="The HTML report: the recommended design, the Pareto front of 2,154 evaluated designs and the agent's progress toward the target" width="560"></p>

## Ask your AI assistant

ChipLeap is also an MCP server, so Claude, Cursor, VS Code or Codex can drive it:

```sh
claude mcp add chipleap -s user -- chipleap mcp
```

Then ask things like *"Design the smallest chip on the IHP open PDK that runs SmolLM2 135M at two tokens per second"* or *"Why is this design memory bound, and what is the cheapest fix?"*. [docs/mcp.md](docs/mcp.md) covers the other clients.

## Works with what you have

- **Any workload:** twelve LLM presets (SmolLM2, Qwen, Gemma, Llama, Phi, TinyLlama), or your own network of convolutions and matrix multiplies.
- **Any host:** a complete X-HEEP RISC-V SoC, or the accelerator alone as AXI4 IP for an Arm, RISC-V or FPGA SoC ([platforms](docs/platforms.md)).
- **Open tools:** Verilator, Yosys and LibreLane (OpenROAD, KLayout, Netgen) on the IHP SG13G2 open PDK.
- **Commercial tools:** Cadence Xcelium and Genus, Synopsys VCS and Design Compiler, Siemens Questa, on your machine or on a design server over ssh ([guide](docs/commercial-tools.md)).
- **Your own PDK:** `chipleap tech add` measures it from its liberty files and keeps the numbers on your machine.

Every tool is optional. `chipleap doctor` lists what it found, and `chipleap verify` skips what is missing and says why.

## Down to the layout

<img src="docs/img/layout.jpg" alt="The routed layout of the 1 x 8 GEMV engine on IHP SG13G2, drawn from its GDS: signal wires on four metal layers under the power grid" width="300" align="right">

`chipleap verify out --steps all` adds place and route with LibreLane, the open flow Tiny Tapeout uses for its IHP shuttles. The picture is the GDS it wrote for the SmolLM2 chip's 1 x 8 GEMV engine: a 0.80 mm² core with clean DRC, LVS and antenna checks that meets its 90 MHz clock at the slow corner, with room to spare (about 101 MHz, and 148 MHz at the typical one).

The accelerator model is the same tile schedule the RTL runs, so it is checked against it:

- **Cycles:** equal to simulation without memory stalls, within a few percent with random stalls, on Verilator, Xcelium, VCS and Questa alike.
- **Area:** logic area within 3 % on average of synthesis on IHP SG13G2, across 12 configurations.
- **Clock:** the model allows what the routed accelerator meets at the slow corner: 100 MHz with one port, less with more ports or a wider GEMV engine.

The other nodes are scaled from published figures and say so. [docs/model.md](docs/model.md) lists every number and its source; [docs/verification.md](docs/verification.md) explains each check.

## Learn more

| Guide | What it covers |
|---|---|
| [Spec format](docs/spec.md) | Workloads, targets, constraints and what the explorer may change |
| [The model](docs/model.md) | Timing, energy and area, with sources |
| [Verification](docs/verification.md) | Simulation, synthesis, place and route, X-HEEP checks |
| [Platforms](docs/platforms.md) | X-HEEP or AXI4 IP, and how to integrate each |
| [Commercial tools](docs/commercial-tools.md) | Cadence, Synopsys and Siemens tools, remote servers, your own PDK |
| [X-HEEP integration](docs/xheep.md) | How the accelerator plugs into the SoC |
| [Where it fits](docs/landscape.md) | Agentic EDA, workload-to-chip services and academic tools, compared |

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). `pytest` runs the suite, and the hardware tests run when their tools are installed.

## License

MIT. Generated X-HEEP configurations carry X-HEEP's Apache-2.0 header. ChipLeap is an independent project, not affiliated with EPFL, IHP or any company named here.
