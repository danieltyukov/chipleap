# Plan: D3 part a, SRAM macros in the layout

Spec: `docs/superpowers/specs/2026-10-12-soc-macros-design.md`. Each task is test first, then code, then `ruff check`, `ruff format --check`, `mypy` and the touched tests, then one commit. LibreLane runs one at a time, in the background.

## Tasks

1. **Macro grid.** `chipleap.macro.grid`: the PDN script for placed PDK macros (LibreLane's `pdn_cfg.tcl`, global connections per supply pin, one macro grid per cell), the TopMetal2 pair check, the refusal of supply pins off Metal4. D4's `chipleap.chip.pdn` uses it for `supply_hookup=False` views. Tests on the script text and the checks.
2. **Macro choice.** `chipleap.macro.sram`: the PDK's 1P macros read from `sg13g2_sram` (cell, words, width, outline), a bank of a given depth and width as macros side by side and stacked, the black-box Verilog view, and ChipLeap's functional model of the 1P ports for simulation. Tests with a stand-in PDK.
3. **Accelerator RTL.** `SRAM_BUF` in `chipleap_accel.sv`: bank ports for the gemv core, the lane layout of the weight buffer, writes as bank, word and mask. The generated top with the macros (`chipleap.gen.rtl`). `tests/test_rtl.py` runs a macro-backed design against the model; the bench checks no bank is read and written in one cycle.
4. **Accelerator place and route.** `chipleap.verify.pnr`: the macro-backed configuration (sizes, macros, placement, grid, liberty per corner, CDL), the fallback with its reason, the result with the macros' area, and the comparison with the model in the report. Unit tests on the configuration; the SmolLM2 accelerator routed for real.
5. **Projects.** The `macros` section (`chipleap.rtl.project`), its use in lint, synthesis, simulation and place and route, and the box treatment in the D2 and D3b steps. Unit tests, and a real run of a project with an SRAM.
6. **Docs.** `docs/projects.md`, `docs/verification.md`, `docs/model.md` where the measured buffer area appears, README and CHANGELOG.
7. **PR.** Merge main, full checks, push, PR, CI.

Part 2 (the whole SoC) gets its own plan once part 1 is in.
