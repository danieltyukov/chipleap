"""Hardened blocks in a chip: macro/v1 packages, imported as views ChipLeap writes itself.

A package comes from someone else (aispice, another flow, a download), so nothing in it
is trusted. Its files are copied into the build directory through ``Project.read``, so
none is reached through a symlink out of the project; the copy is checked with
``chipleap.macro.check_package`` against IHP SG13G2, and errors stop the chip. Then only
the files the validated manifest names are read, and from them ChipLeap writes the
views the flow uses:

- a LEF of the macro alone, rebuilt from the package's statements ChipLeap understands
  (outline, pins with their shapes and antenna areas, obstructions), with its origin
  moved to 0, 0;
- the GDS with the macro's cell moved by the same offset, since KLayout's stream-out
  places a cell by its own origin (with the LEF's ``ORIGIN 8.55 0.6`` the comparator's
  pins and layout disagreed by that much);
- a liberty of the pins the manifest lists, without timing arcs;
- the SPICE subcircuits, device lines only, for LVS.

Every cell in the GDS and every subcircuit must carry the macro's name (``<name>`` or
``<name>_...``): one called ``sg13g2_inv_1`` would replace the standard cell of that
name in the chip's layout or in its LVS netlist.
"""

from __future__ import annotations

import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from chipleap.chip.config import ChipMacro
from chipleap.macro import check_package
from chipleap.macro.check import Report
from chipleap.macro.limits import LIMITS
from chipleap.macro.manifest import MANIFEST, MacroError, Manifest, load_manifest
from chipleap.rtl import builddir

PACKAGE_FILES_MAX = 256
PACKAGE_BYTES_MAX = 512 << 20
CORE_VOLTAGE = 1.2  # the core supply every macro is powered from
# Layers of IHP SG13G2's technology LEF that a macro's LEF may name.
LEF_LAYERS = (
    "GatPoly",
    "Cont",
    "Metal1",
    "Via1",
    "Metal2",
    "Via2",
    "Metal3",
    "Via3",
    "Metal4",
    "Via4",
    "Metal5",
    "TopVia1",
    "TopMetal1",
    "TopVia2",
    "TopMetal2",
    "OVERLAP",
)
# Supply pins on these layers are joined to the standard-cell rails by a strap
# (``chipleap.chip.pdn``); a via stack goes down from them to Metal1.
HOOKUP_LAYERS = ("Metal2", "Metal3", "Metal4", "Metal5")
COORD_MAX_UM = 10000.0
_NUM = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_BIT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[0-9]{1,6}\])?")
_DIRECTIONS = {"INPUT", "OUTPUT", "INOUT"}
_USES = {"SIGNAL", "ANALOG", "CLOCK", "POWER", "GROUND"}
_SHAPES = {"ABUTMENT", "RING", "FEEDTHRU"}
_ANTENNA = {
    "ANTENNAGATEAREA",
    "ANTENNADIFFAREA",
    "ANTENNAPARTIALMETALAREA",
    "ANTENNAPARTIALMETALSIDEAREA",
    "ANTENNAPARTIALCUTAREA",
    "ANTENNAMAXAREACAR",
    "ANTENNAMAXSIDEAREACAR",
    "ANTENNAMAXCUTCAR",
}

Rect = tuple[float, float, float, float]


class MacroImportError(ValueError):
    pass


@dataclass(frozen=True)
class MacroPin:
    name: str  # a bit: ``q[3]`` or ``clk``
    direction: str  # input, output, inout (from the manifest)
    kind: str  # signal, analog, clock, power, ground (from the manifest)
    shapes: tuple[tuple[str, Rect], ...]  # (layer, rect) in the macro's coordinates

    @property
    def is_supply(self) -> bool:
        return self.kind in ("power", "ground")


