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

Technologies of your own, measured from a PDK's liberty files with ``chipleap tech add``,
live as YAML files in ``~/.config/chipleap/tech`` (``CHIPLEAP_TECH_DIR`` moves it; an
empty value turns them off) and load next to these. They stay on your machine.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# Logic of the generated accelerator in kGE (thousands of NAND2 equivalents), fitted to
# Yosys and ABC on SG13G2 across 24 configurations: all three dataflows, one to eight
# ports, 4 to 64 MACs, mapped and sized for the 5 ns target of verify/synth.py, with the
# buffers extrapolated away. The leave-one-out error is 2.8% on average and 7.1% at worst.
# (Fitted to 12 configurations, the eight terms followed Yosys wherever an unrelated change
# to the RTL moved it: the core's chipleap_accel wrapper moved some designs by 3 to 5 %,
# with the logic unchanged, and the error to 5.0% and 9.8%.)
# Gate counts are roughly node independent, so the generic nodes reuse them.
# `scripts/calibrate_sg13g2.py` measures them again.
ACCEL_BASE_KGE = 24.93  # register file, job and tile control, store engine, cycle counter
ACCEL_PORT_KGE = 2.79  # one OBI read port of the DMA, with its own address generator
# Per dataflow: kGE per MAC (8 x 8 multiplier, adder or pipeline registers) and per edge
# unit: a GEMV lane's accumulators, a weight-stationary column's accumulators, or the
# operand skew registers on each row and column of an output-stationary array.
ACCEL_KGE: dict[str, tuple[float, float]] = {
    "gemv": (0.899, 3.511),
    "ws": (1.737, 2.607),
    "os": (1.872, 0.148),
}
# A macro-backed GEMV build (chipleap.gen.banks, SRAM_BUF) has no flop buffers but logic of
# its own: the macros' write path (word, mask and bank select) and the registers that read
# each chunk a cycle ahead, and the chunk select from the bank words. Fitted to 12 such
# builds (one to eight lanes, 4 to 16 columns, single and double buffered), each core
# against its flop-buffer build: a + b x MACs, + c with a double buffer, within 0.67 kGE
# of every build left out (0.39 on average).
ACCEL_BANK_KGE = (5.52, 0.104, 1.03)


# The AXI4 wrapper of the standalone platform: the AXI4-Lite slave's response registers,
# the write master's handshake flags and the address multiplexer. Synthesized on SG13G2
# with the core as a black box, 0.54 kGE for 1 to 16 ports (the read ports are wiring).
AXI_ADAPTER_KGE = 0.54


# Critical path of the generated accelerator on SG13G2 at the typical corner, sized for the
# 5 ns target of verify/synth.py (scripts/calibrate_sg13g2.py): a delay per dataflow, more
# for a GEMV adder tree wider than eight columns, and the DMA's setup of its ports' start
# addresses, which grows slowly with the number of ports. Set to bound every one of the 24
# synthesized configurations from above (3.3 to 4.9 ns), and for the ports the DMA paths of
# configurations with 4 to 16 ports (3.3 to 4.1 ns), so the clock limit below errs on the
# safe side.
ACCEL_DELAY_NS: dict[str, float] = {"gemv": 4.35, "ws": 3.77, "os": 3.53}
ACCEL_DELAY_WIDE_NS = 0.51  # per doubling of GEMV columns beyond eight
ACCEL_DELAY_PORTS_NS = (3.23, 0.052)  # the port logic: a + b x ports
FLOP_OVERHEAD_NS = 0.45  # clock-to-Q plus setup of sg13g2_dfrbpq_1, typical corner
# After placement and routing with LibreLane at the slow corner (1.08 V, 125 C), every
# design at 100 MHz with 16-word buffers:
# - The single-port GEMV 1 x 8, weight-stationary 4 x 4 and output-stationary 4 x 4 designs
#   met 100 MHz at 9.0 to 9.3 ns. Their worst paths were the GEMV engine's row counter, the
#   weight-stationary accumulator update, and a port's address step and the output-stationary
#   operand feed. ACCEL_ROUTED_NS is the worst of them.
# - A synthesized datapath longer than the GEMV 1 x 8 engine's (a wider adder tree) is scaled
#   from that period in proportion: ACCEL_SIGNOFF_FACTOR is ACCEL_ROUTED_NS over the GEMV
#   delay above plus flop overhead.
# - A GEMV engine slows with its lanes, which make it larger: the 2 x 8 engine with four
#   ports took 9.63 ns (weight unpacking), and the 4 x 8 one with two 9.69 ns (the dot
#   product) and, routed again from the same logic, 10.23 ns (the accumulator row select
#   into the accumulators): routing alone moved it by 0.54 ns. ACCEL_LANE_NS per doubling
#   of lanes bounds them from above.
# - More ports make the engine larger and its wires longer: the GEMV 1 x 8 engine took
#   9.11 ns with one port, 10.09 ns with eight (weight unpacking, then the DMA's setup of
#   each port's start address at 9.74 ns) and 10.41 ns with sixteen (at 35 % core
#   utilisation; the activation read address into an operand register). ACCEL_PORT_NS is
#   the line a + b x ports through one and eight ports; sixteen lie 0.8 ns under it. Each
#   port has an address generator of its own, so the DMA no longer hands words to the ports
#   one after another; with that chain the GEMV 2 x 8 engine with four ports took 14.02 ns,
#   and now 9.63 ns.
# - The core reads its tile descriptor from a register loaded a cycle ahead, not through a
#   select by the buffer slot. With the select, the slot's net and its buffer tree led into
#   the GEMV engine's operand registers and accumulators, and set the period of the 4 x 8
#   engine with two ports (10.27 ns) and the 1 x 8 with eight (11.18 ns) and sixteen
#   (11.25 ns).
# The model is at or above every routed period, so it errs on the safe side.
# docs/verification.md lists the routed designs.
ACCEL_ROUTED_NS = 9.33
ACCEL_SIGNOFF_FACTOR = ACCEL_ROUTED_NS / (ACCEL_DELAY_NS["gemv"] + FLOP_OVERHEAD_NS)
ACCEL_LANE_NS = 0.46
ACCEL_PORT_NS = (8.966, 0.1413)


