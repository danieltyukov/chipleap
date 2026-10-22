# Spec and design files

A spec says what you need; a design says how. Specs are YAML files read by `chipleap explore`,
`require`, `evaluate` and `run`, and by the MCP tools as a YAML string. `chipleap init` writes an
example. Errors name the key that is wrong, for example
`workload.llm: unknown keys speed` or `constraints.tech: '7nm' is not one of ...`.

## Spec

```yaml
name: llama-1b-edge                 # letters, digits, _ . -; also the design and file name
description: optional sentence
workload:
  llm:                              # or graph: (see below)
    model: llama-3.2-1b             # preset; list_models shows all
    phase: decode                   # decode | prefill
    context: 1024                   # KV length a decode step attends to
    prompt: 256                     # prompt length for prefill
    batch: 1
    weights: int4                   # int8 | int4 | int2 (int4/int2 carry fp16 group scales)
    kv: int8                        # int8 | int4
    group_size: 64
targets:
  throughput: 5                     # minimum tokens (or inferences) per second
  latency_ms: 300                   # maximum time per step
constraints:
  area_mm2: 8                       # core area budget, pads excluded
  power_mw: 400
  energy_mj: 50                     # per token or inference
  tech: [generic-28nm, generic-16nm]  # allowed: ihp-sg13g2 generic-65nm generic-28nm generic-16nm
  ext_mem: [none, hyperram, lpddr4-x16] # allowed external memories (catalog lists them)
  cpu: [cv32e20, cv32e40p]          # allowed X-HEEP cores
  max_freq_mhz: 500
  firmware_kib: 32                  # SRAM reserved for code, data and stack
allow:
  weights: [int8, int4]             # precisions the explorer may choose between
objective: energy                   # energy | area | throughput | edp
platform: xheep                     # xheep | standalone, see docs/platforms.md
start:                              # optional design the agent starts from
  tech: ihp-sg13g2
```

## Custom workloads

Custom workloads use `graph` instead of `llm`:

```yaml
workload:
  graph:
    name: kws
    unit: inference
    weights: int8
    ops:
      - conv2d: {name: conv1, h: 49, w: 10, cin: 1, cout: 64, kh: 10, kw: 4, stride: 2, act: true}
      - dwconv2d: {name: dw1, h: 25, w: 5, c: 64, kh: 3, kw: 3, act: true}
      - matmul: {name: fc, m: 1, k: 64, n: 12}
      - vector: {name: softmax, kind: softmax, elements: 12}
```

## Design

A design (`design.yaml`, or the `design` argument of the MCP tools) is a mapping. Every field
has a default, so a design file only needs what differs:

```yaml
name: my-design
tech: generic-28nm
freq_mhz: 400
cpu: cv32e40p                       # cv32e20 | cv32e40x | cv32e40p | cv32e40px
bus: NtoM                           # onetoM | NtoM (NtoM needed for interleaving and multi-port)
banks: 4
bank_kib: 32
il_banks: 4                         # 0, 2, 4 or 8 interleaved banks
il_bank_kib: 16
ext_mem: lpddr4-x16
ext_channels: 1                     # 1, 2 or 4
accel:                              # or null
  dataflow: gemv                    # gemv | ws | os
  rows: 8
  cols: 8
  w_bits: 8                         # widest weight format the PEs take
  ports: 4                          # 32-bit OBI read ports
  abuf_kib: 16
  wbuf_kib: 8
  double_buffer: true
  buffers: auto                     # auto (SRAM macros where the build takes them) | flops
platform: xheep                     # xheep (an X-HEEP SoC) | standalone (AXI4 IP, any host)
```

## LLM presets

| Key | Model | Parameters | Layers | Hidden | Heads / KV heads | FFN | Vocabulary | Source |
|---|---|---|---|---|---|---|---|---|
| `smollm2-135m` | SmolLM2 135M | 0.135 B | 30 | 576 | 9 / 3 | 1536 | 49152 | [config](https://huggingface.co/HuggingFaceTB/SmolLM2-135M/blob/main/config.json) |
| `smollm2-360m` | SmolLM2 360M | 0.362 B | 32 | 960 | 15 / 5 | 2560 | 49152 | [config](https://huggingface.co/HuggingFaceTB/SmolLM2-360M/blob/main/config.json) |
| `qwen2.5-0.5b` | Qwen2.5 0.5B | 0.494 B | 24 | 896 | 14 / 2 | 4864 | 151936 | [config](https://huggingface.co/Qwen/Qwen2.5-0.5B/blob/main/config.json) |
| `qwen3-0.6b` | Qwen3 0.6B | 0.596 B | 28 | 1024 | 16 / 8 | 3072 | 151936 | [config](https://huggingface.co/Qwen/Qwen3-0.6B/blob/main/config.json) |
| `gemma-3-1b` | Gemma 3 1B | 1.000 B | 26 | 1152 | 4 / 1 | 6912 | 262144 | [config](https://huggingface.co/unsloth/gemma-3-1b-it/blob/main/config.json) |
| `tinyllama-1.1b` | TinyLlama 1.1B | 1.100 B | 22 | 2048 | 32 / 4 | 5632 | 32000 | [config](https://huggingface.co/TinyLlama/TinyLlama-1.1B-Chat-v1.0/blob/main/config.json) |
| `llama-3.2-1b` | Llama 3.2 1B | 1.236 B | 16 | 2048 | 32 / 8 | 8192 | 128256 | [config](https://huggingface.co/unsloth/Llama-3.2-1B/blob/main/config.json) |
| `qwen2.5-1.5b` | Qwen2.5 1.5B | 1.544 B | 28 | 1536 | 12 / 2 | 8960 | 151936 | [config](https://huggingface.co/Qwen/Qwen2.5-1.5B/blob/main/config.json) |
| `qwen3-1.7b` | Qwen3 1.7B | 1.721 B | 28 | 2048 | 16 / 8 | 6144 | 151936 | [config](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/config.json) |
| `smollm2-1.7b` | SmolLM2 1.7B | 1.711 B | 24 | 2048 | 32 / 32 | 8192 | 49152 | [config](https://huggingface.co/HuggingFaceTB/SmolLM2-1.7B/blob/main/config.json) |
| `llama-3.2-3b` | Llama 3.2 3B | 3.213 B | 28 | 3072 | 24 / 8 | 8192 | 128256 | [config](https://huggingface.co/unsloth/Llama-3.2-3B/blob/main/config.json) |
| `phi-3-mini` | Phi-3 mini 3.8B | 3.821 B | 32 | 3072 | 32 / 32 | 8192 | 32064 | [config](https://huggingface.co/microsoft/Phi-3-mini-4k-instruct/blob/main/config.json) |

Gemma 3 1B uses a 512-token sliding window on five of every six layers. TinyLlama and Phi-3
mini have separate input and output embeddings; the others tie them.

## Objectives

| Objective | Minimizes |
|---|---|
| `energy` | energy per token or inference |
| `area` | core area |
| `throughput` | time per token or inference |
| `edp` | energy times latency |

Targets and budgets come first: the explorer prefers any design that misses its targets by less,
and only among designs that meet everything does it minimize the objective. A design that can
only meet the target at more area than the budget is reported with what it misses.
