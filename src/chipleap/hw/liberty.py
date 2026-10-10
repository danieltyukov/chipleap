"""Measure a process from its Liberty files, for ``chipleap tech add``.

Reads the standard-cell library for the NAND2 and flip-flop areas and the NAND2
leakage, and the SRAM macro libraries for a fit of area against capacity and, where the
library models it, the read energy. Only these few numbers leave the parser: the
technology file ``chipleap tech add`` writes holds measurements, never library content.

The parser handles what foundry and open libraries have in common: ``cell`` groups with
``area``, ``cell_leakage_power``, output pins with a ``function``, ``ff`` groups for
flip-flops and ``memory`` groups (``address_width``, ``word_width``) for SRAM macros.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

_UNITS = {"fW": 1e-15, "pW": 1e-12, "nW": 1e-9, "uW": 1e-6, "mW": 1e-3, "W": 1.0}


class LibertyError(ValueError):
    pass


@dataclass(frozen=True)
class Cell:
    name: str
    area: float  # um2
    leakage_w: float
    functions: tuple[str, ...]  # output pin functions
    inputs: int
    flop: bool
    async_reset: bool
    memory: tuple[int, int] | None = None  # (address_width, word_width)
    read_pj: float = math.nan  # energy of one read access, if modelled


@dataclass
class Library:
    name: str
    cells: dict[str, Cell] = field(default_factory=dict)
    nom_voltage: float = 0.0
    time_unit_ns: float = 1.0


_BRACE = re.compile(r"[{}]")


def _close(text: str, start: int) -> int:
    """Index of the brace closing the group whose body starts at ``start``."""
    depth = 1
    for b in _BRACE.finditer(text, start):
        depth += 1 if b.group() == "{" else -1
        if depth == 0:
            return b.start()
    raise LibertyError("unbalanced braces")


def _groups(text: str, keyword: str) -> list[tuple[str, str]]:
    """(name, body) of each top-level ``keyword (name) { ... }`` group, braces matched."""
    out: list[tuple[str, str]] = []
    head = re.compile(rf"\b{keyword}\s*\(\s*\"?([^\")]*)\"?\s*\)\s*\{{")
    pos = 0
    while True:
        m = head.search(text, pos)
        if not m:
            return out
        end = _close(text, m.end())
        out.append((m.group(1).strip(), text[m.end() : end]))
        pos = end + 1


def _top_level(body: str) -> str:
    """The part of a group body outside its nested groups."""
    out: list[str] = []
    depth, last = 0, 0
    for b in _BRACE.finditer(body):
        if b.group() == "{":
            if depth == 0:
                out.append(body[last : b.start()])
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                last = b.end()
    if depth == 0:
        out.append(body[last:])
    return "".join(out)


def _attr(text: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*:\s*\"?([^;\"]+)\"?\s*;", text)
    return m.group(1).strip() if m else None


def _read_energy(body: str, unit_j: float) -> float:
    """Energy of one read access in pJ, when the library states it: the first entry
    (smallest slew and load) of each internal-power table whose ``when`` condition reads
    without writing, the largest of those."""
    best = math.nan
    for m in re.finditer(r"internal_power\s*\(\s*\)\s*\{", body):
        chunk = body[m.end() : _close(body, m.end())]
        when = _attr(chunk, "when") or ""
        if not when or not re.search(r"(!\s*\w*WEN|\bREN\b|!\s*\w*WE\b)", when):
            continue
        v = re.search(r"values\s*\(\s*\"?\s*([-\d.eE+]+)", chunk)
        if v:
            pj = float(v.group(1)) * unit_j * 1e12
            if math.isnan(best) or pj > best:
                best = pj
    return best


def parse(text: str) -> Library:
    libs = _groups(text, "library")
    if not libs:
        raise LibertyError("no library group")
    lib_name, lib_body = libs[0]
    top = _top_level(lib_body)
    lu = _attr(top, "leakage_power_unit") or "1nW"
    mu = re.fullmatch(r"\s*([\d.]+)\s*([fpnum]?W)\s*", lu)
    leak_unit = float(mu.group(1)) * _UNITS[mu.group(2)] if mu else 1e-9
    tu = _attr(top, "time_unit") or "1ns"
    mt = re.fullmatch(r"\s*([\d.]+)\s*([pn]s)\s*", tu)
    time_ns = (float(mt.group(1)) * (1e-3 if mt.group(2) == "ps" else 1.0)) if mt else 1.0
    # Energy tables are in voltage^2 x capacitance units; the common pF x V^2 is pJ.
    cap = re.search(r"capacitive_load_unit\s*\(\s*([\d.]+)\s*,\s*(\w+)\s*\)", top)
    cap_f = {"ff": 1e-15, "pf": 1e-12}.get(cap.group(2).lower(), 1e-12) if cap else 1e-12
    energy_unit = (float(cap.group(1)) if cap else 1.0) * cap_f
    vnom = float(_attr(top, "nom_voltage") or 0.0)
    lib = Library(lib_name, nom_voltage=vnom, time_unit_ns=time_ns)
    for name, body in _groups(lib_body, "cell"):
        ctop = _top_level(body)
        area = float(_attr(ctop, "area") or 0.0)
        total = _attr(ctop, "cell_leakage_power")
        if total is None:
            # Some libraries give leakage only per input state; take the mean.
            states = [
                float(v)
                for _, lb in _groups(body, "leakage_power")
                if (v := _attr(_top_level(lb), "value")) is not None
            ]
            total = str(sum(states) / len(states)) if states else "0"
        leak = float(total) * leak_unit
        functions, inputs = [], 0
        for _pin, pbody in _groups(body, "pin"):
            ptop = _top_level(pbody)
            direction = _attr(ptop, "direction")
            if direction == "input":
                inputs += 1
            elif direction == "output":
                f = _attr(ptop, "function")
                if f:
                    functions.append(f)
        ffs = _groups(body, "ff")
        async_reset = any(
            _attr(_top_level(fb), "clear") or _attr(_top_level(fb), "preset") for _, fb in ffs
        )
        mem = None
        mg = re.search(r"\bmemory\s*\(\s*\)\s*\{([^}]*)\}", body)
        if mg:
            aw = _attr(mg.group(1), "address_width")
            ww = _attr(mg.group(1), "word_width")
            if aw and ww:
                mem = (int(aw), int(ww))
        lib.cells[name] = Cell(
            name,
            area,
            leak,
            tuple(functions),
            inputs,
            bool(ffs),
            async_reset,
            mem,
            _read_energy(body, energy_unit) if mem else math.nan,
        )
    return lib


def _is_nand2(cell: Cell) -> bool:
    if cell.inputs != 2 or len(cell.functions) != 1 or cell.flop:
        return False
    f = re.sub(r"\s+", " ", cell.functions[0]).strip()
    body = r"\(?\s*(\w+)\s*(?:&|\*| )\s*(\w+)\s*\)?"
    return bool(re.fullmatch(rf"!\s*\(\s*{body}\s*\)|\(\s*!\s*\(\s*{body}\s*\)\s*\)|!\s*{body}", f))


def _drive_one(name: str) -> bool:
    """Unit drive by the usual naming: sg13g2_nand2_1, NAND2X1, NAND2_X1, or ND2D1 with a
    library suffix after the drive (ND2D1BWP)."""
    return bool(re.search(r"(?:_1|X1|D1|_x1)(?:[A-Za-z][A-Za-z0-9]*)?$", name))


def nand2(lib: Library) -> Cell:
    """The library's unit-drive two-input NAND, which defines one gate equivalent."""
    cands = [c for c in lib.cells.values() if _is_nand2(c) and c.area > 0]
    if not cands:
        raise LibertyError("no two-input NAND found")
    unit = [c for c in cands if _drive_one(c.name)]
    return min(unit or cands, key=lambda c: c.area)


