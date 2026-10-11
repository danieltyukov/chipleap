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
dft:                          # scan chains and ATPG; "dft:" alone takes the defaults
  max_chain_length: 100       # flip-flops per chain
  max_chains: 0               # 0: as many as the length needs
  clock_mixing: no_mix        # no_mix | clock_mix
  insert: true                # place and route the scan netlist
  atpg: true                  # stuck-at patterns with Fault, when it is installed
  min_coverage: 90            # optional: fail below this coverage, percent
  patterns: 8                 # ATPG patterns replayed through the chains in simulation
power:
  corners: [typ]              # typ (1.2 V, 25 C), slow (1.08 V, 125 C), fast (1.32 V, -40 C)
  netlist: auto               # auto (routed if there is one) | routed | synth
  scope: ""                   # the design's instance in the tests' top, such as tb/u_dut
  input_activity: 0.1         # for the vectorless figure
ir_drop:
  corner: typ
  max_drop_pct: 5             # optional: fail above this drop, percent of the supply
upf:
  domains:
    - {name: PD_TOP}                          # the top: whatever no other domain holds
    - {name: PD_RX, elements: [u_rx], area_um: [5, 5, 60, 100], voltage: 1.2}
eco:                          # changes to the routed design, applied by the eco step
  - {resize: _211_, cell: sg13g2_nor2_2}
  - {buffer: _212_/Y, cell: sg13g2_buf_2}
  - {repair: setup, margin_ns: 0.5}
```

Paths are relative to the project file and must stay inside its directory: see [Files outside the project](#files-outside-the-project). Environment variables and `~` are not expanded. Every key is checked and errors name the key, for example `clocks[0] needs period_ns or freq_mhz (one of them)` or `tests.modules: test_x.py not found in test`. `chipleap project check` prints the project with every default filled in.

Names (the top, ports, parameters, defines) are identifiers without `$`, parameter values are integers, and a define's value is a short literal (letters, digits and `_ . ' + -`, such as `8` or `8'hFF`). File and directory names may not hold control characters, quotes, backslashes, braces, brackets, `$`, `;`, a backtick, `#`, `*` or `?`, nor start with `-` or `+`. These values go into Yosys and Tcl scripts and onto tool command lines, so anything a script could read as syntax is refused when the project is read, and every value is quoted again when a script is written.

A filelist holds source files, `-f` or `-F` for another filelist (relative to the file that names it), `+incdir+DIR`, `-IDIR`, `+define+NAME=VALUE` and `-DNAME`. Comments start with `//` or `#`. Its files come before `sources`. Library options (`-v`, `-y`) are not supported: list the files.

The golden model is a Python file. Its directory is on the tests' `PYTHONPATH` and its path is in `CHIPLEAP_GOLDEN`, so a test imports it by name (`import uart_model`). Reports name it, so a reader knows what the tests compare against.

A power domain's elements are hierarchical instance paths (`u_rx`, `u_core/u_alu`); exactly one domain has none, the top. ECO targets are names in the routed netlist, which may hold characters other names may not (`\u_rx.data_q[3]`): any printable characters but white space, quotes, braces, `;`, a backtick and `$`. They reach OpenROAD as data, never as script text.

Section names that later versions will read (`formal`, `equivalence`, `cdc`, `gate_sim`, `macros`, `chip`) are rejected with a message saying they are not read yet, so a file written for a newer ChipLeap fails clearly.

## The steps

