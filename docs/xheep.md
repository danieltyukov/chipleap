# Integrating a design into X-HEEP

A bundle carries everything X-HEEP needs: a configuration for its generator, the accelerator RTL with an adapter for X-HEEP's bus types, a driver and a datasheet. This page describes how the pieces connect. The X-HEEP side follows its own guide, [eXtending hardware](https://x-heep.readthedocs.io/en/latest/Extending/eXtendingHW.html).

## The configuration

`xheep/<name>.py` is an X-HEEP Python configuration (the format X-HEEP's `configs/general.py` uses). It sets:

- the CPU (`cv32e20`, `cv32e40x`, `cv32e40p` or `cv32e40px`; the CV32E40P variants with `corev_pulp=True`, since the model assumes the Xpulp extensions);
- the bus (`onetoM` or `NtoM`);
- the continuous banks and, when the design has them, an interleaved group named `data_interleaved`;
- the code and data linker sections (X-HEEP's default 58 KiB of code, less when the banks are small);
- the external slave window: X-HEEP's default 1 GiB at `0xC0000000`, or 2 GiB at `0x80000000` when more than 1 GiB lives in external memory;
- the flash controller without its cache on either bus: on NtoM X-HEEP allows a 16 KiB cache, an SRAM bank of its own that nothing in the model reads through and that the SoC's layout would have to build from macros;
- the peripherals: X-HEEP's always-on ones, which every X-HEEP has, and of the others only those the spec's `peripherals` section asks for ([spec.md](spec.md#peripherals)): the UART, the GPIO blocks for the pins it asks for, the second timer block, the SPI hosts, the I2C, the I2S, the PDM2PCM, the debug module's SPI slave, and the PLIC when one of them raises its interrupts through it; the DMA with the channels and master ports the section asks for. Without the section a design gets X-HEEP's default without the SPI host, the I2C, the second SPI host (SPI2) and the I2S, with a DMA of two channels on one master port, as the model's DMA moves 4 bytes per cycle.

The interrupts, the address map and the pads (`configs/pad_cfg.py`) are X-HEEP's default; the pads keep the pins of the peripherals left out, since X-HEEP's MCU template wires them whatever it has. The exception is the debug SPI slave: without it X-HEEP's MCU must not have its pins, so the bundle has a pad configuration of its own, `xheep/<name>_pads.py`, X-HEEP's default without them. Generate the MCU with:

```sh
make mcu-gen X_HEEP_CFG=<bundle>/xheep/<name>.py
# without the debug SPI slave:
make mcu-gen X_HEEP_CFG=<bundle>/xheep/<name>.py PADS_CFG=<bundle>/xheep/<name>_pads.py
```

`chipleap verify` runs X-HEEP's own validation and template rendering on the file; see [verification.md](verification.md).

## The accelerator

`rtl/chipleap_accel.sv` is the accelerator; `rtl/chipleap_accel_xheep.sv` adapts its plain ports to X-HEEP's types and fixes its parameters for the design:

| Port | X-HEEP connection |
|---|---|
| `reg_req_i`, `reg_rsp_o` (`reg_req_t`, `reg_rsp_t`) | a slot of the external peripheral bus. The driver assumes base `0x20070000`, X-HEEP's `ext_peripheral` window. |
| `rd_req_o[ports]`, `rd_resp_i[ports]` (`obi_req_t`, `obi_resp_t`) | `ext_xbar_master_req_i` / `ext_xbar_master_resp_o`, one master per read port |
| `wr_req_o`, `wr_resp_i` | one more external master |
| `ext_req_o[ports]`, `ext_resp_i[ports]` (`obi_req_t`, `obi_resp_t`) | only with external memory: the accelerator's direct read ports, to `chipleap_mem` beside X-HEEP (below) |
| `irq_o` | a free fast interrupt |
| `accel_clk_i` | only with `accel_mhz`: the accelerator's own clock, from a clock source of its own; everything else stays on X-HEEP's `clk_i` |

The accelerator reads activations from the SRAM banks and weights from wherever they live. With the NtoM bus and interleaved banks, each read port moves a word per cycle. Weights in external memory cannot come over X-HEEP's bus: an external master reaches only X-HEEP's system crossbar, and the external slave window is served by ports X-HEEP splits off before that crossbar, one per X-HEEP master. So the accelerator reads external memory over a direct path of its own, described in the next section.

With `accel_mhz` the adapter instantiates `chipleap_accel_cdc` (`rtl/chipleap_accel_cdc.sv`), the accelerator behind its clock domain crossings; see [verification.md](verification.md#the-accelerator-on-its-own-clock). `rtl/chipleap_accel.core` is a FuseSoC core file for the RTL files (three, or four with the crossings); add it to your top level's core alongside `openhwgroup.org:systems:core-v-mini-mcu`.

## External memory: the accelerator's direct path

In X-HEEP (at the commit ChipLeap pins, bf0748a) only the CPU, the debug module and the DMA reach the external slave window. `system_bus.sv.tpl` gives each of them a port to it (`ext_core_data_req_o`, `ext_dma_read_req_o` and so on), split off by address before the system crossbar, and an external master on `ext_xbar_master_req_i` only enters that crossbar. Weights could only reach the accelerator through the DMA, copied into a bank and read from there. X-HEEP's bus allows no outstanding transactions and serves one transaction per cycle for all its masters, so that path moves at most 2 bytes per cycle, and only with a memory controller that prefetches sequential words and an extra bank of 8 KiB to stage weights in; without the prefetching it moves half a byte per cycle, against 4 on the direct path ([model.md](model.md#where-the-weights-come-from)).

A design with external memory on X-HEEP therefore gets the accelerator's direct read path (`Design.direct_memory`):

- `chipleap_accel` with `EXT_DIRECT` has a second set of read ports, `ext_*`, one per read port, with the same OBI semantics. A DMA transfer whose source lies in X-HEEP's external slave window (`EXT_BASE` and `EXT_SIZE`, the window the X-HEEP configuration sets) goes out on them instead of `rd_*`, through the same address generators and response queues. Each port keeps up to `EXT_OUTSTANDING` reads in flight, enough to cover external memory's latency at the accelerator's clock (16 up to about 175 MHz, more above; `chipleap.model.accel.direct_outstanding`). The choice is made once per transfer from its row base, so an operand lies wholly inside or wholly outside the window. Results always leave through `wr_*` into the banks.
- `chipleap_mem` (`rtl/chipleap_mem.sv`, in the bundle) puts X-HEEP's external slave ports and the accelerator's direct ports on the memory controller's ports. X-HEEP's ports (the CPU's data port and the DMA's read and write ports, the ones ChipLeap's model uses) go through a round-robin arbiter, and the result and the accelerator's direct port 0 through a second round-robin arbiter onto memory port 0. The accelerator's other direct ports, when it has more than one, have memory ports of their own. Each arbiter holds a request the memory has not granted (OBI keeps a request stable until its grant), routes each response back by a queue of who was granted, and while both sides ask it alternates between them.
- In the SoC ChipLeap lays out (`--steps soc`, [verification.md](verification.md#the-whole-soc)), the top `chipleap_soc` wires these: it has one memory port per accelerator read port, `mem_req_o` and `mem_resp_i` with X-HEEP's OBI types, in place of X-HEEP's three external slave ports. No pin is added and X-HEEP is not changed.

With the accelerator on a clock of its own (`accel_mhz`), the memory ports run on that clock, `accel_clk_i`, with the accelerator's direct ports, so the weights' stream crosses no clock. X-HEEP's side crosses into it instead, through `chipleap_cdc_obi` (the same asynchronous FIFOs as the accelerator's crossings, two entries each way). X-HEEP's external accesses are few and slow beside the weights, so the crossing is on the narrow side. The other way round, with the memory controller on X-HEEP's clock and a crossing on the accelerator's direct port, would cost 6.76 kGE per port instead of 2.44 kGE, and cap the stream at 4 bytes per SoC cycle (140 MB/s at 35 MHz), which is what the accelerator's own clock was to get past; ChipLeap does not build it.

X-HEEP's own external accesses keep their latency. The arbiters decide in the cycle a request arrives and route a response in the cycle it comes back, so they add no cycle of their own. Simulated in Verilator on the SmolLM2 example's SoC at 35 MHz, with the memory answering 60 ns plus 4 cycles after a grant (8 cycles) and the CPU loading from external memory now and then, a load took 8 cycles (229 ns) with the CPU's data port straight on the controller, as before this path, and 8 cycles through the arbiters, idle or with the accelerator streaming weights beside it: round robin gives the CPU the next grant. With all three of X-HEEP's ports busy beside the stream a load took 8.3 cycles on average and 13 at most. With the accelerator and the memory ports at 90 MHz, a load crosses twice and the memory answers in 9 of the faster cycles: 7 cycles of X-HEEP's clock (200 ns), idle or streaming.

The direct path costs, measured with Yosys on IHP SG13G2: 1.24 kGE per read port for the longer response queues and the choice of path, 1.47 kGE for `chipleap_mem`'s arbiters, and on the accelerator's own clock 2.44 kGE for X-HEEP's crossing (and 0.28 kGE per port, its queues already holding eight reads). The SmolLM2 example's single port adds 2.7 kGE on the SoC's clock and 4.2 kGE on its own. The model adds it to the accelerator's logic ([model.md](model.md#area)). Laid out with LibreLane at 90 MHz, the same code and floorplan with and without the port, the SmolLM2 accelerator's block took 1.34 kGE more in synthesis and 0.019 mm2 more of routed cells, and met 96.0 MHz at the slow corner against 96.8 MHz without it; both runs were clean (DRC, LVS, antenna, slew and capacitance).

### The memory controller

The memory controller and the memory's PHY are not ChipLeap RTL: ChipLeap's SoC ends at its memory ports, and the controller attaches there. What it must do, and what the model assumes it does:

- One port per accelerator read port, each an OBI port that runs on the accelerator's clock: `accel_clk_i` when the accelerator has a clock of its own (`accel_mhz`), X-HEEP's `clk_i` when it shares X-HEEP's. A controller for octal PSRAM or HyperRAM at 90 to 100 MHz is ordinary. The alternative, a controller on X-HEEP's clock with the accelerator's ports crossing into it, caps the stream at 4 bytes per SoC cycle (140 MB/s at 35 MHz) and costs 6.76 kGE per port (above); ChipLeap does not build it.
- OBI semantics with requests in flight: a request (`req`, `we`, `be`, `addr`, `wdata`) is granted in the cycle `gnt` is high, and the controller may hold `gnt` low as long as it needs; it must accept a new request while earlier ones wait for their responses, and answer each port's requests in the order it granted them, with `rvalid` (and `rdata` for a read) in a later cycle. There is no error response.
- Port 0 carries X-HEEP's reads and byte-enabled writes beside the accelerator's reads; the other ports only read whole words.
- Each port's reads are sequential within a row of a transfer: port `p` of `P` reads words `p`, `p + P`, `p + 2P` and so on, so with one port consecutive words. The controller should turn them into bursts on the memory, which is what lets a serial memory reach its sustained rate.
- What the model assumes from it: the memory's sustained rate (for the SmolLM2 example, two channels of octal PSRAM at 124 MB/s each, IHP's pads limiting them to 155 MT/s at 80 % efficiency: 248 MB/s), the first word 60 ns plus 4 cycles after the grant, and a word per port per cycle whenever the memory keeps up. With the accelerator at 90 MHz that is 2.76 bytes per cycle of its 4, so SmolLM2 is bound by the memory, not by the port.

The bundle's simulation drives the accelerator, `chipleap_mem` and X-HEEP's three ports against a model of such a controller ([verification.md](verification.md#the-direct-path-to-external-memory)).

## Firmware

`sw/chipleap_driver.h` is header-only C:

```c
#include "chipleap_driver.h"

chipleap_plan_t p;
if (chipleap_plan(M, K, N, 4 /* int4 */, &p) == 0) {
  /* X: M rows of p.k_pad bytes; W: N rows of p.k_pad packed int4 weights; Y: M x N int32 */
  chipleap_start(CHIPLEAP_BASE, (uint32_t)x, (uint32_t)w, (uint32_t)y, M, N, 4, &p);
  while (chipleap_busy(CHIPLEAP_BASE)) { /* or wait for the interrupt */ }
  uint32_t cycles = chipleap_cycles(CHIPLEAP_BASE);
}
```

`chipleap_plan` picks the tiles exactly as the explorer's model did, so the cycle counts the explorer predicted are the ones the driver programs. Rows of X and W are padded with zeros to the K alignment (`chipleap_k_align`); weights are packed little-endian, lowest element in the lowest bits.

The register map is in `DATASHEET.md` and, for OpenTitan's `reggen` and X-HEEP's register tools, in `rtl/chipleap_accel.hjson`.

## What runs on the CPU

The accelerator produces int32 results. Requantization back to int8, normalization, softmax, activations, RoPE and int4 group scales run on the X-HEEP core in the current release, and the model charges them there. A design whose time goes to the CPU (the report says "compute" on CPU operators) benefits from a faster core before a bigger array.