@dataclass(frozen=True)
class MacroView:
    cell: str
    kind: str
    width: float
    height: float
    pins: tuple[MacroPin, ...]
    lef: Path
    gds: Path
    lib: Path
    spice: Path
    subckts: tuple[str, ...]  # the subcircuits of the SPICE view
    gds_cells: tuple[str, ...]
    report: Report

    def pin(self, name: str) -> MacroPin | None:
        return next((p for p in self.pins if p.name == name), None)

    @property
    def warnings(self) -> list[str]:
        return [f.to_text() for f in self.report.findings if f.severity == "warning"]


# Copying and checking

Reader = Callable[[Path], bytes]  # Project.read
Lister = Callable[[Path], list[Path]]  # Project.files_in


def _copy_package(read: Reader, package: Path, files: Iterable[Path], dest: Path) -> None:
    """Copy the package's files, each read through ``read`` (``Project.read``, which
    opens a file only inside the project), into ``dest``."""
    root = package.resolve()
    total = 0
    for n, real in enumerate(sorted(files)):
        if n >= PACKAGE_FILES_MAX:
            raise MacroImportError(f"{package} holds more than {PACKAGE_FILES_MAX} files")
        try:
            rel = real.relative_to(root)
        except ValueError:
            raise MacroImportError(
                f"{package} holds a symlink to a file outside the package ({real.name}); "
                "copy the file in instead"
            ) from None
        size = real.stat().st_size
        total += size
        if size > LIMITS.gds_bytes or total > PACKAGE_BYTES_MAX:
            raise MacroImportError(f"{package} is larger than ChipLeap imports")
        builddir.write_bytes(dest, rel, read(real))


def check_copy(copy: Path, pdk: str) -> tuple[Manifest, Report]:
    """Check a package's copy against ``pdk`` (``ihp-sg13g2`` or ``ihp-sg13g2@COMMIT``);
    its manifest, or MacroImportError with the first errors."""
    report = check_package(copy, pdk=pdk)
    if not report.passed:
        errors = [f.to_text() for f in report.findings if f.severity == "error"]
        more = f" and {len(errors) - 3} more" if len(errors) > 3 else ""
        raise MacroImportError(
            f"the package fails macro/v1's checks: {'; '.join(errors[:3])}{more}"
        )
    try:
        return load_manifest(copy), report
    except (MacroError, OSError) as exc:  # the check passed, so this should not happen
        raise MacroImportError(f"cannot read {MANIFEST}: {exc}") from None


# LEF


@dataclass
class _LefPin:
    name: str
    lines: list[str]
    shapes: list[tuple[str, Rect]]
    use: str = ""


def _number(tok: str, where: str) -> float:
    if not _NUM.fullmatch(tok):
        raise MacroImportError(f"LEF: {where}: expected a number, found {tok[:40]!r}")
    v = float(tok)
    if abs(v) > COORD_MAX_UM:
        raise MacroImportError(f"LEF: {where}: {tok} is out of range")
    return v


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".") if v else "0"


def _lef_tokens(text: str) -> list[str]:
    if len(text) > LIMITS.view_bytes:
        raise MacroImportError("LEF: the file is too large")
    toks = re.findall(r'"[^"\n]*"|;|[^\s;]+', re.sub(r"#[^\n]*", "", text))
    if len(toks) > LIMITS.tokens:
        raise MacroImportError("LEF: the file has too many tokens")
    return toks