`chipleap verify PROJECT` runs `lint,sim,coverage,synth`; `--steps pnr` adds place and route, which takes minutes, and `--steps all` adds it and the steps after it (`eco`, `dft`, `power`, `ir_drop`, `upf`, described in [After place and route](#after-place-and-route)). Each step skips itself with the reason when its tool or its section is missing. Work directories are under `PROJECT/build/`, and the results go to `PROJECT/build/verify/`: `report.html`, `VERIFY.md` and `verify.json`.

| Step | What runs | Passes when |
|---|---|---|
| `lint` | `verilator --lint-only -Wall` with the top, include directories, defines, parameters and waivers; `slang -Wextra` as well when it is installed | no warnings or errors |
| `sim` | the cocotb tests on Icarus or Verilator | every test passes |
| `coverage` | the tests again on Verilator built with `--coverage` | the run produced counts |
| `synth` | Yosys on the technology's standard cells | the design maps |
| `pnr` | LibreLane on IHP SG13G2 | setup met at the slow corner, hold at every corner, DRC, LVS and antenna clean |
| `eco` | OpenROAD on the routed DEF, then LibreLane from detailed routing on | the changes apply and the result meets `pnr`'s criteria |
| `dft` | OpenROAD scan insertion, Fault with Quaigh, Icarus 13 | every flip-flop is on a chain, the chains shift and every simulated pattern matches; coverage at least `min_coverage` |
| `power` | the cocotb tests on the gate-level netlist (Icarus 13), OpenROAD | the tests pass on the netlist and their activity annotates it |
| `ir_drop` | OpenROAD PDNSim on the routed grid | the worst drop is within `max_drop_pct`, when given |
| `upf` | OpenROAD `read_upf`, floorplan and placement | every domain resolves, its cells land in its area, no crossing needs a level shifter |

**Lint.** Any message fails the step, as for the generated accelerator. Warnings a project accepts belong in a Verilator waiver file listed under `lint.waivers`, so the decision is in the repository. slang elaborates the same top with the same parameters and often finds what Verilator does not, such as implicit truncations; `CHIPLEAP_SLANG` points at it.

**Simulation.** The tests are cocotb 2 tests (`pip install "chipleap[rtl]"`). They run in a separate process, with whichever Python has cocotb: ChipLeap's own, the one `cocotb-config --python-bin` names, or `CHIPLEAP_COCOTB_PYTHON`. `auto` picks Icarus, which is four-state and catches what an unreset register does, else Verilator. cocotb 2 needs Verilator 5.036 or newer; Ubuntu 24.04 ships 5.020, so point `CHIPLEAP_VERILATOR` at a newer build for Verilator simulation and coverage. Seeds are fixed, so a run gives the same result every time. A failing test is reported by name, with its message and the file and line in the project where it failed. A design that does not compile is reported with the simulator's errors.

**Coverage.** Verilator counts line blocks, branches, expression terms and every toggle of every bit. The step reports line, branch and toggle coverage for each design source (testbench-only files are left out) and lists the points the tests never reached. It reports coverage without judging it.

**Synthesis.** sv2v converts SystemVerilog first when it is installed, because the Yosys in Ubuntu 24.04 cannot read all of it; the project's parameters go to the top with `chparam`. Yosys maps to the liberty's cells and one ABC call maps, buffers, sizes and times the logic, aiming at half the fastest clock period, because routing and the slow corner roughly double a synthesized path on SG13G2. The step reports cell area, cells, flip-flops and the critical path, and an Fmax estimate at the typical corner. The mapped netlist stays in `build/synth/<top>.synth.v`. A technology added with `chipleap tech add` works too, with its liberty.

**Place and route.** The LibreLane configuration comes from the project, with the same signoff settings as the accelerator's ([verification.md](verification.md#place-and-route)): optimisation at the slow corner, repair after global routing, antenna repair and IHP's KLayout rule deck. The setup margins the repairs aim for scale with the clock: 8 % and 16 % of the period, at most 0.8 and 1.6 ns. The constraints follow LibreLane's base SDC (input and output delays of 20 % of the period, the PDK's driving cell and load, clock uncertainty and transition) for every clock in the project, with inputs and outputs timed against the first. Designs with several clocks that are unrelated, or with special timing, should give their own SDC under `constraints.sdc`. The GDS is in `build/pnr/runs/chipleap/final/gds/`.

On this release `examples/uart` without scan chains routes in about six minutes, almost all of it LibreLane: a 104 x 123 um die, 5.7 ns of setup slack at the slow corner and 0.12 ns of hold slack at the fast one, no DRC, LVS, antenna, slew or capacitance violations. With its `dft` section (below) it routes the scan netlist: 346 cells on a 0.0105 mm2 core, 6.2 ns of setup slack at the slow corner and 0.11 ns of hold slack, clean as well.

## After place and route

These steps run with `--steps all`, or named. They use OpenROAD (the `openroad` command, `CHIPLEAP_OPENROAD`), whose scripts ChipLeap writes and whose reports it reads, LibreLane for routing and signoff, and two more tools: Icarus 13 or newer for gate-level simulation of IHP's cells (`CHIPLEAP_GL_IVERILOG`; Icarus 12 leaves every flip-flop at X) and Fault with Quaigh for ATPG. `chipleap doctor` lists them. Every step skips itself, with the reason, when its tool or its section is missing.

`chipleap verify examples/uart --steps all` on this release:

```text
pnr      PASS     placed and routed on IHP SG13G2: 0.0105 mm2 core at 65% utilisation, 346 cells; 100 MHz on clk_i met at the slow corner (about 263 MHz, and about 310 MHz at the typical one); DRC, LVS and antenna clean
eco      SKIPPED  no eco changes in the project
dft      PASS     3 scan chains of 17 flip-flops (51 of 51 scanned); Fault with Quaigh: 99.2% stuck-at coverage (1379 of 1390 faults) with 39 patterns; flush and 8 patterns pass on the scan netlist (Icarus 13.0); flush and 8 patterns pass on the routed netlist (Icarus 13.0)
power    PASS     power of the routed netlist: 0.344 mW at typ (vectorless 0.916), 0.275 mW at slow (vectorless 0.745), 0.425 mW at fast (vectorless 1.14); activity from 5 of 5 cocotb tests passing on the gate-level netlist; 1156 of 1156 pins annotated
ir_drop  PASS     worst IR drop at the typ corner (1.2 V), currents from the tests' VCD: VGND 0.4 mV (0.03%), VPWR 0.51 mV (0.04%)
upf      PASS     OpenROAD read 2 power domains and placed them: PD_TOP 114 cells, PD_RX 122 cells inside its sized area; 13 signals cross between domains
```

**Design for test.** With a `dft` section, D1's synthesis maps the design, and OpenROAD replaces every flip-flop with its scan equivalent (`scan_replace`; IHP's `sg13g2_sdfrbp` and `sg13g2_sdfrbpq` cells) and stitches the chains (`execute_dft_plan`), adding `scan_enable_0`, `scan_in_N` and `scan_out_N`; a chain may also end on a functional output its last flip-flop drives. ChipLeap traces every chain through the netlist itself, from its scan input to its scan output, and checks the order against OpenROAD's report. Fault cuts the flip-flops out of the netlist, Quaigh generates stuck-at patterns and Fault simulates every fault against them; its coverage is the step's number. ChipLeap keeps Quaigh's pattern set, because Fault's compaction of it is not the same from one run to the next. The patterns, laid onto the chains, go to `build/dft/patterns.json` (chains with their cells from scan-in to scan-out; per pattern the bits to load, the inputs, the expected outputs and the bits to unload) and `build/dft/patterns.stil`, a STIL (IEEE 1450) subset with the scan structures and load-unload and capture procedures. Then a generated testbench runs the scan netlist with the cell models on Icarus 13: a flush pattern (0011 repeated) through every chain, and the first `patterns` ATPG patterns shifted in, captured once and shifted out against Fault's expected values. Once the scan netlist is routed, the same patterns run on the routed netlist, after ChipLeap has traced its chains through the buffers place and route added and found them in the same order. With `insert: true` (the default) `pnr` routes the scan netlist: LibreLane's synthesis keeps its cells, its lint is off (Verilator has no cell models), and the scan ports are constrained like any other input or output, so shift paths are timed at the functional clock and hold repair covers them.

