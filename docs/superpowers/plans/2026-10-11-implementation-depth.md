# Plan: D3 part b, DFT, power, IR drop, UPF and ECO

Spec: `docs/superpowers/specs/2026-10-11-implementation-depth-design.md`. Each task is test first, then code, then `ruff check`, `ruff format --check`, `mypy` and the touched tests, then one commit.

## Tools installed outside the repo

- Icarus Verilog 13.0, built from the `v13_0` tag into `~/.local/opt/iverilog-13` (IHP's cell models need it). Selected with `CHIPLEAP_GL_IVERILOG`.
- Fault 0.9.4: `uv tool install fault-dft --with-executables-from nl2bench`.
- Quaigh 0.0.6: `cargo install quaigh --root ~/.local/opt/quaigh`. Selected with `CHIPLEAP_QUAIGH` or the path.
- OpenROAD and LibreLane: the existing container wrappers.

## Tasks

1. **Project sections.** `DftConfig`, `PowerConfig`, `IrDropConfig`, `UpfConfig` with `PowerDomain`, `EcoChange`; parsers with key-path errors; fields on `Project`; `SECTIONS`; out of `RESERVED` (except `macros`); `to_dict`. Tests in `tests/test_project_impl.py`.
2. **Tools.** `chipleap.verify.tools`: `gl_iverilog()` (version 13 or newer), `fault()`, `nl2bench()`, `quaigh()`; `fault_env()` for Fault's Python path and simulator variables; doctor rows.
3. **Netlist reader.** `chipleap/rtl/netlist.py`: a tokenizer and parser for the structural Verilog that Yosys and OpenROAD write (escaped names, buses, assigns, concatenations, constants), bounded in size; `trace_chains`. Tests on hand-written netlists.
4. **OpenROAD runner and PDK views.** `chipleap/rtl/openroad.py`: `run_openroad` through `launch.run`, size-limited log reading, `PdkViews` from a run's `resolved.json` or from the PDK the liberty is in, confined to PDK roots; `routed_run`. Tests with a fake `openroad`.
5. **DFT.** `chipleap/rtl/dft.py`: scan insertion script and report parser, chain trace check, Fault cut and ATPG, pattern mapping, `patterns.json` and STIL, the shift-and-capture testbench and its result parser, the step. Unit tests on parsers and writers; a real run gated on OpenROAD, Fault and Icarus 13.
6. **Scan netlist in place and route.** `chipleap.rtl.pnr.config` routes the scan netlist when `dft.insert`. Unit test on the config.
7. **Power.** VCD from the cocotb runner (`vcd` in the manifest), the gate-level manifest, the dump-and-force module, OpenROAD power per corner, report parser, VCD clock check, the step. Unit tests on parsers and the manifest; real run gated.
8. **IR drop.** PDNSim script, report and CSV parsers, SVG map, the step. Unit tests; real run gated on a routed result.
9. **UPF.** UPF writer, hierarchical synthesis, OpenROAD read, floorplan and placement script, containment and crossings, the step. Unit tests; real run gated.
10. **ECO.** Changes file, OpenROAD edit script, base state and step list from the run, LibreLane resume, before and after, `chipleap-eco.json`, timing paths and their parser, the step and `chipleap eco`. Unit tests with a fake run tree; real run gated on `CHIPLEAP_RUN_PNR`.
11. **Orchestration, reports, MCP.** Steps in `verify.py`, report sections, MCP tools and the `fix_timing` prompt, replay lists. Tests over the in-process client.
12. **Example and acceptance runs.** UART sections; the UART with test changes for `GATES`; real runs: dft, pnr on the scan netlist, power, ir_drop, upf, eco; a timing fixture for `repair: setup`; record the numbers.
13. **Docs.** `docs/projects.md`, `docs/verification.md`, `docs/mcp.md`, README, CHANGELOG.
14. **PR.** Rebase on origin/main, full checks, push, PR, CI.