def rewrite_lef(
    text: str, name: str
) -> tuple[str, tuple[float, float], list[_LefPin], tuple[float, float]]:
    """The LEF of macro ``name`` rebuilt with its origin at 0, 0: the outline, pins and
    obstructions, from statements this reader knows; any other statement in a pin, port
    or obstruction is refused rather than dropped, since a missing shape would let the
    router through the macro. Returns the LEF text, the size, the pins and the origin
    it removed (the offset the GDS moves by)."""
    toks = _lef_tokens(text)
    i = 0
    while i < len(toks) - 1 and not (toks[i] == "MACRO" and toks[i + 1] == name):
        i += 1
    if i >= len(toks) - 1:
        raise MacroImportError(f"LEF: no MACRO {name}")
    i += 2

    def statement() -> list[str]:
        nonlocal i
        out = []
        while i < len(toks) and toks[i] != ";":
            out.append(toks[i])
            i += 1
        if i >= len(toks):
            raise MacroImportError("LEF: a statement does not end")
        i += 1
        return out

    origin = (0.0, 0.0)
    size: tuple[float, float] | None = None
    cls = "BLOCK"
    pins: list[_LefPin] = []
    raw_shapes: list[tuple[str, str, list[float]]] = []  # owner, kind, numbers
    obs_shapes: list[tuple[str, str, list[float]]] = []

    def geometry(owner: str, where: str, sink: list[tuple[str, str, list[float]]]) -> None:
        """LAYER, RECT and POLYGON statements up to END."""
        nonlocal i
        layer = ""
        while True:
            if i >= len(toks):
                raise MacroImportError(f"LEF: {where} does not end")
            kw = toks[i]
            if kw == "END":
                i += 1
                return
            i += 1
            args = statement()
            if kw == "LAYER":
                if not args or args[0] not in LEF_LAYERS:
                    found = args[0][:40] if args else "nothing"
                    raise MacroImportError(
                        f"LEF: {where} names layer {found!r}, not one of IHP SG13G2's"
                    )
                layer = args[0]
            elif kw in ("RECT", "POLYGON"):
                if not layer:
                    raise MacroImportError(f"LEF: {where} has a shape before its LAYER")
                if args[:1] == ["MASK"]:
                    args = args[2:]
                nums = [_number(a, where) for a in args]
                if (kw == "RECT" and len(nums) != 4) or (
                    kw == "POLYGON" and (len(nums) < 6 or len(nums) % 2)
                ):
                    raise MacroImportError(f"LEF: {where} has a malformed {kw}")
                sink.append((f"{owner}\0{layer}", kw, nums))
            else:
                raise MacroImportError(
                    f"LEF: {where} has a {kw[:40]} statement; ChipLeap reads only LAYER, RECT "
                    "and POLYGON there"
                )

    while True:
        if i >= len(toks):
            raise MacroImportError(f"LEF: MACRO {name} does not end")
        kw = toks[i]
        i += 1
        if kw == "END":
            if i >= len(toks) or toks[i] != name:
                raise MacroImportError(f"LEF: MACRO {name} ends with the wrong name")
            break
        if kw == "PIN":
            if i >= len(toks) or not _BIT.fullmatch(toks[i]):
                raise MacroImportError("LEF: a pin's name is not a name or a bit")
            pname = toks[i]
            i += 1
            pin = _LefPin(pname, [], [])
            while True:
                if i >= len(toks):
                    raise MacroImportError(f"LEF: PIN {pname} does not end")
                pk = toks[i]
                i += 1
                if pk == "END":
                    if i >= len(toks) or toks[i] != pname:
                        raise MacroImportError(f"LEF: PIN {pname} ends with the wrong name")
                    i += 1
                    break
                if pk == "PORT":
                    geometry(pname, f"PIN {pname}", raw_shapes)
                    continue
                args = statement()
                if pk == "DIRECTION" and args and args[0] in _DIRECTIONS:
                    pin.lines.append(f"    DIRECTION {args[0]} ;")
                elif pk == "USE" and args and args[0] in _USES:
                    pin.use = args[0]
                    pin.lines.append(f"    USE {args[0]} ;")
                elif pk == "SHAPE" and args and args[0] in _SHAPES:
                    pin.lines.append(f"    SHAPE {args[0]} ;")
                elif pk in _ANTENNA and args:
                    value = _fmt(_number(args[0], f"PIN {pname}"))
                    layer = ""
                    if len(args) == 3 and args[1] == "LAYER" and args[2] in LEF_LAYERS:
                        layer = f" LAYER {args[2]}"
                    elif len(args) != 1:
                        raise MacroImportError(f"LEF: PIN {pname} has a malformed {pk}")
                    pin.lines.append(f"    {pk} {value}{layer} ;")
                # Anything else in a pin (TAPERRULE, NETEXPR, PROPERTY, ...) is not
                # geometry; it is left out.
            pins.append(pin)
        elif kw == "OBS":
            geometry("", "OBS", obs_shapes)
        else:
            args = statement()
            if kw == "ORIGIN":
                if len(args) != 2:
                    raise MacroImportError(f"LEF: ORIGIN of {name} needs two numbers")
                origin = (_number(args[0], "ORIGIN"), _number(args[1], "ORIGIN"))
            elif kw == "SIZE":
                if len(args) != 3 or args[1] != "BY":
                    raise MacroImportError(f"LEF: SIZE of {name} is not 'SIZE w BY h'")
                size = (_number(args[0], "SIZE"), _number(args[2], "SIZE"))
            elif kw == "CLASS" and args[:1] == ["BLOCK"]:
                cls = "BLOCK"
            elif kw == "CLASS":
                raise MacroImportError(f"LEF: {name} is CLASS {' '.join(args)[:40]}, not BLOCK")
            # FOREIGN, SYMMETRY, SITE, PROPERTY and the rest are rewritten or left out.
    if size is None or size[0] <= 0 or size[1] <= 0:
        raise MacroImportError(f"LEF: {name} has no SIZE")
    ox, oy = origin

    def moved(nums: list[float]) -> list[float]:
        return [round(v + (ox if k % 2 == 0 else oy), 4) for k, v in enumerate(nums)]

    by_pin: dict[str, list[tuple[str, str, list[float]]]] = {}
    for owner_layer, kind, nums in raw_shapes:
        owner, layer = owner_layer.split("\0")
        by_pin.setdefault(owner, []).append((layer, kind, moved(nums)))
    out = [
        "VERSION 5.7 ;",
        'BUSBITCHARS "[]" ;',
        'DIVIDERCHAR "/" ;',
        f"MACRO {name}",
        f"  CLASS {cls} ;",
        f"  FOREIGN {name} 0 0 ;",
        "  ORIGIN 0 0 ;",
        f"  SIZE {_fmt(size[0])} BY {_fmt(size[1])} ;",
        "  SYMMETRY X Y ;",
    ]
    for pin in pins:
        shapes = by_pin.get(pin.name, [])
        if not shapes:
            raise MacroImportError(f"LEF: PIN {pin.name} has no shape")
        out += [f"  PIN {pin.name}", *pin.lines, "    PORT"]
        layer = ""
        for lay, kind, nums in shapes:
            if lay != layer:
                out.append(f"      LAYER {lay} ;")
                layer = lay
            out.append(f"        {kind} {' '.join(_fmt(v) for v in nums)} ;")
            if kind == "RECT":
                x0, y0, x1, y1 = nums
                pin.shapes.append((lay, (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))))
            else:
                xs, ys = nums[0::2], nums[1::2]
                pin.shapes.append((lay, (min(xs), min(ys), max(xs), max(ys))))
        out += ["    END", f"  END {pin.name}"]
    if obs_shapes:
        out.append("  OBS")
        layer = ""
        for owner_layer, kind, nums in obs_shapes:
            lay = owner_layer.split("\0")[1]
            if lay != layer:
                out.append(f"    LAYER {lay} ;")
                layer = lay
            out.append(f"      {kind} {' '.join(_fmt(v) for v in moved(nums))} ;")
        out.append("  END")
    out += [f"END {name}", "END LIBRARY"]
    return "\n".join(out) + "\n", size, pins, origin


