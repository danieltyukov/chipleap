"""IHP's single-port SRAM macros (``sg13g2_sram``, ``RM_IHPSG13_1P_*``): which the PDK has,
how a bank of a given depth and width is built from them, and the views a flow needs
beside the macro layer's (``chipleap.macro.layer.pdk_macro``): liberty at every corner, a
black-box module for LibreLane's linter and synthesis, and ChipLeap's functional model of
the ports for simulation where the PDK's own model is not at hand.

A macro's ports: ``A_CLK``, ``A_MEN`` (enable), ``A_WEN``, ``A_REN``, ``A_ADDR``,
``A_DIN``, ``A_DOUT``, ``A_DLY`` (tied high) and, on the ``_bm_bist`` macros, ``A_BM`` (a
bit is written where it is 1) and a BIST port set, tied off. A read returns the word a
cycle after the address, and ``A_DOUT`` holds it until the next read; a write with
``A_REN`` also high reads the new word through.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.macro import layer
from chipleap.macro.layer import MacroView
from chipleap.rtl import builddir

LIBRARY = "sg13g2_sram"
NAME_RE = re.compile(r"RM_IHPSG13_1P_([1-9][0-9]{1,4})x([1-9][0-9]?)_c([1-9])(_bm_bist)?")
# LibreLane's timing corners for IHP (nom_typ_1p20V_25C and the like) and the liberty of
# each: the macro's own arcs at every corner, so timing at the slow one sees them.
CORNERS = {
    "*_typ_*": "typ_1p20V_25C",
    "*_slow_*": "slow_1p08V_125C",
    "*_fast_*": "fast_1p32V_m55C",
}
_SIZE_RE = re.compile(r"^\s*SIZE\s+([0-9.]+)\s+BY\s+([0-9.]+)\s*;", re.M)
_BIT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[([0-9]+)\])?")


class SramError(ValueError):
    pass


@dataclass(frozen=True)
class Sram:
    """One of the PDK's 1P macros: ``words`` deep, ``bits`` wide, ``width_um`` by
    ``height_um``; ``bit_mask`` for the macros with ``A_BM`` (and the BIST ports)."""

    cell: str
    words: int
    bits: int
    width_um: float
    height_um: float
    bit_mask: bool

    @property
    def area_um2(self) -> float:
        return self.width_um * self.height_um

    @property
    def addr_bits(self) -> int:
        return self.words.bit_length() - 1


@dataclass(frozen=True)
class Bank:
    """``words`` of ``bits`` as macros: ``across`` side by side, each holding a slice of
    the word, and ``deep`` stacked, each holding a range of the addresses."""

    words: int
    bits: int
    macro: Sram
    across: int
    deep: int

    @property
    def count(self) -> int:
        return self.across * self.deep

    @property
    def area_um2(self) -> float:
        return self.count * self.macro.area_um2


def _views(ref: Path, cell: str) -> list[Path]:
    return [
        ref / "lef" / f"{cell}.lef",
        ref / "gds" / f"{cell}.gds",
        ref / "cdl" / f"{cell}.cdl",
        *(ref / "lib" / f"{cell}_{corner}.lib" for corner in CORNERS.values()),
    ]


# IHP SG13G2's single-port macros as ``catalog`` reads them from the PDK (IHP-Open-PDK
# 22f2a25, the LEF outlines): what the model prices an accelerator's buffers with, where
# the PDK need not be installed. tests/test_macro_sram.py checks it against the PDK.
IHP_1P = (
    Sram("RM_IHPSG13_1P_1024x16_c2_bm_bist", 1024, 16, 236.8, 336.46, True),
    Sram("RM_IHPSG13_1P_1024x32_c2_bm_bist", 1024, 32, 416.64, 336.46, True),
    Sram("RM_IHPSG13_1P_1024x64_c2_bm_bist", 1024, 64, 784.48, 336.46, True),
    Sram("RM_IHPSG13_1P_1024x8_c2_bm_bist", 1024, 8, 146.88, 336.46, True),
    Sram("RM_IHPSG13_1P_2048x64_c2_bm_bist", 2048, 64, 784.48, 626.7, True),
    Sram("RM_IHPSG13_1P_256x16_c2_bm_bist", 256, 16, 236.8, 118.78, True),
    Sram("RM_IHPSG13_1P_256x32_c2_bm_bist", 256, 32, 416.64, 118.78, True),
    Sram("RM_IHPSG13_1P_256x48_c2_bm_bist", 256, 48, 596.48, 118.78, True),
    Sram("RM_IHPSG13_1P_256x64_c2_bm_bist", 256, 64, 784.48, 118.78, True),
    Sram("RM_IHPSG13_1P_256x8_c3_bm_bist", 256, 8, 236.8, 74.1, True),
    Sram("RM_IHPSG13_1P_4096x16_c3_bm_bist", 4096, 16, 416.64, 618.3, True),
    Sram("RM_IHPSG13_1P_4096x8_c3_bm_bist", 4096, 8, 236.8, 618.3, True),
    Sram("RM_IHPSG13_1P_512x16_c2_bm_bist", 512, 16, 236.8, 191.34, True),
    Sram("RM_IHPSG13_1P_512x32_c2_bm_bist", 512, 32, 416.64, 191.34, True),
    Sram("RM_IHPSG13_1P_512x64_c2_bm_bist", 512, 64, 784.48, 191.34, True),
    Sram("RM_IHPSG13_1P_512x8_c3_bm_bist", 512, 8, 236.8, 110.38, True),
    Sram("RM_IHPSG13_1P_64x64_c2_bm_bist", 64, 64, 784.48, 64.36, True),
    Sram("RM_IHPSG13_1P_8192x32_c4", 8192, 32, 1520.16, 618.3, False),
)
# Each of those macros' read time at the slow corner (1.08 V, 125 C): A_CLK to A_DOUT, the
# later of rise and fall at the liberty's smallest clock slew and load. It grows with the
# depth, from 5.0 ns at 256 words to 9.4 ns at 8192, and bounds the clock of a design that
# registers the word in the next cycle. tests/test_macro_sram.py checks it against the PDK.
IHP_1P_READ_NS = {
    "RM_IHPSG13_1P_1024x16_c2_bm_bist": 7.116,
    "RM_IHPSG13_1P_1024x32_c2_bm_bist": 7.2485,
    "RM_IHPSG13_1P_1024x64_c2_bm_bist": 7.2744,
    "RM_IHPSG13_1P_1024x8_c2_bm_bist": 7.0497,
    "RM_IHPSG13_1P_2048x64_c2_bm_bist": 8.994,
    "RM_IHPSG13_1P_256x16_c2_bm_bist": 4.994,
    "RM_IHPSG13_1P_256x32_c2_bm_bist": 5.1263,
    "RM_IHPSG13_1P_256x48_c2_bm_bist": 5.2586,
    "RM_IHPSG13_1P_256x64_c2_bm_bist": 5.151,
    "RM_IHPSG13_1P_256x8_c3_bm_bist": 4.9619,
    "RM_IHPSG13_1P_4096x16_c3_bm_bist": 8.8937,
    "RM_IHPSG13_1P_4096x8_c3_bm_bist": 8.8071,
    "RM_IHPSG13_1P_512x16_c2_bm_bist": 6.25,
    "RM_IHPSG13_1P_512x32_c2_bm_bist": 6.3821,
    "RM_IHPSG13_1P_512x64_c2_bm_bist": 6.4165,
    "RM_IHPSG13_1P_512x8_c3_bm_bist": 4.9562,
    "RM_IHPSG13_1P_64x64_c2_bm_bist": 5.1522,
    "RM_IHPSG13_1P_8192x32_c4": 9.3689,
}
_DOUT_RE = re.compile(r"\bbus\s*\(\s*A_DOUT\s*\)")
_ARC_RE = re.compile(r"\b(cell_rise|cell_fall)\s*\([^)]*\)\s*\{[^}]*?values\s*\(\s*\"\s*([0-9.]+)")


def read_ns(liberty: str) -> float:
    """A macro's read time in its liberty text: A_CLK to A_DOUT, the later of rise and fall
    at the smallest clock slew and load (the first value of each table)."""
    m = _DOUT_RE.search(liberty)
    if m is None:
        raise SramError("the liberty has no A_DOUT bus")
    block = liberty[m.end() :]
    clk = re.search(r"related_pin\s*:\s*\"A_CLK\"", block)
    arcs = _ARC_RE.findall(block[clk.end() :] if clk else "")[:2]
    if clk is None or {kind for kind, _ in arcs} != {"cell_rise", "cell_fall"}:
        raise SramError("the liberty has no A_CLK to A_DOUT arc")
    return max(float(v) for _, v in arcs)


def catalog(pdk: Path) -> list[Sram]:
    """Every 1P macro of the PDK's ``sg13g2_sram`` with all its views: LEF, GDS, CDL and
    liberty at the three corners. The outline is read from the LEF."""
    ref = pdk / "libs.ref" / LIBRARY
    out = []
    lefs = ref / "lef"
    for lef in sorted(lefs.glob("RM_IHPSG13_1P_*.lef")) if lefs.is_dir() else []:
        m = NAME_RE.fullmatch(lef.stem)
        if m is None or not all(p.is_file() for p in _views(ref, lef.stem)):
            continue
        words, bits = int(m[1]), int(m[2])
        if words & (words - 1):
            continue
        size = _SIZE_RE.search(lef.read_text(errors="replace"))
        if size is None:
            continue
        out.append(Sram(lef.stem, words, bits, float(size[1]), float(size[2]), m[4] is not None))
    return out


def choose(srams: Iterable[Sram], words: int, bits: int, bit_mask: bool = True) -> Bank:
    """The bank of ``words`` by ``bits``: macros of the width that wastes fewest bits
    (then the fewest side by side), of those the fewest stacked that hold the depth, then
    the smallest area. ``bit_mask`` asks for macros with ``A_BM``."""
    if words < 1 or bits < 1:
        raise SramError(f"a bank of {words} x {bits} bits holds nothing")
    fit = [s for s in srams if s.bit_mask or not bit_mask]
    if not fit:
        raise SramError(f"the PDK's {LIBRARY} has no single-port macro to use")

    def shape(s: Sram) -> tuple[int, int, int, float]:
        across = -(-bits // s.bits)
        deep = -(-words // s.words)
        return across * s.bits - bits, across, deep, across * deep * s.area_um2

    best = min(fit, key=lambda s: (*shape(s), s.cell))
    _, across, deep, _ = shape(best)
    return Bank(words, bits, best, across, deep)


def find(srams: Iterable[Sram], cell: str) -> Sram:
    for s in srams:
        if s.cell == cell:
            return s
    raise SramError(f"the PDK's {LIBRARY} has no single-port macro {cell[:64]!r}")


@dataclass(frozen=True)
class SramViews:
    """A macro's views in a flow's directory: the macro layer's, its liberty per
    LibreLane corner and its black-box module."""

    view: MacroView
    libs: dict[str, Path]
    stub: Path


def views(pdk: Path, cell: str, work: Path) -> SramViews:
    """The macro layer's views of ``cell`` (``work/macros/<cell>/``), with the slow and
    fast liberty and the black-box module beside them."""
    if NAME_RE.fullmatch(cell) is None:
        raise SramError(f"{cell[:64]!r} is not a single-port SRAM of {LIBRARY}")
    view = layer.pdk_macro(pdk, cell, work, LIBRARY)
    dest = view.lef.parent
    ref = pdk / "libs.ref" / LIBRARY / "lib"
    libs = {}
    for pattern, corner in CORNERS.items():
        src = ref / f"{cell}_{corner}.lib"
        if not src.is_file():
            raise SramError(f"{LIBRARY} has no {corner} liberty of {cell}")
        libs[pattern] = builddir.write_bytes(dest, src.name, src.read_bytes())
    stub = builddir.write_text(dest, f"{cell}_bb.v", blackbox(view))
    return SramViews(view, libs, stub)


def _buses(view: MacroView) -> list[tuple[str, str, int]]:
    """The macro's signal ports as (name, direction, width) in the order of its LEF, a bus
    from its bits ``0`` up; the supply pins are left out."""
    seen: dict[str, tuple[str, set[int | None]]] = {}
    for pin in view.pins:
        if pin.is_supply:
            continue
        m = _BIT_RE.fullmatch(pin.name)
        if m is None:
            raise SramError(f"{view.cell}: pin {pin.name[:64]!r} is not a Verilog port")
        name, bit = m[1], None if m[2] is None else int(m[2])
        direction, bits = seen.setdefault(name, (pin.direction, set()))
        # A port is a single bit or a bus, in one direction, each bit once.
        if direction != pin.direction or bit in bits or (bits and (None in bits) != (bit is None)):
            raise SramError(f"{view.cell}: the bits of {name} do not make one port")
        bits.add(bit)
    out = []
    for name, (direction, bits) in seen.items():
        if bits == {None}:
            out.append((name, direction, 0))
        elif None not in bits and bits == set(range(len(bits))):
            out.append((name, direction, len(bits)))
        else:
            raise SramError(f"{view.cell}: the bits of {name} are not 0 to {len(bits) - 1}")
    return out


def module(cell: str, ports: list[tuple[str, str, int]], source: str) -> str:
    """An empty module ``cell`` with ``ports`` (name, direction, width; 0 a single bit),
    marked a black box: what lint and synthesis read in a macro's place."""
    lines = [
        f"// Written by ChipLeap from {source}: the macro's ports, for lint and synthesis.",
        "/* verilator lint_off UNUSEDSIGNAL */",
        "/* verilator lint_off UNDRIVEN */",
        "(* blackbox *)",
        f"module {cell} ({', '.join(name for name, _, _ in ports)});",
        *(f"  {d} {f'[{w - 1}:0] ' if w else ''}{n};" for n, d, w in ports),
        "endmodule",
        "/* verilator lint_on UNDRIVEN */",
        "/* verilator lint_on UNUSEDSIGNAL */",
    ]
    return "\n".join(lines) + "\n"


