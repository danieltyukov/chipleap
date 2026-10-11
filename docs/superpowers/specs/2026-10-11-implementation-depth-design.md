# D3, part b: DFT, gate-level power, IR drop, UPF and ECO

Status: accepted, 2026-10-11. Milestone D3 of `chip-design-roadmap.md`, the part that does not touch the accelerator generator. Part a (real SRAM macros, place and route of the whole SoC) comes later; the `macros` key stays reserved for it.

## Goal

The implementation steps industry expects after place and route exist for an RTL project, each as a step of `chipleap verify`, an MCP tool and a report section: scan insertion with ATPG, power from simulation activity, IR drop, basic power intent, and engineering change orders on the routed database. Every number comes from a named engine; ChipLeap writes the inputs, runs the tool and reads its reports.

## What the tools on this machine can do (checked 2026-10-11)

- **OpenROAD** (the `openroad` command, `openroad/orfs:latest`): `scan_replace`, `set_dft_config`, `execute_dft_plan`, `report_dft_plan`; `read_upf`, `write_upf`, `create_power_domain`, `set_domain_area`, and power-domain regions that `initialize_floorplan` and global placement honour; `read_vcd`, `report_power`; `analyze_power_grid`, `set_pdnsim_net_voltage`. `report_power -highest_power_instances` fails in this build, so it is not used. The OpenROAD inside LibreLane's container (2025-06-12) names the DFT commands `insert_dft` and `preview_dft`; scripts check which exist.
- **OpenROAD scan insertion** works on a netlist without placement: on the UART it replaced 51 `sg13g2_dfrbpq_1` with `sg13g2_sdfrbpq_1` and stitched three chains of 17 with `-max_length 20`, adding `scan_enable_0`, `scan_in_N` and `scan_out_N`. Instance names are kept. IHP's liberty has `test_cell` groups for `sg13g2_sdfbbp_1`, `sg13g2_sdfrbp_{1,2}` and `sg13g2_sdfrbpq_{1,2}`.
- **Fault** 0.9.4 (`pip install fault-dft`, Apache-2.0) with **Quaigh** 0.0.6 (`cargo install quaigh`, MIT or Apache-2.0) as the pattern generator. On the UART: `fault cut`, `nl2bench`, `fault atpg -g Quaigh` give 99.2 % stuck-at coverage with 26 compacted patterns in 9 s. Two installation traps: Fault embeds Python and does not see its own virtual environment unless `PYTHONPATH` names its site-packages, and it runs `vvp` from the path unless `FAULT_VVP` is set, so a newer `iverilog` with an older `vvp` makes every simulation fail with "Initial coverage: 0.0%". Atalanta and NTU PODEM, the other generators Fault drives, are proprietary and not used.
- **Icarus 13** (built from the `v13_0` tag into `~/.local/opt/iverilog-13`) simulates IHP's cell models; Icarus 12 leaves every flop at X. All five UART cocotb tests pass on the routed netlist with it.
- **cocotb's Icarus runner** passes `-none` to `vvp` unless waves are on, and then forces FST. OpenSTA reads VCD. The runner script swaps `-none` for `-vcd` when the manifest asks for a VCD.
- **LibreLane** has no DFT, UPF or generic script step, but it resumes: a configuration whose `meta.flow` lists step ids, started with `--with-initial-state` on a step's `state_in.json`, runs only those steps. `--initial-state-element-override` drops the state's metrics (Magic then fails on a missing `DIE_AREA`), so ChipLeap writes the state file itself. `OpenROAD.DEFtoODB` turns an edited DEF back into LibreLane's own database. A gate resized on the UART this way re-placed, re-routed and re-signed off clean (DRC, LVS, antenna) in 5.6 min, 4 min of it KLayout DRC, against 5.3 min for the whole flow.
- **LibreLane with a gate-level input** needs `RUN_LINTER: false` (Verilator has no cell models); `SYNTH_ELABORATE_ONLY` leaves `assign` statements that `Checker.NetlistAssignStatements` rejects, so the scan netlist goes through LibreLane's normal synthesis, which keeps the cells and buffers the assignments.