# GDS


def gds_cells(data: bytes) -> list[str]:
    """The names of the structures in a GDS stream."""
    names = []
    for rtype, body in _records(data):
        if rtype == 0x06:  # STRNAME
            names.append(body.rstrip(b"\0").decode("ascii", "replace"))
    return names


def _records(data: bytes) -> Iterable[tuple[int, bytes]]:
    i, n = 0, len(data)
    while i + 4 <= n:
        length, rtype = struct.unpack(">HB", data[i : i + 3])
        if length < 4 or i + length > n:
            raise MacroImportError("GDS: a record runs past the end of the file")
        yield rtype, data[i + 4 : i + length]
        i += length
        if rtype == 0x04:  # ENDLIB
            return


def shift_gds(data: bytes, cell: str, dx_um: float, dy_um: float) -> bytes:
    """``data`` with every element of structure ``cell`` moved by (dx, dy) um: the
    coordinates of its boundaries, paths, texts, boxes, nodes and references."""
    out = bytearray()
    i, n = 0, len(data)
    meters_per_dbu = 0.0
    inside = False
    dx = dy = 0
    while i + 4 <= n:
        length, rtype = struct.unpack(">HB", data[i : i + 3])
        if length < 4 or i + length > n:
            raise MacroImportError("GDS: a record runs past the end of the file")
        rec = data[i : i + length]
        i += length
        if rtype == 0x03 and length == 20:  # UNITS: user units, metres per database unit
            meters_per_dbu = _real8(rec[12:20])
            if not 0 < meters_per_dbu < 1:
                raise MacroImportError("GDS: the database unit is not a length")
            dx, dy = (round(v * 1e-6 / meters_per_dbu) for v in (dx_um, dy_um))
        elif rtype == 0x06:  # STRNAME
            inside = rec[4:].rstrip(b"\0").decode("ascii", "replace") == cell
        elif rtype == 0x07:  # ENDSTR
            inside = False
        elif rtype == 0x10 and inside and (dx or dy):  # XY
            count = (length - 4) // 8
            coords = list(struct.unpack(f">{2 * count}i", rec[4 : 4 + 8 * count]))
            for k in range(0, 2 * count, 2):
                coords[k] += dx
                coords[k + 1] += dy
            if any(not -(2**31) <= v < 2**31 for v in coords):
                raise MacroImportError("GDS: the shift takes a coordinate out of range")
            rec = rec[:4] + struct.pack(f">{2 * count}i", *coords)
        out += rec
        if rtype == 0x04:  # ENDLIB
            break
    if meters_per_dbu == 0.0:
        raise MacroImportError("GDS: no UNITS record")
    return bytes(out)


