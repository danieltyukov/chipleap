"""Names where the chip is merged: one name, one definition.

The chip's layout and its LVS netlist are put together by cell name: LibreLane's stream-out
reads the standard cells', the IO cells' and every macro's GDS into one layout, and the
LVS netlist holds the standard cells' and every macro's subcircuits. A name two sources
define is resolved by whichever the tool reads last, so a package could replace a
standard cell, an IO cell or a cell the flow writes. ``chipleap.macro.layer`` refuses the
names it can foresee when a package is imported; this module is the guarantee. It reads
what was actually merged, from LibreLane's resolved configuration, and refuses any name
defined twice, unless both definitions come from the same PDK file or are identical.

Three merges are checked: the Verilog modules (the core's own, the macros', the IO
cells' and the chip top) before LibreLane runs, and after it the GDS structures and the
SPICE subcircuits (case folded, as SPICE reads them), with the cells the stream-out
itself makes (the chip top and its vias) counted as a source of their own. The macros'
own GDS structures and subcircuits are checked before LibreLane runs too
(``check_macros``), so two macros that clash stop a run in seconds, not after its
layout. An RTL project with macros (``chipleap.rtl.macros``) goes through the same
checks, its top in the chip top's place and without IO cells.

Two GDS structures are identical when they hold the same layout (``gds_names``), not
only the same records: IHP's SRAM macros each carry their own copy of the same library
cells, written in another order and placing vias that the generator named per macro.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.macro.layer import GDS_NAME, MacroView

FILE_MAX = 1 << 30
MODULES_MAX = 100_000


class MergeError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    """What one input defines: each name with a digest of its content ("" when the
    content is not compared), and the file it is read from."""

    label: str
    names: Mapping[str, str]
    path: Path | None = None


def unique(sources: Iterable[Source], fold: bool, what: str) -> None:
    """Refuse a name two sources define, unless both are the same file or the two
    definitions are identical. ``fold`` compares names without case, as SPICE does."""
    seen: dict[str, tuple[Source, str, str]] = {}
    for src in sources:
        for name, digest in src.names.items():
            key = name.casefold() if fold else name
            if key in seen:
                other, other_name, other_digest = seen[key]
                same_file = src.path is not None and src.path == other.path
                same_content = bool(digest) and digest == other_digest
                if not (same_file or same_content):
                    raise MergeError(
                        f"{what} {name} is defined by {src.label} and, as {other_name}, by "
                        f"{other.label}: one would replace the other"
                    )
                continue
            seen[key] = (src, name, digest)


def _read(path: Path) -> bytes:
    if path.stat().st_size > FILE_MAX:
        raise MergeError(f"{path.name} is larger than ChipLeap reads")
    return path.read_bytes()


# GDS record types: UNITS, ENDLIB, BGNSTR, STRNAME, ENDSTR, BOUNDARY, XY, ENDEL, SNAME,
# PRESENTATION, and the records that begin an element (BOUNDARY, PATH, SREF, AREF, TEXT,
# NODE, BOX).
_UNITS, _ENDLIB, _BGNSTR, _STRNAME, _ENDSTR = 0x03, 0x04, 0x05, 0x06, 0x07
_BOUNDARY, _XY, _ENDEL, _SNAME, _PRESENTATION = 0x08, 0x10, 0x11, 0x12, 0x17
_ELEMENTS = frozenset({0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x15, 0x2D})
# The GDSII stream format numbers its record types from HEADER (0x00) to LIBSECUR (0x3B).
# A record of any other type is not GDSII: what it holds is unknown, so it is refused.
_LAST_TYPE = 0x3B


@dataclass
class _Structure:
    head: list[bytes]  # its records outside its elements, but BGNSTR and STRNAME
    elements: list[list[bytes]]

    def placed(self) -> list[bytes]:
        return [r[4:].rstrip(b"\0") for el in self.elements for r in el if r[2] == _SNAME]


def _structures(data: bytes) -> tuple[bytes | None, dict[bytes, _Structure]]:
    """The UNITS record (None unless it is the stream's only one, before any structure)
    and each structure by its name as written. A record that runs past the end, an
    element outside a structure or a structure defined twice, differently, is an error."""
    pos, n = 0, len(data)
    units: list[bytes] = []
    units_first = False
    out: dict[bytes, _Structure] = {}
    cur: _Structure | None = None
    name = b""
    element: list[bytes] | None = None
    while pos + 4 <= n:
        length, rtype = (data[pos] << 8) | data[pos + 1], data[pos + 2]
        if length < 4 or pos + length > n:
            raise MergeError(f"GDS: bad record length {length} at byte {pos}")
        if rtype > _LAST_TYPE:
            raise MergeError(f"GDS: unknown record type 0x{rtype:02x} at byte {pos}")
        record = data[pos : pos + length]
        pos += length
        if rtype == _ENDLIB:
            break
        if rtype == _UNITS:
            units_first = units_first or (not units and not out and cur is None)
            units.append(record)
        elif rtype == _BGNSTR:
            cur, name, element = _Structure([], []), b"", None
        elif cur is None:
            if rtype in _ELEMENTS:
                raise MergeError(f"GDS: an element outside a structure at byte {pos - length}")
        elif element is not None:
            if rtype == _ENDSTR:
                raise MergeError(f"GDS: an element without its end at byte {pos - length}")
            element.append(record)
            if rtype == _ENDEL:
                cur.elements.append(element)
                element = None
        elif rtype in _ELEMENTS:
            element = [record]
        elif rtype == _ENDSTR:
            if name in out and out[name] != cur:
                raise MergeError(f"GDS cell {name.decode('latin-1')} is defined twice in one file")
            out[name] = cur
            cur = None
        elif rtype == _STRNAME and not name:
            name = record[4:].rstrip(b"\0")
        else:
            cur.head.append(record)
    return (units[0] if len(units) == 1 and units_first else None), out


def _outline(record: bytes) -> bytes:
    """A BOUNDARY's XY with its vertices in one order, whichever vertex the file starts
    the polygon at and whichever way round it goes."""
    body = record[4:]
    if len(body) % 8:
        raise MergeError("GDS: an XY record has an odd number of coordinates")
    vertices = list(struct.iter_unpack(">ii", body))
    if len(vertices) > 1 and vertices[0] == vertices[-1]:
        vertices.pop()
    if not vertices:
        return b""
    low = min(vertices)
    ways = (vertices, vertices[::-1])
    starts = [(w, i) for w in ways for i, v in enumerate(w) if v == low]
    best = min(w[i:] + w[:i] for w, i in starts)
    return struct.pack(f">{2 * len(best)}i", *(c for v in best for c in v))


def _item(tag: bytes, payload: bytes) -> bytes:
    """One item of what a layout's identity is computed from: a tag of one byte for its
    kind and its length before its bytes, so that two different sequences of items are
    never the same bytes, whatever the items hold."""
    return tag + struct.pack(">Q", len(payload)) + payload


def _element(records: list[bytes], ids: Mapping[bytes, str]) -> bytes:
    """The digest of one element: its records, with the structure it places by that
    structure's identity, not its name, its outline in one vertex order when it is a
    BOUNDARY, and without its text's PRESENTATION (font and justification, which no
    check reads). Each is an item of its own kind (``_item``)."""
    h = hashlib.sha256()
    boundary = records[0][2] == _BOUNDARY
    for r in records:
        if r[2] == _PRESENTATION:
            continue
        if r[2] == _SNAME:
            placed = r[4:].rstrip(b"\0")
            ref = ids.get(placed)
            h.update(_item(b"S", ref.encode()) if ref else _item(b"N", placed))
        elif r[2] == _XY and boundary:
            h.update(_item(b"O", _outline(r)))
        else:
            h.update(_item(b"R", r))
    return h.digest()


def gds_names(data: bytes) -> dict[str, str]:
    """Each structure of a GDS stream with the identity of its layout: the file's UNITS
    record, the structure's records outside its elements (not the dates in BGNSTR, but
    STRCLASS and its properties), and the digests of its elements in any order, each
    with the structure it places by that structure's identity, so the whole hierarchy
    under a structure counts and the names of the structures in it do not. Every part is
    an item with its kind and length (``_item``), so two different layouts never hash the
    same bytes. In a file whose UNITS record is not its only one, before its first
    structure, every identity is "", which matches nothing. A name that is not plain is an
    error, and so are a record type GDSII does not have, a structure that contains itself
    and one defined twice, differently, in one file."""
    units, structures = _structures(data)
    for raw in structures:
        if not GDS_NAME.fullmatch(raw):
            raise MergeError(f"GDS cell name {raw[:40]!r} is not plain")
    if units is None:
        return {raw.decode(): "" for raw in structures}
    ids: dict[bytes, str] = {}
    # Depth first without recursion: a structure's identity once those it places have one.
    for top in structures:
        stack, open_ = [(top, False)], set()
        while stack:
            name, ready = stack.pop()
            if name in ids:
                continue
            s = structures[name]
            if ready:
                h = hashlib.sha256(_item(b"U", units))
                for r in s.head:
                    h.update(_item(b"H", r))
                for d in sorted(_element(el, ids) for el in s.elements):
                    h.update(_item(b"E", d))
                ids[name] = h.hexdigest()
                continue
            if name in open_:
                raise MergeError(f"GDS cell {name.decode()} contains itself")
            open_.add(name)
            stack.append((name, True))
            stack += [(c, False) for c in s.placed() if c in structures and c not in ids]
    return {raw.decode(): ids[raw] for raw in structures}


def gds_source(label: str, path: Path) -> Source:
    return Source(label, gds_names(_read(path)), path.resolve())


SUBCKT = re.compile(r"^\s*\.subckt\s+(\S+)(.*?)^\s*\.ends\b", re.I | re.M | re.S)


def spice_names(text: str) -> dict[str, str]:
    """Each subcircuit of a SPICE or CDL file with a digest of its text."""
    joined = re.sub(r"\n\+", " ", text.replace("\r\n", "\n"))
    out: dict[str, str] = {}
    for m in SUBCKT.finditer(joined):
        body = " ".join(m.group(2).split())
        out[m.group(1)] = hashlib.sha256(body.encode()).hexdigest()
    return out


def spice_source(label: str, path: Path) -> Source:
    return Source(label, spice_names(_read(path).decode(errors="replace")), path.resolve())


# Verilog, before LibreLane


# The Yosys command that lists every module of the sources it has read, black boxes and
# empty modules (which read_verilog makes black boxes) included, and every member of
# each as ``module/member``. It runs before ``hierarchy``, which removes the modules the
# top does not instantiate: one of those named like a macro would still replace the
# macro's black box where LibreLane reads both.
LIST_MODULES = "select -list =*"


def listed_modules(text: str) -> list[str]:
    """Module names from ``LIST_MODULES``, as the source declares them. A member's line
    has a slash; a module whose name has one is left out, and could not be named like a
    macro, whose cell name is a plain identifier."""
    names: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if name.startswith("$paramod\\"):
            name = name.split("\\")[1]
        name = name.lstrip("\\")
        if name and "/" not in name and not name.startswith("$"):
            names.append(name)
            if len(names) > MODULES_MAX:
                raise MergeError(f"the RTL has more than {MODULES_MAX} modules")
    return names


def check_modules(
    core: Iterable[str],
    views: Iterable[str],
    chip_name: str = "",
    io_cells: Iterable[str] = (),
) -> None:
    """The modules of the design LibreLane synthesizes: the core's RTL, each macro's
    (``views``, by cell), and for a chip the chip top and the IO cells. A macro named
    like a module of the RTL would be bound to the RTL instead of the macro, silently."""
    sources = [
        Source("the core's RTL", dict.fromkeys(dict.fromkeys(core), "")),
        *([Source("the chip top", {chip_name: ""})] if chip_name else []),
        Source("the IO library", dict.fromkeys(io_cells, "")),
        *(Source(f"macro {cell}", {cell: ""}) for cell in views),
    ]
    unique(sources, fold=False, what="module")


# The macros' layouts and netlists, before LibreLane


def check_macros(views: Iterable[MacroView]) -> None:
    """The GDS structures and subcircuits of the macros a flow places, from their own
    files, before LibreLane runs: what ``check_merges`` would refuse among them after
    the layout, hours later. The standard cells, the IO cells and what the stream-out
    makes are LibreLane's, and checked after it."""
    every = list({v.cell: v for v in views}.values())
    unique((gds_source(f"macro package {v.cell}", v.gds) for v in every), False, "GDS cell")
    unique((spice_source(f"macro package {v.cell}", v.spice) for v in every), True, "subcircuit")


