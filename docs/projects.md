# Your own RTL

ChipLeap's verification and implementation steps run on any Verilog or SystemVerilog design, not only on the accelerators it generates. A project file, `project.yaml`, names the sources, the top module, the clocks and resets, the technology and the cocotb tests. `chipleap verify` then lints, simulates, measures coverage, synthesizes and, when asked, places and routes it on IHP SG13G2.

```text
$ chipleap project init path/to/rtl
Wrote path/to/rtl/project.yaml
  sources: found 3 files
  top: uart, the module no other source instantiates
  clock: clk_i at 10 ns (100 MHz); set your own period
  reset: rst_ni, active low
  tests: cocotb tests in test/
$ chipleap verify path/to/rtl --steps all
lint     PASS     Verilator -Wall and slang -Wextra report no warnings
sim      PASS     Icarus 12.0: 5 of 5 cocotb tests pass
coverage PASS     line 83.3% (10/12), branch 100.0% (28/28), toggle 100.0% (176/176) over 3 files (5 tests); 2 points never reached
synth    PASS     0.0041 mm2 of standard cells on IHP SG13G2 130 nm (open PDK) (222 cells, 51 flip-flops); critical path 1.01 ns, about 684 MHz at the typical corner, within the 100 MHz clock on clk_i
pnr      PASS     placed and routed on IHP SG13G2: 0.0084 mm2 core at 74% utilisation, 325 cells; 100 MHz on clk_i met at the slow corner (about 234 MHz, and about 308 MHz at the typical one); DRC, LVS and antenna clean
```

[examples/uart](../examples/uart) is a complete project: a UART with cocotb tests and a Python reference model.

## The project file

```yaml
schema: project/v1            # optional; chipleap project init writes it
name: uart                    # letters, digits, _ . -; defaults to the top module
description: optional sentence
top: uart
sources: [rtl/uart_tx.sv, rtl/uart_rx.sv, rtl/uart.sv]   # globs allowed, sorted
filelist: rtl/files.f         # optional, in addition to sources
include_dirs: [rtl/include]
defines: {SYNTHESIS: null, WIDTH: 8}   # null defines the name without a value
parameters: {CLKS_PER_BIT: 16}         # integer overrides for the top module
timescale: 1ns/1ps            # for sources without `timescale, in simulation
clocks:
  - {port: clk_i, period_ns: 10}       # or freq_mhz: 100
resets:
  - {port: rst_ni, active: low, sync: false}
tech: ihp-sg13g2              # a key from chipleap tech list
tests:
  dir: test
  modules: [test_uart]        # default: every test_*.py in dir
  simulator: auto             # auto | icarus | verilator
  seed: 1                     # cocotb's random seed
  toplevel: uart              # default: top; a testbench wrapper can be the top here
  sources: []                 # testbench-only HDL, simulated but never synthesized
golden: model/uart_model.py   # optional Python reference model for the tests
lint:
  waivers: [rtl/waivers.vlt]  # Verilator configuration files
  slang: true                 # also lint with slang when it is installed
constraints:
  sdc: constraints/uart.sdc   # optional; replaces the generated constraints
pnr:
  utilisation: 45             # core utilisation, percent
  density: 55                 # placement density, percent
  die_um: [300, 300]          # optional fixed die size
  librelane: {}               # further LibreLane variables, passed through
```

Paths are relative to the project file and must stay inside its directory: see [Files outside the project](#files-outside-the-project). Environment variables and `~` are not expanded. Every key is checked and errors name the key, for example `clocks[0] needs period_ns or freq_mhz (one of them)` or `tests.modules: test_x.py not found in test`. `chipleap project check` prints the project with every default filled in.

Names (the top, ports, parameters, defines) are identifiers without `$`, parameter values are integers, and a define's value is a short literal (letters, digits and `_ . ' + -`, such as `8` or `8'hFF`). File and directory names may not hold control characters, quotes, backslashes, braces, brackets, `$`, `;`, a backtick, `#`, `*` or `?`, nor start with `-` or `+`. These values go into Yosys and Tcl scripts and onto tool command lines, so anything a script could read as syntax is refused when the project is read, and every value is quoted again when a script is written.

A filelist holds source files, `-f` or `-F` for another filelist (relative to the file that names it), `+incdir+DIR`, `-IDIR`, `+define+NAME=VALUE` and `-DNAME`. Comments start with `//` or `#`. Its files come before `sources`. Library options (`-v`, `-y`) are not supported: list the files.

The golden model is a Python file. Its directory is on the tests' `PYTHONPATH` and its path is in `CHIPLEAP_GOLDEN`, so a test imports it by name (`import uart_model`). Reports name it, so a reader knows what the tests compare against.