def check_gds_cells(names: list[str], cell: str) -> None:
    """Every cell of a macro's GDS carries its name, so none can replace a cell of the
    chip (a standard cell, an IO cell, the chip top) when the layouts are merged."""
    bad = [n for n in names if not (n == cell or n.startswith(cell + "_"))]
    if bad:
        raise MacroImportError(
            f"GDS cell {bad[0][:40]!r} must be {cell} or start with {cell}_, so it cannot "
            "replace a cell of the chip"
        )
    if cell not in names:
        raise MacroImportError(f"GDS: no cell {cell}")


def _real8(b: bytes) -> float:
    """A GDS eight-byte real: sign, excess-64 base-16 exponent, 56-bit mantissa."""
    sign = -1.0 if b[0] & 0x80 else 1.0
    exponent = (b[0] & 0x7F) - 64
    mantissa = int.from_bytes(b[1:8], "big") / float(1 << 56)
    return sign * mantissa * 16.0**exponent


# Liberty and SPICE


def liberty(cell: str, m: Manifest, width: float, height: float) -> str:
    """A liberty of the macro's pins from the manifest: supplies as pg_pins, the rest
    with their directions; no timing arcs, so a path through the macro is not timed."""
    from chipleap.chip.io import THRESHOLDS

    lines = [
        f"/* {cell}: written by ChipLeap from the package's manifest; no timing arcs. */",
        f"library ({cell}_chipleap) {{",
        "  delay_model : table_lookup ;",
        '  time_unit : "1ns" ;',
        '  voltage_unit : "1V" ;',
        '  current_unit : "1uA" ;',
        '  pulling_resistance_unit : "1kohm" ;',
        "  capacitive_load_unit (1, pf) ;",
        *THRESHOLDS,
        "  nom_process : 1 ;",
        "  nom_temperature : 25 ;",
        f"  nom_voltage : {CORE_VOLTAGE:g} ;",
        f"  voltage_map (VDD, {CORE_VOLTAGE:g}) ;",
        "  voltage_map (VSS, 0) ;",
    ]
    buses = sorted({p.width for p in m.pins if p.width is not None})
    for w in buses:
        lines += [
            f"  type (bus{w}) {{",
            "    base_type : array ;",
            "    data_type : bit ;",
            f"    bit_width : {w} ;",
            f"    bit_from : {w - 1} ;",
            "    bit_to : 0 ;",
            "    downto : true ;",
            "  }",
        ]
    lines += [
        f"  cell ({cell}) {{",
        f"    area : {width * height:g} ;",
        "    dont_touch : true ;",
        "    dont_use : true ;",
    ]
    for p in m.pins:
        if p.is_supply:
            kind = "primary_power" if p.kind == "power" else "primary_ground"
            volt = "VDD" if p.kind == "power" else "VSS"
            lines += [f"    pg_pin ({p.name}) {{", f"      pg_type : {kind} ;"]
            lines += [f"      voltage_name : {volt} ;", "    }"]
            continue
        body = [f"      direction : {p.direction} ;"]
        if p.direction != "output":
            body.append("      capacitance : 0.005 ;")
        if p.kind == "clock":
            body.append("      clock : true ;")
        if p.width is None:
            lines += [f"    pin ({p.name}) {{", *body, "    }"]
        else:
            lines += [f"    bus ({p.name}) {{", f"      bus_type : bus{p.width} ;"]
            lines += [*body, "    }"]
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


