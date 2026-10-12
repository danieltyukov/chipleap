"""The ``chip`` section of project.yaml: the pad ring and the macros of a chip.

``chipleap.rtl.project`` parses the section into these classes and checks every value
there, as it does for every section; nothing here reads a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SIDES = ("south", "east", "north", "west")
SUPPLIES = ("vdd", "vss", "iovdd", "iovss")
ORIENTATIONS = ("N", "S", "FN", "FS")
PAD_PITCH_MIN = 90.0  # below this a package's bond wires come too close
PAD_PITCH_MAX = 1000.0
SLOTS_MAX = 64  # pads on one side
MACROS_MAX = 32
CORE_MAX_UM = 10000.0
# A bit of a core port: a name, or a name with one index.
PORT_BIT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d{1,4})\])?")


@dataclass(frozen=True)
class Pad:
    """One slot of the pad ring. ``kind`` is ``signal`` (``net`` is a bit of the core's
    ports), ``analog`` (``net`` is a net macros name), a supply (vdd, vss, iovdd, iovss)
    or ``empty``."""

    kind: str
    net: str = ""
    resistor: bool = False  # analog: through padres, the secondary protection


@dataclass(frozen=True)
class ChipMacro:
    instance: str
    package: Path
    orientation: str = "N"
    at_um: tuple[float, float] | None = None  # lower left, from the core's lower left
    connect: dict[str, str] = field(default_factory=dict)  # pin -> net


@dataclass(frozen=True)
class ChipConfig:
    name: str
    pads: dict[str, tuple[Pad, ...]]  # side -> pads in increasing x (south, north) or y
    macros: tuple[ChipMacro, ...] = ()
    pad_pitch_um: float = 100.0
    core_um: tuple[float, float] | None = None

    def all_pads(self) -> list[tuple[str, int, Pad]]:
        return [(side, i, p) for side in SIDES for i, p in enumerate(self.pads.get(side, ()))]


def port_bit(text: str) -> tuple[str, int | None] | None:
    """``("tx_data_i", 3)`` for ``tx_data_i[3]``, ``("clk_i", None)`` for ``clk_i``."""
    m = PORT_BIT_RE.fullmatch(text)
    if m is None:
        return None
    return m.group(1), (int(m.group(2)) if m.group(2) is not None else None)
