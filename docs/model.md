# The analytical model

This page lists what ChipLeap models, every number it uses and where each comes from. The model is built to rank design points quickly and to explain them. It is not a replacement for simulation, which is why `chipleap verify` checks the accelerator part of it against cycle-accurate RTL simulation and synthesis.

## What one evaluation does

`chipleap.model.evaluate.evaluate(workload, design)`:

1. Places data: firmware, then live activations, then the KV cache, then weights go into on-chip SRAM until it is full. Whatever is left goes to external memory.
2. Works out the bandwidth of each data path (on-chip SRAM, external memory, the CPU's DMA).
3. Times every operator: matrix multiplies on the accelerator with the tile schedule below (or on the CPU when there is no accelerator or its PEs are too narrow), element-wise operators and requantization on the CPU.
4. Adds up energy (MACs, buffers, SRAM, external memory, CPU, leakage) and area (CPU, SoC logic, bus, SRAM macros, accelerator, memory interface).
5. Checks feasibility and returns violations in words.

Each operator records which component bounds it (compute, external memory, accelerator ports, on-chip memory, control), and the time shares add up to the step time. Weights streamed from external memory count against the accelerator's ports instead of the memory when the ports, at 32 bits per cycle each, take in less than the memory can sustain: then more ports or a faster clock helps, and a faster memory does not. That is what the agent reads when it names a bottleneck.

## Workloads

### Decoder-only transformers

Presets are copied from each model's `config.json` on Hugging Face; [src/chipleap/workload/llm.py](../src/chipleap/workload/llm.py) keeps the URL next to each. A test checks that the parameter count implied by each shape is within 2 % of the model card.

One decode step for batch `b` at context `c` lowers to, per layer:

- `attn.q`, `attn.kv`, `attn.out`, `ffn.gate_up`, `ffn.down`: matrix multiplies with `m = b` rows against the layer's weights.
- `attn.scores` and `attn.values`: one multiply per KV head and sequence. The query heads of a grouped-query-attention group share that head's keys and values, so they become the rows of the multiply (`m = heads / kv_heads`), and the KV cache is the second operand. Sliding-window layers (Gemma 3) attend to at most the window.
- RMSNorm, RoPE, softmax, SiLU and the gating multiply, residual adds, the KV append: element-wise operators.

Then the final norm and the LM head (`m = b`, `n = vocab`). With tied embeddings the head reads the embedding table, so nearly every parameter streams once per token, which is why decode is memory bound.

Prefill lowers to the same operators with `m = b * prompt` rows; causal attention sees on average half the prompt.

Weight formats: int8, int4 and int2. int4 and int2 carry one fp16 scale per group of 64 (configurable), so int4 costs 4.25 bits per weight in storage and traffic. The KV cache is int8 or int4.

Activation scratch is the live int8 activations of one step plus a 16 KiB staging area for int32 results. Large outputs, the logits above all, are produced in chunks and consumed as they arrive.

### Custom graphs

`conv2d` lowers to an im2col matrix multiply `(out_h * out_w) x (kh * kw * cin) x cout`; a depthwise convolution to one `(out_h * out_w) x (kh * kw) x 1` multiply per channel, which uses wide arrays poorly. `matmul` and `vector` operators are taken as written; `act: relu` costs one compare per element, `act: silu` or `gelu` a lookup.

## The accelerator

The generated accelerator computes `Y[M, N] = X[M, K] W[N, K]^T` with int8 activations, int8, int4 or int2 weights packed into 32-bit words, and int32 results. It attaches to X-HEEP through a register interface, `ports` 32-bit OBI read masters and one OBI write master.

### Tile schedule

A job is cut into tiles of `tm x tn x tk`, walked in `m0`, `n0`, `k0` order. `plan_tiles` (and the identical `chipleap_plan` in the C driver) chooses them:

- `tn` is fixed by the array: `rows` lanes for GEMV, `cols` for the systolic arrays.
- `tm` is at most 8 accumulator rows (GEMV and weight-stationary) or `rows` (output-stationary).
- If the whole `tm x K` block of X fits in the activation buffer, it stays resident while every `n0` reuses it, and only W is tiled along K. Otherwise both are tiled, and `tk` is the largest multiple of the K alignment that fits a buffer slot.
- With double buffering each buffer is split into two slots and the next tile loads while the core computes.

### Cycle counts

The constants below are read off the generated controller's state machines.

| Phase | Cycles |
|---|---|
| Load stage per tile | 3 (`L_NEXT`, `L_W`, `L_PUSH`), plus 1 (`L_X`) when activations load |
| One DMA read | `ceil(bytes / bytes_per_cycle) + latency + 3`, one of them a setup cycle |
| GEMV core, one tile | `tm * tk / cols + 3` |
| Weight-stationary core | `(tk / rows) * (2 * rows + tm + cols) + 1` |
| Output-stationary core | `tk + rows + cols` |
| Core stage overhead | 2 (`C_IDLE`, `C_FREE`) |
| Storing an output tile | `1 + ceil(4 * tm * tn / bytes_per_cycle) + latency + 2` |
| End of job | 1 |

`simulate_job` follows the loader and the core tile by tile through one or two buffer slots, exactly as the RTL hands them over. Without memory stalls it reproduces the RTL's cycle count exactly (see [verification.md](verification.md)). `job_timing`, the closed form the explorer uses, sums over the at most eight distinct tile shapes and is within 0.1 % of `simulate_job` on average; jobs of up to 256 tiles use `simulate_job` directly.

### Bandwidth

- On chip: each 32-bit port moves one word per cycle. A continuous X-HEEP bank serves one word per cycle however many masters ask; interleaved banks spread consecutive words, so `min(ports, interleaved banks)` words per cycle reach the accelerator. The one-to-many bus allows one master at a time, so several ports need the many-to-many (NtoM) bus.
- External: `min(memory sustained bandwidth x channels, 4 bytes x ports x clock)`. The memory controller has to expose as many slave ports as the accelerator has read ports.
- The CPU's DMA moves at most 4 bytes per cycle.
- First-word latency: 2 cycles on chip, 60 ns plus 4 cycles off chip.

## The CPU

Element-wise operators, requantization of accelerator results, and matrix multiplies when there is no accelerator run on the X-HEEP core. Requantization reads the int32 results back and writes int8.

| Key | Core | kGE | Cycles per MAC (int8 / int4 / int2) | Element-wise factor |
|---|---|---|---|---|
| `cv32e20` | CV32E20 (2-stage, RV32IMC) | 25 | 6 / 8 / 9 | 1.6 |
| `cv32e40x` | CV32E40X (4-stage, RV32IMC) | 45 | 3.5 / 5 / 6 | 1 |
| `cv32e40p` | CV32E40P with Xpulp | 65 | 0.6 / 1 / 1.2 | 0.6 |
| `cv32e40px` | CV32E40PX with Xpulp and CV-X-IF | 70 | 0.6 / 1 / 1.2 | 0.6 |

Element-wise cycles per element on CV32E40X, multiplied by the factor above: norm 6, softmax 18, SiLU or GELU 10, ReLU 2.5, RoPE 8, add 3, requantize 4, pool 2, copy 0.5 (the DMA moves the data). These are estimates for hand-written fixed-point kernels; the CV32E40P figures assume the Xpulp 4-way int8 dot product, hardware loops and post-increment loads (`corev_pulp`). The core areas are approximate public figures. They are the least certain inputs of the model and the easiest to replace with your own measurements.

Accelerator jobs cost the CPU 80 cycles each to program and to take the completion interrupt. While the accelerator runs, the CPU waits in WFI at 10 % of its active-cycle energy.

## Placement and feasibility

On-chip SRAM holds, in order: firmware (32 KiB unless the spec says otherwise), activations, the KV cache, weights. Anything left over goes to external memory, and a design is infeasible when:

- data does not fit and there is no external memory, or it exceeds the memory's capacity times its channels;
- the external memory is read-only (flash) and the KV cache or activations would have to live there;
- external data exceeds the 2 GiB window a 32-bit X-HEEP can map;
- the clock exceeds the node's practical maximum, or on IHP SG13G2 what the routed accelerator meets at the slow corner (see the calibration below);
- the memory interface needs a PHY that no one has built at that node, or faster I/O than the node's pads manage (serial memories are clocked down to the pads instead).

## Energy

Per step:

- MACs: `mac_pj(8, 8)`, since the generated PEs multiply 8 x 8 whatever the weight format; systolic arrays pay 25 % more for their pipeline registers. `mac_pj` scales the int8 x int8 figure by the product of widths (multiplier, 75 %) and their sum (accumulate, 25 %) for other operand widths.
- Accelerator buffers: every loaded byte is written once, and the core reads its operands, at the per-byte energy of a macro of the buffer's size.
- SRAM banks: activations in and out, weights or KV held on chip, CPU traffic, at the per-byte energy of a 32-bit access to the bank's macro.
- External memory: bytes x 8 x pJ per bit.
- CPU: active cycles x kGE x pJ per kGE per cycle.
- Leakage: leakage density x total area x step time.

## Area

Core area, excluding pads:

- CPU: kGE x NAND2 area / 0.7 placement utilisation.
- SoC logic: 110 kGE for X-HEEP's debug module, DMA, boot ROM, power manager, timers, interrupt controllers and peripherals, plus the bus (8 kGE one-to-many, 0.6 kGE per master and slave pair for NtoM).
- SRAM: each bank from the largest macros available, as `macros x periphery + KiB x array`.
- Accelerator logic: `accel_logic_kge`, fitted to Yosys synthesis of the generated RTL on SG13G2 (below): 30.5 kGE of control, registers and store engine, 0.3 kGE per OBI port, and per dataflow a cost per MAC plus a cost per lane, column or array edge. Weight width does not enter, because the PEs multiply 8 x 8 and only the unpacking depends on it.
- Accelerator buffers: SRAM macros.
- AXI4 adapter (standalone platform only): 0.54 kGE, synthesized on SG13G2 with the core as a black box; the same for 1 to 16 ports, because the read ports are wiring. With it, the accelerator is what `Evaluation.ip_area` reports as the generated IP; every other entry is the host system the model assumes (docs/platforms.md).
- Memory interface: PHY area (LPDDR only, scaled linearly from 28 nm) plus controller logic.

`chipleap verify` synthesizes the accelerator's logic with Yosys on SG13G2 and reports how far this estimate is from it (cell area against cell area, before the utilisation factor).

## Technology data

| Key | Node | VDD | NAND2 um2 | SRAM mm2/KiB + per macro | SRAM pJ/B (16 KiB) | int8 MAC pJ | CPU pJ/kGE/cycle | Leakage mW/mm2 | Max MHz | I/O MT/s | Basis |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `ihp-sg13g2` | 130 nm | 1.2 V | 7.2576 | 0.02846 + 0.0362 (max 32 KiB) | 15.3 | 1.44 | 2.57 | 0.016 | 100 | 155 | measured |
| `generic-65nm` | 65 nm | 1.0 V | 1.44 | 0.0061 + 0.00905 (max 32 KiB) | 3.14 | 0.499 | 0.892 | 0.04 | 300 | 800 | scaled |
| `generic-28nm` | 28 nm | 0.9 V | 0.49 | 0.0015 + 0.00168 (max 64 KiB) | 1.1 | 0.174 | 0.311 | 0.25 | 800 | 4266 | scaled |
| `generic-16nm` | 16 nm | 0.8 V | 0.2 | 0.00082 + 0.000548 (max 64 KiB) | 0.495 | 0.0787 | 0.14 | 0.6 | 1200 | 6400 | scaled |

Where the IHP SG13G2 numbers come from (`scripts/calibrate_sg13g2.py` re-measures them):

- NAND2 area 7.2576 um2 and leakage: `sg13g2_nand2_1` in `sg13g2_stdcell_typ_1p20V_25C.lib`.
- SRAM: the `RM_IHPSG13_1P_*` macro liberty files. Area is `0.0362 mm2 + 0.02846 mm2 per KiB` per macro, within 2 % for the 64-bit macros from 512 B to 16 KiB and for the 32 KiB 8192x32 macro (1520 um x 618 um). Energy depends on the access width. A wide (64-bit) read costs 15.3 pJ per byte at 16 KiB, scaling with size to the power 0.4, within 10 % of every 64-bit macro; accelerator buffers use this. X-HEEP's banks are 32 bits wide, and a narrow access pays the periphery for fewer bits: `6.5 pJ x KiB^0.6` per byte fits the 1, 2 and 32 KiB 32-bit macros within 12 %, so a 32 KiB bank costs about 52 pJ per byte.
- Accelerator logic, from Yosys and ABC (mapped, buffered and sized for a 5 ns target) against the same library, over 12 configurations of the generated RTL: all three dataflows, one to four ports, 8 to 64 MACs. Per MAC: 0.96 kGE for GEMV (multiplier and adder tree), 1.91 kGE weight-stationary, 1.67 kGE output-stationary; per GEMV lane 2.91 kGE (operand registers and accumulators), per weight-stationary column 1.65 kGE, per output-stationary row or column 0.81 kGE; 26.7 kGE of control and 2.3 kGE per port. Leave-one-out error: 2.9 % on average, 8.4 % at worst. `scripts/calibrate_sg13g2.py` repeats the fit.
- Clock: 100 MHz, in line with ETH Zurich's open-flow IHP chips (Croc/MLEM at 80 MHz typical, Basilisk at 64 to 77 MHz). The accelerator is also held to what it closes timing at after routing with LibreLane, at the slow corner (1.08 V, 125 C). With one port, the routed GEMV 1 x 8, weight-stationary 4 x 4 and output-stationary 4 x 4 engines met 100 MHz at 9.2 to 9.5 ns, so their period is taken as 9.53 ns. A synthesized datapath longer than the GEMV 1 x 8's (4.17 ns, from wider GEMV adder trees) stretches that period in proportion. More ports cost 1.95 ns each, a line through the routed GEMV 4 x 8 with two ports (10.13 ns) and GEMV 2 x 8 with four (14.02 ns), where the DMA, handing words to its ports one after another, sets the period. The period the model allows is never shorter than a routed one; [verification.md](verification.md#place-and-route) lists the runs.
- I/O: 155 MT/s, which on a byte-wide HyperBus at 80 % efficiency is the 124 MB/s Basilisk measured on silicon (Hot Chips 2025 poster).
- MAC and CPU energies: scaled from Horowitz (below), because the PDK gives no switching energy for logic without activity data.

The generic nodes are scaled estimates. Energies follow M. Horowitz, "Computing's energy problem (and what we can do about it)", ISSCC 2014, at 45 nm and 0.9 V (an 8-bit multiply 0.2 pJ, a 32-bit add 0.1 pJ, a 16 KiB SRAM read of 64 bits about 14 pJ), scaled linearly with feature size and with supply squared. SRAM array densities come from typical 6T bit-cell sizes at 70 % array efficiency. Maximum clocks and I/O rates are conservative values for small SoCs.

## External memories

| Key | Interface | Peak GB/s | Efficiency | Device MiB | pJ/bit | Pins | Oldest PHY node | Min MT/s |
|---|---|---|---|---|---|---|---|---|
| `qspi-flash` | Quad SPI NOR flash | 0.0665 | 0.85 | 128 | 60 | 6 | any (digital I/O) | 1 |
| `octal-psram` | Octal DDR PSRAM | 0.4 | 0.8 | 64 | 25 | 11 | any (digital I/O) | 1 |
| `hyperram` | HyperRAM (HyperBus x8) | 0.4 | 0.8 | 64 | 25 | 12 | any (digital I/O) | 1 |
| `lpddr2-x32` | LPDDR2-800 x32 | 3.2 | 0.7 | 1024 | 15 | 60 | 90 nm | 400 |
| `lpddr4-x16` | LPDDR4-3200 x16 | 6.4 | 0.7 | 2048 | 6 | 40 | 40 nm | 1600 |
| `lpddr4x-x32` | LPDDR4X-4266 x32 | 17.1 | 0.7 | 4096 | 5 | 70 | 40 nm | 1600 |
| `lpddr5-x16` | LPDDR5-6400 x16 | 12.8 | 0.7 | 2048 | 4 | 45 | 22 nm | 3200 |

- Peak rates follow from the interface: HyperRAM 2.0 at 200 MHz DDR on 8 bits is 400 MB/s (Infineon S27KS0643 datasheet); octal PSRAM likewise; QSPI flash at 133 MHz on 4 bits is 66.5 MB/s.
- Sustained efficiency for long sequential reads: 70 % for LPDDR. Real systems land between about 47 % (Architect Labs' Redwood on LPDDR4) and 82 to 94 % (openTPU on DDR3), so 70 % sits in the middle.
- Energy per bit covers the device and both ends of the interface, from datasheet active currents for the serial memories and published LPDDR figures. It is good to perhaps a factor of 1.5.
- PHY floors: commercial LPDDR4 PHYs reach 40 nm (for example Dolphin's on TSMC 40 nm G), LPDDR5 PHYs about 22 nm. No LPDDR or DDR PHY exists for any open PDK, so on IHP SG13G2 the serial memories are the only option.
- Serial memories run at whatever rate the node's pads support. On IHP SG13G2 that derates HyperRAM to 124 MB/s per channel.

## Validating against measured systems

The bandwidth bound is what decode tracks, and published systems confirm it. For Qwen3 0.6B at int8 (about 0.6 GB per token):

- openTPU on a Kintex-7 with 17.1 GB/s of DDR3 measured 21.6 tokens per second, against the model's bound of about 28 at full bandwidth.
- Architect Labs' Redwood on LPDDR4 at 16 GB/s peak measured 12.1, at about 47 % of peak bandwidth.

`chipleap require` reproduces the arithmetic. For Llama 3.2 1B at int4, ten tokens per second need about 6.7 GB/s of sustained bandwidth, which no HyperRAM or PSRAM configuration reaches, and the 642 MiB footprint alone rules them out.

## Known limitations

- The CPU kernel cycle counts and core areas are estimates.
- Logic energies outside IHP SG13G2 are scaled, not measured.
- Bank conflicts between the CPU, the DMA and the accelerator are not modelled; the accelerator is assumed to own its interleaved banks while it runs.
- The CPU and the accelerator do not overlap work within a step.
- Power is reported at full throughput; a duty-cycled always-on design draws proportionally less, plus leakage.