Section names that later versions will read (`formal`, `equivalence`, `cdc`, `gate_sim`, `macros`, `dft`, `power`, `ir_drop`, `upf`, `eco`, `chip`) are rejected with a message saying they are not read yet, so a file written for a newer ChipLeap fails clearly.

## The steps

`chipleap verify PROJECT` runs `lint,sim,coverage,synth`; `--steps pnr` or `--steps all` adds place and route, which takes minutes. Each step skips itself with the reason when its tool is missing. Work directories are under `PROJECT/build/`, and the results go to `PROJECT/build/verify/`: `report.html`, `VERIFY.md` and `verify.json`.

| Step | What runs | Passes when |
|---|---|---|
| `lint` | `verilator --lint-only -Wall` with the top, include directories, defines, parameters and waivers; `slang -Wextra` as well when it is installed | no warnings or errors |
| `sim` | the cocotb tests on Icarus or Verilator | every test passes |
| `coverage` | the tests again on Verilator built with `--coverage` | the run produced counts |
| `synth` | Yosys on the technology's standard cells | the design maps |
| `pnr` | LibreLane on IHP SG13G2 | setup met at the slow corner, hold at every corner, DRC, LVS and antenna clean |

**Lint.** Any message fails the step, as for the generated accelerator. Warnings a project accepts belong in a Verilator waiver file listed under `lint.waivers`, so the decision is in the repository. slang elaborates the same top with the same parameters and often finds what Verilator does not, such as implicit truncations; `CHIPLEAP_SLANG` points at it.

**Simulation.** The tests are cocotb 2 tests (`pip install "chipleap[rtl]"`). They run in a separate process, with whichever Python has cocotb: ChipLeap's own, the one `cocotb-config --python-bin` names, or `CHIPLEAP_COCOTB_PYTHON`. `auto` picks Icarus, which is four-state and catches what an unreset register does, else Verilator. cocotb 2 needs Verilator 5.036 or newer; Ubuntu 24.04 ships 5.020, so point `CHIPLEAP_VERILATOR` at a newer build for Verilator simulation and coverage. Seeds are fixed, so a run gives the same result every time. A failing test is reported by name, with its message and the file and line in the project where it failed. A design that does not compile is reported with the simulator's errors.

**Coverage.** Verilator counts line blocks, branches, expression terms and every toggle of every bit. The step reports line, branch and toggle coverage for each design source (testbench-only files are left out) and lists the points the tests never reached. It reports coverage without judging it.

**Synthesis.** sv2v converts SystemVerilog first when it is installed, because the Yosys in Ubuntu 24.04 cannot read all of it; the project's parameters go to the top with `chparam`. Yosys maps to the liberty's cells and one ABC call maps, buffers, sizes and times the logic, aiming at half the fastest clock period, because routing and the slow corner roughly double a synthesized path on SG13G2. The step reports cell area, cells, flip-flops and the critical path, and an Fmax estimate at the typical corner. The mapped netlist stays in `build/synth/<top>.synth.v`. A technology added with `chipleap tech add` works too, with its liberty.

**Place and route.** The LibreLane configuration comes from the project, with the same signoff settings as the accelerator's ([verification.md](verification.md#place-and-route)): optimisation at the slow corner, repair after global routing, antenna repair and IHP's KLayout rule deck. The setup margins the repairs aim for scale with the clock: 8 % and 16 % of the period, at most 0.8 and 1.6 ns. The constraints follow LibreLane's base SDC (input and output delays of 20 % of the period, the PDK's driving cell and load, clock uncertainty and transition) for every clock in the project, with inputs and outputs timed against the first. Designs with several clocks that are unrelated, or with special timing, should give their own SDC under `constraints.sdc`. The GDS is in `build/pnr/runs/chipleap/final/gds/`.

On this release `examples/uart` routes in about seven minutes, almost all of it LibreLane: a 104 x 123 um die, 5.7 ns of setup slack at the slow corner and 0.12 ns of hold slack at the fast one, no DRC, LVS, antenna, slew or capacitance violations.

## Commercial tools

The options for generated bundles apply to projects ([commercial-tools.md](commercial-tools.md)):

- `--sim-tool xcelium`, `vcs` or `questa` runs the same cocotb tests on that simulator. With `--remote`, the project's sources, tests and golden model are copied into the work directory, which goes to the server; the server needs a `python3` with cocotb on the path after the setup snippet.
- `--synth-tool genus` or `dc` writes a Genus or Design Compiler script for the project's top, include directories, defines, parameters and clocks, and reports cell area and timing at the fastest clock.
- `--pnr-tool innovus` runs Genus and then Innovus with the same script as for the accelerator, given the project's top.