## Decisions

### Steps

`PROJECT_STEPS` becomes `lint, sim, coverage, synth, dft, pnr, eco, power, ir_drop, upf`. The default stays `lint,sim,coverage,synth`; `all` runs every step. A step whose section is missing skips with a reason, so `all` is safe on any project.

| Step | Needs | Engine | Passes when |
|---|---|---|---|
| `dft` | a `dft` section | Yosys netlist, OpenROAD scan insertion, Fault with Quaigh, Icarus 13 | every flip-flop is on a chain (or reported as unscannable), the chain shifts a flush pattern, every simulated pattern matches, and coverage reaches `min_coverage` when one is set |
| `pnr` | | LibreLane, on the scan netlist when `dft.insert` is true | as in D1 |
| `eco` | an `eco` section | OpenROAD on the routed DEF, then LibreLane resumed from detailed routing | the changes apply and the result meets D1's place-and-route criteria |
| `power` | | Icarus 13 with the project's cocotb tests on the gate-level netlist, OpenROAD `read_vcd` and `report_power` | the tests produce a VCD whose activity annotates the netlist |
| `ir_drop` | a routed design | OpenROAD PDNSim with the VCD's activity | the worst drop on every supply is within `max_drop_pct` when one is set |
| `upf` | a `upf` section | OpenROAD `read_upf`, floorplan and placement on a hierarchical netlist | every domain resolves, its cells land inside its area, and no crossing needs a cell the library lacks |

`eco` runs before `power` and `ir_drop` so that they measure the changed layout. The routed result the later steps read is `build/pnr/runs/eco/final` when an ECO ran on the current routed run, else `build/pnr/runs/chipleap/final`.

### The project sections

```yaml
dft:
  max_chain_length: 100       # flip-flops per chain (set_dft_config -max_length)
  max_chains: 0               # 0: as many as the length needs
  clock_mixing: no_mix        # no_mix | clock_mix
  insert: true                # place and route the scan netlist
  atpg: true                  # run Fault when it is installed
  min_coverage: 90            # percent; optional
  patterns: 8                 # ATPG patterns replayed through the chain in simulation
power:
  corners: [typ]              # typ, slow, fast: IHP's 1.2 V 25 C, 1.08 V 125 C, 1.32 V -40 C
  netlist: auto               # auto | routed | synth
  scope: ""                   # the design's instance in the test top, when the tests wrap it
  input_activity: 0.1         # for the vectorless estimate it is compared with
ir_drop:
  corner: typ
  max_drop_pct: 5             # optional pass threshold, percent of the supply
upf:
  domains:
    - {name: PD_TOP}                                   # the top: every element not listed below
    - {name: PD_RX, elements: [u_rx], area_um: [5, 5, 60, 100], voltage: 1.2}
eco:
  - {resize: _174_, cell: sg13g2_a21oi_2}             # same pins, another drive strength
  - {buffer: _123_/A, cell: sg13g2_buf_2}             # a buffer in front of a sink, or after a driver
  - {repair: setup, margin_ns: 0.5}                   # OpenROAD's repair after global routing
```

Every key is checked at load, with the key path in the error, as in D1. Domain names and elements are identifiers (elements are hierarchical instance paths, `u_rx` or `u_core/u_alu`); `area_um` is four non-negative numbers with a positive width and height. ECO instance and pin names are netlist names, which carry characters D1 refuses (`\u_rx.data_q[3]`), so they are held to a looser rule: printable, no white space, no quotes, braces, `;`, backtick or `$`, at most 200 characters. They never enter a script as code (below). Cells are identifiers that must exist in the library, which OpenROAD checks.

### Engines and how they meet

