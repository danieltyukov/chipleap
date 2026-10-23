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
| `irq_o` | a free fast interrupt |
| `accel_clk_i` | only with `accel_mhz`: the accelerator's own clock, from a clock source of its own; everything else stays on X-HEEP's `clk_i` |

The accelerator reads activations from the SRAM banks and weights from wherever they live. With the NtoM bus and interleaved banks, each read port moves a word per cycle. For weights in external memory, the memory controller sits behind X-HEEP's external slave port(s). It needs as many slave ports as the accelerator has read ports to reach the bandwidth the model assumed, or a wider AXI path through X-HEEP's OBI-to-AXI bridge.

With `accel_mhz` the adapter instantiates `chipleap_accel_cdc` (`rtl/chipleap_accel_cdc.sv`), the accelerator behind its clock domain crossings; see [verification.md](verification.md#the-accelerator-on-its-own-clock). `rtl/chipleap_accel.core` is a FuseSoC core file for the RTL files (three, or four with the crossings); add it to your top level's core alongside `openhwgroup.org:systems:core-v-mini-mcu`.

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
