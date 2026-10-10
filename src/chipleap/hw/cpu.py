"""The RISC-V cores X-HEEP can instantiate, as seen by the evaluator.

Cycle counts are for hand-written int8 kernels: a GEMV inner loop for ``cycles_per_mac``
and fixed-point element-wise kernels for ``BASE_VECTOR_CYCLES``. The CV32E40P and CV32E40PX
figures assume the Xpulp extensions are enabled (``corev_pulp``), which give 4-way int8
dot products, post-increment loads and hardware loops. Areas are approximate public
figures for each core in its X-HEEP configuration; docs/model.md explains how to replace
them with numbers from your own synthesis.
"""

from __future__ import annotations

from dataclasses import dataclass

from chipleap.workload.ops import VectorKind

# Cycles per element on a plain RV32IMC core (CV32E40X), before the per-core factor.
BASE_VECTOR_CYCLES: dict[VectorKind, float] = {
    VectorKind.NORM: 6.0,
    VectorKind.SOFTMAX: 18.0,
    VectorKind.ACT: 10.0,
    VectorKind.RELU: 2.5,
    VectorKind.ROPE: 8.0,
    VectorKind.ADD: 3.0,
    VectorKind.REQUANT: 4.0,
    VectorKind.POOL: 2.0,
    VectorKind.COPY: 0.5,  # moved by the DMA, the CPU only programs it
}


@dataclass(frozen=True)
class CPUModel:
    key: str
    name: str
    kge: float
    cycles_per_mac: dict[int, float]  # keyed by weight bits
    vector_factor: float
    xheep_expr: str  # Python expression for the X-HEEP config generator
    has_xif: bool = False
    isa: str = "rv32imc"
    note: str = ""

    def mac_cycles(self, w_bits: int) -> float:
        if w_bits in self.cycles_per_mac:
            return self.cycles_per_mac[w_bits]
        return self.cycles_per_mac[8] * (1.4 if w_bits < 8 else 1.0)

    def element_cycles(self, kind: VectorKind) -> float:
        return BASE_VECTOR_CYCLES[kind] * (1.0 if kind is VectorKind.COPY else self.vector_factor)


CPUS: dict[str, CPUModel] = {
    c.key: c
    for c in (
        CPUModel(
            "cv32e20",
            "CV32E20 (2-stage, RV32IMC)",
            kge=25.0,
            cycles_per_mac={8: 6.0, 4: 8.0, 2: 9.0},
            vector_factor=1.6,
            xheep_expr='cv32e20(rv32e=False, rv32m="RV32MFast")',
            note="Smallest core. Slow multiply and no SIMD.",
        ),
        CPUModel(
            "cv32e40x",
            "CV32E40X (4-stage, RV32IMC)",
            kge=45.0,
            cycles_per_mac={8: 3.5, 4: 5.0, 2: 6.0},
            vector_factor=1.0,
            xheep_expr="cv32e40x()",
            has_xif=True,
            note="Single-cycle multiply, CV-X-IF for coprocessors.",
        ),
        CPUModel(
            "cv32e40p",
            "CV32E40P with Xpulp",
            kge=65.0,
            cycles_per_mac={8: 0.6, 4: 1.0, 2: 1.2},
            vector_factor=0.6,
            xheep_expr="cv32e40p(corev_pulp=True)",
            isa="rv32imc_xcv",
            note="4-way int8 dot product, hardware loops, post-increment loads.",
        ),
        CPUModel(
            "cv32e40px",
            "CV32E40PX with Xpulp and CV-X-IF",
            kge=70.0,
            cycles_per_mac={8: 0.6, 4: 1.0, 2: 1.2},
            vector_factor=0.6,
            xheep_expr="cv32e40px(corev_pulp=True)",
            has_xif=True,
            isa="rv32imc_xcv",
            note="CV32E40P plus the coprocessor interface.",
        ),
    )
}


def get_cpu(key: str) -> CPUModel:
    try:
        return CPUS[key]
    except KeyError:
        raise KeyError(f"unknown CPU {key!r}. Known: {', '.join(CPUS)}") from None
