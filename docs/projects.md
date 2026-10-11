# Your own RTL

ChipLeap's verification and implementation steps run on any Verilog or SystemVerilog design, not only on the accelerators it generates. A project file, `project.yaml`, names the sources, the top module, the clocks and resets, the technology and the cocotb tests. `chipleap verify` then lints, simulates, measures coverage, proves the design's properties, checks its clock and reset domain crossings, synthesizes, shows the netlist equal to the RTL and, when asked, places and routes it on IHP SG13G2 and runs the tests again on the routed netlist with its delays.

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
sim      PASS     Icarus 13.0: 5 of 5 cocotb tests pass
coverage PASS     line 83.3% (10/12), branch 100.0% (28/28), toggle 100.0% (176/176) over 3 files (5 tests); 2 points never reached, 0 newly reached since the last run
formal   PASS     prove (prove, abc pdr): every assertion holds
cdc      PASS     domains clk_i (47 flip-flops): 1 crossing, synchronized; 1 warning
synth    PASS     0.0041 mm2 of standard cells on IHP SG13G2 130 nm (open PDK) (221 cells, 51 flip-flops); critical path 1.01 ns, about 684 MHz at the typical corner, within the 100 MHz clock on clk_i
pnr      PASS     placed and routed on IHP SG13G2: 0.0084 mm2 core at 74% utilisation, 325 cells; 100 MHz on clk_i met at the slow corner (about 234 MHz, and about 308 MHz at the typical one); DRC, LVS and antenna clean
equiv    PASS     rtl-synth: proved 74 of 75 points; the other 1 (u_rx.state_q[2]) equal for 40 cycles from reset, not proved for every cycle; synth-pnr: proved, all 752 matched points equal
gls      PASS     routed netlist with SDF at nom_typ_1p20V_25C (typ delays): Icarus 13.0: 5 of 5 cocotb tests pass
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
formal:                       # optional; without it the formal step skips
  sources: [formal/uart_props.sv]   # property files or a wrapper top, read with -formal
  checks:                     # default: [{name: prove, mode: prove, depth: 20}]
    - {name: prove, mode: prove, depth: 20}           # mode: prove | bmc | cover
    - {name: deep, mode: bmc, depth: 60, top: uart_tx, engine: smtbmc yices}
  sby: [formal/extra.sby]     # SymbiYosys files of your own, run as they are
equivalence:
  checks: [rtl-synth, synth-pnr]   # default: rtl-synth, and synth-pnr after place and route
  engine: yosys               # yosys | eqy (rtl-synth only)
  depth: 40                   # cycles from reset for what induction leaves unproven
cdc:
  async_inputs: [rx_i]        # inputs no clock samples safely: each is a domain of its own
  input_domains: {cfg_i: clk_b}     # inputs that belong to a clock other than the first
  reset_synchronized: [rst_ni]      # resets already synchronized outside the design
  waive:                      # findings you reviewed, with the reason
    - {from: "dbg_*", to: "*", reason: quasi-static, set before the clock starts}
gate_sim:
  netlist: routed             # routed: LibreLane's final netlist with SDF | synth: zero delay
  corner: nom_typ_1p20V_25C   # the SDF corner; default the typical one
  delays: typ                 # min | typ | max of the SDF triples
```

Paths are relative to the project file and must stay inside its directory: see [Files outside the project](#files-outside-the-project). Environment variables and `~` are not expanded. Every key is checked and errors name the key, for example `clocks[0] needs period_ns or freq_mhz (one of them)` or `tests.modules: test_x.py not found in test`. `chipleap project check` prints the project with every default filled in.

Names (the top, ports, parameters, defines) are identifiers without `$`, parameter values are integers, and a define's value is a short literal (letters, digits and `_ . ' + -`, such as `8` or `8'hFF`). File and directory names may not hold control characters, quotes, backslashes, braces, brackets, `$`, `;`, a backtick, `#`, `*` or `?`, nor start with `-` or `+`. These values go into Yosys and Tcl scripts and onto tool command lines, so anything a script could read as syntax is refused when the project is read, and every value is quoted again when a script is written.

A filelist holds source files, `-f` or `-F` for another filelist (relative to the file that names it), `+incdir+DIR`, `-IDIR`, `+define+NAME=VALUE` and `-DNAME`. Comments start with `//` or `#`. Its files come before `sources`. Library options (`-v`, `-y`) are not supported: list the files.

