# D1: bring your own RTL

Status: accepted, 2026-10-11. Milestone D1 of `chip-design-roadmap.md`.

## Goal

Every step after architecture works on any user design, not only on the accelerators ChipLeap generates. A user points ChipLeap at a directory of Verilog or SystemVerilog with cocotb tests and gets lint, simulation, coverage, synthesis and place and route on IHP SG13G2, with the same reporting, the same commercial backends and the same MCP access as a generated bundle.

## Decisions

### A separate file, not a new spec kind

A user RTL project is described by `project.yaml`, a file of its own, and not by a new kind of `spec.yaml` or `design.yaml`.

- A spec says what a workload needs and a design says how the generated accelerator is built. A user design has neither a workload nor a design point, so neither file has anything to say about it.
- The project file lives next to the user's RTL, in their repository, and changes with it. Bundles are written by ChipLeap and regenerated.
- `chipleap verify PATH` tells the two apart by file: a directory with `project.yaml` (or a path to a file whose `schema` is `project/v1`) is a project, a directory with `design.yaml` is a bundle. `chipleap init` keeps writing `chipleap.yaml` specs, so the names never collide.
- A later milestone can give generated bundles a `project.yaml` too (D3 routes the whole SoC through the project path).

### The format

```yaml
schema: project/v1            # optional on input, written by `chipleap project init`
name: uart                    # letters, digits, _ . -
description: optional sentence
top: uart_top
sources: [rtl/uart_tx.sv, rtl/uart_rx.sv, rtl/uart_top.sv]   # globs allowed, sorted
filelist: rtl/files.f         # optional, in addition to sources: -f, +incdir+, +define+
include_dirs: [rtl/include]
defines: {SYNTHESIS: null, WIDTH: 8}
parameters: {CLKS_PER_BIT: 16}  # top-level parameter overrides
timescale: 1ns/1ps            # for sources without `timescale, in simulation
clocks:
  - {port: clk, period_ns: 20}
resets:
  - {port: rst_n, active: low, sync: false}
tech: ihp-sg13g2              # a key from `chipleap tech list`
tests:
  dir: test
  modules: [test_uart]        # default: every test_*.py in dir
  simulator: auto             # auto | icarus | verilator
  seed: 1
  toplevel: uart_top          # default: top
  sources: []                 # testbench-only HDL, simulated but never synthesized
golden: model/uart_model.py   # optional Python reference model for the tests
lint:
  waivers: [rtl/waivers.vlt]  # Verilator configuration files
  slang: true                 # run slang too when it is installed
constraints:
  sdc: constraints/uart.sdc   # optional; replaces the generated SDC
pnr:
  utilisation: 45             # FP_CORE_UTIL, percent
  density: 55                 # PL_TARGET_DENSITY_PCT, percent
  die_um: [300, 300]          # optional fixed die
  librelane: {}               # further LibreLane keys, passed through
