# Chips

A project with a `chip` section becomes a whole chip: its RTL as the core, any number of hardened blocks in [macro packages](macro-package.md) (an aispice comparator, say) and a pad ring of IHP SG13G2's `sg13g2_io` cells, placed and routed by OpenROAD in LibreLane, with bond pads, a seal ring and fill, then checked with IHP's own DRC deck on the whole die and its LVS deck down to the transistors of the standard cells and the macros.

```sh
chipleap chip plan examples/uart              # the floorplan, without running a tool
chipleap verify examples/uart --steps chip    # build the chip and check it
```

The [UART example](../examples/uart/project.yaml) is such a chip: 33 pads on a 1.4 mm die (1.52 mm with the seal ring), with aispice's StrongARM comparator whose inputs are two analog pads and whose output is the UART's receive line.

## The chip section

```yaml
chip:
  name: uart_chip             # the GDS top cell; default <top>_chip
  pad_pitch_um: 100           # 90 to 1000; default 100
  core_um: [400, 400]         # the standard-cell area, centred; default: what the ring leaves
  pads:                       # each side in increasing x (south, north) or y (west, east)
    south:
      - tx_data_i[0]          # a bit of a core port: an input or output pad
      - vdd                   # supply pads: vdd, vss (core, 1.2 V), iovdd, iovss (3.3 V)
      - vss
      - clk_i
    east: [iovdd, iovss]
    west:
      - {analog: ain_p}                   # an analog pad; its core-side pin is net ain_p
      - {analog: vref, resistor: true}    # through the pad's secondary protection (padres)
      - null                              # an empty slot, filled
  macros:
    - instance: comp
      package: macros/strongarm.macro     # a macro/v1 package inside the project
      at_um: [60, 260]        # lower left, from the core's lower left; default: a row along its top
      orientation: N          # N, S, FN or FS
      connect: {clk: clk_i, inp: ain_p, inm: ain_m, outp: rx_i}
  io:                         # the world outside the pads, for timing at the chip's ports
    input_delay_ns: 2         # default: a fifth of the first clock's period
    output_delay_ns: 2        # default: a fifth of the first clock's period
    output_load_pf: 5         # what each output pad drives; default 5
    input_transition_ns: 1    # the edge rate at the input pads; default 1
```

A pad names a bit of the core top's ports (`clk_i`, `tx_data_i[3]`); an input becomes `sg13g2_IOPadIn` and an output `sg13g2_IOPadOut4mA`, by the port's direction as Yosys elaborates the top with the project's parameters. Inout ports are refused. `{analog: NET}` is `sg13g2_IOPadAnalog`, its core-side `pad` pin on `NET` (`padres` with `resistor: true`). Each supply kind is needed at least once.

A macro pin connects to a bit of the core's ports, an analog pad's net, or a net other macro pins name. Its supply pins (kind `power` and `ground` in `macro.toml`) go to the core's `VDD` and `VSS` by themselves. Every core input must be driven by exactly one input pad or macro output; every macro input must be connected; a core output on no pad, a macro output left open and a net only one pin reaches are warnings. A net the chip section names may not be one ChipLeap names itself (`<bit>_core`, `pad_<bit>`).

The section is checked like every other: names are identifiers and not Verilog keywords, kinds and orientations come from the lists above, pitches, sizes and coordinates are bounded, and a package is a path inside the project like any other.

## Floorplan

From the die edge inward: a 70 um band for the bond pads, the 180 um IO ring, then the core, centred on the site grid with at least 50 um to the ring for its power ring. The ring has a corner cell at each corner (`N`, `FN`, `FS`, `S`), pads 80 um wide centred in their slots at the pitch, facing the die edge (south `N`, north `S`, west `E`, east `W`), and every gap filled exactly with 50, 20, 10, 5, 2 and 1 um fillers. A side is as long as its pads at the pitch, or as the core needs, whichever is more. Without `core_um` the core takes what the ring leaves, 100 um in from it on each side. Macros sit inside the core on the site grid, 10 um apart (their halo, kept free of standard cells). The core's own macros, from the project's `macros` section (an SRAM of the PDK, say), are placed with them, inside the core's instance (`u_core.u_mem`): where their `at_um` puts them in the chip's core, or along the core's top edge. An SRAM sits under pdngen's macro grid, which needs a whole TopMetal2 VDD and VSS pair of the core's grid over it, so one placed along the top edge moves down, on the row grid, until a pair lies over it. The seal ring goes 60 um outside the die, and its outer edge is the chip's outline. `chipleap chip plan` prints all of it, and with `--json` the same as data.