The golden model is a Python file. Its directory is on the tests' `PYTHONPATH` and its path is in `CHIPLEAP_GOLDEN`, so a test imports it by name (`import uart_model`). Reports name it, so a reader knows what the tests compare against.

The verification sections are checked like the rest: check names are identifiers, modes, engines, equivalence checks, netlist kinds and delays come from the lists above, depths are integers up to 1000, a corner is a plain name, and a waiver's patterns hold only identifier characters, `.`, `/`, brackets and `*`, and it needs a reason. Formal sources and `.sby` files are paths like any other, inside the project, and the formal sources' `` `include `` and `$readmem` files are checked as the sources' are. A `.sby` file is run as it is: see [Security](#security).

Section names that later versions will read (`macros`, `dft`, `power`, `ir_drop`, `upf`, `eco`, `chip`) are rejected with a message saying they are not read yet, so a file written for a newer ChipLeap fails clearly.

## The steps

`chipleap verify PROJECT` runs `lint,sim,coverage,formal,cdc,synth,equiv`. Place and route takes minutes and gate-level simulation needs its result, so `pnr` and `gls` run only when named (`--steps pnr,equiv,gls`) or with `--steps all`, which runs everything in the order `lint, sim, coverage, formal, cdc, synth, pnr, equiv, gls`: equivalence and gate-level simulation see the netlists the same run made. Each step skips itself with the reason when its tool is missing, and `formal` when the project has no `formal` section. Work directories are under `PROJECT/build/`, and the results go to `PROJECT/build/verify/`: `report.html`, `VERIFY.md` and `verify.json`.

| Step | What runs | Passes when |
|---|---|---|
| `lint` | `verilator --lint-only -Wall` with the top, include directories, defines, parameters and waivers; `slang -Wextra` as well when it is installed | no warnings or errors |
| `sim` | the cocotb tests on Icarus or Verilator | every test passes |
| `coverage` | the tests again on Verilator built with `--coverage` | the run produced counts |
| `formal` | SymbiYosys on the design's own assertions, assumptions and covers | every assertion holds and every cover is reached |
| `cdc` | ChipLeap's clock and reset domain crossing checker on a Yosys netlist | no error that is not waived |
| `synth` | Yosys on the technology's standard cells | the design maps |
| `pnr` | LibreLane on IHP SG13G2 | setup met at the slow corner, hold at every corner, DRC, LVS and antenna clean |
| `equiv` | Yosys's equivalence passes (or EQY) on the RTL and the synthesized netlist, and on LibreLane's synthesized and routed netlists | every pair equal |
| `gls` | the cocotb tests on the routed netlist with SDF delays, on Icarus 13 | every test passes |

**Lint.** Any message fails the step, as for the generated accelerator. Warnings a project accepts belong in a Verilator waiver file listed under `lint.waivers`, so the decision is in the repository. slang elaborates the same top with the same parameters and often finds what Verilator does not, such as implicit truncations; `CHIPLEAP_SLANG` points at it.

**Simulation.** The tests are cocotb 2 tests (`pip install "chipleap[rtl]"`). They run in a separate process, with whichever Python has cocotb: ChipLeap's own, the one `cocotb-config --python-bin` names, or `CHIPLEAP_COCOTB_PYTHON`. `auto` picks Icarus, which is four-state and catches what an unreset register does, else Verilator. cocotb 2 needs Verilator 5.036 or newer; Ubuntu 24.04 ships 5.020, so point `CHIPLEAP_VERILATOR` at a newer build for Verilator simulation and coverage. Seeds are fixed, so a run gives the same result every time. A failing test is reported by name, with its message and the file and line in the project where it failed. A design that does not compile is reported with the simulator's errors.

**Coverage.** Verilator counts line blocks, branches, expression terms and every toggle of every bit. The step reports line, branch and toggle coverage for each design source (testbench-only files are left out) and lists the points the tests never reached. It reports coverage without judging it. It keeps its summary in `build/coverage/summary.json` and says how many points the latest tests reached that the run before did not, so closing coverage is a loop: read the gaps (the MCP tool `coverage_gaps` returns each point never reached with the source lines around it and the project's test functions), write a test that reaches them, run coverage again.

**Formal.** SymbiYosys proves the properties the design states itself: immediate `assert`, `assume` and `cover` statements in `always` blocks, with `$past`, `$rose`, `$fell`, `$stable` and `$changed`, usually under `` `ifdef FORMAL ``, which SymbiYosys defines when it reads the sources (`read -formal`) and lint, simulation and synthesis never do. `formal.sources` adds files only the proof reads, such as a wrapper top with the properties of a module it instantiates. The Yosys 0.33 that Ubuntu ships rejects concurrent SVA (`assert property (@(posedge clk) a |-> b)`) and silently ignores `bind`, so write properties as immediate assertions; [examples/uart](../examples/uart/rtl/uart_tx.sv) proves its transmitter's line in each state that way.

