"""Process technology presets.

``ihp-sg13g2`` is measured. Gate, flop and SRAM macro areas and SRAM access energies come
from the open IHP SG13G2 liberty files (typical corner, 1.2 V, 25 C), and MAC areas come
from Yosys synthesis against the same library. ``scripts/calibrate_sg13g2.py`` reproduces
every number. The SRAM fits are close: a macro is 0.0362 mm2 of fixed periphery plus
0.0285 mm2 per KiB (within 1 % for every IHP macro from 512 B to 16 KiB, and for the
32 KiB 8192x32 macro), and read energy per byte grows as size to the power 0.4.

The ``generic-*`` presets are scaled estimates for commercial nodes. Logic and MAC
energies follow Horowitz, "Computing's energy problem" (ISSCC 2014, 45 nm, 0.9 V), scaled
linearly with feature size and with supply squared; SRAM arrays use typical 6T bit-cell
sizes. docs/model.md lists each number with its origin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Logic of the generated accelerator in kGE (thousands of NAND2 equivalents), fitted to
# Yosys and ABC (mapped, buffered and sized) on SG13G2 across 11 configurations: all three
# dataflows, one to four ports, 8 to 64 MACs, with the buffers extrapolated away. The
# leave-one-out error is 3.8% on average and 10% at worst.
# Gate counts are roughly node independent, so the generic nodes reuse them.
# `scripts/calibrate_sg13g2.py --accel` measures them again.
ACCEL_BASE_KGE = 28.83  # register file, job and tile control, store engine, cycle counter
ACCEL_PORT_KGE = 2.18  # one OBI read port of the DMA
# Per dataflow: kGE per MAC (8 x 8 multiplier, adder or pipeline registers) and per edge
# unit: a GEMV lane's accumulators, a weight-stationary column's accumulators, or the
# operand skew registers on each row and column of an output-stationary array.
ACCEL_KGE: dict[str, tuple[float, float]] = {
    "gemv": (1.210, 3.634),
    "ws": (2.048, 0.874),
    "os": (1.669, 0.982),
}


# The AXI4 wrapper of the standalone platform: the AXI4-Lite slave's response registers,
# the write master's handshake flags and the address multiplexer. Synthesized on SG13G2
# with the core as a black box, 0.54 kGE for 1 to 16 ports (the read ports are wiring).
AXI_ADAPTER_KGE = 0.54


def accel_logic_kge(dataflow: str, rows: int, cols: int, ports: int) -> float:
    """Logic of the generated accelerator, without its activation and weight buffers.

    The PEs multiply 8 x 8 whatever the weight format, so weight width does not enter.
    """
    per_mac, per_edge = ACCEL_KGE[dataflow]
    edges = {"gemv": rows, "ws": cols, "os": rows + cols}[dataflow]
    return ACCEL_BASE_KGE + ACCEL_PORT_KGE * ports + per_mac * rows * cols + per_edge * edges


def _horowitz(node_nm: float, vdd: float, value_at_45nm_09v: float) -> float:
    """Scale a 45 nm, 0.9 V energy to another node: linear in size, quadratic in supply."""
    return value_at_45nm_09v * (node_nm / 45) * (vdd / 0.9) ** 2


@dataclass(frozen=True)
class Technology:
    key: str
    name: str
    node_nm: int
    vdd: float
    ge_um2: float  # NAND2 area
    logic_util: float  # standard-cell placement utilisation
    sram_mm2_per_kib: float  # bit-cell array and its local periphery
    sram_macro_mm2: float  # fixed periphery per macro
    sram_macro_max_kib: int  # largest macro; bigger memories are built from several
    sram_pj_per_byte: float  # read energy per byte of a 16 KiB macro at full width
    mac_pj_8x8: float  # one int8 x int8 MAC including the accumulate
    cpu_pj_per_kge_cycle: float  # dynamic energy of CPU logic per kGE per active cycle
    leak_mw_per_mm2: float
    max_freq_mhz: float  # practical clock for a small RISC-V SoC with an accelerator
    max_io_mts: int  # fastest external memory interface practical at this node
    measured: bool = False
    note: str = ""

    def logic_mm2(self, kge: float) -> float:
        return kge * 1000 * self.ge_um2 / self.logic_util / 1e6

    def sram_mm2(self, kib: float) -> float:
        """Area of a memory of ``kib`` KiB built from the largest macros available."""
        if kib <= 0:
            return 0.0
        macros = math.ceil(kib / self.sram_macro_max_kib)
        return macros * self.sram_macro_mm2 + kib * self.sram_mm2_per_kib

    def sram_energy_per_byte(self, macro_kib: float) -> float:
        """Read energy per byte of a wide (64-bit) access, as accelerator buffers use."""
        size = min(max(0.5, macro_kib), self.sram_macro_max_kib)
        return float(self.sram_pj_per_byte * (size / 16) ** 0.4)

    def bank_energy_per_byte(self, bank_kib: float) -> float:
        """Energy per byte of a 32-bit access to an X-HEEP bank. Narrow macros spread each
        access's periphery energy over fewer bits, and large ones more so: on SG13G2 a 32-bit
        read of the 32 KiB macro costs 230 pJ, 58 pJ per byte. Fitted to IHP's 32-bit macros
        (1, 2 and 32 KiB, within 10 %) and scaled to other nodes with the wide figure."""
        size = min(max(0.5, bank_kib), self.sram_macro_max_kib)
        return float(self.sram_pj_per_byte * (6.5 / 15.3) * size**0.6)

    def mac_pj(self, a_bits: int, w_bits: int) -> float:
        # Multiplier energy scales with the product of operand widths and the accumulate
        # with their sum; at int8 x int8 the split is 75 to 25.
        mult = 0.75 * (a_bits * w_bits) / 64
        acc = 0.25 * (a_bits + w_bits + 8) / 24
        return self.mac_pj_8x8 * (mult + acc)


# 45 nm, 0.9 V reference values (Horowitz 2014): an 8-bit multiply is 0.2 pJ and a
# 32-bit add 0.1 pJ, so an int8 MAC with a 24-bit accumulate is about 0.28 pJ. A 16 KiB
# SRAM read of 64 bits is about 14 pJ (between the 8 KB and 32 KB figures).
_MAC_45 = 0.28
_SRAM16K_BYTE_45 = 14.1 / 8
_CPU_KGE_45 = 0.5


TECHNOLOGIES: dict[str, Technology] = {
    t.key: t
    for t in (
        Technology(
            key="ihp-sg13g2",
            name="IHP SG13G2 130 nm (open PDK)",
            node_nm=130,
            vdd=1.2,
            ge_um2=7.2576,
            logic_util=0.7,
            sram_mm2_per_kib=0.02846,
            sram_macro_mm2=0.0362,
            sram_macro_max_kib=32,  # RM_IHPSG13_1P_8192x32, 1520 um x 618 um
            sram_pj_per_byte=122.4 / 8,
            mac_pj_8x8=_horowitz(130, 1.2, _MAC_45),
            cpu_pj_per_kge_cycle=_horowitz(130, 1.2, _CPU_KGE_45),
            leak_mw_per_mm2=0.016,
            max_freq_mhz=100,
            max_io_mts=155,
            measured=True,
            note=(
                "Gate, flop and SRAM areas and SRAM energies from the IHP SG13G2 liberty "
                "files; MAC areas from Yosys synthesis; logic energy scaled from Horowitz 2014."
            ),
        ),
        Technology(
            key="generic-65nm",
            name="Generic 65 nm low power",
            node_nm=65,
            vdd=1.0,
            ge_um2=1.44,
            logic_util=0.7,
            sram_mm2_per_kib=0.0061,
            sram_macro_mm2=0.0362 * (65 / 130) ** 2,
            sram_macro_max_kib=32,
            sram_pj_per_byte=_horowitz(65, 1.0, _SRAM16K_BYTE_45),
            mac_pj_8x8=_horowitz(65, 1.0, _MAC_45),
            cpu_pj_per_kge_cycle=_horowitz(65, 1.0, _CPU_KGE_45),
            leak_mw_per_mm2=0.04,
            max_freq_mhz=300,
            max_io_mts=800,
            note="Scaled estimate. LPDDR2-class PHYs are the fastest practical interface.",
        ),
        Technology(
            key="generic-28nm",
            name="Generic 28 nm",
            node_nm=28,
            vdd=0.9,
            ge_um2=0.49,
            logic_util=0.7,
            sram_mm2_per_kib=0.0015,
            sram_macro_mm2=0.0362 * (28 / 130) ** 2,
            sram_macro_max_kib=64,
            sram_pj_per_byte=_horowitz(28, 0.9, _SRAM16K_BYTE_45),
            mac_pj_8x8=_horowitz(28, 0.9, _MAC_45),
            cpu_pj_per_kge_cycle=_horowitz(28, 0.9, _CPU_KGE_45),
            leak_mw_per_mm2=0.25,
            max_freq_mhz=800,
            max_io_mts=4266,
            note="Scaled estimate. LPDDR4 and LPDDR4X PHYs are available at this node.",
        ),
        Technology(
            key="generic-16nm",
            name="Generic 16 nm FinFET",
            node_nm=16,
            vdd=0.8,
            ge_um2=0.20,
            logic_util=0.7,
            sram_mm2_per_kib=0.00082,
            sram_macro_mm2=0.0362 * (16 / 130) ** 2,
            sram_macro_max_kib=64,
            sram_pj_per_byte=_horowitz(16, 0.8, _SRAM16K_BYTE_45),
            mac_pj_8x8=_horowitz(16, 0.8, _MAC_45),
            cpu_pj_per_kge_cycle=_horowitz(16, 0.8, _CPU_KGE_45),
            leak_mw_per_mm2=0.6,
            max_freq_mhz=1200,
            max_io_mts=6400,
            note="Scaled estimate. LPDDR5 PHYs are available at this node.",
        ),
    )
}


def get_tech(key: str) -> Technology:
    try:
        return TECHNOLOGIES[key]
    except KeyError:
        raise KeyError(f"unknown technology {key!r}. Known: {', '.join(TECHNOLOGIES)}") from None
