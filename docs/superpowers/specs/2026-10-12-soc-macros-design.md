# D3, part a: SRAM macros in the layout

Status: accepted, 2026-10-12. Milestone D3 of `chip-design-roadmap.md`, the part that touches the layout of memories; part b (DFT, power, IR drop, UPF, ECO) is in. Built on D4's macro layer (`chipleap.macro.layer`).

## Goal

The routed result is the real chip, not an extrapolation: IHP's own SRAM macros (`sg13g2_sram`, `RM_IHPSG13_1P_*`) are placed in LibreLane with halos and their power grid, and the area and timing a report gives for them are measured. This part covers the generated accelerator's buffers and a `macros` section for RTL projects; the whole SoC (X-HEEP with the accelerator) follows in part 2, on the same pieces.

## Facts this design rests on

Measured on 2026-10-12 with LibreLane 3.0.0.dev44 (its IHP PDK at `cb7daaa`) and IHP-Open-PDK `22f2a25`, on a prototype: `RM_IHPSG13_1P_256x32_c2_bm_bist` behind registered ports on a 560 x 300 um die.

1. An SRAM's supply pins (`VDD!`, `VDDARRAY!`, `VSS!`) are vertical Metal4 stripes, 2.81 um wide, across the block: `VSS!` the full height, `VDD!` the periphery at the bottom (0 to 38.8 um on the 256-word macros) and `VDDARRAY!` the array above it. Its obstructions cover Metal1 to Metal4; Metal5 and the top metals are free over it.
2. LibreLane's default PDN for IHP (TopMetal1 vertical stripes, TopMetal2 horizontal, Metal1 rails) leaves the SRAM unconnected (`PDN-0231`), and Netgen then counts one net more in the layout than in the netlist.
3. A macro grid closes it: `define_pdn_grid -macro -cells {...}` with Metal5 horizontal straps (2 um wide, 10 um pitch, 3 um apart) and TopMetal1 vertical stripes (2.2 um, 37.8 um pitch) over the macro, connected Metal4 to Metal5, Metal5 to TopMetal1 and TopMetal1 to the core's TopMetal2. With the three supply pins globally connected to the design's supply nets, PSM finds every shape connected, Netgen LVS matches (the SRAM as a box with its pins), KLayout DRC with IHP's deck is clean with the SRAM's GDS in it, routing DRC and antenna are clean, and setup holds at the slow corner (+4.38 ns at 100 MHz). The whole run took 5 minutes, KLayout DRC 4 of them.
4. The PDN script must use LibreLane's supply nets (`$::env(VDD_NET)`, `$::env(GND_NET)`: `VPWR` and `VGND` for an RTL project, `VDD` and `VSS` in a chip); global connections to nets that do not exist crash PSM's connectivity check (signal 11 in `generateTopLayerFillerNodes`). Sourcing LibreLane's own `pdn_cfg.tcl` and adding the macro grid after it keeps the core grid exactly as LibreLane builds it.
5. LibreLane's linter and synthesis need the macro as a module: a black-box Verilog view (`vh` in `MACROS`, `(* blackbox *)` with the ports of the PDK's model). The PDK's own Verilog model is behavioural only under `FUNCTIONAL` and carries `specify` timing otherwise.
6. The macro's ports: `A_CLK`, `A_MEN` (enable), `A_WEN`, `A_REN`, `A_ADDR`, `A_DIN`, `A_DOUT`, `A_BM` (bit mask, a bit is written where it is 1), `A_DLY` (tied high, as Croc does) and a BIST port set tied off. A read returns the word a cycle later and `A_DOUT` holds until the next read.
7. IHP's 1P macros: widths 8, 16, 32, 48 and 64 bits, depths 64 to 8192; a 64-bit macro is 784 um wide. Every 1P macro has typical, slow (1.08 V, 125 C) and fast liberty, so timing at the slow corner sees the macro's own arcs.

## Decisions

### The macro grid

`chipleap.macro.grid` writes the PDN script for any set of placed PDK macros: LibreLane's `pdn_cfg.tcl` sourced, a global connection per supply pin of each instance to `VDD_NET` or `GND_NET`, and one macro grid per cell as in fact 3. D4's chip PDN takes the same grid for every view with `supply_hookup=False`, instead of refusing it. A macro whose supply pins are not on Metal4 is refused, with the layer named.