The bond map numbers the pads from 1 at the top of the west side, counter-clockwise: down the west side, along the south side, up the east side and back along the north side.

## What runs

1. **The core's ports**: Yosys reads the project and writes the top's ports.
2. **The macros**: each package is copied into `build/chip/flow/macros/<instance>/`, checked there with `chipleap macro check`'s rules against IHP SG13G2 at the PDK's commit (errors stop the chip; warnings, such as another PDK commit, are reported), then converted into views ChipLeap writes itself (see [Security](#security)).
3. **The flow's files**: the chip top in Verilog (the ring's cells kept as they are, the core with the project's parameters, the macros), the IO library's GDS and CDL, its LEF without the analog pad's bond-side port, a liberty of the IO cells, the macro placement, a pin template, the SDC and the power grid script.
4. **LibreLane's Classic flow**, with the ring's cells as placed instances. The pins are 2 um squares on Metal3 just outside each digital pad's bond side, joined to the pad by the router. Routing obstructions over the ring on Metal1 to Metal5 keep signals on the core side, so the router cannot reach a pin through the ring's rails. Signals route up to Metal5; TopMetal1 and TopMetal2 carry power. Removed or added steps: no IO placement (the template places the pins), no Magic extraction or overlap check, no stream-out by Magic, an OpenROAD CDL netlist after the last timing step, and no DRC or LVS inside the flow: IHP's decks check the finished chip instead.
5. **Power**: a core ring on TopMetal1 and TopMetal2 joined to the IO ring's own `vdd` and `vss` rails by pdngen (`-connect_to_pads`), stripes over the core and Metal1 rails along the rows. A macro's supply pins must reach its bottom or top edge on Metal2 to Metal5 (aispice puts them there); each gets a strap from the pin to the nearest standard-cell rail of its net beyond that edge, and a via stack to Metal1.
6. **Finishing**, in KLayout: IHP's `bondpad` pcell (70 um square) against every pad's bond side, IHP's `sealring` pcell around the die, a PR boundary on the die when the layout has none (`<name>_nofill.gds`), then IHP's `filler.py` for Activ, GatPoly, Metal1 to Metal5 and both top metals (`<name>.gds`).
7. **LVS** with IHP's deck, on the chip before fill without its IO cells, bond pads and seal ring: each IO cell's core-side pins stay as metal labelled with the net OpenROAD put on them. The netlist is OpenROAD's without the IO cells, whose core-side nets become its ports, with the standard cells' CDL and each macro's subcircuits, so the macro's transistors are compared too. A pad wired to the wrong net, a macro pin left open or two nets shorted all fail it. IHP's deck cannot compare the IO cells, so whether the supply pads reach the core is checked from the DEF (step 9). IHP's SRAMs are compared as boxes too, the way the IO cells are: IHP's deck does not match IHP's own SRAM at transistor level (at PDK 22f2a25 the SRAM's GDS fails against its own CDL on its own: the dummy delay cell `RSC_IHPSG13_CDLYX1_DUMMY` draws a metal resistor on Metal1, which the deck extracts as `res_metal1`, where the CDL has an `lvsres`, which it pairs only with Metal2 resistors). So an SRAM leaves the LVS netlist and the LVS view, and each of its pins a net reaches (supplies included) stays as metal labelled with that net: whether the right net reaches each pin of the macro is checked, what is inside the macro is not. Macro packages are still compared down to their transistors.
8. **DRC** with IHP's deck on the finished chip: every table, density and the extra rules included. Recommended rules are left out (`--no_recommended`): a bond pad against its IO cell, as on IHP's own reference designs, always breaks `Pad.d1R` and `Pad.fR_*`, which are recommendations. IHP's SRAMs draw Metal1 pin markers off the 5 nm grid (`metal1_pin_Offgrid`; IHP's deck flags them on an SRAM on its own as well). A pin marker is no mask layer, so an off-grid pin marker inside the cell tree of one of the PDK's own macros (the macro and every cell of its GDS in the PDK's library) is reported apart, with its rule, cell and count (`pin_markers_in_pdk_macros`), and not counted; the same rule anywhere else, and every other rule inside the macro, counts. Place and route reads LibreLane's KLayout report by the same rule (`chipleap.verify.drc`).
9. **Timing at the ports** (see [below](#timing-at-the-ports)): OpenSTA on the routed chip with IHP's IO liberty, at every corner.
10. **Supply hookup**: from the final DEF, the TopVia2 cuts pdngen put on the IO ring's `vdd` and `vss` rails, per net and side. None for `VDD` or `VSS` fails the chip.
11. **Precheck**, what a shuttle's precheck looks at that is public: one top cell with the chip's name, only layers in IHP's layer map, no `#` or `/` in a cell name (Tiny Tapeout's checks), no shape without area, a PR boundary on the die, and the layout's extent on the seal ring's outline and the 5 nm grid. IHP's density rules are in the DRC run.
12. **The bond map**: `<name>.bondmap.csv` and `.json` with each pad's number, side, net, kind, IO cell, instance and bond pad centre and size in um.

Everything goes to `build/chip/`: `<name>.gds`, the bond map, `chip.json` (the step's data) and each stage's inputs and logs in `flow/`, `finish/`, `lvs/`, `drc/` and `precheck/`. The step passes when routing DRC, IHP's DRC and LVS are clean, both supplies reach the ring, the precheck passes, timing is met at the core's side of the pads and at the chip's ports at every corner, and every signal pad is timed. On the UART example it took about 13 minutes here: LibreLane 2.5, fill 2.5, LVS a quarter of a minute and DRC 7.

## Timing at the ports

LibreLane optimises and times the core against its side of the pads, the IO cells' `p2c` and `c2p` pins, with the budget the pads leave it there: the world outside (`chip.io`, below) less the pads' own delays, from IHP's `sg13g2_io` liberty at each signoff corner (typical, slow and fast) before LibreLane runs. An output pad's `c2p` to `pad` arc is taken at `chip.io`'s output load and at a 0.33 ns edge from the core (the 0.14 to 0.38 ns LibreLane 3.0.0.dev44 gave the UART chip's pads); an input pad's `pad` to `p2c` arc, a clock pad's too, at `chip.io`'s input edge rate and a 0.048 pF load in the core (0.03 to 0.09 pF on that chip). With `d` the derate and `clock_pad` the clock pad's rising delay (0 without a clock), at each corner:

- an input pad's core pin has its data `input_delay + pad * (1 + d) - clock_pad * (1 - d)` after the clock edge at the clock pad's core pin at the latest (the pad's slower edge), and `input_delay + pad * (1 - d) - clock_pad * (1 + d)` at the earliest (its faster edge);
- an output pad's core pin must have its data `output_delay + (pad + clock_pad) * (1 + d)` before the next edge (the slower edge), and holds it `output_delay + (pad + clock_pad) * (1 - d)` after (the faster edge).

The latest over the corners is the core's `-max` delay, the earliest its `-min`; `chip.json` keeps each corner's numbers (`core_budget`). So repair sizes the core's drivers for the pads they drive. A pad whose IO cell has no arc at a corner adds nothing there; the timing at the ports below still finds it untimed. After routing, ChipLeap times the whole chip again at its own ports, with OpenSTA in OpenROAD, at every corner LibreLane signed the core off at (typical, slow and fast): the routed netlist and its nominal parasitics, the standard cells' liberty of the corner, IHP's `sg13g2_io` liberty of the same process, core supply and temperature (1.2 V and 3.3 V at 25 C, 1.08 V and 3.0 V at 125 C, 1.32 V and 3.6 V at -40 C), which has each pad's arc, and the macros' liberty. A corner with no such IO liberty is an error, as is a standard-cell liberty or LEF from outside the PDK LibreLane installed. Each clock is created on its pad's port, so the input pad is part of the clock's insertion delay, with LibreLane's clock uncertainty. Both timings apply the PDK's derate (`TIME_DERATING_CONSTRAINT`, 5 % on IHP SG13G2: late paths 5 % slower, early ones 5 % faster), as place and route does. The data ports get the budget of the world outside, `chip.io`:

- `input_delay_ns`: how long after the clock edge at the clock pad the outside world's data reaches an input pad (the other chip's clock-to-output and the board's flight time).
- `output_delay_ns`: how long before the next clock edge at the clock pad an output must be at its pad for the other chip to take it (its setup time and the board's flight time).
- `output_load_pf`: what each output pad drives (bond wire, package pin, trace and the other chip's input).
- `input_transition_ns`: the edge rate of the signals at the input pads.

Both delays default to a fifth of the first clock's period, as LibreLane's own IO constraint does for a core alone, and each is used for the earliest and the latest arrival alike (setup and hold). The timing at the ports takes them as they are, at the ports: the pads' delays are in its paths, through IHP's liberty, and never in its constraints. They refer to the first clock, or to a virtual clock of 10 ns when the chip has none. IHP characterises its input pads for edges of 0.12 to 3.5 ns and its output pads for loads of 1 to 10 pF; a budget outside that range is reported as a warning, since OpenSTA then extrapolates the pad's delay.

The report gives each corner's worst setup and hold slack at the ports and, at the slow corner, the worst pad-to-core, core-to-pad and pad-to-pad paths with how much of their delay is inside the pads (the pad cells' own arcs, not the wires into them). A signal pad whose IO cell has no arc with both a rising and a falling delay table in the corner's liberty, or whose cell ChipLeap knows no arc for, is listed with its ports as not timed, and the chip does not pass: its delay is never taken as zero. Macros have no timing arcs (their liberty is written from the manifest), so paths through a macro are not timed; the report says so when the chip has macros.

On the UART chip at 100 MHz with the defaults and LibreLane 3.0.0.dev44, the slow corner's worst setup slack is +0.908 ns at the core's side of the pads and +0.673 ns at the ports: a receive-data output's path spends 5.7 ns in its 4 mA pad driving 5 pF. The ports have less than the core partly because the core's budget leaves out the wire from a pad to its port (0.11 ns at 5 pF). The worst hold slack is +0.149 ns, at the fast corner, where it is a path inside the core. Before the pads' delays came off the core's budget, the core had +6.356 ns at its side of the pads, so repair never sized the pads' drivers for them; LibreLane 3.0.14 then left an `sg13g2_inv_1` driving an output pad and the chip missed its ports by 0.582 ns.

## What it needs

LibreLane (as for `pnr`), Yosys, OpenROAD (for the timing at the ports), KLayout 0.30 or newer, a Python with KLayout's module for IHP's runners (`pip install klayout`; `CHIPLEAP_KLAYOUT_PYTHON` names another Python), and an IHP-Open-PDK checkout: `CHIPLEAP_IHP_PDK` names its `ihp-sg13g2` directory, or ChipLeap takes the one the standard-cell liberty is in. The step skips itself, with the reason, when any is missing. IHP's runners build shell command lines from the paths they are given, so ChipLeap runs them in a private directory on copies of their inputs, and the PDK's path must be letters, digits and `_ . / + -`. `CHIPLEAP_CHIP_TIMEOUT` limits each KLayout and runner call (3 hours by default); LibreLane has `CHIPLEAP_PNR_TIMEOUT`.

The IO cells, fill, DRC and LVS come from the PDK checkout; the standard cells and the routing come from the PDK LibreLane runs on: the commit it pins and installs for itself, or the PDK root `CHIPLEAP_LIBRELANE_PDK_ROOT` names, which can hold the checkout's commit ([verification.md](verification.md#librelanes-version-and-pdk)). A macro built at another commit than the checkout's is a warning.

## Security

A macro package is someone else's work, read as data and never trusted:

- Its files are copied into the build directory through the same confined read as every project file, so none is reached through a symlink out of the project, and a symlink inside the package to another project file is refused. At most 256 files and 512 MB are copied.
- The copy is checked against `macro/v1` first, with every limit `chipleap macro check` applies; then only the files the validated manifest names are read.
- ChipLeap writes the views the flow uses. The LEF is rebuilt from the statements ChipLeap reads (outline, pins with their shapes and antenna areas, obstructions) with its origin moved to 0, 0; any other statement in a pin, port or obstruction is refused, layers must be IHP SG13G2's, numbers are bounded. The GDS gets its top cell moved by the same offset. The liberty is written from the manifest's pins. The SPICE keeps the `.subckt` blocks with device lines and comments only (no includes, libraries, parameters or control blocks).
- Names are where a package could do harm: the layouts and the LVS netlists are merged by cell name, so a package cell named `sg13g2_inv_1` would replace the standard cell. A package's name is lowercase letters, digits and `_`; it may not be, or begin, a name the PDK or the flow uses (every cell, macro and subcircuit of the PDK's libraries, `sg13g2*`, `rm_ihpsg13*`, LibreLane's `via_*` cells, `bondpad`, `sealring`, IHP's fill cells, the chip and its core top), nor hold one in its name space. Each GDS cell and subcircuit must be the package's name or that name, `_` and a suffix, exactly as written, except the PDK's own standard cells a hardened digital block holds in its GDS: the chip step checks every package with the PDK's libraries (and stops when it cannot read them), and only cells that macro/v1's `pdk.cell_name` shows identical to the standard-cell GDS pass, which the merge check below compares again with the file LibreLane merged; GDS names are plain characters, read as raw bytes, and defined once; every cell the GDS references must be in it; two subcircuits whose names differ only in case are refused (SPICE folds case). Two packages may not share a name space (`foo` and `foo_bar`), nor may a package sit in the chip's or its core's name space (`uart_x` beside the core `uart`). Those rules give an early, friendly error; the guarantee is at the merges themselves. Before LibreLane runs, the chip top's modules (every module the core's sources define, black boxes and modules the core's top does not instantiate included, every macro's, the IO cells' and the chip's) must be distinct, so a macro named like one of the core's modules cannot be bound to that RTL. After it runs, ChipLeap reads what LibreLane actually merged from its resolved configuration (the standard cells' GDS and CDL from LibreLane's PDK, and only files ChipLeap put in the flow's directory besides) and refuses any GDS cell or subcircuit (case folded) that two of them define, counting the chip top and the vias of the routed DEF as a source of their own, unless both are the same PDK file or identical; nothing LibreLane made is used before that check passes. A PDK library file too large to read is an error, not a library whose names go unchecked.
- A macro's domains must be the 1.2 V core supply.

Every name that reaches the chip top, the SDC, the DEF, the PDN script or a KLayout plan is an identifier checked when the project is read, checked again by the writers (no collisions, no Verilog keywords) and by the KLayout scripts, and quoted where a script reads it; numbers are bounded and on the grid. KLayout and IHP's runners run from argument vectors, confined like the other tools to the system, their own installation, the PDK, the build directory and a private temporary and home directory (and `/dev/shm`, for the DRC runner's process pool), with a PATH of absolute directories outside the project. Results hold no absolute paths. `build/chip` and every directory in it are reached without following a symlink, as for every step.