def accel_delay_ns(dataflow: str, cols: int, ports: int) -> float:
    """The accelerator's synthesized critical path on SG13G2, typical corner."""
    d = ACCEL_DELAY_NS[dataflow]
    if dataflow == "gemv" and cols > 8:
        d += ACCEL_DELAY_WIDE_NS * math.log2(cols / 8)
    a, b = ACCEL_DELAY_PORTS_NS
    return max(d, a + b * ports)


def accel_signoff_mhz(dataflow: str, cols: int, ports: int, rows: int = 1) -> float:
    """The clock the routed accelerator meets on SG13G2 at the slow corner."""
    datapath = (accel_delay_ns(dataflow, cols, ports) + FLOP_OVERHEAD_NS) * ACCEL_SIGNOFF_FACTOR
    datapath = max(ACCEL_ROUTED_NS, datapath)
    if dataflow == "gemv" and rows > 1:
        datapath += ACCEL_LANE_NS * math.log2(rows)
    a, b = ACCEL_PORT_NS
    return 1000.0 / max(datapath, a + b * ports)


# X-HEEP's SoC on SG13G2 (``chipleap verify --steps soc``) does in one cycle what the
# accelerator alone never does, across a die of millimetres:
# - it reads a bank: the bank's macro puts the word out a clock-to-output after the clock
#   edge, and the crossbar takes it to the CPU's load path or the accelerator's buffer;
# - its bus grants a master in the cycle it asks: the CPU's request leaves its decoder and
#   load-store unit, passes the crossbar's arbiter, and the grant it leaves another master
#   (the accelerator's write port) ends in that master's registers.
# Those paths set the SoC's clock, whatever the accelerator meets on its own. They are
# measured after detailed routing and extraction, at the slow corner with the 5 % derate,
# on the SmolLM2 SoC (cv32e20, one-to-many bus, one 32 KiB bank of eight 1024 x 32 macros)
# laid out at SOC_EVIDENCE_MHZ (docs/model.md, "The SoC's clock"): the period the slowest
# path from a bank needs, without its macro's clock to output (SOC_READ_PATH_NS), and the
# period the slowest other path needs (SOC_BUS_PATH_NS). The model takes the clock to
# output of the macro that holds each of the design's banks (SOC_BANK_CLK_TO_Q_NS: the
# 1024 x 32 for banks of 4 KiB and more, the 256 x 32 and 512 x 32 for 1 and 2 KiB).
# The estimate after global routing is no basis: extraction after routing put 14 to 20 %
# more capacitance on these wires, and at 50 MHz the slow corner went from +0.54 ns after
# global routing to -3.63 ns after routing.
SOC_EVIDENCE_MHZ = 40.0
SOC_BANK_CLK_TO_Q_NS = {
    "RM_IHPSG13_1P_256x32_c2_bm_bist": 5.263,
    "RM_IHPSG13_1P_512x32_c2_bm_bist": 6.519,
    "RM_IHPSG13_1P_1024x32_c2_bm_bist": 7.385,
}
SOC_READ_PATH_NS = 21.50 - SOC_BANK_CLK_TO_Q_NS["RM_IHPSG13_1P_1024x32_c2_bm_bist"]
SOC_BUS_PATH_NS = 23.63


