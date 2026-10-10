# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

[Unreleased]: https://github.com/danieltyukov/chipleap/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/danieltyukov/chipleap/releases/tag/v0.1.0
