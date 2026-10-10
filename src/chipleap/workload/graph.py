"""Custom workloads described as a list of operators.

Convolutions are lowered to matrix multiplies the way an im2col kernel would run them:
``conv2d`` becomes ``(out_h * out_w) x (kh * kw * cin) x cout`` and a depthwise
convolution becomes one ``(out_h * out_w) x (kh * kw) x 1`` multiply per channel, which
is why depthwise layers use wide arrays poorly.
"""

from __future__ import annotations

import math
from typing import Any

from chipleap.workload.llm import format_bits
from chipleap.workload.ops import MatMul, Op, Operand, Storage, VectorKind, VectorOp, Workload


class GraphError(ValueError):
    pass


def _act_kind(value: object, where: str) -> VectorKind:
    """``act: true`` or ``act: relu`` is a ReLU; ``act: silu`` or ``gelu`` a lookup table."""
    if value is True or value == "relu":
        return VectorKind.RELU
    if value in ("silu", "gelu", "sigmoid", "tanh"):
        return VectorKind.ACT
    raise GraphError(f"{where}: act must be true, relu, silu, gelu, sigmoid or tanh")


def _int(d: dict[str, Any], key: str, default: int | None = None) -> int:
    if key not in d:
        if default is None:
            raise GraphError(f"missing {key!r} in {d}")
        return default
    value = d[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GraphError(f"{key!r} must be a positive integer, got {value!r}")
    return value


def build_graph(cfg: dict[str, Any]) -> Workload:
    """Build a workload from the ``graph`` section of a spec file."""
    name = str(cfg.get("name", "graph"))
    unit = str(cfg.get("unit", "inference"))
    wfmt = str(cfg.get("weights", "int8"))
    _, wbits = format_bits(wfmt, int(cfg.get("group_size", 64)))
    raw_ops = cfg.get("ops")
    if not isinstance(raw_ops, list) or not raw_ops:
        raise GraphError("graph.ops must be a non-empty list")

    ops: list[Op] = []
    weight_bytes = 0.0
    peak_act = 0.0
    for i, entry in enumerate(raw_ops):
        if not isinstance(entry, dict) or len(entry) != 1:
            raise GraphError(f"graph.ops[{i}] must be a mapping with one key such as conv2d")
        kind, body = next(iter(entry.items()))
        if not isinstance(body, dict):
            raise GraphError(f"graph.ops[{i}].{kind} must be a mapping")
        label = str(body.get("name", f"{kind}{i}"))
        count = _int(body, "count", 1)
        bits = wbits if "weights" not in body else format_bits(str(body["weights"]))[1]

        if kind == "conv2d":
            h, w = _int(body, "h"), _int(body, "w")
            cin, cout = _int(body, "cin"), _int(body, "cout")
            kh, kw = _int(body, "kh"), _int(body, "kw", body.get("kh"))
            stride = _int(body, "stride", 1)
            oh, ow = math.ceil(h / stride), math.ceil(w / stride)
            mm = MatMul(
                label, m=oh * ow, k=kh * kw * cin, n=cout, count=count, w_bits=bits, group=label
            )
            ops.append(mm)
            weight_bytes += mm.w_bytes
            peak_act = max(peak_act, h * w * cin + oh * ow * cout)
            if body.get("act"):
                ops.append(
                    VectorOp(f"{label}.act", _act_kind(body["act"], label), oh * ow * cout, count)
                )
        elif kind == "dwconv2d":
            h, w, c = _int(body, "h"), _int(body, "w"), _int(body, "c")
            kh, kw = _int(body, "kh"), _int(body, "kw", body.get("kh"))
            stride = _int(body, "stride", 1)
            oh, ow = math.ceil(h / stride), math.ceil(w / stride)
            mm = MatMul(label, m=oh * ow, k=kh * kw, n=1, count=c * count, w_bits=bits, group=label)
            ops.append(mm)
            weight_bytes += mm.w_bytes
            peak_act = max(peak_act, h * w * c + oh * ow * c)
            if body.get("act"):
                ops.append(
                    VectorOp(f"{label}.act", _act_kind(body["act"], label), oh * ow * c, count)
                )
        elif kind == "matmul":
            source = str(body.get("w", "weights"))
            try:
                w_kind = Operand(source)
            except ValueError as exc:
                raise GraphError(f"{label}: w must be weights, kv or act") from exc
            mm = MatMul(
                label,
                m=_int(body, "m"),
                k=_int(body, "k"),
                n=_int(body, "n"),
                count=count,
                w_bits=bits,
                w_kind=w_kind,
                group=label,
            )
            ops.append(mm)
            if w_kind is Operand.WEIGHTS:
                weight_bytes += mm.w_bytes
            peak_act = max(peak_act, mm.m * (mm.k + mm.n))
            if body.get("act"):
                ops.append(
                    VectorOp(f"{label}.act", _act_kind(body["act"], label), mm.m * mm.n, count)
                )
        elif kind == "vector":
            try:
                vkind = VectorKind(str(body.get("kind", "add")))
            except ValueError as exc:
                kinds = ", ".join(k.value for k in VectorKind)
                raise GraphError(f"{label}: kind must be one of {kinds}") from exc
            ops.append(VectorOp(label, vkind, _int(body, "elements"), count))
        else:
            raise GraphError(
                f"graph.ops[{i}]: unknown operator {kind!r}; use conv2d, dwconv2d, matmul or vector"
            )

    storage = Storage(weights=weight_bytes, kv=0.0, activations=peak_act)
    return Workload(
        name=name,
        description=str(cfg.get("description", f"custom graph {name}")),
        ops=tuple(ops),
        storage=storage,
        tokens_per_step=1,
        unit=unit,
        params=int(
            sum(
                op.k * op.n * op.count
                for op in ops
                if isinstance(op, MatMul) and op.w_kind is Operand.WEIGHTS
            )
        ),
        meta=(("model", name), ("weights", wfmt)),
    )