# Layout and netlist, after LibreLane


def _paths(value: Any) -> list[Path]:
    items = value if isinstance(value, list) else [value]
    return [Path(v) for v in items if isinstance(v, str)]


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def via_names(def_text: str, tech_lefs: Iterable[Path]) -> set[str]:
    """The vias the routed DEF uses or defines, by their own names and as KLayout's
    stream-out names their cells (``VIA_<name>``)."""
    names: set[str] = set()
    vias = re.search(r"^VIAS\s+\d+\s*;(.*?)^END VIAS", def_text, re.M | re.S)
    if vias is not None:
        names.update(re.findall(r"^\s*-\s+(\S+)", vias.group(1), re.M))
    for lef in tech_lefs:
        text = _read(lef).decode(errors="replace")
        names.update(re.findall(r"^\s*VIA\s+(\S+)", text, re.M | re.I))
    return names | {f"VIA_{n}" for n in names}


def check_merges(resolved: Path, flow_dir: Path, chip_name: str, def_text: str) -> dict[str, int]:
    """The GDS structures and SPICE subcircuits LibreLane merged, from its resolved
    configuration: the standard cells' files must be the PDK's LibreLane installed,
    every other file one ChipLeap put in the flow's directory, and no name may have two
    definitions. Returns how many names each merge held."""
    data = json.loads(_read(resolved))
    pdk_root = Path(str(data.get("PDK_ROOT", "")))
    if not pdk_root.is_absolute() or not pdk_root.is_dir():
        raise MergeError("LibreLane's resolved configuration names no PDK")
    gds: list[Source] = []
    spice: list[Source] = []
    for path in _paths(data.get("CELL_GDS")):
        _trusted(path, pdk_root)
        gds.append(gds_source(f"the standard cells ({path.name})", path))
    for path in _paths(data.get("CELL_CDLS")):
        _trusted(path, pdk_root)
        spice.append(spice_source(f"the standard cells ({path.name})", path))
    ours = [*_paths(data.get("EXTRA_GDS")), *_paths(data.get("EXTRA_CDLS"))]
    macros = data.get("MACROS") or {}
    if not isinstance(macros, dict):
        raise MergeError("LibreLane's resolved MACROS is not a mapping")
    for entry in macros.values():
        entry = entry if isinstance(entry, dict) else {}
        ours += _paths(entry.get("gds")) + _paths(entry.get("spice"))
    for path in dict.fromkeys(ours):
        if not _inside(path, flow_dir):
            raise MergeError(f"LibreLane merged {path.name}, which ChipLeap did not put there")
        if path.suffix == ".gds":
            gds.append(gds_source(_label(path, flow_dir), path))
        else:
            spice.append(spice_source(_label(path, flow_dir), path))
    techs = data.get("TECH_LEFS") or {}
    tech_lefs = [p for v in (techs.values() if isinstance(techs, dict) else []) for p in _paths(v)]
    for path in tech_lefs:
        _trusted(path, pdk_root)
    made = {chip_name: ""} | dict.fromkeys(via_names(def_text, dict.fromkeys(tech_lefs)), "")
    gds.append(Source("the stream-out (the chip top and its vias)", made))
    spice.append(Source("the chip top's netlist", {chip_name: ""}))
    unique(gds, fold=False, what="GDS cell")
    unique(spice, fold=True, what="subcircuit")
    return {
        "gds_cells": sum(len(s.names) for s in gds),
        "subcircuits": sum(len(s.names) for s in spice),
    }


def _trusted(path: Path, pdk_root: Path) -> None:
    if not _inside(path, pdk_root) or not path.is_file():
        raise MergeError(f"LibreLane read {path.name} from outside its PDK")


def _label(path: Path, flow_dir: Path) -> str:
    rel = path.resolve().relative_to(flow_dir.resolve()).as_posix()
    if rel.startswith("macros/"):
        return f"macro package {path.stem}"
    return f"the IO library ({path.name})" if rel.startswith("io/") else rel
