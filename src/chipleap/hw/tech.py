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
# After placement and routing with LibreLane 3.0.14 at the slow corner (1.08 V, 125 C), every
# design at 100 MHz with 16-word buffers in flip-flops:
# - The single-port GEMV 1 x 8, weight-stationary 4 x 4 and output-stationary 4 x 4 designs
#   met 100 MHz at 9.30, 8.91 and 8.55 ns (the output-stationary 4 x 8 at 8.33 ns). The
#   GEMV engine's worst paths are its reset input into the flip-flops' recovery checks; its
#   worst path between flip-flops took 9.03 ns. ACCEL_ROUTED_NS is the worst of the three.
# - A GEMV adder tree wider than eight columns, whose synthesized datapath is longer than the
#   1 x 8 engine's, routes in proportion to that datapath and its flop overhead: the 1 x 16
#   engine took 10.20 ns, 1.920 times it, and the 1 x 32 11.53 ns, 1.981 times.
#   ACCEL_SIGNOFF_FACTOR is the larger. It used to be ACCEL_ROUTED_NS over the GEMV 1 x 8's
#   datapath; on 3.0.14 that ratio (1.938) would put the 1 x 32 at 88.7 MHz, where it met
#   86.7.
# - A GEMV engine slows with its lanes, which make it larger: the 2 x 8 engine with four
#   ports took 9.60 ns, under the port line below, and the 4 x 8 with two 9.96 ns.
#   ACCEL_LANE_NS per doubling of lanes bounds the 4 x 8 from above.
# - More ports make the engine larger and its wires longer. The GEMV 1 x 8 engine took
#   9.30 ns with one port, 10.82 ns with eight (between flip-flops, through 6.4 ns of
#   repeaters across the engine and a 0.65 ns hold buffer) and 11.05 ns with sixteen (a read
#   port's data input into the engine through weak buffers, placed at 35 % core utilisation).
#   ACCEL_PORT_NS is the line a + b x ports through one and eight ports; sixteen lie 1.5 ns
#   under it. On 3.0.0.dev44 the engine took 9.11, 10.09 and 10.41 ns, sixteen 0.8 ns under
#   that flow's line. Each port has an address generator of its own, so the DMA no longer
#   hands words to the ports one after another; with that chain the GEMV 2 x 8 engine with
#   four ports took 14.02 ns on 3.0.0.dev44.
# - The core reads its tile descriptor from a register loaded a cycle ahead, not through a
#   select by the buffer slot. With the select, the slot's net and its buffer tree led into
#   the GEMV engine's operand registers and accumulators, and set the period of the 4 x 8
#   engine with two ports (10.27 ns) and the 1 x 8 with eight (11.18 ns) and sixteen
#   (11.25 ns) on 3.0.0.dev44.
# The model is at or above every routed period, so it errs on the safe side.
# docs/verification.md lists the routed designs.
ACCEL_ROUTED_NS = 9.303
ACCEL_SIGNOFF_FACTOR = 1.981
ACCEL_LANE_NS = 0.329
ACCEL_PORT_NS = (9.087, 0.2169)
# A GEMV engine whose buffers are IHP's SRAM macros (chipleap.gen.banks, routed with
# LibreLane 3.0.14 at the slow corner, 100 MHz):
# - It registers each bank word in the cycle after its read, so the macro's read time
#   (chipleap.macro.sram.IHP_1P_READ_NS: 5.0 to 5.3 ns at 256 words, 7.0 to 7.3 ns at 1024, 8.8
#   to 9.4 ns at 2048 and more) bounds its period, with what follows the read: the word's
#   select and weight unpacking into the operand registers, and the wires from the macro
#   rows. ACCEL_BANK_PATH_NS is the most that took after the liberty's read time in a
#   one-lane build whose worst path is the read: 3.80 ns in the GEMV 1 x 4 with 256 x 32
#   macros, 3.95 ns in the 1 x 8 with 1024 x 64 and 4.03 ns in the 1 x 8 with 2048 x 64,
#   whose read path (8.99 + 4.03 ns) set its clock at 76.8 MHz. On 3.0.0.dev44 it took 2.36
#   to 2.64 ns. 3.0.14 applies the PDK's 5 % derate, which dev44 did not, the macro's read
#   included: in the layouts the read alone took 5.46 ns at 256 words where the liberty has
#   5.15, and 9.59 ns at 2048 where it has 8.99.
# - Its logic is spread along the rows of macros, across a die several times the flop
#   build's, and can route slower than the flop build: the 1 x 8 with 256 x 64 macros at
#   9.85 ns, where the model has 9.30 ns for its flop build, and the 2 x 8 with six
#   256 x 64 macros at 10.63 ns, on its macros' read (5.48 ns after it, its six macros and
#   two lanes spreading further than the one-lane builds), where the model has 9.63 ns.
#   ACCEL_BANK_NS is added to the flop build's period and bounds both.
ACCEL_BANK_PATH_NS = 4.03
ACCEL_BANK_NS = 0.998


def accel_delay_ns(dataflow: str, cols: int, ports: int) -> float:
    """The accelerator's synthesized critical path on SG13G2, typical corner."""
    d = ACCEL_DELAY_NS[dataflow]
    if dataflow == "gemv" and cols > 8:
        d += ACCEL_DELAY_WIDE_NS * math.log2(cols / 8)
    a, b = ACCEL_DELAY_PORTS_NS
    return max(d, a + b * ports)


def accel_signoff_mhz(
    dataflow: str, cols: int, ports: int, rows: int = 1, bank_read_ns: float = 0.0
) -> float:
    """The clock the routed accelerator meets on SG13G2 at the slow corner. ``bank_read_ns``
    is the slowest read of the SRAM macros its buffers are, for a macro-backed GEMV build
    (0 for flop buffers)."""
    path = accel_delay_ns(dataflow, cols, ports) + FLOP_OVERHEAD_NS
    datapath = ACCEL_ROUTED_NS
    if path > ACCEL_DELAY_NS["gemv"] + FLOP_OVERHEAD_NS:
        datapath = max(datapath, path * ACCEL_SIGNOFF_FACTOR)
    if dataflow == "gemv" and rows > 1:
        datapath += ACCEL_LANE_NS * math.log2(rows)
    a, b = ACCEL_PORT_NS
    period = max(datapath, a + b * ports)
    if bank_read_ns > 0:
        period = max(period + ACCEL_BANK_NS, bank_read_ns + ACCEL_BANK_PATH_NS)
    return 1000.0 / period


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
        self,
        dataflow: str = "",
        cols: int = 0,
        ports: int = 0,
        rows: int = 1,
        bank_read_ns: float = 0.0,
    ) -> float:
        """The fastest clock a design on this node may use: the node's limit, and with an
        accelerator on a node with measured timing, what that accelerator meets after
        routing at the slow corner (``bank_read_ns`` as in ``accel_signoff_mhz``)."""
        if self.accel_timing and dataflow:
            return min(
                self.max_freq_mhz,
                accel_signoff_mhz(dataflow, cols, ports, rows, bank_read_ns),
            )
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