def blackbox(view: MacroView) -> str:
    """The macro as an empty module with its LEF's signal ports, for LibreLane's linter
    and synthesis (``vh`` in ``MACROS``); the netlist then keeps it as an instance."""
    return module(view.cell, _buses(view), "its LEF")


def stub(s: Sram) -> str:
    """The same module from the macro's name alone (``ports``), where the PDK is not
    needed: a project's lint and synthesis."""
    if NAME_RE.fullmatch(s.cell) is None:
        raise SramError(f"{s.cell[:64]!r} is not a single-port SRAM of {LIBRARY}")
    return module(s.cell, ports(s), "its name")


def ports(sram: Sram) -> list[tuple[str, str, int]]:
    """The macro's signal ports as (name, direction, width), as the PDK's model has them;
    width 0 for a single bit."""
    a, d = sram.addr_bits, sram.bits
    out = [
        ("A_CLK", "input", 0),
        ("A_MEN", "input", 0),
        ("A_WEN", "input", 0),
        ("A_REN", "input", 0),
        ("A_ADDR", "input", a),
        ("A_DIN", "input", d),
        ("A_DLY", "input", 0),
        ("A_DOUT", "output", d),
    ]
    if sram.bit_mask:
        out += [
            ("A_BM", "input", d),
            ("A_BIST_CLK", "input", 0),
            ("A_BIST_EN", "input", 0),
            ("A_BIST_MEN", "input", 0),
            ("A_BIST_WEN", "input", 0),
            ("A_BIST_REN", "input", 0),
            ("A_BIST_ADDR", "input", a),
            ("A_BIST_DIN", "input", d),
            ("A_BIST_BM", "input", d),
        ]
    return out


