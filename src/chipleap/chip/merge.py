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
itself makes (the chip top and its vias) counted as a source of their own. An RTL
project with macros (``chipleap.rtl.macros``) goes through the same checks, its top in
the chip top's place and without IO cells.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chipleap.macro.gds import GdsError, structure_identities
from chipleap.macro.layer import GDS_NAME

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


def gds_names(data: bytes) -> dict[str, str]:
    """Each structure of a GDS stream with its identity (its records and the file's
    units, not the dates in its header), as macro/v1 defines identical structures
    (``chipleap.macro.gds.structure_identities``). A name that is not plain is an error,
    and so is one defined twice, differently, in one file."""
    out: dict[str, str] = {}
    try:
        for raw, digest in structure_identities(data):
            if not GDS_NAME.fullmatch(raw):
                raise MergeError(f"GDS cell name {raw[:40]!r} is not plain")
            current = raw.decode()
            if current in out and out[current] != digest:
                raise MergeError(f"GDS cell {current} is defined twice in one file")
            out[current] = digest
    except GdsError as exc:
        raise MergeError(f"GDS: {exc}") from None
    return out


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