ChipLeap writes each check's `.sby` file itself, from the project's sources, include directories, defines and parameters and the check's mode (`prove`, `bmc` or `cover`), depth and engine. Engines are `smtbmc z3` (the default), `smtbmc yices`, `smtbmc bitwuzla` and `abc pdr`, which proves without a depth bound and needs yices to write a counterexample. A check passes, fails (a counterexample from reset, or a cover never reached), is unknown (induction failed without a counterexample from reset: the property may hold but needs a stronger invariant or more depth) or errs. A failure names the property, its file and line and the step it fails in, and its counterexample is both a VCD under `build/formal/` and data in `verify.json`: every signal of the top module, one value per step, so an assistant can tell a wrong assertion from a design bug. The step passes when every check does.

SymbiYosys and EQY are not in Ubuntu: `scripts/install_formal_tools.sh` builds both from the YosysHQ tag that matches the Yosys on the path (EQY's plugins are loaded into that Yosys, so the versions must agree) into a prefix of their own, with a z3 4.15 beside them, and prints the two variables to set: `CHIPLEAP_SBY` and `CHIPLEAP_EQY`. It needs `yosys-dev`, Python's `venv` and click. The z3 beside `sby` comes first: Ubuntu's z3 4.8.12 ran out of memory on the UART's proof, which z3 4.15 finishes in a second. Solvers are capped at 8 GB each (`CHIPLEAP_SOLVER_MEMORY_MB`, 0 for no cap) and a check at 30 minutes (`CHIPLEAP_FORMAL_TIMEOUT`); a solver that reaches either fails its check with the reason.

**Clock and reset domain crossings.** No open-source CDC tool fits this flow, so ChipLeap checks the structural rules commercial tools apply first, on the netlist Yosys elaborates. Each flip-flop's clock is traced to a clock port, which is its domain; a clock made by a flip-flop or an undeclared port is a domain of its own, with a warning. Inputs belong to the first clock unless `cdc.input_domains` names another, and `cdc.async_inputs` (a UART's receive line, say) are domains of their own. Every crossing, the data, enable and synchronous reset cones of a flip-flop that reach a source in another domain, is classified:

| Finding | Severity | Structure |
|---|---|---|
| `synchronizer` | accepted | the destination samples the source directly and feeds only another flip-flop of its domain (two flops or more) |
| `handshake` | accepted | data captured through an enable, or a multiplexer that otherwise holds its value, controlled by a synchronizer's output |
| `gray_synchronizer` | accepted | the bits of a gray-coded register, each through a synchronizer |
| `fifo_memory` | accepted | a memory written in one domain and read with an address from the reading domain only, as in an asynchronous FIFO, whose pointers the other rules check |
| `unsynchronized` | error | anything else, with the path of nets and cells from source to destination |
| `logic_before_synchronizer` | error | gates between the source and the first flop, which can glitch |
| `multibit_synchronizer` | error | the bits of a register that is not gray coded, each through its own synchronizer: they can arrive in different cycles |
| `reset_crossing` | error | a crossing into a flip-flop whose asynchronous reset does not include every reset of its source |
| `reset_release` | warning | flip-flops reset straight from a port: the reset is released asynchronously to their clock (list the reset in `cdc.reset_synchronized` if it is synchronized outside) |

A reset synchronizer is recognized and accepted. Findings name the source and destination registers, their domains and the path; `cdc.waive` takes a finding you reviewed out of the count, with its reason, and the report still lists it. The step fails on any error that is not waived. Not checked: reconvergence of signals synchronized separately, pulse widths across clock ratios and glitch-free clock multiplexers.

**Synthesis.** sv2v converts SystemVerilog first when it is installed, because the Yosys in Ubuntu 24.04 cannot read all of it; the project's parameters go to the top with `chparam`. Yosys maps to the liberty's cells and one ABC call maps, buffers, sizes and times the logic, aiming at half the fastest clock period, because routing and the slow corner roughly double a synthesized path on SG13G2. The step reports cell area, cells, flip-flops and the critical path, and an Fmax estimate at the typical corner. The mapped netlist stays in `build/synth/<top>.synth.v`. A technology added with `chipleap tech add` works too, with its liberty.

