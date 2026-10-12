# D4: digital-on-top chip assembly

Status: accepted, 2026-10-12. Milestone D4 of `chip-design-roadmap.md`, on the project format of D1 and the checks of D2. aispice builds analog-on-top chips with its own router (A4 part 2); ChipLeap builds digital-on-top chips with OpenROAD in the LibreLane flow. The two share the macro package (`docs/macro-package.md`), not code.

## Goal

A project's RTL, any number of hardened `macro/v1` packages (an aispice comparator, say) and a pad ring from IHP's `sg13g2_io` library become one chip GDS: placed and routed by OpenROAD in LibreLane, with power from the pad ring to the core and to every macro, bond pads, a seal ring and fill, checked with IHP's own DRC deck on the whole chip and its LVS deck down to the transistors of the standard cells and the macros, with a bond map and the GDS-level checks a shuttle runs.

## Facts this design rests on

Measured on 2026-10-12 with LibreLane 3.0.0.dev44 (its IHP PDK at `cb7daaa`), KLayout 0.30.4, IHP-Open-PDK `22f2a25` and the StrongARM comparator package aispice's `feat/macro-export` exports, in a prototype that became this design.

1. LibreLane 3.0.0.dev44 has no pad ring flow. Its Classic flow places any instance from `MACRO_PLACEMENT_CFG`, takes a die and a core area (`DIE_AREA`, `CORE_AREA`), a PDN script of the user's own (`PDN_CFG`), routing obstructions, a DEF template for pins (`FP_DEF_TEMPLATE`) and step substitutions in the configuration (`meta.substituting_steps`, which can also insert a step). That is enough to build the chip in one flow.
2. The IO cells' liberty in LibreLane's PDK has no `sg13g2_IOPadAnalog`; aispice's liberty has no threshold attributes, which OpenSTA rejects. ChipLeap writes the liberty of every IO cell and macro it uses: pins, directions, supply pins, capacitances, thresholds, no timing arcs.
3. `sg13g2_IOPadAnalog`'s `pad` pin has a port on the bond side as well as the core side. The router took the bond-side one and routed through the ring (shorts with the ring's rails). A copy of the IO LEF without that port, and routing obstructions over the ring (Metal1 to Metal5, from 3 um inside its outer edge to 1 um short of its inner edge), keep every route in the core and on the core-side pins. Signals route up to Metal5; TopMetal1 and TopMetal2 carry power.
4. A design without signal ports breaks parasitic extraction (an empty `*PORTS` section), and a port placed on top of an IO cell's pin breaks OpenRCX (`ODB-0420 tilepin inside iterm`). The chip's ports are the digital pads' nets, each a 2 um pin just outside its IO cell's bond side, which the router joins to the pad's bond-side port. Analog pads have no port.
5. pdngen's `add_pdn_ring -connect_to_pads -connect_to_pad_layers {TopMetal1 TopMetal2}` connects the core ring to the IO ring's rails: TopMetal2 straps with a TopVia2 onto the `vdd` rail (TopMetal1, 140 to 158 um from the outer edge) and onto the `vss` rail (TopMetal1, 160 to 178 um). Both rails run unbroken through every IO cell, filler and corner, so the supply pads reach the core through the ring.
6. aispice's macros have their supply pins as 0.3 um Metal4 columns reaching the bottom edge; pdngen's macro grid finds nothing to connect there (PDN-0232). ChipLeap draws the hookup itself in the PDN script: a Metal4 strap from each supply pin straight down to the nearest standard-cell rail of the same net, with a via stack to Metal1.
7. The comparator's LEF has `ORIGIN 8.55 0.6`; KLayout's stream-out places a macro's GDS cell by its own (0, 0), so pins and layout disagreed by that offset (one Metal4 minimum-area error, and an open in LVS). ChipLeap moves the macro's LEF and GDS copies to `ORIGIN 0 0`. Magic's stream-out needs a PR boundary in the macro's GDS, which aispice's has not; ChipLeap streams out with KLayout only.
8. Netgen LVS on LibreLane's LEF/DEF extraction shorted the whole design once the IO cells were in it, and Magic's GDS extraction of IHP's flip-flops does not match their netlists. IHP's KLayout LVS deck compares the chip at transistor level, standard cells and the macro included, once the IO cells (which it cannot compare: aispice's fact 3) are taken out and their core-side pins kept as shapes labelled with their nets: PASS in 13 s on the prototype, and on the filled chip in 15 minutes.
9. IHP's `filler.py` (Activ, GatPoly, Metal1 to Metal5, TopMetal1 and 2) brings the prototype chip within every density rule; IHP's DRC deck (main, density, offgrid, antenna-free) passes on the finished chip except its recommended pad rules (`Pad.d1R`, `Pad.fR_*`), which bond pads abutting their IO cells, as Croc places them, always break. They are recommendations, so chip DRC runs with `--no_recommended` and says so.
10. The `bondpad` and `sealring` pcells build only with string parameters (`"70u"`); given floats, the pcell code spins forever. The seal ring's `w` is its height and `l` its width, each without the 25 um edge box on both sides.
11. Times on the prototype (1.4 mm die, 33 pads, the UART and one comparator): LibreLane 2.5 min, bond pads and seal ring 2 s, fill 2.5 min, DRC 8 min, LVS 13 s.

## Decisions

### The `chip` section

```yaml
chip:
  name: uart_chip             # the GDS top cell; default <top>_chip
  pad_pitch_um: 100           # 90 or more; default 100
  core_um: [400, 400]         # the standard-cell area, centred; default from synthesis
  pads:                       # each side in increasing x (south, north) or y (west, east)
    south: [tx_data_i[0], tx_data_i[1], vdd, vss, clk_i, rst_ni, tx_valid_i]
    east: [tx_ready_o, tx_o, iovdd, iovss]
    north: [rx_data_o[0], rx_data_o[1], vdd, vss, rx_valid_o, rx_error_o]
    west: [iovdd, iovss, {analog: ain_p}, {analog: ain_m}, null]
  macros:
    - instance: comp
      package: macros/strongarm.macro
      at_um: [60, 260]        # lower left, from the core's lower left
      orientation: N          # N, S, FN, FS
      connect: {clk: clk_i, inp: ain_p, inm: ain_m, outp: rx_i}
```

- A pad names a bit of the core top's ports (`clk_i`, `tx_data_i[3]`): an input becomes `sg13g2_IOPadIn`, an output `sg13g2_IOPadOut4mA` (the 16 and 30 mA cells hold clamps whose Metal1 pins are off grid on IHP's deck). `vdd`, `vss`, `iovdd` and `iovss` are supply pads; `{analog: NET}` is `sg13g2_IOPadAnalog`, its core-side `pad` pin on net `NET` (`resistor: true` takes `padres`, through the secondary protection); `null` leaves the slot to fillers. Each kind of supply pad is required at least once.
- A macro pin connects to a net: a bit of the core's ports, an analog pad's net or a net another macro pin names. Supply pins (kind `power` and `ground` in `macro.toml`) go to `VDD` and `VSS` and need no entry. Every core input must be driven by exactly one input pad or macro output; core outputs and macro outputs may stay unconnected (a warning); inout ports are refused.
- Validation as for every section: identifiers, port bits that exist, kinds from fixed lists, pitches, sizes and coordinates in bounded ranges, package paths confined to the project like every other path.

