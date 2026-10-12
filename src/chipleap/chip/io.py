"""IHP SG13G2's IO library (``libs.ref/sg13g2_io``) as a chip uses it.

The ring's geometry comes from the library's LEF, read here: each cell's size and the
shapes of its pins, so pin positions are never typed in by hand. Two views are written
for the flow: a copy of the LEF in which ``sg13g2_IOPadAnalog``'s ``pad`` pin keeps only
its core-side port (with the bond-side one, the router once reached the pin through the
ring and shorted its rails), and a liberty of every cell the chip uses, which LibreLane's
synthesis reads as black boxes and OpenSTA as cells without timing arcs (the library's
own liberty files lack ``sg13g2_IOPadAnalog`` in the PDK LibreLane uses, and a liberty
without thresholds stops OpenSTA).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from chipleap.verify.tools import ihp_liberty

PDK_ENV = "CHIPLEAP_IHP_PDK"
IO = "sg13g2_io"
PAD_CELLS = {
    "in": "sg13g2_IOPadIn",
    "out": "sg13g2_IOPadOut4mA",
    "analog": "sg13g2_IOPadAnalog",
    "vdd": "sg13g2_IOPadVdd",
    "vss": "sg13g2_IOPadVss",
    "iovdd": "sg13g2_IOPadIOVdd",
    "iovss": "sg13g2_IOPadIOVss",
}
CORNER = "sg13g2_Corner"
# Fillers by width in um, widest first: any gap of whole micrometres decomposes exactly.
FILLERS = (
    (50, "sg13g2_Filler10000"),
    (20, "sg13g2_Filler4000"),
    (10, "sg13g2_Filler2000"),
    (5, "sg13g2_Filler1000"),
    (2, "sg13g2_Filler400"),
    (1, "sg13g2_Filler200"),
)
SUPPLY_PINS = ("vdd", "vss", "iovdd", "iovss")
CORE_EDGE_UM = 179.0  # a pin port reaching this far from the bond side is on the core side
LEF_MAX_BYTES = 8 << 20

Rect = tuple[float, float, float, float]


class IoError(ValueError):
    pass


@dataclass
class IoCell:
    name: str
    width: float
    height: float
    # pin -> ports -> (layer, rect) shapes, in the cell's own coordinates
    pins: dict[str, list[list[tuple[str, Rect]]]] = field(default_factory=dict)
    directions: dict[str, str] = field(default_factory=dict)  # pin -> INPUT/OUTPUT/INOUT
    uses: dict[str, str] = field(default_factory=dict)  # pin -> SIGNAL/POWER/GROUND

    def core_ports(self, pin: str) -> list[list[tuple[str, Rect]]]:
        """The pin's ports that reach the core-side edge."""
        return [
            port for port in self.pins.get(pin, []) if any(r[3] >= CORE_EDGE_UM for _, r in port)
        ]


def pdk_dir() -> Path | None:
    """The ``ihp-sg13g2`` directory of the PDK ChipLeap uses: ``CHIPLEAP_IHP_PDK`` (an
    absolute path), else the one the standard-cell liberty is in. None without one that
    holds the IO library and KLayout's technology."""
    given = os.environ.get(PDK_ENV, "")
    if given:
        if not os.path.isabs(given):
            return None
        root = Path(given)
    else:
        lib = ihp_liberty()
        if lib is None:
            return None
        root = lib.resolve().parents[3]  # .../ihp-sg13g2/libs.ref/sg13g2_stdcell/lib/x.lib
    io = root / "libs.ref" / IO
    needed = [io / "lef" / f"{IO}.lef", io / "gds" / f"{IO}.gds", io / "cdl" / f"{IO}.cdl"]
    if not all(p.is_file() for p in needed) or not (root / "libs.tech" / "klayout").is_dir():
        return None
    return root


def pdk_commit(root: Path) -> str:
    """The git commit of the PDK checkout ``root`` is in, read from ``.git`` without
    running git; "" when it is not a checkout."""
    for d in (root.parent, root):
        head = d / ".git" / "HEAD"
        if not head.is_file():
            continue
        text = head.read_text(errors="replace").strip()
        if re.fullmatch(r"[0-9a-f]{40}", text):
            return text
        m = re.fullmatch(r"ref: (refs/[A-Za-z0-9_./-]+)", text)
        if m is None or ".." in m.group(1):
            return ""
        ref = d / ".git" / m.group(1)
        if ref.is_file():
            sha = ref.read_text(errors="replace").strip()
            return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""
        packed = d / ".git" / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(errors="replace").splitlines():
                parts = line.split()
                if (
                    len(parts) == 2
                    and parts[1] == m.group(1)
                    and re.fullmatch(r"[0-9a-f]{40}", parts[0])
                ):
                    return parts[0]
        return ""
    return ""


def lef_path(root: Path) -> Path:
    return root / "libs.ref" / IO / "lef" / f"{IO}.lef"