_DEVICE = re.compile(r"[MRCXDQmrcxdq][A-Za-z0-9_.$#\[\]<>:/-]*")
SPICE_WORD = re.compile(r"[A-Za-z0-9_.$#\[\]<>:/=+*-]+")


def reduce_spice(text: str, cell: str) -> tuple[str, list[str]]:
    """The ``.subckt`` blocks of a SPICE file, with comments and device lines only (M,
    R, C, X, D, Q). Includes, libraries, parameters, models and control blocks are not
    copied: IHP's LVS deck reads the result with the PDK's own models. Every
    subcircuit must be ``cell`` or start with ``cell_``. Returns the text and the
    subcircuits' names."""
    if len(text) > LIMITS.view_bytes:
        raise MacroImportError("SPICE: the file is too large")
    pieces: list[list[str]] = []  # each line with its continuations, joined once
    for raw in text.replace("\r\n", "\n").split("\n"):
        s = raw.strip()
        if s.startswith("+") and pieces:
            pieces[-1].append(s[1:].strip())
        else:
            pieces.append([s])
    lines = [" ".join(p) for p in pieces]
    if len(lines) > LIMITS.tokens:
        raise MacroImportError("SPICE: the file has too many lines")
    out: list[str] = []
    names: list[str] = []
    current = ""
    for k, line in enumerate(lines, 1):
        if not line or (line.startswith("*") and not current):
            continue
        words = line.split()
        head = words[0].lower()
        if not current:
            if head == ".subckt":
                if len(words) < 2 or not (words[1] == cell or words[1].startswith(cell + "_")):
                    raise MacroImportError(
                        f"SPICE line {k}: subcircuit {words[1][:40] if len(words) > 1 else ''!r} "
                        f"must be {cell} or start with {cell}_"
                    )
                current = words[1]
                names.append(current)
                out.append(_spice_line(words, k))
            # Lines outside a subcircuit (.end, .include, .param, options) stay out.
            continue
        if head.startswith(".ends"):
            out.append(f".ends {current}")
            current = ""
            continue
        if line.startswith("*"):
            if line.lower().startswith("*.pininfo"):
                out.append(_spice_line(words, k))
            continue
        if not _DEVICE.fullmatch(words[0]):
            raise MacroImportError(
                f"SPICE line {k}: {words[0][:40]!r} inside .subckt {current}; ChipLeap copies "
                "only device lines (M, R, C, X, D, Q) and comments"
            )
        out.append(_spice_line(words, k))
    if current:
        raise MacroImportError(f"SPICE: .subckt {current} does not end")
    if cell not in names:
        raise MacroImportError(f"SPICE: no .subckt {cell}")
    return "\n".join(out) + "\n", names