def soc_signoff_mhz(bank_cells: Iterable[str]) -> float:
    """The clock X-HEEP's SoC meets on SG13G2 at the slow corner, with its banks in the
    macros ``bank_cells``: the slower of a bank's read (the slowest macro's clock to output
    and the measured rest, ``SOC_READ_PATH_NS``) and the bus's other paths
    (``SOC_BUS_PATH_NS``)."""
    cq = max(SOC_BANK_CLK_TO_Q_NS[c] for c in bank_cells)
    return 1000.0 / max(cq + SOC_READ_PATH_NS, SOC_BUS_PATH_NS)


def accel_logic_kge(dataflow: str, rows: int, cols: int, ports: int) -> float:
    """Logic of the generated accelerator, without its activation and weight buffers.

    The PEs multiply 8 x 8 whatever the weight format, so weight width does not enter.
    """
    per_mac, per_edge = ACCEL_KGE[dataflow]
    edges = {"gemv": rows, "ws": cols, "os": rows + cols}[dataflow]
    return ACCEL_BASE_KGE + ACCEL_PORT_KGE * ports + per_mac * rows * cols + per_edge * edges


def accel_bank_kge(rows: int, cols: int, double_buffer: bool) -> float:
    """What a macro-backed GEMV build's logic adds to ``accel_logic_kge``: the interface to
    its SRAM banks."""
    a, per_mac, double = ACCEL_BANK_KGE
    return a + per_mac * rows * cols + (double if double_buffer else 0.0)


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
    # The accelerator's clock is capped by its measured slow-corner timing (accel_signoff_mhz),
    # not only by max_freq_mhz. True for the node the timing model was measured on.
    accel_timing: bool = False
    # X-HEEP's SoC clock is capped by its measured slow-corner timing (soc_signoff_mhz).
    soc_timing: bool = False
    note: str = ""
    # Files the synthesis and layout backends use, for technologies added with
    # ``chipleap tech add``. Paths are on ``host`` when it is set (a design server), so a
    # foundry library never has to leave the machine it is licensed on.
    liberty: str = ""
    liberty_slow: str = ""  # slow corner, for place-and-route timing at both corners
    db: str = ""  # compiled liberty for Design Compiler
    lef: tuple[str, ...] = ()  # technology LEF first, then the cell LEFs
    host: str = ""
    user: bool = False  # added by the user, not shipped with ChipLeap

    def clock_limit_mhz(
        self, dataflow: str = "", cols: int = 0, ports: int = 0, rows: int = 1
    ) -> float:
        """The fastest clock a design on this node may use: the node's limit, and with an
        accelerator on a node with measured timing, what that accelerator meets after
        routing at the slow corner."""
        if self.accel_timing and dataflow:
            return min(self.max_freq_mhz, accel_signoff_mhz(dataflow, cols, ports, rows))
        return self.max_freq_mhz

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
            accel_timing=True,
            soc_timing=True,
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


BUILTIN = tuple(TECHNOLOGIES)


def user_tech_dir() -> Path | None:
    """Where technologies added with ``chipleap tech add`` live, None when turned off."""
    env = os.environ.get("CHIPLEAP_TECH_DIR")
    if env is not None:
        return Path(env).expanduser() if env else None
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "chipleap" / "tech"


def tech_from_dict(data: dict[str, Any]) -> Technology:
    names = {f.name for f in fields(Technology)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"unknown technology fields: {', '.join(sorted(unknown))}")
    values = dict(data)
    values["lef"] = tuple(values.get("lef") or ())
    values["user"] = True
    return Technology(**values)


def tech_to_dict(t: Technology) -> dict[str, Any]:
    out = {f.name: getattr(t, f.name) for f in fields(Technology) if f.name != "user"}
    out["lef"] = list(t.lef)
    return out


def load_user_techs(directory: Path | None = None) -> dict[str, Technology]:
    """Technologies from YAML files; a file that does not load is reported and skipped."""
    import yaml

    d = directory if directory is not None else user_tech_dir()
    found: dict[str, Technology] = {}
    if d is None or not d.is_dir():
        return found
    for path in sorted(d.glob("*.yaml")):
        try:
            t = tech_from_dict(yaml.safe_load(path.read_text()) or {})
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            print(f"chipleap: skipping technology {path}: {exc}", file=sys.stderr)
            continue
        if t.key in BUILTIN:
            print(f"chipleap: skipping {path}: {t.key!r} is built in", file=sys.stderr)
            continue
        found[t.key] = t
    return found


TECHNOLOGIES.update(load_user_techs())


def get_tech(key: str) -> Technology:
    try:
        return TECHNOLOGIES[key]
    except KeyError:
        raise KeyError(f"unknown technology {key!r}. Known: {', '.join(TECHNOLOGIES)}") from None