def read_lef(text: str) -> dict[str, IoCell]:
    """The cells of the IO library's LEF: sizes and pin shapes. The PDK's LEF is trusted,
    but the reader still only understands what it needs and refuses the rest."""
    if len(text) > LEF_MAX_BYTES:
        raise IoError("the IO LEF is larger than expected")
    cells: dict[str, IoCell] = {}
    toks = re.findall(r"[^\s;]+|;", re.sub(r"#[^\n]*", "", text))
    i = 0
    n = len(toks)

    def number(k: int) -> float:
        try:
            return float(toks[k])
        except (IndexError, ValueError):
            raise IoError(f"a number expected near token {k} of the IO LEF") from None

    while i < n:
        if toks[i] != "MACRO":
            i += 1
            continue
        name = toks[i + 1]
        cell = IoCell(name, 0.0, 0.0)
        i += 2
        while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == name):
            t = toks[i]
            if t == "SIZE":
                cell.width, cell.height = number(i + 1), number(i + 3)
                i += 4
            elif t == "PIN":
                pin = toks[i + 1]
                i += 2
                layer = ""
                ports: list[list[tuple[str, Rect]]] = []
                while i < n and not (toks[i] == "END" and i + 1 < n and toks[i + 1] == pin):
                    u = toks[i]
                    if u == "DIRECTION":
                        cell.directions[pin] = toks[i + 1]
                    elif u == "USE":
                        cell.uses[pin] = toks[i + 1]
                    elif u == "PORT":
                        ports.append([])
                    elif u == "LAYER":
                        layer = toks[i + 1]
                    elif u == "RECT" and ports:
                        r = (number(i + 1), number(i + 2), number(i + 3), number(i + 4))
                        ports[-1].append((layer, r))
                        i += 4
                    i += 1
                cell.pins[pin] = ports
                i += 2
            else:
                i += 1
        if cell.width <= 0 or cell.height <= 0:
            raise IoError(f"{name} has no size in the IO LEF")
        cells[name] = cell
        i += 2
    return cells


def chip_lef(text: str) -> str:
    """The IO LEF with ``sg13g2_IOPadAnalog``'s bond-side ``pad`` port removed."""
    cell = PAD_CELLS["analog"]
    a0 = text.find(f"MACRO {cell}\n")
    a1 = text.find(f"END {cell}", a0)
    if a0 < 0 or a1 < 0:
        raise IoError(f"no {cell} in the IO LEF")
    block = text[a0:a1]
    p0 = block.find("  PIN pad\n")
    p1 = block.find("  END pad\n", p0)
    if p0 < 0 or p1 < 0:
        raise IoError(f"{cell} has no pad pin in the IO LEF")
    pin = block[p0:p1]
    ports = re.findall(r"    PORT\n.*?    END\n", pin, re.S)
    bond = [
        p
        for p in ports
        if all(
            float(top) < CORE_EDGE_UM
            for top in re.findall(r"RECT [\d.]+ [\d.]+ [\d.]+ ([\d.]+)", p)
        )
    ]
    if len(bond) != 1 or len(ports) != 2:
        raise IoError(f"{cell}'s pad pin is not one bond-side and one core-side port")
    return text[:a0] + block[:p0] + pin.replace(bond[0], "") + block[p1:] + text[a1:]


THRESHOLDS = (
    "  input_threshold_pct_rise : 50 ;",
    "  input_threshold_pct_fall : 50 ;",
    "  output_threshold_pct_rise : 50 ;",
    "  output_threshold_pct_fall : 50 ;",
    "  slew_lower_threshold_pct_rise : 20 ;",
    "  slew_lower_threshold_pct_fall : 20 ;",
    "  slew_upper_threshold_pct_rise : 80 ;",
    "  slew_upper_threshold_pct_fall : 80 ;",
    "  slew_derate_from_library : 1 ;",
)


def liberty(name: str, cells: list[IoCell], voltages: dict[str, float]) -> str:
    """A liberty of ``cells``: area, supply pins with their voltages, signal pins with
    their directions, no timing. Every name was checked by the caller or comes from
    the PDK; numbers are printed as numbers."""
    lines = [
        f"/* {name}: written by ChipLeap for synthesis and timing; no timing arcs. */",
        f"library ({name}) {{",
        "  delay_model : table_lookup ;",
        '  time_unit : "1ns" ;',
        '  voltage_unit : "1V" ;',
        '  current_unit : "1uA" ;',
        '  pulling_resistance_unit : "1kohm" ;',
        "  capacitive_load_unit (1, pf) ;",
        *THRESHOLDS,
        "  nom_process : 1 ;",
        "  nom_temperature : 25 ;",
        f"  nom_voltage : {voltages.get('vdd', 1.2):g} ;",
    ]
    lines += [f"  voltage_map ({p}, {v:g}) ;" for p, v in sorted(voltages.items())]
    for c in cells:
        lines += [
            f"  cell ({c.name}) {{",
            f"    area : {c.width * c.height:g} ;",
            "    dont_touch : true ;",
            "    dont_use : true ;",
        ]
        for pin in sorted(c.pins):
            use = c.uses.get(pin, "SIGNAL")
            if use in ("POWER", "GROUND"):
                kind = "primary_power" if use == "POWER" else "primary_ground"
                lines += [f"    pg_pin ({pin}) {{", f"      pg_type : {kind} ;"]
                lines += [f"      voltage_name : {pin} ;", "    }"]
                continue
            direction = {"INPUT": "input", "OUTPUT": "output"}.get(
                c.directions.get(pin, ""), "inout"
            )
            lines += [f"    pin ({pin}) {{", f"      direction : {direction} ;"]
            if direction != "output":
                lines.append("      capacitance : 0.005 ;")
            lines.append("    }")
        lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def fillers(gap_um: int) -> list[tuple[int, str]]:
    """Filler cells that fill ``gap_um`` exactly, widest first."""
    if gap_um < 0:
        raise IoError(f"a negative gap of {gap_um} um in the pad ring")
    out = []
    for width, cell in FILLERS:
        while gap_um >= width:
            out.append((width, cell))
            gap_um -= width
    return out