**Gate-level power.** The project's cocotb tests run on the gate-level netlist (the routed one when there is one, else the synthesized one) with IHP's cell models on Icarus 13, and a second top module dumps a VCD of the design and holds any scan port at its functional value. A netlist has no parameters and no RTL signal names, so tests see `GATES=yes` (Tiny Tapeout's convention) and the project's parameters as `CHIPLEAP_PARAM_<name>`; the UART's tests read `CLKS_PER_BIT` that way. When the tests wrap the design in a testbench, `power.scope` names its instance. For each corner OpenROAD reads that corner's liberty, the netlist, the constraints and, when routed, the parasitics, and reports power twice: vectorless (inputs toggling at `input_activity`) and from the VCD, with the number of pins the VCD annotated. ChipLeap checks the clock in the VCD against the constraints, because activity is counted per clock period. A test that fails on the netlist fails the step.

**IR drop.** OpenROAD reads the routed DEF with its power grid, the corner's liberty, constraints, parasitics and the power step's VCD of the same netlist (or makes one), sets every power net to the corner's supply and every ground net to zero, and runs PDNSim on each. The step reports the worst and average drop per net, the logic cells with the worst drop, and draws the drop over the die (`build/ir_drop/<net>.svg`, also in the report).

**Power intent.** A `upf` section lists power domains: one top domain without elements, and others that name hierarchical instances, with an area or not, and a voltage or not. ChipLeap writes the UPF (`build/upf/<top>.upf`), synthesizes keeping the module hierarchy, and OpenROAD links it hierarchically, reads the UPF, floorplans (a domain with an area becomes a placement region; one without gets a strip as wide as its share of the cells), places, and writes back the UPF it understood. The step reports each domain's cells and whether they all landed in its area, and the signals between domains. When two connected domains have different voltages the step fails: they need level shifters, and IHP SG13G2's standard cells have none. The open flow cannot do more with power intent: no open simulator is power aware, so a domain switched off is never simulated as off, and LibreLane routes one supply, so the GDS from `pnr` has one domain.

**Engineering change orders.** An ECO changes the routed design and signs it off again without synthesis, floorplanning, placement or clock-tree synthesis. The base is the routed run's state just before detailed routing. OpenROAD reads that DEF and applies the changes through its database: `resize` swaps an instance's cell for one with the same pins (`sg13g2_nor2_1` to `sg13g2_nor2_2`), `buffer` puts a new buffer after a driver's output pin (it then drives every load) or in front of a sink's input pin. LibreLane, resumed from that state, legalizes the placement, routes globally, repairs timing again when a `repair` change asks for it (with its margin), and then runs exactly the steps the original run ran from detailed routing on: routing, fill, extraction, timing at every corner, DRC, LVS and antenna checks. Changes always apply to the original routed run, as a whole list, so an ECO result depends only on the run and its list, and the `eco` section reproduces it; the result is in `build/pnr/runs/eco/` and the later steps read it while it belongs to the current routed run. `chipleap eco PROJECT --paths` lists the worst paths, stage by stage, to choose changes from, and `chipleap eco PROJECT --resize INST=CELL --buffer INST/PIN=CELL --repair setup` applies changes without editing the project file.

ECO_RESULTS

## Commercial tools

The options for generated bundles apply to projects ([commercial-tools.md](commercial-tools.md)):

- `--sim-tool xcelium`, `vcs` or `questa` runs the same cocotb tests on that simulator. With `--remote`, the project's sources, tests and golden model are copied into the work directory, which goes to the server; the server needs a `python3` with cocotb on the path after the setup snippet.
- `--synth-tool genus` or `dc` writes a Genus or Design Compiler script for the project's top, include directories, defines, parameters and clocks, and reports cell area and timing at the fastest clock.
- `--pnr-tool innovus` runs Genus and then Innovus with the same script as for the accelerator, given the project's top.

## With an assistant

The MCP server has tools for projects ([mcp.md](mcp.md)): `describe_project`, `lint_rtl`, `simulate_rtl`, `coverage_rtl`, `synthesize_rtl` and `verify_rtl`, and for the steps after place and route `dft_rtl`, `power_rtl`, `ir_drop_rtl`, `upf_rtl`, `timing_paths_rtl` and `eco_rtl`; the `fix_timing` prompt has an assistant close timing with ECO changes, one at a time. Lint, simulation and synthesis return diagnostics with the tool, severity, file, line, column, the tool's code and, for a failing test, its name, so an assistant can fix the RTL in a loop. The `fix_rtl` prompt walks it through that. Run logs record a digest of every file a project names and the versions of the tools, and `chipleap replay` says whether a different result comes from changed files or changed tools.

## Files outside the project

Every path a project names (sources, filelists and what they name, include directories, tests, the golden model, waivers, constraints, and the project file itself) must resolve, symlinks followed, to a place inside the project's directory. A project file from someone else's repository therefore cannot point a run at `~/.ssh` or anywhere else on your machine. In detail:

- A path is checked before ChipLeap opens or lists it, so an error never says whether a file outside exists. The resolved, checked path is the one every step uses, and when ChipLeap reads a project file (to copy, hash or scan it) it checks the open file again, so a directory swapped for a symlink after the first check is refused, not read.
- Glob patterns may not be absolute or hold `..`, and the walk that expands them never follows a symlink out of the project, so nothing outside is listed.
- Every `` `include `` in the sources, the testbench sources and Verilator waiver files (Verilator preprocesses those too), and in the files they include, is followed to every place a tool could find it, and each must be inside the project. Its name must be in quotes (not built by a macro), relative, and free of `..` and backslashes; name the directory in `include_dirs` instead of writing `../`. Strings and comments are read the way the tools read them, so a `"/*"` in a string cannot hide the `` `include `` after it. A `$readmemh` or `$readmemb` file named by a string must be inside the project too.
- Copies (to a server, to the place-and-route run) and the digests in run logs take only files reached without leaving the project, and skip build directories (`build`, `sim_build`, `runs`, `obj_dir`).
- A `project.yaml` that is a symlink to a file elsewhere is refused before it is read, and a YAML error gives the line and column, not the text there.
- ChipLeap's own directories are never reached through a symlink. `build/`, each step's directory in it (`build/synth`, `build/pnr`, `build/sim-icarus`, `build/genus` and so on) and the directories a step makes there are opened one at a time without following a symlink, so a repository that ships `build` as a symlink to your home directory gets an error that names it, not a deleted or overwritten home directory. A symlink further inside a step's directory is removed before the step writes there (nothing it points at is touched), the reports are written without following one, and `chipleap project init --force` does not write through a `project.yaml` that is a symlink. On Windows a directory junction, or any other reparse point, counts as a symlink here. A program that swaps a directory for a symlink while a step runs is not stopped; that takes code already running as you, such as the project's own tests (see [Security](#security)).

Reading the text cannot catch everything a tool will open: Verilator and slang follow an `` `include `` that a macro builds, and Yosys reads a memory file that a parameter names. So on Linux with Landlock (kernel 5.13 or newer; Ubuntu, Debian and Fedora turn it on), lint and synthesis start their tools (Verilator, slang, sv2v, and Yosys with ABC) able to read only the system directories, their own installation, the project and the directories you allowed, the liberty, and a private temporary directory. Any other file, the rest of your home directory included, fails to open, so it cannot reach a diagnostic or a netlist. `chipleap doctor` says whether Landlock is there. Without it (macOS, older kernels) the checks above still run, but an `` `include `` built by a macro or a memory file named by a parameter is not stopped. `CHIPLEAP_CONFINE=0` turns confinement off for a tool that needs files elsewhere; a tool that is a container wrapper is not confined here, since its container already limits what it sees.

To use files from elsewhere, such as shared IP in a sibling checkout, allow their directory yourself:

```sh
chipleap verify path/to/rtl --allow-path ../shared-ip
CHIPLEAP_ALLOW_PATHS=/opt/ip:/home/me/ip chipleap mcp    # the MCP server reads it too
```

Nothing in a project file can widen this, and an MCP client cannot either: the server only reads `CHIPLEAP_ALLOW_PATHS` from the environment you start it in. A run on a server (`--remote`) copies only files inside the project.

## Security

**Verifying a project runs its code.** The simulation, coverage and power steps run the project's cocotb tests and its golden model, which are Python, and simulate its RTL, which can read and write files (`$readmemh`, `$fopen`); a project's own SDC is Tcl that OpenROAD or Innovus runs. None of that is confined. Only verify projects you trust, as you would only run their tests; the same goes for `simulate_rtl`, `coverage_rtl` and `verify_rtl` through the MCP server.

What the checks and the confinement above protect against is disclosure through ChipLeap's own output where none of the project's code runs: a project file cannot make `chipleap project check`, lint, Yosys synthesis, the reports, the run logs, the MCP results or a copy to a server read or show files outside its directory (without Landlock, short of the macro and parameter cases above), and it cannot smuggle commands into the scripts ChipLeap writes from its values ([the project file](#the-project-file)). They do not stop the project's own code from doing what code can do. Place and route and the commercial tools read the copies in their run directory and are not confined beyond that; LibreLane in its container (the usual way to run it) sees only that directory and the PDK. The steps after place and route reuse results in `build/` (the routed run, an ECO's run, the power step's VCD), which could come from someone else's repository too, and the tools run what those files say: a LibreLane configuration names scripts, an SDC file is Tcl, a netlist can hold initial blocks. So a step reuses a result only when ChipLeap on this machine recorded it. When place and route, an ECO or the gate-level tests finish, ChipLeap writes a manifest beside the result (`build/pnr/chipleap-pnr.manifest.json`, `chipleap-eco.manifest.json`, `build/power/gl/chipleap-gl.manifest.json`) with the SHA-256 of every file, authenticated with a key only your user reads (`~/.local/state/chipleap/build.key`, made on first use; `CHIPLEAP_BUILD_KEY` names another file). A result without a manifest, with one that does not verify (edited, or made on another machine), or with a file whose hash differs fails the step without the file being opened by a tool; run the step that makes it again. Files are copied with the hash checked on the bytes copied, and `build/pnr` and the run directories are never reached through a symlink, so a project cannot point a step at another project's results either. An ECO passes on to LibreLane only the variables ChipLeap's own configuration sets and the project's numeric `pnr.librelane` settings, never one that names a script, hook or plugin, and never a substitution. Every PDK file a run's `resolved.json` names must be under a PDK directory of this machine (`PDK_ROOT`, `~/.ciel`, `~/.volare`, `~/.local/share/pdk`, `/usr/local/share/pdk`, `/opt/pdk`). Each OpenROAD run gets a private directory, emptied first, holding copies of its inputs and nothing else of the project; a container wrapper that mounts its working directory (as the usual `openroad` wrapper does) therefore sees only those copies and the PDK. Every value a script takes from the project is quoted, and ECO targets and cells reach OpenROAD as a data file it reads line by line, never as script text. Netlists are read with limits on their size, modules, instances, net bits, the width of a bus and the depth of the hierarchy (a module that contains itself is refused), and tool reports with size limits. As for bundles, a server and its setup come only from your command line or environment, never from an MCP client.

**Tools come from your machine, not the project.** A tool is found by an absolute path: `PATH` without its empty and relative entries (an empty entry means the current directory, which during a step is the project or a directory in its `build/`), and a `CHIPLEAP_<TOOL>` variable must name an absolute path or a command on that path. While a step runs, no tool is taken from inside the project, and the `PATH`, `PYTHONPATH` and `LD_LIBRARY_PATH` ChipLeap hands every tool hold no empty, relative or project entry, so a repository that ships its own `vvp`, `iverilog`, `fault`, `docker` or a Python package such as `sitecustomize` does not get it run. Fault's Python path comes from the interpreter Fault itself is installed with, asked in isolated mode.