The macro grid's halo is 5 um, half the 10 um the rows are cut to around a macro. With both at 10 um, the first real run left the rails just beside a row of macros without a core strap and pdngen could not repair them (PDN-0179).

The core's TopMetal2 stripes (75.6 um pitch) connect each macro grid, so a macro must lie under a whole VDD and VSS pair of them. Macros at least 104 um tall (a pitch, a pair and the grid's inset at both ends) always do; a shorter one moves towards the logic until a pair falls inside, and the PDN script checks the pair is there before LibreLane runs, naming where a misplaced macro could go.

### The generated accelerator

The SRAMs are single-ported with a registered read, while the accelerator reads its buffers combinationally, several words a cycle. A build is **macro-backed** when the mapping costs no cycle, so `chipleap.model.accel` stays exact:

- **Read.** The gemv core loads its operand registers from the buffers when it issues a chunk. A macro-backed build presents the same address to the SRAM in the same cycle, and the SRAM's output, sliced by the offsets registered with it, replaces the operand registers: the multiplier sees the same values in the same cycle.
- **Read, as built.** Amended after the first routed runs. With the SRAM's word in place of the operand registers, the macro's clock to output at the slow corner (5.25 ns on a 256x64 macro) came before the unpacking, the multipliers and the adder tree, and the SmolLM2 accelerator missed 90 MHz by 4.27 ns. Each chunk is now read a cycle ahead (at `core_start` for a tile's first, in each issue cycle for the next) and the issue registers the word in the operand registers, as with flops. Computing that next address in the issue cycle then put two 32-bit adders before the macros' enables (-2.61 ns after clock tree synthesis), so the next chunk's address is kept in registers a cycle ahead: the row's next chunk is the last read plus a chunk, the next row's first is this row's first plus the stride. The schedule is unchanged.
- **Width.** A chunk of COLS int8 activations or packed weights is COLS x 8 bits at most, at a position aligned to its size (the K alignment), so it lies inside one aligned word of a COLS x 8-bit macro. Writes put 32-bit words into it with the bit mask.
- **Lanes.** Each of the ROWS lanes reads its own weight row every cycle, so each lane has its own bank: weight row `l` of a tile is stored at `l x W_ROW` in its slot, `W_ROW` the slot's words divided by ROWS. The tile never needs more (the planner already caps a tile's K at `W_SLOT / ROWS` words a row).
- **Slots.** With double buffering the loader writes one slot while the core reads the other, so each slot is its own set of macros. A resident X block spans both, and the loader only rewrites it when the core is idle.
- **Ports.** Each DMA read port writes one word a cycle into the buffer. A single-port macro takes one access a cycle and an OBI response cannot wait, so more than one port would need write buffering that changes the timing. Macro-backed builds have one DMA port.

So a build is macro-backed when the dataflow is gemv, it has one DMA port, ROWS is a power of two and COLS is 4, 8 or 16. Otherwise `verify --steps pnr` keeps today's 16-word buffers and says which condition failed. The SmolLM2 example (gemv 1 x 8, one port, 2 + 2 KiB, double-buffered) qualifies.

The RTL gets a parameter, `SRAM_BUF`; with 0 nothing changes. With 1, `chipleap_accel` exposes one port per bank (enable, write, address, data, mask, read data) instead of holding the arrays, and the top ChipLeap generates for the layout instantiates the macros at its own level with fixed instance names (`abuf_s0_m0`, `wbuf_s1_l3_m0`), so LibreLane's `MACROS` can name them. A bank wider than 64 bits is macros side by side; one deeper than the deepest macro of its width is macros stacked, with the read select registered. Macros are chosen from the PDK's libraries by width, then the fewest that hold the depth, then area.

Tests run the macro-backed RTL against the model with Verilator, with ChipLeap's own functional model of the 1P macro ports (the PDK's model is not fetched in CI), and the generated test bench checks that no bank is read and written in one cycle.

### Place and route of the accelerator