### Floorplan

From the die edge inward: a 70 um bond-pad band, the 180 um IO ring (corners 180 x 180 um, pads 80 um wide at `pad_pitch_um`, centred in their slots, every gap filled exactly with 50, 20, 10, 5, 2 and 1 um fillers), then the core area, centred. The die is as wide as the most pads on the south or north side need and as tall as the east or west side need. Orientations: south `N`, north `S`, west `E`, east `W`; corners `N`, `FN`, `FS`, `S` (a corner's rails leave at its top and right in `N`). Macros sit inside the core with a 10 um placement halo, on the manufacturing grid, without overlap. The seal ring goes around the die with a 60 um gap (Croc uses 42); the chip's outline is the seal ring's.

### LibreLane

ChipLeap writes into `build/chip/`: the chip top (`chip_top.v`: the core top, the macros and every ring cell, the cells kept with `(* keep *)`), the IO library views (GDS and CDL copied from the PDK ChipLeap uses, the LEF without the analog pad's bond-side port, a liberty), each macro's views, the macro placement, the pin template, the SDC (clocks on the input pads' `p2c` pins, IO delays on `p2c` and `c2p`) and `pdn.tcl`. The Classic flow runs with: `IOPlacement`, Magic's extraction and its overlap checker removed, `WriteCDL` added after post-route STA, `RUN_LVS`, `RUN_KLAYOUT_DRC` and `RUN_MAGIC_STREAMOUT` off (each runs later on the finished chip, or cannot run on this chip: facts 7 and 8), `RT_MAX_LAYER Metal5`, `GRT_ALLOW_CONGESTION` (the pin-access congestion at the ring's edge that detailed routing resolves; detailed-routing DRC must be zero), the routing obstructions over the ring and LibreLane's own checks (routing DRC, disconnected pins, power grid, antenna, timing) as they are.

### Macros

Every package is resolved inside the project, checked with `chipleap.macro.check_package` against `ihp-sg13g2` (errors stop the chip, warnings are reported; a commit other than the PDK LibreLane uses is a warning) and read only by the file names its validated manifest gives. From it ChipLeap writes its own views: the LEF rebuilt from the statements it reads and the GDS moved to `ORIGIN 0 0`, a liberty from the manifest's pins (no timing arcs, so nothing a package says reaches OpenSTA as text), the SPICE reduced to its `.subckt` blocks with device lines only for LVS. Every GDS cell and subcircuit must carry the macro's name, so none can replace a cell of the chip; `chipleap.chip.merge` then checks the names LibreLane actually merged (and the chip top's Verilog modules before it runs), so a name defined twice is refused even where the predicted names miss it. Supply hookup (fact 6) joins supply pins that reach the macro's bottom or top edge on Metal2 to Metal5; supply pins on TopMetal1 or TopMetal2 (pdngen's macro grid) are left for later and refused for now.

### Finishing

A KLayout script ChipLeap writes from checked values: IHP's `bondpad` pcell (70 um square) against the bond side of every pad, the `sealring` pcell around the die, written as `<name>_nofill.gds`; then IHP's `filler.py` writes `<name>.gds`. KLayout runs with a private `TMPDIR` and `HOME` (the pcell library writes fixed file names into the temporary directory) and no display.

### Checks

- **DRC**: IHP's `run_drc.py` on the final GDS: every table, density included, without recommended rules (fact 9). The result lists every violated rule with its count.
- **LVS**: IHP's `run_lvs.py` on an LVS view of the chip before fill (the IO cells, bond pads and seal ring taken out, each IO cell's core-side pins kept and labelled with their nets) against a netlist made from OpenROAD's CDL (the IO cells taken out, their core-side nets made ports) with the standard cells' CDL and each macro's reduced SPICE. A pad wired to the wrong net, a macro pin left open or two nets shorted all fail it. Fill is floating metal that DRC keeps at its spacing from everything, so it cannot short nets; LVS of the filled chip passed too (fact 8) and takes minutes, so it is not the default.
- **Supply hookup**: from the final DEF, the core ring's straps that reach the IO ring's rails, per net and side; none for `VDD` or `VSS` fails the chip, since the IO cells are not in LVS.
- **Precheck**, the GDS-level checks a shuttle runs that are public: the top cell's name, layers only from IHP's layer map (`sg13g2.lyp`), no cell names with `#` or `/` (Tiny Tapeout's checks), no zero-area shapes, a PR boundary on the die (LibreLane writes one; the finishing adds it when missing), the layout's extent on the seal ring's outline and the manufacturing grid. Density is in the DRC run. Each check reports pass or fail with what it found.
- **Bond map**: `<name>.bondmap.csv` and `.json`: pad number (1 is the top pad of the west side, then counter-clockwise), side, net, kind, IO cell, bond pad centre and size in um.

### Where it lives

`chipleap.chip`: `config` (the section), `io` (the IO library), `floorplan`, `netlist`, `pdn`, `flow` (LibreLane), `run` (KLayout and IHP's runners), `checks` (DRC, LVS, hookup, precheck, bond map), `assemble` (the step), and KLayout scripts in `klayout/`. The macro layer is `chipleap.macro.layer`, shared with D3 part a's SRAMs and later the generated accelerators: `import_packages` (`macro/v1` packages), `pdk_macro` (a PDK macro such as `RM_IHPSG13_1P_*`), `MacroView` and `MacroPin`, `reserved_names`, and `librelane_config` (`MACROS`, `EXTRA_CDLS`). `chipleap verify PROJECT --steps chip` runs it (only when named, like `pnr`), `chipleap chip plan PROJECT` shows the floorplan without running a tool, and the MCP tools `plan_chip` and `assemble_chip` do the same. Every step runs under `runs_tools`, writes through `builddir`, quotes every value it puts in Tcl, Verilog, DEF, JSON for KLayout or SDC, and confines what it reads. Results hold no absolute paths.

## Security

- Packages are untrusted: the validator runs first, file names come from the manifest after validation, the files are copied into the build directory through `Project.read` (confined, no symlink out) and only the copies reach the tools. The SPICE is reduced to one subcircuit; nothing from a package reaches a script as code.
- Every name that reaches Verilog, DEF, Tcl or KLayout's JSON plan is an identifier (pads, nets, instances, cells) checked at load time and again by the writers and by the KLayout scripts. Numbers are bounded and on the grid.
- KLayout, LibreLane, IHP's DRC and LVS runners run from argument vectors with a cleaned `PATH` (D2's `tools.clean_env`) inside `runs_tools`, confined by Landlock to the system, their installation, the PDK, the build directory and a private `TMPDIR` and `HOME` (the DRC runner also `/dev/shm`, for its process pool's semaphores). IHP's runners build shell command lines from their arguments, so they run in a private directory of plain characters on copies of their inputs, and the PDK's path must be plain.

## Acceptance

- The UART example with aispice's StrongARM comparator (its output into the UART's receive line, its inputs on two analog pads) in a pad ring: a full-chip GDS that is DRC clean on IHP's deck, LVS clean on IHP's deck with the comparator's transistors compared, with the precheck passing and a bond map.
- Tests without tools for the section, the floorplan, the netlist, the IO views, the macro views, the PDN script, the LVS netlist and view plan, the precheck and the bond map; refusals of bad packages, names and paths; and an LVS that fails when two pads' nets are swapped.
