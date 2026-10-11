# How ChipLeap is built

This page is for contributors: what each part does, how data flows between them, and why the main decisions were made. User-facing material lives in the [README](../README.md) and the other docs.

## The loop

```text
spec.yaml ──> workload (operators) ──> evaluate(workload, design) ──> Evaluation
                                              ^                         |
                                              |                         v
                         explore: agent moves + sampling  <──  assess against the spec
                                              |
                                              v
                    recommended design ──> generate ──> bundle ──> verify
                                                                     |
                     model constants  <─── cycle counts, area  <─────┘
```

## Packages

| Package | Role |
|---|---|
| `chipleap.workload` | Operators (`MatMul`, `VectorOp`), LLM presets and their lowering (`llm.py`), custom graphs with convolution lowering (`graph.py`). |
| `chipleap.hw` | Process technologies (`tech.py`), external memories (`memory.py`), X-HEEP cores (`cpu.py`), the design point and its X-HEEP validity rules (`design.py`). |
| `chipleap.model` | The accelerator's tile planner and cycle model (`accel.py`), the SoC evaluator (`evaluate.py`), inverse sizing (`bounds.py`). |
| `chipleap.spec` | Spec parsing and validation. |
| `chipleap.explore` | Scoring against a spec (`score.py`), single-decision moves (`moves.py`), the agent loop, sampling and the Pareto front (`engine.py`). |
| `chipleap.gen` | The X-HEEP configuration (`xheep.py`), RTL, driver and testbench (`rtl.py` and the sources in `rtl/`, `sw/`, `tb/`), register map and datasheet (`datasheet.py`), the bundle writer (`bundle.py`). |
| `chipleap.verify` | Tool discovery, the consistency check, Verilator simulation, Yosys synthesis, X-HEEP checks, and the orchestration behind `chipleap verify`. |
| `chipleap.rtl` | RTL the user wrote: the project file (`project.py`), diagnostics (`diag.py`), lint, the cocotb runner and simulation, coverage, synthesis, place and route, commercial backends, reports and the orchestration behind `chipleap verify PROJECT`. |
| `chipleap.report` | Text, Markdown, JSON and self-contained HTML reports; the floorplan sketch. |
| `chipleap.macro` | Macro packages shared with aispice: the manifest, the schema interpreter, readers for GDS, LEF, liberty, Verilog and SPICE, the validator and evidence writing. The format is [macro-package.md](macro-package.md). |
| `chipleap.mcp_server` | The MCP tools, prompt and resource, the run log and replay. |
| `chipleap.cli` | The command line. |

## Decisions

**One machine, two descriptions.** The accelerator exists twice: as SystemVerilog and as `chipleap.model.accel`. The model is not a separate idealisation; it describes the RTL's own schedule, with constants read off its state machines, and the C driver's tile planner is the Python one transcribed. That is what lets the verifier compare the two cycle for cycle, and what makes the explorer's numbers trustworthy for the accelerator. When the RTL changes, the constants in `accel.py` and the test `tests/test_rtl.py` change with it.

**A fast closed form and an exact schedule.** The explorer evaluates thousands of designs, each with dozens of operators, some with tens of thousands of tiles (a 128k-entry vocabulary). `job_timing` sums over the at most eight distinct tile shapes. `simulate_job` walks the tiles through the loader and the core and is exact. Jobs of up to 256 tiles use the exact one, and a test bounds the error of the closed form on the rest.

**An explainable agent, not a black box.** The agent changes one decision at a time, evaluates every legal change, takes the best and records the hypothesis behind it, the result and the alternatives it rejected. Greedy steps can stall, so a seeded random sample of the whole space runs too, and the agent then polishes the best sampled point. Everything evaluated feeds the Pareto front. This follows the bottleneck-driven approach of Explainable-DSE (Dave, Nowatzki and Shrivastava, ASPLOS 2024), extended to whole-SoC decisions (external memory, process node, core, bus) and tied to generated hardware. An LLM agent reaches the same moves through the MCP tool `explain_bottleneck` and can weigh trade-offs the objective does not encode.

**Thresholds instead of noise.** A move must improve the objective by 1 %, or save 2 % of area while costing at most 0.2 % of the objective, to count as a step. Without that the agent spends steps on improvements no one would act on.

**Feasibility in words.** A design that cannot be built is still evaluated, and its violations are sentences ("no LPDDR4 PHY is known at 130 nm"). The agent prefers fewer violations, then smaller target misses, then the objective. So it can climb out of an infeasible start and explain what it fixed.

**Placement by reuse.** Activations are touched several times per step, then the KV cache, then weights once each, so on-chip SRAM is filled in that order. For decode all three are read once per token, which is why the order matters less there than for prefill.

**X-HEEP's rules, enforced early.** `Design.validate` encodes the X-HEEP constraints the generator would reject: at most 16 banks, interleaved banks only on the NtoM bus, power-of-two sizes. The explorer never produces a configuration X-HEEP refuses, and `chipleap verify` confirms it with X-HEEP's own code.

**Open tools only, and say when they are missing.** Every verification step runs on open-source tools and skips itself with a reason when its tool is absent. Yosys 0.33 cannot read the RTL's SystemVerilog directly, so synthesis goes through sv2v; the RTL avoids constructs both cannot handle (2-D unpacked arrays are flattened, for instance).

**Your RTL through the same steps.** A user's design is described by a project file of its own, not by a kind of spec or design, because it has neither a workload nor a design point and lives in the user's repository. Its steps return the same `StepResult` as the accelerator's and share the place-and-route signoff settings (`verify.pnr.signoff`), so a change to signoff applies to both. Simulation is cocotb only: one test suite runs on Icarus, Verilator and the three commercial simulators. Every problem comes back as a `Diagnostic` with a file and line relative to the project, which is what lets an assistant fix RTL in a loop and lets `chipleap replay` compare results across machines.

**Data with provenance.** Every technology number either comes from the IHP SG13G2 PDK (measured, `scripts/calibrate_sg13g2.py`) or is scaled from a cited source and labelled as such. LLM shapes are copied from each model's `config.json`, with the URL in the code.

## Adding things

- **A model preset:** add an `LLMArch` to `PRESETS` in `workload/llm.py` with its `config.json` URL, and its published parameter count to `tests/test_workload.py`.
- **A technology:** add a `Technology` in `hw/tech.py` with a `note` saying where its numbers come from; add a row to [model.md](model.md).
- **An external memory:** add an `ExternalMemory` in `hw/memory.py`, including `phy_max_node_nm` and `min_mts`.
- **A move:** add it to `explore/moves.py` with a `kind`, a label and the hypothesis an engineer would state; make sure it produces a valid `Design`.
- **An RTL change:** keep `model/accel.py` in step, run `pytest tests/test_rtl.py`, and check that the cycle error without stalls is still zero.
- **A project section** (formal properties, macros, a pad ring): a frozen dataclass and a parser in `rtl/project.py`, a field on `Project` with a default, the key in `SECTIONS` and out of `RESERVED`; `input_files` lists any file it names, so replay notices when it changes.