## The macro layer

`chipleap.macro.layer` turns any hardened block into the views a flow reads, so the chip, an RTL project's own macros and the generated accelerators can share it: `import_packages` for `macro/v1` packages (checked and converted as above), `pdk_macro` for a macro of the PDK such as an SRAM of `sg13g2_sram` (its own views, copied), both giving a `MacroView` (outline, `MacroPin`s with their shapes, LEF, GDS, liberty, SPICE, cell names, and whether its supply pins can be strapped); `reserved_names` for the names no package may use; and `librelane_config` for LibreLane's `MACROS` and the macros' `EXTRA_CDLS`. The chip's power grid straps supply pins that reach an edge (`chipleap.chip.pdn`); an SRAM's supply stripes need pdngen's macro grid, which is not set up yet, so a PDK macro is refused there for now.

## Limits

- Pads are `sg13g2_IOPadIn`, `sg13g2_IOPadOut4mA`, `sg13g2_IOPadAnalog` and the four supply pads; the 16 and 30 mA output cells hold clamps whose Metal1 pins are off grid on IHP's deck, and bidirectional pads are not offered.
- One core voltage domain; macros on other supplies are refused.
- A macro's supply pins must reach its bottom or top edge on Metal2 to Metal5. Supply pins on the top metals (for pdngen's macro grid) are not handled yet.
- The parasitics of the ports' timing are LibreLane's nominal extraction; the bond wires and package are in `output_load_pf` and the input budget, not modelled.
