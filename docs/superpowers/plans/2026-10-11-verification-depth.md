# Plan: D2, verification depth

Spec: `docs/superpowers/specs/2026-10-11-verification-depth-design.md`. Each task is test first, then code, then `ruff check`, `ruff format --check`, `mypy` and the touched tests, then one commit.

## Tools installed outside the repo

- SymbiYosys and EQY from the `yosys-0.33` tags in `~/.local/opt/yosyshq-0.33` (`scripts/install_formal_tools.sh`, needs `yosys-dev`); selected with `CHIPLEAP_SBY` and `CHIPLEAP_EQY`. The older `/usr/local/bin/sby` stays as it is.
- Icarus Verilog 13 (Tiny Tapeout's build) in `~/.local/opt/iverilog-13` (`scripts/install_iverilog13.sh`); selected with `CHIPLEAP_IVERILOG`.
- z3 from the distribution as the default solver.

## Tasks

1. **Sections.** `chipleap/rtl/project.py`: `FormalConfig`, `EquivConfig`, `CdcConfig`, `GateSimConfig` parsed from `formal`, `equivalence`, `cdc`, `gate_sim`; the keys leave `RESERVED`; formal sources and `.sby` files confined and scanned; `to_dict` round trip. Tests: `tests/test_project_verif.py` (valid sections, every refusal with its key path).
2. **Tools and confinement.** `verify/tools.py`: `sby()`, `eqy()`, an Icarus version helper; `chipleap doctor` rows. `rtl/confine.py`: a script tool's interpreter is readable too. Tests in `tests/test_project_security.py`.
3. **VCD reader.** `chipleap/rtl/vcd.py`: a small reader that turns a counterexample into steps and values for the top scope. Tests on a fixture VCD in the shape smtbmc writes.
4. **Formal.** `chipleap/rtl/formal.py`: `.sby` writer, runner (confined), log parser (status, failed assertions, covers, traces), the step. Fixtures: `tests/fixtures/formal/fifo.sv` (proves) and a broken copy (fails with a trace); cover reached and unreached. Tests: `tests/test_project_formal.py`.
5. **Proposed assertions.** `formal.check_assertions`: expression validation, module copy with the inserted block, label mapping. Tests: accepted and refused expressions; one holding and one failing proposal on the FIFO.
6. **Equivalence.** `chipleap/rtl/equiv.py`: `.eqy` writer for `rtl-synth`, `synth-pnr`, `rtl-pnr`; runner (confined); log parser; the step. Tests: the counter proves; a netlist with one changed gate fails with the partition named; `synth-pnr` skips without a routed run.
7. **CDC and RDC.** `chipleap/rtl/cdc.py`: the Yosys JSON netlist, domains, cones, the rules, findings with paths, waivers, the step. Fixtures under `tests/fixtures/cdc/`: unsynchronized, two-flop, three-flop, logic before a synchronizer, handshake, gray FIFO, binary FIFO, asynchronous input with and without a synchronizer, reset domain crossing, reset synchronizer. Tests: `tests/test_project_cdc.py`.
8. **Gate-level simulation.** `chipleap/rtl/gls.py`: finding the routed netlist and SDF, the netlist copy with `$sdf_annotate`, cell models from the PDK, the manifest (`-gspecify`, `-T`, `GATES`), Icarus 13 check, the step. `CHIPLEAP_PARAMETERS` in every simulation; the UART test reads its parameter from it on a netlist. Tests: the synthesized-netlist run of the counter and the UART in CI; the routed UART with SDF gated on a routed run.
9. **Coverage closure.** `coverage.py` keeps `summary.json` and reports what the last run newly covered; `coverage_gaps` with source context. Tests in `tests/test_project_coverage.py`.
10. **Orchestration and reports.** `rtl/verify.py` steps and order, CLI `--steps`, report sections (findings, counterexamples, partitions). Tests in `tests/test_project_cli.py`.
11. **MCP.** Tools, prompts, replay sets, descriptions that say which tools run project code. Tests in `tests/test_project_mcp.py`.
12. **Commercial parity.** `rtl/commercial.py`: JasperGold, Conformal LEC, Xcelium with SDF, SpyGlass: scripts from quoted values and result parsers, tested without the tools; real runs on the design server, results kept private.
13. **Real runs.** Route the UART (LibreLane), run `equiv` (`synth-pnr`, `rtl-pnr`) and `gls` with SDF on it; record the results.
14. **Docs and CI.** `docs/projects.md` sections, `docs/verification.md`, `docs/mcp.md`, `docs/commercial-tools.md`, CHANGELOG. CI's hardware job installs z3, yosys-dev, SymbiYosys and EQY (the install script), Icarus 13 (the install script) and click, and runs the new tests and `chipleap verify examples/uart --steps lint,sim,formal,cdc,synth,equiv`.
15. **PR.** Rebase on origin/main, full checks, push, open the PR, watch CI.
