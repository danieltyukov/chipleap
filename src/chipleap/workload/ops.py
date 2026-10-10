"""Operator-level description of a workload.

A workload is a flat list of operators executed once per *step* (one decoded token for
LLM decode, one prompt for prefill, one inference for a CNN). Identical operators that
repeat across layers or heads are folded into one entry with a ``count``, which keeps
evaluation fast enough to sweep thousands of designs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Operand(str, Enum):
    """Where the second (B) operand of a matrix multiply lives."""

    WEIGHTS = "weights"  # static parameters, stored once
    KV = "kv"  # attention KV cache, grows with context
    ACT = "act"  # produced at run time and consumed on chip


class VectorKind(str, Enum):
    """Element-wise and reduction operators that run on the CPU."""

    NORM = "norm"  # RMSNorm or LayerNorm
    SOFTMAX = "softmax"
    ACT = "act"  # SiLU, GELU and the gating multiply (lookup table and multiply)
    RELU = "relu"  # ReLU or clip, one compare per element
    ROPE = "rope"
    ADD = "add"  # residual add
    REQUANT = "requant"  # int32 accumulators back to int8
    POOL = "pool"
    COPY = "copy"  # gather, KV append, layout moves


@dataclass(frozen=True)
class MatMul:
    """``Y[m, n] = sum_k X[m, k] * W[n, k]``, executed ``count`` times per step.

    Each of the ``count`` instances has its own W operand (a different layer's weights,
    a different head's KV cache), so storage and traffic both scale with ``count``.
    ``w_bits`` is the effective storage cost per element, including quantisation scales
    (int4 with a 16-bit scale per group of 64 is 4.25 bits).
    """

    name: str
    m: int
    k: int
    n: int
    count: int = 1
    x_bits: int = 8
    w_bits: float = 8.0
    w_kind: Operand = Operand.WEIGHTS
    group: str = ""

    def __post_init__(self) -> None:
        for field in ("m", "k", "n", "count"):
            if getattr(self, field) < 1:
                raise ValueError(f"{self.name}: {field} must be at least 1")

    @property
    def macs(self) -> int:
        return self.m * self.k * self.n * self.count

    @property
    def w_bytes(self) -> float:
        """Bytes of the W operand across all instances."""
        return self.k * self.n * self.w_bits / 8 * self.count

    @property
    def x_bytes(self) -> float:
        return self.m * self.k * self.x_bits / 8 * self.count

    @property
    def y_elements(self) -> int:
        return self.m * self.n * self.count

    @property
    def intensity(self) -> float:
        """MACs per byte of W operand: the reuse a dataflow can exploit."""
        return self.macs / self.w_bytes


@dataclass(frozen=True)
class VectorOp:
    """An element-wise or reduction operator, run on the CPU.

    ``elements`` is the number of elements processed per instance. ``kv_bytes`` is
    KV-cache traffic the operator causes (appending this step's keys and values); the
    evaluator charges it to wherever the KV cache is placed.
    """

    name: str
    kind: VectorKind
    elements: int
    count: int = 1
    bits: int = 8
    kv_bytes: float = 0.0

    @property
    def total_elements(self) -> int:
        return self.elements * self.count


Op = MatMul | VectorOp


@dataclass(frozen=True)
class Storage:
    """Bytes that must be resident somewhere for the workload to run."""

    weights: float  # every static parameter, including embedding tables
    kv: float = 0.0  # KV cache at the evaluated context length
    activations: float = 0.0  # peak live activations (scratch)

    @property
    def total(self) -> float:
        return self.weights + self.kv + self.activations


@dataclass(frozen=True)
class Workload:
    """Everything the evaluator needs to know about one step of a workload."""

    name: str
    description: str
    ops: tuple[Op, ...]
    storage: Storage
    tokens_per_step: int = 1
    unit: str = "token"
    params: int = 0
    meta: tuple[tuple[str, str], ...] = ()

    @property
    def matmuls(self) -> list[MatMul]:
        return [op for op in self.ops if isinstance(op, MatMul)]

    @property
    def vector_ops(self) -> list[VectorOp]:
        return [op for op in self.ops if isinstance(op, VectorOp)]

    @property
    def macs(self) -> int:
        return sum(op.macs for op in self.matmuls)

    @property
    def weight_bytes_read(self) -> float:
        """W-operand bytes touched per step, split later into on-chip and external."""
        return sum(op.w_bytes for op in self.matmuls if op.w_kind is Operand.WEIGHTS)

    @property
    def kv_bytes_read(self) -> float:
        return sum(op.w_bytes for op in self.matmuls if op.w_kind is Operand.KV)

    def info(self) -> dict[str, str]:
        return dict(self.meta)
