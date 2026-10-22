# The analytical model

This page lists what ChipLeap models, every number it uses and where each comes from. The model is built to rank design points quickly and to explain them. It is not a replacement for simulation, which is why `chipleap verify` checks the accelerator part of it against cycle-accurate RTL simulation and synthesis.

## What one evaluation does

`chipleap.model.evaluate.evaluate(workload, design)`:

1. Picks the clock: the design's, or on an X-HEEP SoC on IHP SG13G2 what the SoC meets, when that is lower ([the SoC's clock](#the-socs-clock)). The CPU, its bus and the accelerator share it.
2. Places data: firmware, then live activations, then the KV cache, then weights go into on-chip SRAM until it is full. Whatever is left goes to external memory.
3. Works out the bandwidth of each data path (on-chip SRAM, external memory, the CPU's DMA).
4. Times every operator: matrix multiplies on the accelerator with the tile schedule below (or on the CPU when there is no accelerator or its PEs are too narrow), element-wise operators and requantization on the CPU.
5. Adds up energy (MACs, buffers, SRAM, external memory, CPU, leakage) and area (CPU, SoC logic, bus, SRAM macros, accelerator, memory interface).
6. Checks feasibility and returns violations in words.

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

The DMA deals the words of a transfer to the read ports in turn: port `p` reads words `p`, `p + ports`, `p + 2 ports` and so on, row by row, and each port has its own address generator, so the ports work in parallel. Without stalls every port issues a word per cycle, which is where `ceil(bytes / bytes_per_cycle)` comes from. A word stays with its port, so under random stalls a transfer waits for its slowest port. For the testbench's stalls (each request granted with probability `g` in a cycle, independently per port) the model adds the slowest port's expected lateness: one port's `n` words take `n / g` cycles with a standard deviation of `sqrt(n (1 - g)) / g`, and the slowest of `ports` ports is late by the expected largest of `ports` standard normal draws (0.56, 1.03, 1.42 and 1.77 deviations for 2, 4, 8 and 16 ports). The explorer works with sustained bandwidths and leaves this term out.

### Bandwidth

- On chip: each 32-bit port moves one word per cycle. A continuous X-HEEP bank serves one word per cycle however many masters ask; interleaved banks spread consecutive words, so `min(ports, interleaved banks)` words per cycle reach the accelerator. The one-to-many bus allows one master at a time, so several ports need the many-to-many (NtoM) bus.
- External: `min(memory sustained bandwidth x channels, 4 bytes x ports x clock)`. On X-HEEP the weights come over the accelerator's direct read path, a port per read port beside X-HEEP's bus, at the accelerator's clock ([xheep.md](xheep.md#external-memory-the-accelerators-direct-path)); the memory controller has one port per read port and keeps reads in flight. On the standalone platform they come over the accelerator's AXI4 masters.
- The CPU's DMA moves at most 4 bytes per cycle.
- First-word latency: 2 cycles on chip, 60 ns plus 4 cycles off chip (at least 8). The direct path keeps enough reads in flight per port to cover it at a word per cycle (`chipleap.model.accel.direct_outstanding`: 16 up to about 175 MHz, a power of two above), which is what lets a transfer take `ceil(bytes / bytes_per_cycle) + latency + 3` cycles.

## The CPU

Element-wise operators, requantization of accelerator results, and matrix multiplies when there is no accelerator run on the X-HEEP core. Requantization reads the int32 results back and writes int8.

| Key | Core | kGE | Cycles per MAC (int8 / int4 / int2) | Element-wise factor |
|---|---|---|---|---|
| `cv32e20` | CV32E20 (2-stage, RV32IMC) | 33.9 | 6 / 8 / 9 | 1.6 |
| `cv32e40x` | CV32E40X (4-stage, RV32IMC) | 45 | 3.5 / 5 / 6 | 1 |
| `cv32e40p` | CV32E40P with Xpulp | 65 | 0.6 / 1 / 1.2 | 0.6 |
| `cv32e40px` | CV32E40PX with Xpulp and CV-X-IF | 70 | 0.6 / 1 / 1.2 | 0.6 |

Element-wise cycles per element on CV32E40X, multiplied by the factor above: norm 6, softmax 18, SiLU or GELU 10, ReLU 2.5, RoPE 8, add 3, requantize 4, pool 2, copy 0.5 (the DMA moves the data). These are estimates for hand-written fixed-point kernels; the CV32E40P figures assume the Xpulp 4-way int8 dot product, hardware loops and post-increment loads (`corev_pulp`). CV32E20's area is measured in the SmolLM2 SoC, by the same synthesis as X-HEEP's logic (below); the other cores' areas are approximate public figures. They are the least certain inputs of the model and the easiest to replace with your own measurements.

Accelerator jobs cost the CPU 80 cycles each to program and to take the completion interrupt. While the accelerator runs, the CPU waits in WFI at 10 % of its active-cycle energy.

## The SoC's clock

The CPU, its bus and the accelerator share X-HEEP's clock. On IHP SG13G2 the model runs them at the design's clock or at what X-HEEP's SoC meets after routing, whichever is lower, and reports that clock, the SoC's limit and the accelerator's own limit apart (`chipleap explore` and `chipleap evaluate`, and the datasheet).

The SoC does in one cycle two things the accelerator alone never does, across a die of millimetres:

- It reads a bank. The bank's macro puts the word out its read time after the clock edge (`chipleap.macro.sram.IHP_1P_READ_NS`, as for the accelerator's buffers: 7.25 ns for the 1024 x 32 at the slow corner), and the crossbar takes it to the CPU's load path or into the accelerator's buffer macro.
- Its OBI bus grants a master in the cycle it asks. The CPU's request leaves its decoder and load-store unit, passes the crossbar's arbiter, and the grant it leaves another master, the accelerator's write port, ends in that master's store engine.

So the model takes the SoC's period as the slower of the two (`chipleap.hw.tech.soc_signoff_mhz`):

    period = max(read time of the bank's macro + SOC_READ_PATH_NS, SOC_BUS_PATH_NS)

Both terms are measured on the SmolLM2 SoC (CV32E20, the one-to-many bus, one 32 KiB bank of eight 1024 x 32 macros, the GEMV 1 x 8 accelerator) laid out with `chipleap verify --steps soc --soc-clock 40`, after detailed routing and extraction, at the slow corner (1.08 V, 125 C) with the 5 % derate the step's constraints apply: SOC_READ_PATH_NS is the period the slowest path from a bank needs, less the 1024 x 32's read time, and SOC_BUS_PATH_NS the period the slowest other path needs. The macro of each of the design's banks gives its read time (the 1024 x 32 for banks of 4 KiB and more, 512 x 32 for 2 KiB, 256 x 32 for 1 KiB), so smaller banks shorten the read but not the bus.

The first estimate of this limit, 17.64 ns (56.7 MHz), was the slowest bank read after the repair that follows global routing, in a layout at 90 MHz, without the derate. That is no basis for a clock. The repairs work against the global router's estimate of each wire, and on the SoC's long wires the extraction after detailed routing puts 14 to 20 % more capacitance on them: at 50 MHz an SRAM output net repaired to 0.0576 pF came out 0.0657 pF, a clock net's slew went from 0.607 to 0.728 ns, and the slow corner's worst setup slack went from +0.54 ns after global routing to -3.63 ns after routing, a period of 23.63 ns. The SoC's step now keeps margins for that (docs/verification.md), and the model's limit is what the routed layout needs.

## The accelerator's own clock

`accel_mhz` in a design puts the accelerator on a clock of its own, behind clock domain crossings at its bus interface ([verification.md](verification.md#the-accelerator-on-its-own-clock)). The CPU, its bus and its DMA stay on the SoC's clock above, and the accelerator's operators are timed at `accel_mhz`. The accelerator's clock limit (`chipleap.hw.tech.Technology.clock_limit_mhz`) then applies to `accel_mhz`, not to the design's clock.

Every word still crosses at the bus's rate, so the rates the bus gives, in bytes per bus cycle, become rates per accelerator cycle divided by `k`, the accelerator's cycles per bus cycle. They are capped at a word per read port, and one for stores, per accelerator cycle. A request and its response each pass two synchronizer flip-flops of the clock they enter, so a response that comes `L` bus cycles after its grant reaches the accelerator after

    ceil(k (L + CDC_BUS_CYCLES) + CDC_ACCEL_CYCLES)

of its cycles (`chipleap.model.accel.crossed`). Both terms are fitted to 1,040 jobs simulated in Verilator without stalls: five accelerators, sixteen pairs of clocks with `k` from 0.39 to 4, each at a random phase. With CDC_BUS_CYCLES = 1.25 and CDC_ACCEL_CYCLES = 3, every simulated count is within 5.4 % of the model's, 0.1 % on average. The CPU's 80 cycles to program and start a job (JOB_SETUP_CYCLES) are counted at the SoC's clock.

The crossings add ACCEL_CDC_KGE = 8.43 kGE (the register handshake, the write port's FIFOs, the reset and interrupt synchronizers) and ACCEL_CDC_PORT_KGE = 6.76 kGE per read port (its two FIFOs of eight 32-bit words) to the accelerator's logic. They are measured with Yosys on IHP SG13G2: 15.53 kGE with one port, 21.46 with two and 35.64 with four, and the line through them is within 2.3 % of each.

The weights do not cross. With external memory the accelerator reads it over its direct path, whose memory ports run on the accelerator's clock (X-HEEP's side crosses into it instead, [xheep.md](xheep.md#external-memory-the-accelerators-direct-path)), so the external stream's rate and latency are taken at `accel_mhz` and are not passed through `crossed`. On the SmolLM2 example at the SoC's 42.3 MHz, an accelerator at 90 MHz gives 1.79 token/s against 1.33 on the SoC's clock: its stream goes from 169 MB/s, four bytes per SoC cycle, to the memory's 248 MB/s. The crossings add 0.158 mm2, and the direct path on the accelerator's clock 0.015 mm2 more than on the SoC's (X-HEEP's side crossing, less the response queues the crossings already size).

## Where the weights come from

Decoding a token streams nearly every weight once, so how the weights reach the accelerator decides the rate. For the SmolLM2 example (SmolLM2 135M at int4, 80.35 MB from external memory per token: two channels of octal PSRAM, 248 MB/s; the GEMV 1 x 8 with one read port), at the routed SoC's 35 MHz and the model's SoC limit of 42.3 MHz:

| Weights reach the accelerator | Stream at 35 MHz | token/s at 35 MHz | token/s at 42.3 MHz |
|---|---|---|---|
| X-HEEP's DMA copies them into a bank, a PSRAM access per word (0.5 B per cycle) | 18 MB/s | 0.21 | 0.25 |
| the same with a controller that prefetches sequential words and an extra 8 KiB bank (2 B per cycle) | 70 MB/s | 0.69 | 0.84 |
| the direct path, the accelerator on the SoC's clock (4 B per cycle) | 140 MB/s | 1.10 | 1.33 |
| the direct path, the accelerator at 90 MHz on its own clock | 248 MB/s | 1.67 | 1.79 |
| the direct path, the accelerator at 100 MHz | 248 MB/s | 1.68 | 1.80 |
| the DMA path (2 B per cycle) with the accelerator at 90 MHz | 70 MB/s | 0.72 | 0.87 |

The DMA rows are what X-HEEP alone allows, since its bus lets no external master reach the external slave window and serves one transaction per cycle with none outstanding ([xheep.md](xheep.md#external-memory-the-accelerators-direct-path)); they are this model with the accelerator's external stream set to the DMA's rate. The direct rows are the model as it is. The SoC as laid out today has neither the prefetching controller nor the extra bank, so without the direct path it would reach 0.21 token/s.

With the direct path and the accelerator on its own clock, the accelerator's share of a token is the memory's: 324 ms to stream 80.35 MB, 367 ms in all at 35 MHz. What remains is the CPU's work, 233 ms per token at 35 MHz (193 ms at 42.3): RMSNorm, RoPE, softmax, SiLU and the requantization of every accelerator result run on the CV32E20 ([the CPU](#the-cpu)). That is why even the best path reaches 1.67 token/s, not 2: at 2 token/s a token has 500 ms, and the memory's 324 ms and the CPU's 233 ms do not fit, since the model runs the CPU and the accelerator one after the other. A faster memory would help only until the CPU's time is all that is left (4.3 token/s at 35 MHz); moving that work off the CPU, onto the accelerator or a vector unit beside it, is a possible next step that ChipLeap does not model or build today.

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
- SoC logic: 208.4 kGE for X-HEEP as ChipLeap configures it ([xheep.md](xheep.md)): the debug module with its SPI slave (33 kGE), the DMA (35 kGE), the flash's SPI host and controller (68 kGE), GPIO, UART, timers, interrupt controllers, boot ROM, power manager and pad control; plus the bus (6.1 kGE one-to-many, 0.6 kGE per master and slave pair for NtoM). Measured on the SmolLM2 SoC with Yosys and ABC against IHP SG13G2's typical liberty, the hierarchy kept, the CPU, the accelerator and the bus taken out: 208.4 kGE of 288.0. With X-HEEP's default peripherals (two more SPI hosts at 62 kGE each, the I2C, the I2S, a DMA with two master ports) it was 401.8 kGE, 409.6 with the bus. LibreLane's synthesis of the same SoC, which maps for area, comes out 12 % smaller (253.8 kGE in all), so the step that lays the SoC out reads the model about that much high.
- SRAM: on IHP SG13G2 each of X-HEEP's banks is the PDK's single-port macros of 32-bit words with a bit mask that hold it (`chipleap.model.evaluate.bank_mm2`), as the SoC's layout builds it (X-HEEP writes bytes, and the one macro without a mask, 8192 x 32, cannot): a 32 KiB bank is eight 1024 x 32 macros, 1.121 mm2, where the density formula gave 0.947 mm2. Other nodes build each bank from the largest macros available, as `macros x periphery + KiB x array`.
- Accelerator logic: `accel_logic_kge`, fitted to Yosys synthesis of the generated RTL on SG13G2 (below): 24.9 kGE of control, registers and store engine, 2.8 kGE per OBI read port (its address generator, request and response queue), and per dataflow a cost per MAC plus a cost per lane, column or array edge. Weight width does not enter, because the PEs multiply 8 x 8 and only the unpacking depends on it. On IHP SG13G2 a GEMV engine whose buffers are banks of macros (next item) also has the banks' interface (`accel_bank_kge`): the macros' write path and the registers that read each chunk a cycle ahead, 5.5 kGE plus 0.10 kGE per MAC, plus 1.0 kGE with a double buffer. With external memory on X-HEEP it also has its direct read path (`chipleap.model.evaluate.direct_kge`): 1.24 kGE per read port for the response queue of sixteen reads in flight and the choice of path (0.28 kGE per port on its own clock, whose crossings already queue eight), 0.016 kGE per entry and bit of buffer address for each read in flight beyond sixteen, and `chipleap_mem`, the arbiters in front of the memory controller, 1.47 kGE, with 2.44 kGE more on its own clock for X-HEEP's side crossing into it. Measured with Yosys on IHP SG13G2 with and without the path: 1.77, 2.29 and 4.91 kGE for one, two and four ports; the per-port term is within 0.53 kGE of each. The SmolLM2 example's path is 0.028 mm2 on the SoC's clock.
- Accelerator buffers: SRAM macros. On IHP SG13G2 they are IHP's own single-port macros (`chipleap.macro.sram.IHP_1P`, the PDK's outlines, no PDK needed): a GEMV engine with COLS of 4, 8 or 16 gets exactly the banks the layout places for a one-port build (`chipleap.gen.banks`: per slot of a double buffer, and per lane for the weights, COLS bytes wide), whatever its number of ports, since more ports read the same words; any other build gets each slot of each buffer in macros of 32-bit words. So does a GEMV engine whose banks' two rows of macros would leave a core more than four times as wide as tall, such as the 1 x 16 with a double buffer (eight 784 um macros, a core of 3249 x 438 um): it did not route, so place and route keeps flop buffers, and the model prices and times it as a build without banks, with no bank interface and the flop-buffer build's clock. Both decide by one function, `chipleap.gen.banks.layout_reason`, and the evaluation names the reason in its warnings. A slot costs the macro that holds it: the SmolLM2 example's 1 KiB slots of 64-bit words take 256x64 macros, half empty, 0.373 mm2 for 4 KiB of buffers where the density formula gives 0.186 mm2. Other nodes use `macros x periphery + KiB x array` per buffer.
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
- Accelerator logic, from Yosys and ABC (mapped, buffered and sized for a 5 ns target) against the same library, over 24 configurations of the generated RTL: all three dataflows, one to eight ports, 4 to 64 MACs. Per MAC: 0.90 kGE for GEMV (multiplier and adder tree), 1.74 kGE weight-stationary, 1.87 kGE output-stationary; per GEMV lane 3.51 kGE (operand registers and accumulators), per weight-stationary column 2.61 kGE, per output-stationary row or column 0.15 kGE; 24.9 kGE of control and 2.8 kGE per port. Leave-one-out error: 2.8 % on average, 7.1 % at worst. The fit used 12 configurations before, too few for eight terms: when the core went inside its `chipleap_accel` wrapper, Yosys's result moved by 3 to 5 % on some designs with the logic unchanged, and the leave-one-out error went to 5.0 % and 9.8 %. The bank interface of a macro-backed GEMV build is fitted to 12 such builds (one to eight lanes, 4 to 16 columns, single and double buffered), each core synthesized on its own against its flop-buffer build, within 0.67 kGE of every build left out. The basis is ChipLeap's own synthesis throughout, which buffers and sizes for 5 ns as placement and routing would. LibreLane's synthesis maps for area without buffering or sizing and comes out about 10 % smaller on the same core (the SmolLM2 accelerator's macro-backed core: 0.299 mm2 against 0.331 mm2), so against LibreLane's synthesis the model reads about 10 % high. What placement and routing add on top (48 % over LibreLane's synthesis for that design) is reported apart from the model ([verification.md](verification.md#place-and-route)). `scripts/calibrate_sg13g2.py` repeats both fits.
- Clock: 100 MHz, in line with ETH Zurich's open-flow IHP chips (Croc/MLEM at 80 MHz typical, Basilisk at 64 to 77 MHz). The accelerator is also held to what it closes timing at after routing with LibreLane, at the slow corner (1.08 V, 125 C). With one port, the routed GEMV 1 x 8, weight-stationary 4 x 4 and output-stationary 4 x 4 engines met 100 MHz at 9.0 to 9.3 ns, so their period is taken as 9.33 ns. A synthesized datapath longer than the GEMV engine's (4.35 ns, from wider GEMV adder trees) stretches that period in proportion. A GEMV engine adds 0.46 ns per doubling of its lanes, which bounds the routed 2 x 8 engine with four ports (9.63 ns) and the 4 x 8 with two, which took 9.69 ns in one run and 10.23 ns in another of the same logic: routing alone moves a design's period by half a nanosecond. More ports make the engine larger and its wires longer: the period is at least 8.966 + 0.1413 ns per port, a line through the routed GEMV 1 x 8 with one port (9.11 ns) and with eight (10.09 ns), above the one with sixteen (10.41 ns, routed at 35 % core utilisation). The core reads its tile descriptor from a register loaded a cycle ahead; while it selected the descriptor by its buffer slot after the registers, that select led into the GEMV engine's operand registers and accumulators and set the period of the 4 x 8 engine with two ports (10.27 ns) and the 1 x 8 with eight (11.18 ns) and sixteen (11.25 ns). The DMA itself no longer sets the period either, since each port has an address generator of its own; when it handed words to its ports one after another, the GEMV 2 x 8 engine with four ports took 14.02 ns, and it now takes 9.63 ns. A GEMV engine whose buffers are banks of IHP macros (above) registers each bank word in the cycle after its read, so its period is at least the slowest macro's read time at the slow corner (`chipleap.macro.sram.IHP_1P_READ_NS`, from the macros' liberty: 5.0 to 5.3 ns at 256 words, 7.0 to 7.3 ns at 1024, 8.8 to 9.0 ns at 2048 and 4096) plus 2.65 ns for the word's select and unpacking into the operand registers and the wires from the macro rows; and since its logic spreads along the rows of macros, its period is also the flop build's plus 0.75 ns. Routed at 100 MHz, the GEMV 1 x 8 met about 106 MHz with 256 x 64 macros (the model allows 99 MHz), 102 MHz and, in another run, 99 MHz with 1024 x 64 macros (99 MHz), and 86.5 MHz with 2048 x 64 macros, on the macros' read path (85.9 MHz); the GEMV 2 x 8 with 256 x 64 macros met about 99 MHz (95 MHz). A build whose macro rows would leave a core too flat to route has flop buffers in the layout, and the model times it as a flop build. The period the model allows is never shorter than a routed one; [verification.md](verification.md#place-and-route) lists the runs.
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
- X-HEEP's own external accesses share the first memory port with the accelerator's direct path, and while both ask the arbiter alternates between them; the model does not charge the accelerator for them (the CPU waits while the accelerator streams, and the DMA has nothing to move in decode).
- The direct path can also read activations from external memory, but the model always keeps them on chip.
- Power is reported at full throughput; a duty-cycled always-on design draws proportionally less, plus leakage.