def _spice_line(words: list[str], k: int) -> str:
    for w in words:
        if not SPICE_WORD.fullmatch(w):
            raise MacroImportError(f"SPICE line {k}: {w[:40]!r} is not a plain SPICE word")
    return " ".join(words)


# Import


@dataclass(frozen=True)
class Imported:
    views: dict[str, MacroView]  # cell -> views
    cells: dict[str, str]  # instance -> cell


def import_macros(
    macros: Iterable[ChipMacro],
    files_in: Lister,
    read: Reader,
    work: Path,
    pdk: str,
) -> Imported:
    """Copy, check and convert every package the chip's macros name. ``files_in`` and
    ``read`` are the project's (``Project.files_in``, ``Project.read``), ``work`` the
    chip's build directory, ``pdk`` what the packages are checked against."""
    views: dict[str, MacroView] = {}
    by_package: dict[Path, str] = {}
    cells: dict[str, str] = {}
    for m in macros:
        key = m.package.resolve()
        if key in by_package:
            cells[m.instance] = by_package[key]
            continue
        try:
            view = _import(m, files_in, read, work, pdk)
        except MacroImportError as exc:
            raise MacroImportError(f"macro {m.instance} ({m.package.name}): {exc}") from None
        if view.cell in views:
            raise MacroImportError(
                f"macro {m.instance}: two packages hold a cell named {view.cell}"
            )
        views[view.cell] = view
        by_package[key] = view.cell
        cells[m.instance] = view.cell
    return Imported(views, cells)


def _import(m: ChipMacro, files_in: Lister, read: Reader, work: Path, pdk: str) -> MacroView:
    base = builddir.work_dir(work, "macros", m.instance, empty=True)
    # The copy keeps the package's directory name, which macro/v1 checks.
    copy = builddir.work_dir(base, m.package.name)
    _copy_package(read, m.package, files_in(m.package), copy)
    manifest, report = check_copy(copy, pdk)
    cell = manifest.name
    for d in manifest.domains:
        if abs(d.voltage - CORE_VOLTAGE) > 1e-6:
            raise MacroImportError(
                f"domain {d.name} is {d.voltage:g} V; ChipLeap powers macros from the "
                f"{CORE_VOLTAGE:g} V core supply"
            )
    lef_text, size, lef_pins, origin = rewrite_lef(
        (copy / manifest.file("lef")).read_text(encoding="ascii", errors="replace"), cell
    )
    gds_data = (copy / manifest.file("gds")).read_bytes()
    names = gds_cells(gds_data)
    check_gds_cells(names, cell)
    spice_text, subckts = reduce_spice(
        (copy / manifest.file("spice")).read_text(encoding="ascii", errors="replace"), cell
    )
    # The pins: every bit the manifest lists must have a shape in the LEF.
    shapes = {p.name: p for p in lef_pins}
    pins = []
    for p in manifest.pins:
        for bit in p.bits():
            lp = shapes.get(bit)
            if lp is None or not lp.shapes:
                raise MacroImportError(f"LEF: pin {bit} of the manifest has no shape")
            pins.append(MacroPin(bit, p.direction, p.kind, tuple(lp.shapes)))
    views_dir = builddir.work_dir(base, "views", empty=True)
    lef = builddir.write_text(views_dir, f"{cell}.lef", lef_text)
    gds = builddir.write_bytes(views_dir, f"{cell}.gds", shift_gds(gds_data, cell, *origin))
    lib = builddir.write_text(views_dir, f"{cell}.lib", liberty(cell, manifest, *size))
    spice = builddir.write_text(views_dir, f"{cell}.spice", spice_text)
    return MacroView(
        cell=cell,
        kind=manifest.kind,
        width=size[0],
        height=size[1],
        pins=tuple(pins),
        lef=lef,
        gds=gds,
        lib=lib,
        spice=spice,
        subckts=tuple(subckts),
        gds_cells=tuple(names),
        report=report,
    )