def model(sram: Sram) -> str:
    """ChipLeap's functional model of ``sram``'s ports, in the PDK's model's place for
    simulation: the same write (masked by ``A_BM``), read a cycle later, and write-through
    with ``A_REN``; the BIST ports are ignored. A read and a write in one cycle stop the
    simulation, because ChipLeap's designs never ask for write-through."""
    if NAME_RE.fullmatch(sram.cell) is None:
        raise SramError(f"{sram.cell[:64]!r} is not a single-port SRAM of {LIBRARY}")
    p = ports(sram)
    mask = "A_BM" if sram.bit_mask else f"{{{sram.bits}{{1'b1}}}}"
    lines = [
        "// Written by ChipLeap: a functional model of the SRAM macro's ports.",
        "/* verilator lint_off UNUSEDSIGNAL */",
        f"module {sram.cell} ({', '.join(name for name, _, _ in p)});",
        *(f"  {d} {f'[{w - 1}:0] ' if w else ''}{n};" for n, d, w in p),
        f"  reg [{sram.bits - 1}:0] mem [0:{sram.words - 1}];",
        f"  reg [{sram.bits - 1}:0] dout_q;",
        "  assign A_DOUT = dout_q;",
        "  always @(posedge A_CLK) begin",
        "    if (A_MEN && A_WEN) begin",
        "      if (A_REN) begin",
        f'        $display("{sram.cell}: a read and a write in one cycle");',
        "        $stop;",
        "      end",
        f"      mem[A_ADDR] <= (mem[A_ADDR] & ~{mask}) | (A_DIN & {mask});",
        "    end else if (A_MEN && A_REN) begin",
        "      dout_q <= mem[A_ADDR];",
        "    end",
        "  end",
        "endmodule",
        "/* verilator lint_on UNUSEDSIGNAL */",
    ]
    return "\n".join(lines) + "\n"


def librelane_macros(
    srams: Mapping[str, SramViews],
    placements: Iterable[tuple[str, str, float, float, str]],
    flow_dir: Path,
    others: Mapping[str, MacroView] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """LibreLane's ``MACROS`` and the macros' ``EXTRA_CDLS`` for placed macros (instance,
    cell, x, y, orientation): the macro layer's entries (``others`` holds the views of
    macros that are not SRAMs, such as packages), each SRAM's with the liberty of every
    corner and its black-box module (``vh``)."""
    views = {**(others or {}), **{cell: v.view for cell, v in srams.items()}}
    macros, cdls = layer.librelane_config(views, list(placements), flow_dir)
    for cell, entry in macros.items():
        if cell not in srams:
            continue
        v = srams[cell]
        entry["lib"] = {pattern: [layer._dir(path, flow_dir)] for pattern, path in v.libs.items()}
        entry["vh"] = [layer._dir(v.stub, flow_dir)]
    return macros, cdls