**Place and route.** The LibreLane configuration comes from the project, with the same signoff settings as the accelerator's ([verification.md](verification.md#place-and-route)): optimisation at the slow corner, repair after global routing, antenna repair and IHP's KLayout rule deck. The setup margins the repairs aim for scale with the clock: 8 % and 16 % of the period, at most 0.8 and 1.6 ns. The constraints follow LibreLane's base SDC (input and output delays of 20 % of the period, the PDK's driving cell and load, clock uncertainty and transition) for every clock in the project, with inputs and outputs timed against the first. Designs with several clocks that are unrelated, or with special timing, should give their own SDC under `constraints.sdc`. The GDS is in `build/pnr/runs/chipleap/final/gds/`.

On this release `examples/uart` routes in about seven minutes, almost all of it LibreLane: a 104 x 123 um die, 5.7 ns of setup slack at the slow corner and 0.12 ns of hold slack at the fast one, no DRC, LVS, antenna, slew or capacitance violations.

**Equivalence.** Two pairs, each a question a tape-out asks. `rtl-synth`: does ChipLeap's synthesized netlist implement the RTL? The RTL is synthesized to generic gates by the same Yosys commands as synthesis, up to the mapping to the liberty's cells, and compared with the mapped netlist. `synth-pnr`: did place and route change the logic? LibreLane's own synthesized netlist (it synthesizes again, so its netlist is where routing started) against its final one, from the routed run in `build/pnr`; without a routed run the pair is left out. Both sides read the liberty, which turns each cell into its logic, and `async2sync` models asynchronous resets for the solvers. Yosys's `equiv_make`, `equiv_simple` and `equiv_induct` match signals by name and prove them equal by induction. What induction leaves (usually a register stored inverted, as a flip-flop that resets to 1 is on a library with only resetting flip-flops) is compared from reset for `equivalence.depth` cycles on a miter by Yosys's SAT solver: a difference there is real and comes back as a counterexample from reset, and none means equal for that many cycles, which the result says rather than calling it proved. On the UART, 74 of 75 points of `rtl-synth` are proved and the last is equal for 40 cycles; `synth-pnr` proves all 752. The chain has a gap: the RTL is not compared with LibreLane's own synthesized netlist, whose registers have other names, so name matching finds only the outputs and nothing is proved beyond a bound; closing it needs register correspondence by structure, or place and route started from ChipLeap's netlist.

`equivalence.engine: eqy` uses EQY instead for `rtl-synth`, against the RTL as read: it partitions the design between matched registers and proves each partition. It is not offered for netlist pairs, because EQY 0.33 proved a netlist with one gate changed equal to another netlist of liberty cells (it catches the same change against the RTL). The test suite turns one AND gate of the counter's netlist into an OR (or a NAND into a NOR) and checks that both engines find the difference, with a counterexample from reset.

