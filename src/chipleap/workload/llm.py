"""Decoder-only transformer workloads (LLM decode and prefill).

The presets are copied from each model's published ``config.json`` on Hugging Face; the
URL is kept next to each entry. ``tests/test_llm.py`` checks the parameter counts that
follow from these shapes against the totals the model cards report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from chipleap.workload.ops import (
    MatMul,
    Op,
    Operand,
    Storage,
    VectorKind,
    VectorOp,
    Workload,
)


@dataclass(frozen=True)
class LLMArch:
    """Shape of a decoder-only transformer."""

    key: str
    name: str
    hidden: int
    layers: int
    heads: int
    kv_heads: int
    head_dim: int
    ffn: int
    vocab: int
    tied_embeddings: bool = True
    gated_ffn: bool = True
    sliding_window: int = 0  # 0 means every layer attends to the full context
    global_every: int = 0  # with a sliding window: every n-th layer is global
    source: str = ""

    def __post_init__(self) -> None:
        if self.heads % self.kv_heads:
            raise ValueError(f"{self.key}: heads must be a multiple of kv_heads")

    @property
    def q_dim(self) -> int:
        return self.heads * self.head_dim

    @property
    def kv_dim(self) -> int:
        return self.kv_heads * self.head_dim

    @property
    def group(self) -> int:
        """Query heads that share one KV head (grouped-query attention)."""
        return self.heads // self.kv_heads

    @property
    def layer_params(self) -> int:
        attn = self.hidden * self.q_dim + 2 * self.hidden * self.kv_dim + self.q_dim * self.hidden
        ffn = (3 if self.gated_ffn else 2) * self.hidden * self.ffn
        norms = 2 * self.hidden
        return attn + ffn + norms

    @property
    def embedding_params(self) -> int:
        return self.vocab * self.hidden

    @property
    def params(self) -> int:
        head = 0 if self.tied_embeddings else self.vocab * self.hidden
        return self.embedding_params + self.layers * self.layer_params + self.hidden + head

    def local_layers(self) -> int:
        """Layers that use the sliding window (the rest attend globally)."""
        if not self.sliding_window:
            return 0
        if not self.global_every:
            return self.layers
        return self.layers - self.layers // self.global_every


def _hf(repo: str) -> str:
    return f"https://huggingface.co/{repo}/blob/main/config.json"


PRESETS: dict[str, LLMArch] = {
    a.key: a
    for a in (
        LLMArch(
            "smollm2-135m",
            "SmolLM2 135M",
            576,
            30,
            9,
            3,
            64,
            1536,
            49152,
            source=_hf("HuggingFaceTB/SmolLM2-135M"),
        ),
        LLMArch(
            "smollm2-360m",
            "SmolLM2 360M",
            960,
            32,
            15,
            5,
            64,
            2560,
            49152,
            source=_hf("HuggingFaceTB/SmolLM2-360M"),
        ),
        LLMArch(
            "qwen2.5-0.5b",
            "Qwen2.5 0.5B",
            896,
            24,
            14,
            2,
            64,
            4864,
            151936,
            source=_hf("Qwen/Qwen2.5-0.5B"),
        ),
        LLMArch(
            "qwen3-0.6b",
            "Qwen3 0.6B",
            1024,
            28,
            16,
            8,
            128,
            3072,
            151936,
            source=_hf("Qwen/Qwen3-0.6B"),
        ),
        LLMArch(
            "gemma-3-1b",
            "Gemma 3 1B",
            1152,
            26,
            4,
            1,
            256,
            6912,
            262144,
            sliding_window=512,
            global_every=6,
            source=_hf("unsloth/gemma-3-1b-it"),
        ),
        LLMArch(
            "tinyllama-1.1b",
            "TinyLlama 1.1B",
            2048,
            22,
            32,
            4,
            64,
            5632,
            32000,
            tied_embeddings=False,
            source=_hf("TinyLlama/TinyLlama-1.1B-Chat-v1.0"),
        ),
        LLMArch(
            "llama-3.2-1b",
            "Llama 3.2 1B",
            2048,
            16,
            32,
            8,
            64,
            8192,
            128256,
            source=_hf("unsloth/Llama-3.2-1B"),
        ),
        LLMArch(
            "qwen2.5-1.5b",
            "Qwen2.5 1.5B",
            1536,
            28,
            12,
            2,
            128,
            8960,
            151936,
            source=_hf("Qwen/Qwen2.5-1.5B"),
        ),
        LLMArch(
            "qwen3-1.7b",
            "Qwen3 1.7B",
            2048,
            28,
            16,
            8,
            128,
            6144,
            151936,
            source=_hf("Qwen/Qwen3-1.7B"),
        ),
        LLMArch(
            "smollm2-1.7b",
            "SmolLM2 1.7B",
            2048,
            24,
            32,
            32,
            64,
            8192,
            49152,
            source=_hf("HuggingFaceTB/SmolLM2-1.7B"),
        ),
        LLMArch(
            "llama-3.2-3b",
            "Llama 3.2 3B",
            3072,
            28,
            24,
            8,
            128,
            8192,
            128256,
            source=_hf("unsloth/Llama-3.2-3B"),
        ),
        LLMArch(
            "phi-3-mini",
            "Phi-3 mini 3.8B",
            3072,
            32,
            32,
            32,
            96,
            8192,
            32064,
            tied_embeddings=False,
            source=_hf("microsoft/Phi-3-mini-4k-instruct"),
        ),
    )
}


# Effective storage bits per element. int4 and int2 carry one fp16 scale per group.
WEIGHT_FORMATS = ("int8", "int4", "int2")
KV_FORMATS = ("int8", "int4")


def format_bits(fmt: str, group_size: int = 64) -> tuple[int, float]:
    """Return (compute bits, storage bits per element) for a weight or KV format."""
    if fmt == "int8":
        return 8, 8.0
    if fmt in ("int4", "int2"):
        bits = int(fmt[3:])
        if group_size < 8:
            raise ValueError("group_size must be at least 8")
        return bits, bits + 16 / group_size
    raise ValueError(f"unknown format {fmt!r}; use one of int8, int4, int2")


@dataclass(frozen=True)
class LLMWorkloadSpec:
    model: str
    phase: str = "decode"  # decode | prefill
    context: int = 1024  # KV length the decode step attends to
    prompt: int = 256  # prompt length for prefill
    batch: int = 1
    weights: str = "int4"
    kv: str = "int8"
    group_size: int = 64

    def __post_init__(self) -> None:
        if self.phase not in ("decode", "prefill"):
            raise ValueError("phase must be decode or prefill")
        if self.weights not in WEIGHT_FORMATS:
            raise ValueError(f"weights must be one of {', '.join(WEIGHT_FORMATS)}")
        if self.kv not in KV_FORMATS:
            raise ValueError(f"kv must be one of {', '.join(KV_FORMATS)}")
        for field in ("context", "prompt", "batch"):
            if getattr(self, field) < 1:
                raise ValueError(f"{field} must be at least 1")


def get_arch(model: str | LLMArch) -> LLMArch:
    if isinstance(model, LLMArch):
        return model
    key = model.lower()
    if key not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise KeyError(f"unknown model {model!r}. Known models: {known}")
    return PRESETS[key]


def build_llm(spec: LLMWorkloadSpec, arch: LLMArch | None = None) -> Workload:
    """Lower one decode step (or one prefill pass) to MatMul and VectorOp operators."""
    a = arch or get_arch(spec.model)
    _, wbits = format_bits(spec.weights, spec.group_size)
    _, kvbits = format_bits(spec.kv, spec.group_size)
    b = spec.batch
    decode = spec.phase == "decode"
    rows = b if decode else b * spec.prompt  # activation rows per weight matmul
    L = a.layers

    def mm(name: str, k: int, n: int, group: str, count: int = L) -> MatMul:
        return MatMul(name, m=rows, k=k, n=n, count=count, x_bits=8, w_bits=wbits, group=group)

    ops: list[Op] = []
    ops.append(VectorOp("embed.gather", VectorKind.COPY, a.hidden * rows))
    ops.append(VectorOp("attn.norm", VectorKind.NORM, a.hidden * rows, count=L))
    ops.append(mm("attn.q", a.hidden, a.q_dim, "attn"))
    ops.append(mm("attn.kv", a.hidden, 2 * a.kv_dim, "attn"))
    ops.append(VectorOp("attn.rope", VectorKind.ROPE, (a.q_dim + a.kv_dim) * rows, count=L))
    new_kv = 2 * a.kv_dim * rows * kvbits / 8
    ops.append(
        VectorOp("kv.append", VectorKind.COPY, 2 * a.kv_dim * rows, count=L, kv_bytes=new_kv)
    )

    # Attention: one score and one value matmul per KV head and sequence. The query heads
    # of a GQA group share the head's K and V, so they become the rows of one matmul.
    local = a.local_layers()
    tokens_seen = spec.context if decode else spec.prompt
    spans: list[tuple[str, int, int]] = []  # (name, layers, KV tokens held)
    if local:
        spans.append(("attn.local", local, min(tokens_seen, a.sliding_window)))
    if L - local:
        spans.append(("attn", L - local, tokens_seen))
    for prefix, layers, held in spans:
        if decode:
            q_rows, keys = a.group, held
        else:
            # Causal prefill: on average each query attends to half of what it can see.
            q_rows, keys = a.group * spec.prompt, max(1, math.ceil(held / 2))
        count = layers * a.kv_heads * b
        ops.append(
            MatMul(
                f"{prefix}.scores",
                m=q_rows,
                k=a.head_dim,
                n=keys,
                count=count,
                x_bits=8,
                w_bits=kvbits,
                w_kind=Operand.KV,
                group="kv",
            )
        )
        ops.append(VectorOp(f"{prefix}.softmax", VectorKind.SOFTMAX, q_rows * keys, count=count))
        ops.append(
            MatMul(
                f"{prefix}.values",
                m=q_rows,
                k=keys,
                n=a.head_dim,
                count=count,
                x_bits=8,
                w_bits=kvbits,
                w_kind=Operand.KV,
                group="kv",
            )
        )

    ops.append(mm("attn.out", a.q_dim, a.hidden, "attn"))
    ops.append(VectorOp("attn.residual", VectorKind.ADD, a.hidden * rows, count=L))
    ops.append(VectorOp("ffn.norm", VectorKind.NORM, a.hidden * rows, count=L))
    if a.gated_ffn:
        ops.append(mm("ffn.gate_up", a.hidden, 2 * a.ffn, "ffn"))
        ops.append(VectorOp("ffn.act", VectorKind.ACT, a.ffn * rows, count=L))
    else:
        ops.append(mm("ffn.up", a.hidden, a.ffn, "ffn"))
        ops.append(VectorOp("ffn.act", VectorKind.ACT, a.ffn * rows, count=L))
    ops.append(mm("ffn.down", a.ffn, a.hidden, "ffn"))
    ops.append(VectorOp("ffn.residual", VectorKind.ADD, a.hidden * rows, count=L))
    ops.append(VectorOp("final.norm", VectorKind.NORM, a.hidden * rows))

    # Decode only needs logits for the new token. Prefill needs them for the last
    # prompt position of each sequence.
    head_rows = b
    ops.append(
        MatMul(
            "lm_head",
            m=head_rows,
            k=a.hidden,
            n=a.vocab,
            count=1,
            x_bits=8,
            w_bits=wbits,
            group="lm_head",
        )
    )

    weight_bytes = a.params * wbits / 8
    kv_store = sum(2 * a.kv_dim * held * layers * b * kvbits / 8 for _, layers, held in spans)
    # Live int8 activations plus a 16 KiB staging area for int32 results. Large outputs,
    # the logits above all, are produced in chunks and consumed (sampled) as they arrive.
    act_scratch = rows * (2 * a.hidden + 2 * a.ffn + a.q_dim + 2 * a.kv_dim) + 16 * 1024
    storage = Storage(weights=weight_bytes, kv=kv_store, activations=float(act_scratch))

    phase = "decode" if decode else "prefill"
    tokens = b if decode else b * spec.prompt
    desc = (
        f"{a.name} {phase}, batch {b}, "
        + (f"context {spec.context}" if decode else f"prompt {spec.prompt}")
        + f", weights {spec.weights}, KV {spec.kv}"
    )
    meta = (
        ("model", a.name),
        ("phase", phase),
        ("params", f"{a.params / 1e9:.3f} B"),
        ("weights", spec.weights),
        ("kv", spec.kv),
        ("source", a.source),
    )
    return Workload(
        name=f"{a.key}-{phase}",
        description=desc,
        ops=tuple(ops),
        storage=storage,
        tokens_per_step=tokens,
        unit="token",
        params=a.params,
        meta=meta,
    )


def with_context(spec: LLMWorkloadSpec, context: int) -> LLMWorkloadSpec:
    return replace(spec, context=context)
