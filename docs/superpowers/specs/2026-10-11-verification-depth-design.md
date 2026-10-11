# D2: verification depth

Status: accepted, 2026-10-11. Milestone D2 of `chip-design-roadmap.md`, built on the project format of D1 (`2026-10-11-rtl-projects-design.md`).

## Goal

A project that passes lint, its tests and synthesis can be checked the way a tape-out is: its properties proved, its netlists shown equal to its RTL, its clock and reset crossings checked, and its tests run again on the routed netlist with real delays. An assistant can drive each check through MCP and read results it can act on: the failing property with a counterexample trace, the partition that differs, the crossing with its path, the coverage points no test reaches.

## Decisions

### Four sections, four steps

The sections D1 reserved become real: `formal`, `equivalence`, `cdc` and `gate_sim`. Each drives one step of `chipleap verify PROJECT`:

| step | section | engine | needs |
|---|---|---|---|
| `formal` | `formal` | SymbiYosys | the section (skipped without it) |
| `cdc` | `cdc` (optional) | Yosys netlist, ChipLeap's checker | nothing |
| `equiv` | `equivalence` (optional) | EQY | a synthesized netlist; a routed run for the routed pairs |
| `gls` | `gate_sim` (optional) | cocotb on Icarus 13 with SDF | a routed run (or the synthesized netlist) |

The step order is `lint, sim, coverage, formal, cdc, synth, pnr, equiv, gls`, so equivalence and gate-level simulation see the netlists the run just made. The default steps grow to `lint, sim, coverage, formal, cdc, synth, equiv`; `formal` skips itself without a section and `equiv` checks only RTL against the synthesized netlist unless a routed run exists. `gls` runs only when named or with `all`, like `pnr`.

```yaml
formal:
  sources: [formal/fifo_check.sv]   # optional: property files or a wrapper top, read with -formal
  checks:                           # default: [{name: prove, mode: prove, depth: 20}]
    - {name: prove, mode: prove, depth: 20}
    - {name: cover, mode: cover, depth: 30}
    - {name: deep, mode: bmc, depth: 60, top: fifo_check, engine: smtbmc z3}
  sby: [formal/extra.sby]           # optional: run as they are (they are code, see Security)
equivalence:
  checks: [rtl-synth, synth-pnr]    # default: rtl-synth, plus synth-pnr when a routed run exists
cdc:
  async_inputs: [rx_i]              # inputs no clock samples safely: each is its own domain
  input_domains: {cfg_i: clk_b}     # inputs that belong to a clock other than the first
  reset_synchronized: [rst_ni]      # resets already synchronized outside the design
  waive:                            # findings the designer has reviewed
    - {from: "dbg_*", to: "*", reason: quasi-static, set before the clock starts}
gate_sim:
  netlist: routed                   # routed (default) or synth (zero delay)
  corner: nom_typ_1p20V_25C         # the SDF corner of the routed run
  delays: typ                       # min, typ or max of the SDF triples
```

Every value is checked at load time as in D1: names are identifiers, glob patterns in `waive` use only identifier characters and `*`, modes, engines, corners and netlist kinds come from fixed lists, depths are bounded integers, and paths are confined.

### Formal: SymbiYosys on the project's own properties