## With an assistant

The MCP server has tools for projects ([mcp.md](mcp.md)): `describe_project`, `lint_rtl`, `simulate_rtl`, `coverage_rtl`, `synthesize_rtl` and `verify_rtl`. Lint, simulation and synthesis return diagnostics with the tool, severity, file, line, column, the tool's code and, for a failing test, its name, so an assistant can fix the RTL in a loop. The `fix_rtl` prompt walks it through that. Run logs record a digest of every file a project names and the versions of the tools, and `chipleap replay` says whether a different result comes from changed files or changed tools.

## Files outside the project

Every path a project names (sources, filelists and what they name, include directories, tests, the golden model, waivers, constraints, and the project file itself) must resolve, symlinks followed, to a place inside the project's directory. A project file from someone else's repository therefore cannot point a run at `~/.ssh` or anywhere else on your machine. In detail:

- A path is checked before ChipLeap opens or lists it, so an error never says whether a file outside exists. The resolved, checked path is the one every step uses, and when ChipLeap reads a project file (to copy, hash or scan it) it checks the open file again, so a directory swapped for a symlink after the first check is refused, not read.
- Glob patterns may not be absolute or hold `..`, and the walk that expands them never follows a symlink out of the project, so nothing outside is listed.
- Every `` `include `` in the sources, the testbench sources and Verilator waiver files (Verilator preprocesses those too), and in the files they include, is followed to every place a tool could find it, and each must be inside the project. Its name must be in quotes (not built by a macro), relative, and free of `..` and backslashes; name the directory in `include_dirs` instead of writing `../`. Strings and comments are read the way the tools read them, so a `"/*"` in a string cannot hide the `` `include `` after it. A `$readmemh` or `$readmemb` file named by a string must be inside the project too.
- Copies (to a server, to the place-and-route run) and the digests in run logs take only files reached without leaving the project, and skip build directories (`build`, `sim_build`, `runs`, `obj_dir`).
- A `project.yaml` that is a symlink to a file elsewhere is refused before it is read, and a YAML error gives the line and column, not the text there.

Reading the text cannot catch everything a tool will open: Verilator and slang follow an `` `include `` that a macro builds, and Yosys reads a memory file that a parameter names. So on Linux with Landlock (kernel 5.13 or newer; Ubuntu, Debian and Fedora turn it on), lint and synthesis start their tools (Verilator, slang, sv2v, and Yosys with ABC) able to read only the system directories, their own installation, the project and the directories you allowed, the liberty, and a private temporary directory. Any other file, the rest of your home directory included, fails to open, so it cannot reach a diagnostic or a netlist. `chipleap doctor` says whether Landlock is there. Without it (macOS, older kernels) the checks above still run, but an `` `include `` built by a macro or a memory file named by a parameter is not stopped. `CHIPLEAP_CONFINE=0` turns confinement off for a tool that needs files elsewhere; a tool that is a container wrapper is not confined here, since its container already limits what it sees.

To use files from elsewhere, such as shared IP in a sibling checkout, allow their directory yourself:

```sh
chipleap verify path/to/rtl --allow-path ../shared-ip
CHIPLEAP_ALLOW_PATHS=/opt/ip:/home/me/ip chipleap mcp    # the MCP server reads it too
```

Nothing in a project file can widen this, and an MCP client cannot either: the server only reads `CHIPLEAP_ALLOW_PATHS` from the environment you start it in. A run on a server (`--remote`) copies only files inside the project.

## Security

**Verifying a project runs its code.** The simulation and coverage steps run the project's cocotb tests and its golden model, which are Python, and simulate its RTL, which can read and write files (`$readmemh`, `$fopen`); a project's own SDC is Tcl that OpenROAD or Innovus runs. None of that is confined. Only verify projects you trust, as you would only run their tests; the same goes for `simulate_rtl`, `coverage_rtl` and `verify_rtl` through the MCP server.

What the checks and the confinement above protect against is disclosure through ChipLeap's own output where none of the project's code runs: a project file cannot make `chipleap project check`, lint, Yosys synthesis, the reports, the run logs, the MCP results or a copy to a server read or show files outside its directory (without Landlock, short of the macro and parameter cases above), and it cannot smuggle commands into the scripts ChipLeap writes from its values ([the project file](#the-project-file)). They do not stop the project's own code from doing what code can do. Place and route and the commercial tools read the copies in their run directory and are not confined beyond that; LibreLane in its container (the usual way to run it) sees only that directory and the PDK. As for bundles, a server and its setup come only from your command line or environment, never from an MCP client.