- OpenROAD is the `openroad` command (`CHIPLEAP_OPENROAD`), run through `chipleap.verify.launch.run`, so a time limit, Ctrl-C or an MCP cancel stops it and its container as it stops LibreLane.
- Two OpenROAD builds are in play (the command and LibreLane's own). Their databases are not exchanged; DEF, Verilog, SPEF, SDC, liberty and LEF are.
- Gate-level simulation uses an Icarus 13 or newer found through `CHIPLEAP_GL_IVERILOG`, else `iverilog` on the path when it reports version 13 or newer. `vvp` comes from the same directory. The cell models are the PDK's `sg13g2_stdcell.v`, plus `sg13g2_udp.v` where the PDK splits them out.
- PDK files come from the routed run's `resolved.json` when there is one, else from the PDK that holds the liberty ChipLeap already finds (`chipleap.verify.tools.ihp_liberty`). Either way each file must lie under a PDK root ChipLeap knows (`PDK_ROOT`, `~/.ciel`, `~/.volare`, `~/.local/share/pdk`, `/usr/local/share/pdk`, `/opt/pdk`, or the directory of `CHIPLEAP_LIBERTY`) and have the expected suffix.

### DFT

1. Synthesize with D1's `synthesize` into `build/dft/`.
2. OpenROAD reads the netlist, the project's clocks, `set_dft_config` from the section, `scan_replace`, `execute_dft_plan` (or `insert_dft`), `report_dft_plan -verbose`, and writes `<top>.scan.v`.
3. ChipLeap traces every chain through the scan netlist itself, from `scan_in_N` along `SCD` and `Q` to `scan_out_N`, and checks the order against OpenROAD's report. The trace is the order the patterns use.
4. Fault cuts the pre-scan netlist (its flip-flops become inputs and outputs), `nl2bench` writes the bench file, and `fault atpg -g Quaigh` generates and fault-simulates the patterns with a fixed seed. Coverage is Fault's simulated stuck-at coverage; Quaigh's own figure is reported beside it.
5. ChipLeap maps each pattern onto the chains (flip-flop values in shift order, primary inputs, expected outputs and captured values) and exports `patterns.json` (documented below) and `patterns.stil`, a STIL (IEEE 1450) subset with the scan structures, a load-unload procedure, a capture procedure and one pattern per vector.
6. A generated Verilog testbench runs on Icarus 13 against the scan netlist and the cell models: a flush test (0011 repeated, length plus four, through every chain) and then `patterns` ATPG patterns (shift in, apply inputs, compare outputs, one capture clock, shift out and compare). Mismatches are reported by chain, cell and pattern.

`patterns.json` holds `chains` (name, `scan_in`, `scan_out`, `scan_enable`, `clock`, cells from scan-in to scan-out) and `patterns` (for each: `load` per chain in cell order, `inputs`, expected `outputs`, expected `unload` per chain in cell order). Bits are strings of `0` and `1`, most significant first for buses, so the file reads without a schema.

With `insert: true` (the default), `pnr` routes the scan netlist: the configuration names `src/<top>.scan.v`, sets `RUN_LINTER: false`, drops `SYNTH_PARAMETERS`, and the generated SDC constrains the scan ports like any other input or output. Shift paths are timed at the functional clock, which is conservative, and hold repair covers them.

### Power

The netlist is the routed one (`final/nl`, with `final/spef/nom` and `final/sdc`) when a routed result exists and `netlist` is `auto` or `routed`, else D1's synthesized netlist without parasitics. The project's cocotb tests run on it with Icarus 13, the cell models and a second top module that dumps a VCD. Tests see `GATES=yes` (Tiny Tapeout's convention) and `CHIPLEAP_GATES=1`, so a white-box test can skip what a netlist no longer has; the UART's tests read `CLKS_PER_BIT` that way. Scan ports, if any, are forced to their functional values (`scan_enable` low) by the dump module.

For each corner OpenROAD reads the liberty, the netlist, SDC and SPEF, reports power vectorless (input activity `input_activity`), then reads the VCD with `-scope` and reports again; `report_activity_annotation` gives how many pins the VCD annotated. ChipLeap checks the clock period in the VCD against the SDC's and says when they differ by more than 1 %, because activity rates are per clock. The step reports internal, switching, leakage and total power per group (sequential, combinational, clock, macro, pad) at every corner, both ways, and LibreLane's own `power__total` when routed. The gate-level test results are reported as well: a test that fails on the netlist fails the step.

### IR drop

OpenROAD reads the routed DEF (with its power grid), the corner's liberty, SDC, SPEF and the power step's VCD (vectorless when there is none), finds the supply nets from the DEF's power and ground nets, sets their voltages from the corner, and runs `analyze_power_grid` on each with a voltage file. ChipLeap reports worst and average drop per net in millivolts and percent, the worst instances, and draws a map: an SVG of the core with each instance terminal coloured by its drop, written to `build/ir_drop/<net>.svg` and inlined in the report. No external image library is needed.

### UPF

The open flow can read power intent and use it for floorplanning and placement; it cannot simulate it. ChipLeap writes the UPF from the section (so every name is checked and quoted), with the top domain as `-elements {.}`, one `create_power_domain` per other domain and `set_domain_area` for each area given; a domain without an area gets a strip at the left of the core sized from its cell area at the project's placement density. Synthesis for this step keeps the hierarchy (no `-flatten`) so the elements exist. OpenROAD links with `-hier`, reads the UPF, floorplans, places, and ChipLeap counts each domain's cells and checks they are inside its area, then `write_upf` shows what OpenROAD understood.

From the hierarchical netlist ChipLeap lists the signals that cross between domains. Domains may carry a `voltage`; when two connected domains differ, the crossings need level shifters, which IHP SG13G2's standard cells do not have, and the step fails saying so. Isolation and power switches are out of scope for the same reason.

Documented limits: no open simulator is power aware (Icarus, Verilator and cocotb ignore UPF, so a switched-off domain is never simulated as off); LibreLane routes one supply and ignores the UPF, so the GDS from `pnr` has one domain; level shifters, isolation cells, retention and power switches need cells IHP's library lacks. Commercial tools cover these.

### ECO

An ECO changes the routed database and checks it again without running synthesis, floorplanning, placement or clock-tree synthesis again.

1. The base is the current `chipleap` run: the `state_in.json` of its `OpenROAD.DetailedRouting` step, which holds the placed, clock-treed, globally routed design. The step list after it comes from each step directory's `config.json` (`meta.step`), so the ECO re-runs exactly the signoff the original ran.
2. OpenROAD reads that DEF and applies the changes from a tab-separated data file it reads with `gets`, using the database API (`findInst`, `findMaster`, `swapMaster`, `dbInst_create`, `dbNet_create`, `connect`, `disconnect`). No name from the project or an MCP client is ever part of a script. A resize must keep the pin list (`swapMaster` refuses otherwise); a buffer goes after a driver's output pin or in front of a sink's input pin, placed at the target and legalized next. It writes the edited DEF and a log of what it did.
3. LibreLane resumes in `build/pnr` with tag `eco`: `OpenROAD.DEFtoODB`, `OpenROAD.DetailedPlacement`, `OpenROAD.GlobalRouting`, `OpenROAD.ResizerTimingPostGRT` when a `repair` change asks for it (with its margin), then the original steps from `OpenROAD.DetailedRouting` on. ChipLeap writes the initial state file from the base, with the edited DEF.
4. The result is parsed like D1's place and route and reported next to the base: setup and hold slack per corner, cells, DRC, LVS and antenna counts. `build/pnr/runs/eco/chipleap-eco.json` records the changes and a digest of the base's metrics, so later steps use the ECO result only while it belongs to the current routed run.

Changes always apply to the original routed run, as a whole list. Trying A and then A plus B is two ECOs from the same base, so an ECO result is a function of the base and its list and the `eco` section in `project.yaml` reproduces it. `chipleap eco PROJECT` takes changes on the command line (`--resize INST=CELL`, `--buffer INST/PIN=CELL`, `--repair setup|hold`), and `--paths` prints the worst paths to choose them from: OpenROAD's `report_checks` on the routed netlist and SPEF at a corner, parsed into start, end, slack and every stage's pin, cell, instance and delay.

### MCP tools

| Tool | Returns | Replayed |
|---|---|---|
| `dft_rtl(project)` | chains, ports, unscannable cells, coverage, pattern counts, simulation result, pattern files | yes |
| `power_rtl(project, corners)` | per corner and group, vectorless and from the VCD; annotation; gate-level test results | no (reads `build/`) |
| `ir_drop_rtl(project, corner)` | worst and average drop per supply net, worst instances, map path | no |
| `upf_rtl(project)` | domains, cell counts, containment, crossings, the UPF OpenROAD wrote | yes |
| `timing_paths_rtl(project, corner, kind, count)` | the worst setup or hold paths of the routed design, stage by stage | no |
| `eco_rtl(project, changes)` | what was applied, before and after timing and checks | no |

The long ones run in worker threads that stop their OpenROAD and LibreLane runs when the call is cancelled. `fix_timing`, a new prompt, walks an assistant through `timing_paths_rtl` then `eco_rtl`, one change at a time, and tells it to put a change that works into the project's `eco` section.

### Security

D1's rules hold, and:

- **Data, not code.** Names that may come from an MCP client or a project file (ECO targets and cells) reach OpenROAD only through data files. Values that go into generated Tcl, UPF or Verilog (identifiers, numbers, paths) are validated at load and quoted with `chipleap.rtl.quote`; a value the quoting refuses fails the step.
- **What the steps read.** A project's `build/` directory could come from someone else's repository, so the paths in LibreLane's `resolved.json` and `state_in.json` are confined: run files to the project's `build/pnr`, PDK files to the PDK roots above. A path outside fails the step without being opened.
- **Tool output.** Reports, CSV voltage files, VCD headers, Fault's JSON and YAML and OpenROAD logs are read with size limits (a few tens of megabytes, enough for these designs), line by line where they are text, and every loop ends at end of file. A malformed report fails the step with the parser's reason.
- Fault, Quaigh, Icarus and OpenROAD run the project's netlist, not its Python; only the power step runs the cocotb tests (code), as `sim` does.

### Commercial parity

Optional and private: Cadence Modus for ATPG, Voltus for IR drop and power, Tempus for signoff timing, Joules for RTL power, through `verify/backends.py` where they fit. Results from licensed tools never go into the repository.

## Public API for D4 and part a

- `chipleap.rtl.project`: `DftConfig`, `PowerConfig`, `IrDropConfig`, `UpfConfig`, `PowerDomain`, `EcoChange`; fields `Project.dft`, `.power`, `.ir_drop`, `.upf`, `.eco`.
- `chipleap.rtl.openroad`: `run_openroad(script, workdir, ...)`, `PdkViews` and `pdk_views(project, run=None)`, `routed_run(project)`.
- `chipleap.rtl.netlist`: `parse_netlist(text)` (modules, ports, instances, connections, assigns) and `trace_chains`.
- `chipleap.rtl.dft`: `insert_scan(project) -> ScanResult`, `run_atpg`, `simulate_patterns`, `dft(project)`.
- `chipleap.rtl.power`: `gate_level_vcd(project, netlist)`, `report_power(...)`, `power(project)`.
- `chipleap.rtl.irdrop`: `analyze(project)`, `ir_drop(project)`, `voltage_map_svg`.
- `chipleap.rtl.upf`: `write_upf(project)`, `upf(project)`, `crossings`.
- `chipleap.rtl.eco`: `EcoResult`, `apply_eco(project, changes)`, `timing_paths(project, corner, kind, count)`, `eco(project)`.

## Acceptance

- The UART, with a `dft` section, gets three scan chains; Fault reports its coverage; the patterns pass a shift-and-capture simulation on the scan netlist; the scan netlist routes clean.
- The UART's report shows gate-level power from its cocotb tests at three corners beside the vectorless figure, and the worst IR drop with a map.
- The UART with a `upf` section places `u_rx` inside its domain area, and a fixture with two voltages fails for want of level shifters.
- An ECO on the UART resizes a gate on a path, re-routes and re-signs off clean; a fixture that misses timing at the slow corner passes after `repair: setup`.
- Existing tests stay green; the new unit tests run without OpenROAD; tests that need OpenROAD, Fault, Icarus 13 or LibreLane skip without them.

## Out of scope

SRAM macros and whole-SoC place and route (part a); pad rings (D4); transition-delay and at-speed patterns, compression and JTAG (Fault's `tap` is not used); power-aware simulation; electromigration.
