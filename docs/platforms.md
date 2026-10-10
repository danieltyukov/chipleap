# Platforms: X-HEEP or any host

ChipLeap explores the same design space either way. The platform decides what it
generates around the accelerator.

| | `xheep` (default) | `standalone` |
|---|---|---|
| What is generated | A RISC-V SoC: the X-HEEP configuration plus the accelerator and its adapter | The accelerator alone, as IP with AXI4 interfaces |
| Host | X-HEEP's CV32E20, CV32E40X, CV32E40P or CV32E40PX | Any processor that can drive AXI4-Lite and give AXI4 masters a path to memory: Arm Cortex-M or Cortex-A, another RISC-V core, an FPGA processing system |
| Interfaces | Register interface and OBI, wired into X-HEEP's external bus | AXI4-Lite slave for the registers, AXI4 read and write masters for data |
| Area reported | The whole SoC | The generated IP separately from the host system the model assumes |
| `chipleap verify` | Consistency, lint, OBI simulation, synthesis, X-HEEP's own checks | Consistency, lint and simulation of the AXI4 wrapper, synthesis; the X-HEEP step is skipped |

Choose it in the spec, on the command line, or per design:

```yaml
platform: standalone                # in a spec, or in design.yaml
```

```sh
chipleap explore spec.yaml --platform standalone -o out
chipleap generate design.yaml --platform standalone -o out
```

The MCP tool `generate_soc` takes `platform` too. The explorer never changes the platform:
every design it considers keeps the one the spec asks for.

## What the model assumes on the standalone platform

The accelerator's timing does not depend on the host: it is the tile schedule the RTL runs,
and the AXI4 wrapper keeps the core's cycle counts exactly (below). What does depend on the
host is everything around it, so the design fields keep their meaning and describe the host
system the model assumes:

- `cpu` sets how fast the host runs the work the accelerator does not (normalization,
  softmax, activation functions, requantization). The presets are X-HEEP's RISC-V cores.
  To model another core, pick the preset closest in int8 kernel throughput, or measure your
  core's cycles per element and add a `CPUModel` to `chipleap/hw/cpu.py`. ChipLeap does not
  ship Arm presets: Arm publishes peak rates (a Cortex-M55 retires 8 int8 MACs per clock,
  [Hot Chips 2020](https://www.hc32.hotchips.org/assets/program/conference/day1/HotChips2020_Edge_Computing_Arm_Cortex-M55.pdf)),
  but not the achieved kernel cycles and areas the model needs, and the model does not
  invent them.
- `bus`, `banks` and the interleaved banks stand for the host's on-chip SRAM and how many
  words per cycle it serves the accelerator.
- `ext_mem` and `ext_channels` stand for the memory the weights stream from, and `tech` and
  `freq_mhz` for the process and clock of the whole system.

Pin these to your host with `constraints` (`cpu`, `ext_mem`, `tech`, `max_freq_mhz`) or a
`start` design, so the explorer only varies what you can change: the accelerator, its
buffers and ports. Targets, budgets and the objective still apply to the system estimate,
which keeps the explorer from buying accelerator throughput with a host you do not have.
Reports print the generated IP's area next to the system estimate:

```text
Generated IP: the accelerator with AXI4 interfaces, 0.651 mm2. The host CPU, its SRAM,
interconnect and memory controller are assumed by the model, not generated; with them the
system estimate is 3.39 mm2.
```

## The AXI4 IP

`rtl/chipleap_accel_axi_top.sv` instantiates `chipleap_accel_axi` (in
`rtl/chipleap_accel_axi.sv`) with the design's parameters. It has:

| Port group | Protocol | Notes |
|---|---|---|
| `s_axil_*` | AXI4-Lite slave, 32 bit | The register map of the datasheet in a 64-byte window. One access at a time; a write and a read in the same cycle are taken write first. Responses are OKAY. |
| `m_axi_ar*`, `m_axi_r*` | AXI4 read masters, one per port, 32 bit | Single-beat INCR reads, `ARCACHE` 0011, ID 0, so data returns in order. Each port keeps up to 4 reads in flight and accepts read data every cycle (`RREADY` is tied high). |
| `m_axi_aw*`, `m_axi_w*`, `m_axi_b*` | AXI4 write master, 32 bit | Single-beat writes of the int32 results; address and data may be accepted in either order. `BREADY` is tied high. |
| `irq_o` | Level interrupt | Job done, when `IRQ_EN` is set. |

Read port 0 together with the write channels is one complete AXI4 master; read ports 1 and
up are read-only masters. `AXI_ID_WIDTH` in the top file sets the ID width (the wrapper
drives ID 0). Error responses are not reported to software.

The core reads one 32-bit word per port per cycle when the interconnect keeps up. That is
the bandwidth the model assumes, so an interconnect that accepts back-to-back single-beat
reads (most do; check width converters and clock crossings) is what makes the prediction
hold.

### Connecting it

- **AMD Zynq and Zynq UltraScale+**: package `chipleap_accel_axi_top` as IP (map the
  `s_axil_` and `m_axi_` port groups to AXI4-Lite and AXI4 interfaces in the IP packager),
  put `s_axil` on a general-purpose master port of the processing system through an AXI
  interconnect, and the read and write masters on high-performance slave ports so they
  reach DDR. Route `irq_o` to a fabric-to-processor interrupt.
- **Arm Cortex-M or Cortex-A SoCs**: `s_axil` on a peripheral (APB or AHB through a bridge,
  or AXI4-Lite) port, the masters on a manager port of the main interconnect that reaches
  the memory with the weights, `irq_o` to the NVIC or GIC.
- **Other RISC-V SoCs** (for example PULP's Cheshire or a LiteX SoC): the same, with the
  platform's AXI crossbar; in LiteX, add the Verilog sources and connect through its
  AXI-Lite and AXI interfaces.

### Firmware

`sw/chipleap_driver.h` is plain C99 with no platform includes. Define `CHIPLEAP_BASE` as
the address the interconnect gives the AXI4-Lite port before including it; without it the
header stops with an error rather than guessing. The calls are the same as on X-HEEP:
`chipleap_plan`, `chipleap_start`, `chipleap_busy`, `chipleap_cycles`. On a cached system,
flush X and W from the data cache before starting a job and invalidate Y after it, or map
the buffers uncached, because the accelerator reads and writes memory directly.

## How it is verified

`chipleap verify` on a standalone bundle lints `chipleap_accel_axi` with the core under
Verilator `-Wall`, and simulates the same randomized jobs as on X-HEEP through the wrapper:
the testbench programs the registers over AXI4-Lite and serves memory from an AXI4 slave
model that drops `ARREADY`, `AWREADY` and `WREADY` at random, answers after a fixed latency,
and checks that every transfer is a 32-bit single beat. On this release every job is
bit-exact on all three dataflows, and without memory stalls the cycle counts equal the
OBI core's and the model's exactly. With stalls they stay within a few percent.