def flop(lib: Library) -> Cell:
    """The smallest unit-drive flip-flop with an asynchronous reset or set."""
    cands = [c for c in lib.cells.values() if c.flop and c.area > 0 and not c.memory]
    if not cands:
        raise LibertyError("no flip-flop found")
    pick = [c for c in cands if c.async_reset] or cands
    unit = [c for c in pick if _drive_one(c.name)]
    return min(unit or pick, key=lambda c: c.area)


@dataclass(frozen=True)
class SramFit:
    macro_mm2: float  # periphery per macro
    mm2_per_kib: float
    max_kib: int
    pj_per_byte_16kib: float  # wide read, NaN when the libraries do not model it
    macros: int


def sram_fit(cells: list[Cell]) -> SramFit:
    """Least-squares fit of macro area against capacity, and read energy per byte."""
    mems = [c for c in cells if c.memory and c.area > 0]
    if len(mems) < 2:
        raise LibertyError("need at least two SRAM macros to fit area against capacity")
    pts = [((2 ** c.memory[0]) * c.memory[1] / 8 / 1024, c.area / 1e6) for c in mems if c.memory]
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx == 0:
        raise LibertyError("all SRAM macros have the same capacity")
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    a = my - b * mx
    max_kib = int(max(x for x, _ in pts))
    # Read energy per byte of the widest macros (accelerator buffers read wide words),
    # fitted as a power law in size and taken at 16 KiB.
    widest = max(c.memory[1] for c in mems if c.memory)
    e = [
        ((2 ** c.memory[0]) * c.memory[1] / 8 / 1024, c.read_pj / (c.memory[1] / 8))
        for c in mems
        if c.memory and c.memory[1] == widest and not math.isnan(c.read_pj) and c.read_pj > 0
    ]
    pj16 = math.nan
    if len(e) >= 2:
        lx = [math.log(k / 16) for k, _ in e]
        ly = [math.log(v) for _, v in e]
        mlx, mly = sum(lx) / len(lx), sum(ly) / len(ly)
        den = sum((x - mlx) ** 2 for x in lx)
        p = sum((x - mlx) * (y - mly) for x, y in zip(lx, ly, strict=True)) / den if den else 0
        pj16 = math.exp(mly - p * mlx)
    elif len(e) == 1:
        pj16 = e[0][1] * (16 / e[0][0]) ** 0.4
    return SramFit(max(a, 0.0), b, max(1, max_kib), pj16, len(mems))


def read_files(paths: list[Path]) -> list[Library]:
    return [parse(p.read_text(errors="replace")) for p in paths]


def liberty_files(path: Path) -> list[Path]:
    """A liberty file, or every .lib file in a directory."""
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.suffix == ".lib")
    return [path]
