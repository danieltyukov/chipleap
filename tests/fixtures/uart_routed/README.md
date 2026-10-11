# Routed UART netlists

`examples/uart` placed and routed by `chipleap verify examples/uart --steps synth,pnr` on
2026-10-11 with LibreLane 3.0.0.dev44 (`ghcr.io/librelane/librelane:3.0.0.dev44`) and the
IHP SG13G2 PDK at commit `cb7daaa8901016cf7c5d272dfa322c41f024931f`, which LibreLane's
ciel installs: 325 cells, 100 MHz met at the slow corner, DRC, LVS and antenna clean.

- `uart.synth.nl.v`: LibreLane's synthesized netlist (`06-yosys-synthesis/uart.nl.v`)
- `uart.nl.v`: the final netlist (`final/nl/uart.nl.v`)
- `uart__nom_typ_1p20V_25C.sdf`: delays at the typical corner (`final/sdf/...`)

CI cannot run place and route, so the equivalence of the two netlists and gate-level
simulation with SDF are tested on these copies. Rerun the command above to refresh them.
