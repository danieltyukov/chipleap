# Commercial tools and your own PDK

ChipLeap runs on open tools by default, and the same checks run on the commercial ones a
university or company already licenses: Cadence Xcelium, Synopsys VCS or Siemens Questa
for simulation, Cadence Genus or Synopsys Design Compiler for synthesis, with Cadence
Innovus scripts for the mapped netlist. The tools can be on this machine or on a design
server reached over ssh, and the process can be a foundry PDK measured from its liberty
files.

```text
$ chipleap verify out --sim-tool xcelium --synth-tool genus --remote eda-server \
    --remote-setup "source /eda/scripts/flexlm.sh; source /eda/cadence/scripts/XCELIUM.sh; source /eda/cadence/scripts/GENUS.sh"
sim    PASS     Xcelium: 21 randomized jobs bit-exact; cycle model error 0.0% max without stalls, ...
synth  PASS     Genus: logic cell area ... mm2 on ihp-sg13g2 (model ... mm2, ...), critical path ... ns at a 100 MHz target
```

## Simulation

`--sim-tool xcelium`, `vcs` or `questa` runs `tb_chipleap.sv`, a SystemVerilog testbench
that every simulator runs unchanged. ChipLeap writes its stimulus on this machine with a
small C++ program (`stim_gen.cpp`) that compiles the generated C driver: it plans and
starts each job exactly as firmware would, records the driver's register writes, and
computes the golden result with the same data generator as the Verilator testbench. The
simulator then replays the writes, polls `STATUS`, reads `CYCLES` and compares every
output word.

So a commercial simulator checks the same things as Verilator: every job bit-exact, and
the cycle count against the model's prediction. Memory stalls come from a fixed
generator, so Xcelium, VCS, Questa and Verilator report the same cycle counts for the same
stimulus. `--sim-tool verilator-sv` runs this testbench on Verilator 5 with `--timing`,
which is how the flow is tested without a licence.

| Simulator | Command ChipLeap runs |
|---|---|
| Xcelium | `xrun -64bit -sv -timescale 1ns/1ps -top tb_chipleap chipleap_accel.sv tb_chipleap.sv +stim=stim.txt` |
| VCS | `vcs -full64 -sverilog -timescale=1ns/1ps -top tb_chipleap ... -o simv && ./simv +stim=stim.txt` |
| Questa | `vlib work && vlog -sv ... && vsim -c tb_chipleap +stim=stim.txt -do "run -all; quit -f"` |

The work directory, with the stimulus, the simulator's output and its logs, is
`out/verify/<simulator>/`.

## Synthesis

`--synth-tool genus` or `dc` synthesizes the accelerator the way the Yosys step does:
twice, with 8- and 16-word buffers, extrapolating the logic area to zero buffer words,
because the buffers are SRAM macros in silicon. Each run's parameters are written into a
copy of the RTL, so no tool-specific parameter syntax is involved. The clock is the
design's when the technology is the design's own, otherwise the technology's practical
clock, and the step reports the worst setup slack at it as a critical path.

`--synth-tech` picks the library: `ihp-sg13g2` (the default, from the open PDK) or a
technology of your own (below). Genus reads the liberty directly. Design Compiler needs a
compiled `.db`: name one with `chipleap tech add --db`, or ChipLeap compiles the liberty
with Library Compiler (`lc_shell`) first.

When the technology's LEF files are known (the IHP SG13G2 ones are found next to its
liberty), the step also writes `innovus.tcl` and `mmmc.tcl` for the 16-word netlist:
floorplan, placement, clock tree, routing, post-route timing and reports, and a GDS when
the technology names a layer map. ChipLeap does not run Innovus itself; the open flow it
runs for layout is LibreLane.

## Running on a server

`--remote HOST` (or `CHIPLEAP_REMOTE`) copies the step's work directory to a fresh
scratch directory on the host, runs the tool there and copies the results back, then
deletes the scratch directory. `--remote-setup` (or `CHIPLEAP_REMOTE_SETUP`) is the shell
snippet that puts the tools on the path and points at the licence server; a snippet that
sources `.csh` files or uses `setenv` runs under tcsh, anything else under bash.

- Every run gets a new shell, so a site rule such as "one PDK per shell" holds by
  construction.
- ssh runs with `BatchMode=yes`: key-based logins only, a password prompt is an error
  rather than a hang. Hops and multiplexing come from your `~/.ssh/config`.
- `chipleap doctor --remote HOST --remote-setup ...` lists which of `xrun`, `vcs`,
  `vsim`, `genus`, `dc_shell`, `lc_shell` and `innovus` the setup puts on the path there.
  Locally, `chipleap doctor` looks them up on the path without starting them, so no
  licence is checked out.

The MCP tool `verify_soc` takes the same choices (`sim_tool`, `synth_tool`, `synth_tech`,
`remote`, `remote_setup`), so an assistant can run the commercial flow too.

## Your own PDK

```text
$ chipleap tech add mylib40 --node 40 --liberty /pdk/std/typ.lib \
    --sram-liberty /pdk/sram/lib --lef /pdk/tech.lef --lef /pdk/std/cells.lef
Added technology 'mylib40' (40 nm, 1.1 V) in ~/.config/chipleap/tech/mylib40.yaml
```

`chipleap tech add` measures what the liberty files state and scales what they do not:

| Number | From |
|---|---|
| Gate equivalent | the unit-drive two-input NAND's area |
| Logic leakage density | that NAND's leakage at the library's corner |
| SRAM area | a least-squares fit of macro area against capacity over the macro libraries (cells with a `memory` group) |
| SRAM read energy | the read-condition internal power of the widest macros, fitted against size and taken at 16 KiB |
| MAC and CPU energy | Horowitz (ISSCC 2014) scaled to the node and supply, as for the generic nodes |
| Clock and external memory rate | interpolated between the built-in nodes, or `--max-mhz` and `--max-io-mts` |

Without macro libraries the SRAM figures are scaled from IHP SG13G2 by the gate area. The
technology then appears everywhere a built-in one does: in exploration (restrict a spec to
it with `constraints: {tech: [mylib40]}`), in `chipleap catalog` and `chipleap tech list`,
and as `--synth-tech mylib40` for Genus and Design Compiler.

With `--remote HOST`, the liberty files stay on the server: each is read over ssh into
memory, measured and dropped, and the technology records the paths on that host, so
synthesis with `--remote HOST` uses them where they are.

## What stays private

- Technology files live in `~/.config/chipleap/tech` (`CHIPLEAP_TECH_DIR` moves it, an
  empty value turns them off). They hold the measured numbers and the file paths, never
  library content, and nothing writes them into a bundle or a repository. Under an NDA,
  keep that directory out of version control and backups you share.
- A liberty that is not on this machine is never copied to it.
- Synthesis and simulation results stay in the bundle's `verify/` directory. Licence
  terms for commercial tools often forbid publishing benchmark results; ChipLeap does not
  publish anything, and the project's own documentation quotes only open-tool numbers.