```

Paths are relative to the project file. Every key is validated, and an error names the key, as `spec.yaml` errors do. Unknown keys are errors.

Section names reserved for later milestones are rejected with a message that says so, so a file written for a later ChipLeap fails clearly on this one:

| Milestone | Reserved keys |
|---|---|
| D2 | `formal`, `equivalence`, `cdc`, `gate_sim` |
| D3 | `macros`, `dft`, `power`, `ir_drop`, `upf`, `eco` |
| D4 | `chip` (pad ring, seal ring, fill, precheck) |

A milestone adds a section by writing a frozen dataclass and a parser in `chipleap.rtl.project`, a field on `Project` with a default, and an entry in `SECTIONS`; then it removes the key from `RESERVED`.

The golden model is a Python file. Its directory goes on the tests' `PYTHONPATH` and its path is in `CHIPLEAP_GOLDEN`, so a test imports it by module name. Reports name it, so a reader knows what the tests compare against.

### Steps

`chipleap verify PROJECT` runs `lint,sim,coverage,synth` by default; `pnr` runs when named or with `--steps all`. Each step returns the existing `StepResult` and skips itself with a reason when its tool is missing. Work directories are under `PROJECT/build/`, results in `PROJECT/build/verify/` (`verify.json`, `VERIFY.md`, `report.html`).

| Step | Engine | Passes when |
|---|---|---|
| lint | `verilator --lint-only -Wall` with the project's top, includes, defines, parameters and waivers; slang as a second front end when installed | no warnings or errors from any front end |
| sim | the project's cocotb tests on Icarus or Verilator (auto prefers Icarus, which is four-state; Verilator must be 5.036 or newer for cocotb 2) | every test passes |
| coverage | the same tests on Verilator with `--coverage`; line (Verilator's line and branch points) and toggle summaries per file, plus the uncovered points | the run completes; coverage is reported, not judged |
| synth | Yosys on the technology's liberty, through sv2v when it is installed: cell area, cells, flops, ABC's critical path and the clock it allows, the netlist | Yosys maps the design |
| pnr | LibreLane with a configuration and SDC generated from the project, on the accelerator's signoff settings | setup met at the slow corner, hold at every corner, DRC, LVS and antenna clean |

Notes on the choices:

- **Lint fails on any warning**, as for the generated accelerator. Style warnings a project accepts go in a waiver file, which keeps the decision in the repository.
- **Simulation is cocotb only.** cocotb runs on every simulator the commercial backends support (Xcelium, VCS, Questa) and on both open ones, so one test suite serves all of them. A Python runner script (`chipleap/rtl/cocotb_run.py`) that imports only cocotb drives `cocotb_tools.runner`; it runs in a subprocess with whichever Python has cocotb (this one, `cocotb-config --python-bin`, or `CHIPLEAP_COCOTB_PYTHON`), so ChipLeap does not need cocotb in its own environment, and the same script runs on a design server.
- **Synthesis aims ABC at half the fastest clock period.** This is the accelerator's rule (5 ns for 10 ns): routing and the slow corner roughly double a synthesized path on SG13G2. The Fmax figure is a typical-corner estimate and the report says so; the routed number comes from `pnr`.
- **Place and route reuses `pnr.py`.** The shared settings (slow-corner optimisation, post-route repair, slack margins, antenna repair, KLayout DRC instead of Magic) move into one function both flows call, so a change to signoff applies to both. Margins scale with the clock for projects (8 % and 16 % of the period, capped at the accelerator's 0.8 and 1.6 ns). The SDC mirrors LibreLane's base SDC, extended to several clocks; a project's own SDC replaces it. Sources reach the container through sv2v as one file, or copied with their include directories when sv2v is absent, because the LibreLane wrapper mounts only the run directory.
- **Antenna violations fail the project's `pnr` step.** The roadmap's acceptance asks for clean antenna checks. The accelerator step is unchanged.

### Commercial parity

The existing `--sim-tool`, `--synth-tool`, `--pnr-tool`, `--remote` and `--remote-setup` options apply to projects:

- `--sim-tool xcelium|vcs|questa` runs the same cocotb runner with that simulator, here or on the server. The project's sources and tests are copied into the work directory, which `Remote.run` copies to the server. The server needs a Python with cocotb on the path after the setup snippet.
- `--synth-tool genus|dc` writes Genus or Design Compiler scripts for the project's top, includes, defines and parameters, with the project's clocks.
- `--pnr-tool innovus` runs Genus then Innovus with the existing `cpnr.script`, given the project's top.

As before, the MCP server never chooses a host or a setup command.

### Agent support

Lint, simulation and synthesis return structured diagnostics:

```python
Diagnostic(tool, severity, message, file, line, column, code, test)
```

Parsers exist for Verilator, slang, Icarus, Yosys, sv2v, Xcelium, VCS and Questa messages and for cocotb's `results.xml`. File names are relative to the project. A failing cocotb test becomes a diagnostic with the test's name and the deepest frame of the traceback inside the project.

New MCP tools: `describe_project`, `lint_rtl`, `simulate_rtl`, `coverage_rtl`, `synthesize_rtl` and `verify_rtl`. All but `verify_rtl` are replayable: their results contain no absolute paths, wall times, host names or timestamps, and cocotb runs with the project's seed. Their log entries also carry `inputs_sha256`, a digest of the project file and every file it names, and the versions of the tools used. `chipleap replay` re-runs them and, when a result differs, says whether the inputs or the tools changed since the call was logged.

### Security

Verifying a project runs its cocotb tests, which are Python, and compiles its RTL. That is the point of the step, so it is not sandboxed: verify projects you trust. The commercial host and setup still come only from the user's command line or environment.

A project file may still come from a repository the user did not write, and its values reach tool scripts, command lines and results an assistant reads. Two rules, added after review:

- **Confinement.** Every path resolves, symlinks followed, inside the project directory, or inside a directory the user allows with `--allow-path` or `CHIPLEAP_ALLOW_PATHS`; nothing in the project file or an MCP argument widens it. No environment variable or `~` expansion. Paths are checked before they are opened or listed and only the resolved, checked path is used afterwards. Globs may not be absolute or hold `..` and never walk through a symlink out of the project. Every `` `include `` is followed through the include directories and must be a quoted relative name without `..` whose every candidate location is inside. Copies to a server and input digests take only confined files, outside build directories. This protects against disclosure through ChipLeap's output; it does not sandbox the project's tests, golden model or RTL, which are code.
- **No script syntax.** Names are identifiers without `$`, parameter values integers, define values short literals, paths free of quotes, backslashes, braces, brackets, `$`, `;`, backticks, `#`, globs and control characters, and no name starts with `-` or `+`. Every script writer quotes each value again (`chipleap.rtl.quote`: double quotes for Yosys, braces for Tcl) and refuses what it cannot quote.

## Public API for later milestones

- `chipleap.rtl.project`: `PROJECT_FILE`, `SCHEMA`, `ProjectError`, `Project`, `Clock`, `Reset`, `Tests`, `LintConfig`, `PnrConfig`, `SECTIONS`, `RESERVED`, `load_project`, `parse_project`, `find_project`, `init_project`; `Project.rel`, `Project.build_dir`, `Project.input_files`, `Project.inputs_digest`.
- `chipleap.rtl.diag`: `Diagnostic` and the `parse_*` functions.
- `chipleap.rtl.lint.lint`, `chipleap.rtl.sim.run_tests` and `TestResult`, `chipleap.rtl.coverage.summarize`, `chipleap.rtl.synth.synthesize` (writes the mapped netlist D2's equivalence check reads), `chipleap.rtl.pnr.config`, `sdc` and `place_route`.
- `chipleap.rtl.verify.verify_project`, `PROJECT_STEPS`, `DEFAULT_STEPS`.
- `chipleap.verify.result.StepResult` (moved from `run.py`, still importable there).

## Acceptance

- `examples/uart` runs `chipleap verify examples/uart --steps all` to a routed GDS on IHP SG13G2 that is clean for DRC, LVS and antenna, with timing met at the slow corner.
- A project with a failing cocotb test reports `fail` with the test's name (`tests/test_project_sim.py`).
- Existing accelerator tests and `tests/test_rtl.py` pass unchanged.
- The RTL MCP tools are covered by `chipleap replay` (`tests/test_project_mcp.py`).

## Out of scope

Formal, equivalence, CDC and gate-level simulation (D2); SRAM macros, DFT, power and IR drop (D3); pad rings (D4); VHDL; Verilator's own C++ testbenches as a test format.