`verify --steps pnr` on a bundle, when the build is macro-backed: the RTL as above at the design's real buffer sizes, the macros in rows with a 10 um halo, the die sized from the macros plus the model's standard-cell area at 45 % utilisation, the macro grid as the PDN, the macros' slow, typical and fast liberty for timing at every corner, and their CDL for LVS. The result adds the macros' area and count, and the report compares the measured area (standard cells plus macros) with the model's accelerator and buffer estimate.

An IHP SRAM has every signal pin on its bottom edge, along its whole width (784 um on a 64-bit macro), so a column at the core's left edge would send every wire past the macros. The macros go in a row along the top of the core, upright with their pins facing down, and a mirrored row (`FS`) along the bottom with its pins facing up; the logic sits in the band between. OpenROAD's core is the bounding box of its rows, and the rows under a full-width row of macros are cut, which left the macros outside the core's TopMetal2 stripes in the first real run (their grids were islands). So a 30 um column of rows stays at the core's left edge, holding the core's first TopMetal1 pair.

### RTL projects: the `macros` section

The section is the generic part: anyone's RTL can use hard macros, and the accelerator's buffers use the same pieces. An entry names an SRAM of the PDK (`cell`) or a `macro/v1` package (`package`), with the instance, package, `at_um` and `orientation` keys a chip's `macros` have; the instance is a path, since the RTL instantiates the macro. Packages go through `chipleap.macro.layer` with the chip's reserved names, no macro cell may be a module of the RTL, and after LibreLane the merged GDS cells and subcircuits are checked with `chipleap.chip.merge`.

```yaml
macros:
  - instance: u_mem               # the instance in the RTL; u_core/u_mem inside u_core
    cell: RM_IHPSG13_1P_1024x32_c2_bm_bist
    at_um: [20, 20]               # lower left in the core; optional
    orientation: N                # N, S, FN, FS
```

The RTL instantiates the macro by its cell name. The section is checked like every other: the instance is a path of identifiers (at most 8 levels), the cell is one the PDK's `sg13g2_sram` has (checked when a step needs its views), coordinates are bounded and on the 5 nm grid, at most 32 macros. Steps:

- **lint, synth**: black-box views (fact 5), written from the cell's name so neither step needs the PDK, join the sources; synthesis reports the macros' area from the PDK's LEF when it is installed.
- **sim, coverage**: the PDK's behavioural model (`FUNCTIONAL`) when the PDK is installed; without it the step fails saying which model is missing.
- **pnr**: LibreLane with `MACROS`, the macro grid, halos, absolute die sizing from the macros and the standard cells, and Netgen LVS with the macros' CDL. The flattened instance name is the path with `.`. Without `pnr.die_um` ChipLeap sizes the die from a synthesis's cell area and places every macro in the rows above; with it, every macro needs its `at_um`.
- The steps that read the design or its netlist in ways that do not model a macro yet (formal, CDC, equivalence, gate-level simulation, ECO, DFT, power, IR drop, UPF, the chip, and the commercial tools) skip on a project with macros and name them. Treating a macro as a box in each is part 2's, with the whole SoC, which needs it.

### Security

As for D4's packages and D1's projects: cell names are checked against the PDK's own list before any path is formed, every view is copied from the PDK ChipLeap uses into the build directory, every name that reaches Tcl, Verilog or LibreLane's configuration is an identifier, quoted again by the writers, and coordinates are bounded numbers.

## Part 2: the whole SoC

The pinned X-HEEP, rendered by `mcu-gen` for the design's configuration, a generated SoC top that wires the accelerator to X-HEEP's external ports, an `sram_wrapper` that maps each X-HEEP bank onto IHP macros (a 32 KiB bank is one `RM_IHPSG13_1P_8192x32_c4`), the sources from FuseSoC, synthesis with yosys-slang in LibreLane's container, and the same macro grid. Its acceptance is the roadmap's: the SmolLM2 example chip routes with real SRAM macros, clean for DRC, LVS and antenna, timing met at the slow corner, with the measured area beside the model's.

## Acceptance (part 1)

- The SmolLM2 example's accelerator routes with real SRAM macros as its buffers: clean for DRC, LVS and antenna, setup met at the slow corner, and the result compares the measured area with the model's estimate.
- An RTL project with a `macros` section places, routes and signs off an SRAM.
- The macro-backed RTL matches the model cycle for cycle in `tests/test_rtl.py`, and every existing test stays green.
