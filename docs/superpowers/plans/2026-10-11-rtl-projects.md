# Plan: D1, bring your own RTL

Spec: `docs/superpowers/specs/2026-10-11-rtl-projects-design.md`. Each task is test first, then code, then `ruff check`, `ruff format --check`, `mypy` and the touched tests, then one commit.

## Tools installed outside the repo

- Verilator 5.052 in `~/.local/opt/verilator-5.052` (cocotb 2 needs 5.036 or newer; the system's 5.020 stays the default). Selected with `CHIPLEAP_VERILATOR`.
- slang 9.1 in `~/.local/opt/slang-9.1`, selected with `CHIPLEAP_SLANG` or the path.
- cocotb 2 in the dev environment (`pip install -e ".[dev]"`), and as the extra `chipleap[rtl]`.

## Tasks

1. **StepResult in its own module.** `chipleap/verify/result.py`; `run.py` imports it. No behaviour change.
2. **Project format.** `chipleap/rtl/project.py`: dataclasses, `parse_project` with key-path errors, reserved sections, filelists (`-f`, `+incdir+`, `+define+`, comments, `${VAR}`), globs, `input_files`, `inputs_digest`, `init_project` with top, clock and reset guesses. Tests: `tests/test_project.py`.
3. **Diagnostics.** `chipleap/rtl/diag.py`: `Diagnostic` and parsers for Verilator, slang (JSON and text), Icarus, Yosys, sv2v, Xcelium, VCS, Questa. Fixture strings in `tests/test_project_diag.py`.
4. **Lint.** `chipleap/rtl/lint.py`: Verilator `-Wall` plus slang when present. Real-tool tests on small fixture projects.
5. **Simulation.** `chipleap/rtl/cocotb_run.py` (standalone runner) and `chipleap/rtl/sim.py`: manifest, interpreter discovery, simulator choice, Verilator version gate, results.xml parsing, compile diagnostics, staging for remote runs. Tests: a passing and a failing fixture on Icarus; Verilator when 5.036 or newer is available.
6. **Coverage.** `chipleap/rtl/coverage.py`: `coverage.dat` parser, per-file line and toggle summaries, uncovered points. Parser fixture test; real run gated on Verilator.
7. **Synthesis.** `chipleap/rtl/synth.py`: sv2v, Yosys script with chparam, ABC target at half the fastest period, area, cells, flops, critical path, netlist, Yosys diagnostics. Real test on a fixture, gated on Yosys and the PDK.
8. **Place and route.** Move the shared signoff keys of `pnr.config` into `pnr.signoff` and the LibreLane run into `pnr.run_librelane`; `chipleap/rtl/pnr.py` builds the config and SDC from a project. Unit tests for the config and SDC; a real run gated on `CHIPLEAP_RUN_PNR`.
9. **Orchestration and reports.** `chipleap/rtl/verify.py` (`verify_project`), `chipleap/rtl/report.py` (HTML and Markdown). CLI: `chipleap project init`, `chipleap project check`, `chipleap verify PROJECT` with step defaults for projects and `--sim-tool icarus`.
10. **Commercial parity.** `chipleap/rtl/commercial.py`: cocotb on Xcelium, VCS and Questa through `run_steps`; Genus and Design Compiler scripts for a project; Innovus through `cpnr.script(top=...)`. Script and parser tests without the tools.
11. **MCP and replay.** Tools `describe_project`, `lint_rtl`, `simulate_rtl`, `coverage_rtl`, `synthesize_rtl`, `verify_rtl`; run-log entries with `inputs_sha256` and tool versions; replay explains a difference. Tests over the in-process client.
12. **Example.** `examples/uart`: transmitter, receiver, top, cocotb tests, a Python golden model. Run `chipleap verify examples/uart --steps all` for real (LibreLane in the background) and record DRC, LVS, antenna and timing.
13. **Docs and CI.** `docs/projects.md`, README positioning, `docs/verification.md`, `docs/mcp.md`, `docs/commercial-tools.md`, CHANGELOG under Unreleased. CI installs Icarus and cocotb and runs the project tests and `chipleap verify examples/uart --steps lint,sim,synth`.
14. **PR.** Rebase on origin/main, full checks, push, open the PR, watch CI.