**Gate-level simulation.** The project's cocotb tests run on the routed netlist (`final/nl/<top>.nl.v` of the run in `build/pnr`) with IHP's cell models and the SDF of the same run, at the typical corner unless `gate_sim.corner` names another, with the `min`, `typ` or `max` value of each delay. The netlist is copied into `build/gls` with `$sdf_annotate` added to its top module, so the design under test is still the top with the same ports, and Icarus runs with `-gspecify`. The tests get `GATES=yes` (Tiny Tapeout's convention, for a test that must skip what only RTL has) and, as in every simulation, `CHIPLEAP_PARAMETERS`, the project's parameters as JSON, because a netlist has none left for a test to read; [the UART's tests](../examples/uart/test/test_uart.py) read their bit period from it. `gate_sim.netlist: synth` runs the same tests on ChipLeap's synthesized netlist with zero delay, which needs no routed run.

IHP's cell models need Icarus 13: Icarus 12, which Ubuntu 24.04 ships, elaborates them but leaves every flip-flop at X, so every test fails the same way. `scripts/install_iverilog13.sh` unpacks Tiny Tapeout's build of Icarus 13 beside the system one, and `CHIPLEAP_IVERILOG` selects it; the step checks the version and skips with that instruction otherwise. Icarus cannot annotate an interconnect that ends at a port of the top module, so those few delays stay zero, and the result counts them.

## Commercial tools

The options for generated bundles apply to projects ([commercial-tools.md](commercial-tools.md)):

- `--sim-tool xcelium`, `vcs` or `questa` runs the same cocotb tests on that simulator. With `--remote`, the project's sources, tests and golden model are copied into the work directory, which goes to the server; the server needs a `python3` with cocotb on the path after the setup snippet.
- `--synth-tool genus` or `dc` writes a Genus or Design Compiler script for the project's top, include directories, defines, parameters and clocks, and reports cell area and timing at the fastest clock.
- `--pnr-tool innovus` runs Genus and then Innovus with the same script as for the accelerator, given the project's top.

## With an assistant

The MCP server has tools for projects ([mcp.md](mcp.md)): `describe_project`, `lint_rtl`, `simulate_rtl`, `coverage_rtl`, `synthesize_rtl` and `verify_rtl`. Lint, simulation and synthesis return diagnostics with the tool, severity, file, line, column, the tool's code and, for a failing test, its name, so an assistant can fix the RTL in a loop. The `fix_rtl` prompt walks it through that. Run logs record a digest of every file a project names and the versions of the tools, and `chipleap replay` says whether a different result comes from changed files or changed tools.

For verification depth there are `prove_rtl`, `check_assertions`, `equivalence_rtl`, `cdc_rtl`, `gate_sim_rtl` and `coverage_gaps`. Each returns what an assistant can act on: the failing property with its counterexample as values per step, the points two netlists disagree on with a counterexample from reset, the crossing with its path, the coverage points never reached with their source lines.

`check_assertions` proves properties an assistant proposes without editing the project. Each is a boolean expression with a kind (`assert`, `assume` or `cover`) and a name; ChipLeap copies the file that defines the module into `build/formal/proposed`, inserts the properties before its `endmodule`, sampled at the project's clock outside reset, and proves the copy in place of the original. The design's own assertions and covers are left out of that run, so each answer is the proposal's own: `holds`, `fails` with its counterexample, or `unknown`, and `reached` or `unreached` for a cover. An expression may hold identifiers, numbers, operators, brackets and the system functions `$past`, `$rose`, `$fell`, `$stable`, `$changed`, `$countones`, `$onehot`, `$onehot0`, `$isunknown`, `$signed` and `$unsigned`, at most 400 characters, and nothing else: no `;`, quotes, backticks, `#`, `@`, comments, assignments or other `$` names, so a proposal cannot add a statement, a directive or a file read. The `propose_assertions` prompt has an assistant propose invariants and covers for a module, read each counterexample to decide whether the property or the design is wrong, and never weaken a property only to make it pass; `close_coverage` has it raise coverage with new tests, never by changing the RTL or weakening a test.

## Files outside the project

Every path a project names (sources, filelists and what they name, include directories, tests, the golden model, waivers, constraints, and the project file itself) must resolve, symlinks followed, to a place inside the project's directory. A project file from someone else's repository therefore cannot point a run at `~/.ssh` or anywhere else on your machine. In detail:

- A path is checked before ChipLeap opens or lists it, so an error never says whether a file outside exists. The resolved, checked path is the one every step uses, and when ChipLeap reads a project file (to copy, hash or scan it) it checks the open file again, so a directory swapped for a symlink after the first check is refused, not read.
- Glob patterns may not be absolute or hold `..`, and the walk that expands them never follows a symlink out of the project, so nothing outside is listed.
- Every `` `include `` in the sources, the testbench sources and Verilator waiver files (Verilator preprocesses those too), and in the files they include, is followed to every place a tool could find it, and each must be inside the project. Its name must be in quotes (not built by a macro), relative, and free of `..` and backslashes; name the directory in `include_dirs` instead of writing `../`. Strings and comments are read the way the tools read them, so a `"/*"` in a string cannot hide the `` `include `` after it. A `$readmemh` or `$readmemb` file named by a string must be inside the project too.
- Copies (to a server, to the place-and-route run) and the digests in run logs take only files reached without leaving the project, and skip build directories (`build`, `sim_build`, `runs`, `obj_dir`).
- A `project.yaml` that is a symlink to a file elsewhere is refused before it is read, and a YAML error gives the line and column, not the text there.
- ChipLeap's own directories are never reached through a symlink. `build/`, each step's directory in it (`build/synth`, `build/pnr`, `build/sim-icarus`, `build/genus` and so on) and the directories a step makes there are opened one at a time without following a symlink, so a repository that ships `build` as a symlink to your home directory gets an error that names it, not a deleted or overwritten home directory. A symlink further inside a step's directory is removed before the step writes there (nothing it points at is touched), the reports are written without following one, and `chipleap project init --force` does not write through a `project.yaml` that is a symlink. On Windows a directory junction, or any other reparse point, counts as a symlink here. A program that swaps a directory for a symlink while a step runs is not stopped; that takes code already running as you, such as the project's own tests (see [Security](#security)).

Reading the text cannot catch everything a tool will open: Verilator and slang follow an `` `include `` that a macro builds, and Yosys reads a memory file that a parameter names. So on Linux with Landlock (kernel 5.13 or newer; Ubuntu, Debian and Fedora turn it on), lint, synthesis, formal, CDC and equivalence start their tools (Verilator, slang, sv2v, Yosys with ABC, SymbiYosys with its solvers, EQY) able to read only the system directories, their own installation, the project and the directories you allowed, the liberty, and a private temporary directory. Any other file, the rest of your home directory included, fails to open, so it cannot reach a diagnostic or a netlist. `chipleap doctor` says whether Landlock is there. Without it (macOS, older kernels) the checks above still run, but an `` `include `` built by a macro or a memory file named by a parameter is not stopped. `CHIPLEAP_CONFINE=0` turns confinement off for a tool that needs files elsewhere; a tool that is a container wrapper is not confined here, since its container already limits what it sees.

To use files from elsewhere, such as shared IP in a sibling checkout, allow their directory yourself:

```sh
chipleap verify path/to/rtl --allow-path ../shared-ip
CHIPLEAP_ALLOW_PATHS=/opt/ip:/home/me/ip chipleap mcp    # the MCP server reads it too
```

Nothing in a project file can widen this, and an MCP client cannot either: the server only reads `CHIPLEAP_ALLOW_PATHS` from the environment you start it in. A run on a server (`--remote`) copies only files inside the project.

## Security

**Verifying a project runs its code.** The simulation, coverage and gate-level steps run the project's cocotb tests and its golden model, which are Python, and simulate its RTL, which can read and write files (`$readmemh`, `$fopen`); a project's own SDC is Tcl that OpenROAD or Innovus runs, and a `.sby` file under `formal.sby` can hold Python and Yosys commands that start programs, so SymbiYosys runs it as it is. None of that is confined. Only verify projects you trust, as you would only run their tests; the same goes for `simulate_rtl`, `coverage_rtl`, `gate_sim_rtl`, `prove_rtl` (for a project with its own `.sby` files) and `verify_rtl` through the MCP server.

The verification steps follow the rules above. The `.sby` files, EQY configurations and Yosys scripts ChipLeap writes take project values only after the checks at load time and quote them again; an assistant's proposed assertions are checked as described [above](#with-an-assistant). Yosys for the CDC netlist and for equivalence, SymbiYosys with its solvers and EQY run under the same Landlock confinement as lint and synthesis; SymbiYosys and EQY are Python scripts, so their interpreter's installation is readable too. The routed netlist and SDF that equivalence and gate-level simulation read are copied through the same checks, and only from the project's own `build/pnr`, never through a symlink. Every program runs from an argument vector, never a shell.

**Tools come only from absolute directories outside the project.** An empty entry, `.` or any other relative entry in `PATH` names the current directory, and during a run that is the project or its build directory, so a repository that ships an executable named `z3`, `yosys` or `sby` would have it run in place of the real tool, by ChipLeap or by a tool that starts another by name. ChipLeap therefore looks tools up only in absolute `PATH` directories, while a step runs leaves out any that lead into the project, and gives every program it starts a `PATH` made of those alone. A tool variable such as `CHIPLEAP_SBY` must hold an absolute path or a bare name; a relative path is ignored.

What the checks and the confinement above protect against is disclosure through ChipLeap's own output where none of the project's code runs: a project file cannot make `chipleap project check`, lint, Yosys synthesis, formal proofs of ChipLeap's own `.sby` files, CDC, equivalence, the reports, the run logs, the MCP results or a copy to a server read or show files outside its directory (without Landlock, short of the macro and parameter cases above), and it cannot smuggle commands into the scripts ChipLeap writes from its values ([the project file](#the-project-file)). They do not stop the project's own code from doing what code can do. Place and route and the commercial tools read the copies in their run directory and are not confined beyond that; LibreLane in its container (the usual way to run it) sees only that directory and the PDK. As for bundles, a server and its setup come only from your command line or environment, never from an MCP client.
