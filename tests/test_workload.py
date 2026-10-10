import math

import pytest

from chipleap.workload import PRESETS, LLMWorkloadSpec, build_graph, build_llm
from chipleap.workload.graph import GraphError
from chipleap.workload.llm import format_bits
from chipleap.workload.ops import Operand

# Parameter counts published on each model card, in billions.
PUBLISHED = {
    "smollm2-135m": 0.135,
    "smollm2-360m": 0.362,
    "qwen2.5-0.5b": 0.49,
    "qwen3-0.6b": 0.6,
    "gemma-3-1b": 1.0,
    "tinyllama-1.1b": 1.1,
    "llama-3.2-1b": 1.23,
    "qwen2.5-1.5b": 1.54,
    "qwen3-1.7b": 1.7,
    "smollm2-1.7b": 1.71,
    "llama-3.2-3b": 3.21,
    "phi-3-mini": 3.8,
}


@pytest.mark.parametrize("key", sorted(PRESETS))
def test_parameter_counts_match_model_cards(key):
    assert key in PUBLISHED
    assert PRESETS[key].params / 1e9 == pytest.approx(PUBLISHED[key], rel=0.02)


def test_decode_reads_every_weight_once():
    w = build_llm(LLMWorkloadSpec("llama-3.2-1b", weights="int8", kv="int8", context=1024))
    arch = PRESETS["llama-3.2-1b"]
    # Tied embeddings: the LM head reads the embedding table, so every parameter except the
    # norms is read once per token. Norm weights are tiny and live on chip.
    norms = arch.layers * 2 * arch.hidden + arch.hidden
    assert w.weight_bytes_read == pytest.approx(arch.params - norms, rel=1e-6)
    assert w.macs == pytest.approx(w.weight_bytes_read + _attention_macs(arch, 1024), rel=1e-6)


def _attention_macs(arch, ctx):
    return 2 * arch.layers * arch.heads * arch.head_dim * ctx


def test_gqa_groups_share_kv():
    w = build_llm(LLMWorkloadSpec("llama-3.2-1b", context=512))
    scores = next(op for op in w.matmuls if op.name == "attn.scores")
    assert scores.m == 4  # 32 query heads over 8 KV heads
    assert scores.count == 16 * 8
    assert scores.w_kind is Operand.KV


def test_kv_cache_size():
    a = PRESETS["llama-3.2-1b"]
    w = build_llm(LLMWorkloadSpec("llama-3.2-1b", context=1024, kv="int8"))
    assert w.storage.kv == 2 * a.layers * a.kv_dim * 1024


def test_sliding_window_caps_local_layers():
    short = build_llm(LLMWorkloadSpec("gemma-3-1b", context=256))
    long = build_llm(LLMWorkloadSpec("gemma-3-1b", context=8192))
    local = next(op for op in long.matmuls if op.name == "attn.local.scores")
    glob = next(op for op in long.matmuls if op.name == "attn.scores")
    assert local.n == 512 and glob.n == 8192
    assert local.count == (26 - 26 // 6) * 1
    assert long.storage.kv > short.storage.kv


def test_prefill_scales_with_prompt():
    a = build_llm(LLMWorkloadSpec("smollm2-135m", phase="prefill", prompt=64))
    b = build_llm(LLMWorkloadSpec("smollm2-135m", phase="prefill", prompt=128))
    assert a.tokens_per_step == 64
    assert b.macs > 1.9 * a.macs


def test_int4_group_scales():
    assert format_bits("int4", 64) == (4, 4.25)
    assert format_bits("int8") == (8, 8.0)
    with pytest.raises(ValueError):
        format_bits("fp16")


def test_bad_llm_spec():
    with pytest.raises(ValueError):
        LLMWorkloadSpec("llama-3.2-1b", phase="train")
    with pytest.raises(KeyError):
        build_llm(LLMWorkloadSpec("gpt-9"))


def test_graph_lowering():
    w = build_graph(
        {
            "name": "kws",
            "weights": "int8",
            "ops": [
                {
                    "conv2d": {
                        "name": "c1",
                        "h": 49,
                        "w": 10,
                        "cin": 1,
                        "cout": 64,
                        "kh": 10,
                        "kw": 4,
                        "stride": 2,
                        "act": True,
                    }
                },
                {"dwconv2d": {"name": "dw", "h": 25, "w": 5, "c": 64, "kh": 3}},
                {"matmul": {"name": "fc", "m": 1, "k": 64, "n": 12}},
                {"vector": {"name": "sm", "kind": "softmax", "elements": 12}},
            ],
        }
    )
    c1 = w.matmuls[0]
    assert (c1.m, c1.k, c1.n) == (math.ceil(49 / 2) * 5, 40, 64)
    dw = w.matmuls[1]
    assert (dw.m, dw.k, dw.n, dw.count) == (125, 9, 1, 64)
    assert w.params == 40 * 64 + 9 * 64 + 64 * 12
    assert w.unit == "inference"


@pytest.mark.parametrize(
    "ops",
    [
        [],
        [{"conv3d": {}}],
        [{"matmul": {"m": 0, "k": 1, "n": 1}}],
        [{"vector": {"kind": "nope", "elements": 3}}],
    ],
)
def test_graph_errors(ops):
    with pytest.raises(GraphError):
        build_graph({"ops": ops})