Open Yosys reads immediate assertions (`assert`, `assume`, `cover` in `always` blocks, with `$past`, `$rose`, `$fell`, `$stable`, `$changed`) and not concurrent SVA (`assert property (@(posedge clk) a |-> b)`) or `bind`: both were tried on Yosys 0.33, which rejects the first and silently drops the second. So properties live in the RTL (usually under `` `ifdef FORMAL ``, which `read -formal` defines) or in `formal.sources`, typically a wrapper top that instantiates the design. Concurrent SVA is what the JasperGold backend is for.

ChipLeap writes each check's `.sby` file itself: the project's sources, include directories, defines and parameters (`chparam`), the check's mode, depth and engine, every path absolute, resolved and quoted with `chipleap.rtl.quote`. Engines come from a fixed list: `smtbmc z3` (the default: z3 is in every distribution), `smtbmc yices`, `smtbmc bitwuzla` and `abc pdr`. `smtbmc boolector` is left out: on Yosys 0.33 and Boolector 1.5 the induction step crashes. `abc pdr` turns a counterexample into a trace with yices, so it needs yices too.

A result reports, per check, `pass`, `fail` (a counterexample in the base case), `unknown` (induction failed without a base-case counterexample: the property may hold but needs a stronger invariant or more depth) or `error`; every failed assertion with its location and step; for `cover`, every cover statement reached (and in which step) or not; and each counterexample as a VCD path in the build directory and as structured data: the top module's signals, one value per step. The status line of each counterexample names the property and the source line, so an assistant can tell a wrong assertion from a design bug.

A `.sby` file the project names is run as it is, in the build directory. It is code: `.sby` files may hold Python (`--pycode-begin--`) and Yosys commands that run programs. It is treated like the project's cocotb tests (see Security).

### Proposed assertions through MCP

`check_assertions(project, module, assertions)` takes assertions an assistant proposes, each a boolean expression with a kind (`assert`, `assume`, `cover`) and an optional name, and checks them without touching the project. ChipLeap copies the file that defines `module` into the build directory, inserts a block before that module's `endmodule`:

```systemverilog
  reg chipleap_past_valid = 1'b0;
  always @(posedge clk_i) chipleap_past_valid <= 1'b1;
  always @(posedge clk_i) begin
    if (chipleap_past_valid && rst_ni) begin
      chipleap_p0: assert (count <= DEPTH);
    end
  end
```

and proves it with the copy in place of the original. Labels map each result back to the proposal (verified: sby reports `failed assertion fifo.chipleap_p0 at ... in step 4`). The clock and reset come from the project (the first clock and the first reset, or ones the call names). An expression is accepted only if it is made of identifiers, numbers (sized literals included), operators, parentheses, brackets, commas, `?` and `:`, and the system functions `$past`, `$rose`, `$fell`, `$stable`, `$changed`, `$countones`, `$onehot`, `$onehot0` and `$isunknown`; no `;`, quotes, backticks, comments or other `$` names, and at most 400 characters. Nothing an assistant sends can add a statement, a directive or a file read.

### Equivalence: EQY

EQY 0.33 is built from the YosysHQ tag that matches the system Yosys 0.33 (its plugins are loaded into that Yosys, so the versions must agree) and installed in its own prefix by `scripts/install_formal_tools.sh`, which also installs SymbiYosys from the same tag. Nothing goes on the path: `CHIPLEAP_SBY` and `CHIPLEAP_EQY` point at them, and `chipleap doctor` lists both. The OSS CAD Suite was the alternative; at 750 MB a download it is too heavy for CI, and building the two tools against the distribution's Yosys gives the same tools here and in CI.

Pairs:

- `rtl-synth`: the project's RTL (as synthesis reads it: sources, includes, defines, parameters) against `build/synth/<top>.synth.v`, which the step synthesizes first.
- `synth-pnr`: LibreLane's own synthesized netlist against its final netlist, from the routed run in `build/pnr`. LibreLane synthesizes again from the RTL, so its synthesized netlist, not ChipLeap's, is the one place and route started from.
- `rtl-pnr`: the RTL against the final netlist, for a direct answer.

The gate side reads the technology's liberty with `read_liberty`, which imports each cell's function, so standard cells become logic Yosys can compare; fill, tap, decap and antenna cells have no function and become empty modules. Both sides get `async2sync` (sby cannot model asynchronous resets otherwise: verified). The strategy is EQY's `sat`: registers are matched by name and each partition between them is proved combinationally with a short induction. That proved the D1 counter against its IHP netlist in a second and caught one AND gate changed to OR. Retimed or renamed registers would need the `sby` strategy; that is left for later.

A result reports `pass`, `fail` with the partitions that differ (each with its counterexample VCD) or `error`, and the number of partitions and matched points.

### CDC and RDC: a structural checker on the Yosys netlist

ChipLeap's own checker, in Python, on a netlist Yosys writes as JSON (`proc; flatten; opt -fast; memory -nomap; techmap`): single-bit gates and flip-flops, with memories kept whole. No open-source CDC tool exists that works on this flow, and the structural rules are the ones commercial tools apply first.

Clock domains: each flop's clock pin is traced back through buffers, inverters and clock gating to a clock port of the project. A flop clocked from elsewhere (a divided clock from a flop, an undeclared port) is a domain of its own and a warning. Data inputs belong to the first clock unless `cdc.input_domains` names another; `cdc.async_inputs` are domains of their own, so they must be synchronized like any crossing.

For each flop, the combinational cone of its data, enable and synchronous reset inputs is traced back to its sources: flops, memories and inputs. A source in another domain is a crossing, classified by structure:

- **Synchronizer** (accepted): the destination flop's input is the source directly, and its only load is another flop of its domain. Chains of three or more are recognized too.
- **Logic before a synchronizer** (error): the same shape with gates between the source and the first flop; a glitch can be captured.
- **Bus through bit-wise synchronizers**: several bits of one register each through a synchronizer. Accepted when the source register is gray coded (each bit's input is the XOR of two neighbouring bits of one signal, the top bit a copy), an error otherwise, since the bits can be captured in different cycles.
- **Data qualified by a synchronized control** (accepted, "handshake"): the destination captures through its enable, or through a multiplexer that otherwise holds its own value, and that enable or select depends on a synchronizer's output in the destination domain.
- **Memory read** (accepted, "FIFO memory"): the source is a memory written in one domain and read with an address that comes only from the reading domain, as in an asynchronous FIFO; its pointers are checked by the rules above, so binary pointers still fail.
- **Anything else** (error): an unsynchronized crossing.

Reset checks: each asynchronous reset pin is traced to its reset ports. A reset synchronizer (flops reset by the port, the first with a constant input, the last driving other flops' resets) is recognized. A flop reset straight from a port that is not listed in `cdc.reset_synchronized` gets a warning: its reset is released asynchronously to its clock. A crossing from a flop reset by a port the destination's reset does not include, into a destination that has an asynchronous reset, is a reset domain crossing (error): asserting that reset alone changes the source while the destination keeps sampling. Destinations without a reset are not checked, which keeps ordinary datapaths quiet.

Findings carry the rule, the severity, the source and destination registers (bits grouped), the domains, a message and the path (the nets and cells from source to destination, shortest first). `cdc.waive` entries match source and destination register names with `*` patterns and need a reason; waived findings are reported as waived. The step fails on any error that is not waived.

Known limits, written in the docs: reconvergence of separately synchronized signals, pulse width across clock ratios and glitch-free clock multiplexers are not checked.

### Gate-level simulation with SDF

The project's cocotb tests run on the routed netlist (`final/nl/<top>.nl.v`) with the cell models of the technology and an SDF from the same run, on Icarus 13. Icarus 12 elaborates IHP's cell models but every flip-flop stays X, so every test fails the same way; `scripts/install_iverilog13.sh` unpacks Tiny Tapeout's build of Icarus 13 into its own prefix with wrappers for its library directory, and `CHIPLEAP_IVERILOG` selects it. The step checks the version and skips with that instruction otherwise.

The netlist is copied into the build directory with `initial $sdf_annotate("<sdf>");` added to the top module, so the design under test is still the netlist's top and the tests see the same ports. Icarus runs with `-gspecify` and `-T<delays>`. Tests run with `GATES=yes` in the environment (the convention Tiny Tapeout's gate-level tests use) and with `CHIPLEAP_PARAMETERS`, the project's parameters as JSON, in every simulation, because a netlist has no parameters left for a test to read. `netlist: synth` runs the same tests on ChipLeap's synthesized netlist without delays, which needs no routed run.

### Coverage closure

The coverage step keeps its last summary in `build/coverage/summary.json` and reports, against the run before, how many points the latest tests newly reached. The MCP tool `coverage_gaps(project, file, limit, context)` returns the uncovered points of the last run with the source lines around each, read through the same confinement as everything else, and the project's test modules, so an assistant can write a test that reaches them without reading the tree itself. The `close_coverage` prompt walks it through the loop: run coverage, read the gaps, write a test in the project's test directory, run coverage again, stop at the goal or when the remaining points are unreachable (and say which).

### MCP

New tools: `prove_rtl(project, check)`, `check_assertions(project, module, assertions, mode, depth, clock, reset)`, `equivalence_rtl(project, check)`, `cdc_rtl(project)`, `gate_sim_rtl(project, tests)`, `coverage_gaps(project, file, limit, context)`. Prompts: `propose_assertions` and `close_coverage`. `prove_rtl`, `check_assertions`, `equivalence_rtl` and `cdc_rtl` are replayable like the D1 project tools; `coverage_gaps` reads the last coverage run and is not. Results hold no absolute paths and no wall times.

### Commercial parity

Through the D1 mechanism (`run_steps`, a staged work directory, `--remote` and a setup snippet from the user): JasperGold for formal (concurrent SVA included), Conformal LEC for equivalence, Xcelium for gate-level simulation with SDF, SpyGlass for CDC. Each writes its Tcl from validated, quoted values. Their results on licensed tools are reported privately and never committed.

### Security

The D1 rules hold for everything new:

- Every file the new steps read is a confined project path or a file ChipLeap wrote. `formal.sources` and `formal.sby` go through the same checks as `sources`, including the `` `include `` and `$readmem` scan. The routed netlist and SDF are read from the project's own build directory, through `read_inside`.
- Every generated file (`.sby`, `.eqy`, Yosys scripts, the netlist copy with its `$sdf_annotate`, Tcl for the commercial tools) takes project values only after validation, and quotes them again (`chipleap.rtl.quote`).
- Yosys (the CDC netlist), SymbiYosys and EQY run under the Landlock confinement of D1: read access to the system, their own installation, the project, the liberty and a private temporary directory. For a script tool (`sby` and `eqy` are Python) the interpreter's installation is allowed too.
- Tools run from argument vectors, never a shell.
- Gate-level simulation runs the project's cocotb tests, and a project's own `.sby` files run as they are: both are code, and the docs, the MCP descriptions and the reference say so, as for simulation.

## Acceptance

Each check has a passing and a failing fixture, run in CI:

- CDC flags a missing synchronizer and passes a correct two-flop synchronizer (plus a handshake, a gray-pointer FIFO, a binary-pointer FIFO, logic before a synchronizer and a reset domain crossing).
- EQY proves the counter's netlist equal to its RTL and catches one deliberately changed gate.
- SymbiYosys proves a FIFO's properties and finds the counterexample in a broken FIFO.
- Gate-level simulation runs the UART's cocotb tests on its post-route netlist with SDF (a real LibreLane run here; CI runs the same tests on the synthesized netlist, since it cannot route).

Existing tests pass unchanged.
