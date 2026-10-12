"""Stand-ins for the chip tests: an IO library LEF with the outlines and pin shapes of IHP's
sg13g2_io cells (the numbers are the library's; nothing is copied from it), a PDK
directory that holds it, and a chip section for the counter project."""

from __future__ import annotations

from pathlib import Path

PAD = 80.0
DEPTH = 180.0
SUPPLY_PINS = (
    ("vdd", "POWER", "TopMetal1", (0.0, 140.0, 80.0, 158.0)),
    ("vss", "GROUND", "TopMetal1", (0.0, 160.0, 80.0, 178.0)),
    ("iovdd", "POWER", "TopMetal2", (0.0, 40.0, 80.0, 70.0)),
    ("iovss", "GROUND", "TopMetal2", (0.0, 80.0, 80.0, 110.0)),
)


def _pin(name: str, direction: str, use: str, ports: list[list[tuple[str, tuple]]]) -> str:
    lines = [f"  PIN {name}", f"    DIRECTION {direction} ;", f"    USE {use} ;"]
    for port in ports:
        lines.append("    PORT")
        for layer, (a, b, c, d) in port:
            lines += [f"      LAYER {layer} ;", f"        RECT {a} {b} {c} {d} ;"]
        lines.append("    END")
    lines.append(f"  END {name}")
    return "\n".join(lines)


def _macro(name: str, w: float, pins: list[str]) -> str:
    supplies = [
        _pin(n, "INOUT", u, [[(lay, (r[0], r[1], min(r[2], w), r[3]))]])
        for n, u, lay, r in SUPPLY_PINS
    ]
    return "\n".join(
        [
            f"MACRO {name}",
            "  CLASS PAD ;",
            f"  SIZE {w} BY {DEPTH} ;",
            *pins,
            *supplies,
            f"END {name}",
        ]
    )


BOND = [("Metal2", (5.0, 0.0, 75.0, 3.0))]


def io_lef() -> str:
    cells = [
        _macro(
            "sg13g2_IOPadIn",
            PAD,
            [
                _pin("pad", "INOUT", "SIGNAL", [BOND]),
                _pin(
                    "p2c",
                    "OUTPUT",
                    "SIGNAL",
                    [
                        [
                            ("Metal2", (39.83, 157.81, 40.12, 180.0)),
                            ("Metal3", (39.725, 179.71, 40.225, 180.0)),
                        ]
                    ],
                ),
            ],
        ),
        _macro(
            "sg13g2_IOPadOut4mA",
            PAD,
            [
                _pin("pad", "INOUT", "SIGNAL", [BOND]),
                _pin("c2p", "INPUT", "SIGNAL", [[("Metal3", (38.145, 179.71, 41.855, 180.0))]]),
            ],
        ),
        _macro(
            "sg13g2_IOPadAnalog",
            PAD,
            [
                _pin(
                    "pad", "INOUT", "SIGNAL", [BOND, [("Metal3", (26.105, 179.71, 50.875, 180.0))]]
                ),
                _pin("padres", "INOUT", "SIGNAL", [[("Metal3", (57.355, 179.71, 57.855, 180.0))]]),
            ],
        ),
        *(_macro(f"sg13g2_IOPad{n}", PAD, []) for n in ("Vdd", "Vss", "IOVdd", "IOVss")),
        _macro("sg13g2_Corner", DEPTH, []),
        *(_macro(f"sg13g2_Filler{int(w * 200)}", w, []) for w in (1.0, 2.0, 5.0, 10.0, 20.0, 50.0)),
    ]
    return "VERSION 5.8 ;\n" + "\n".join(cells) + "\nEND LIBRARY\n"


def fake_pdk(root: Path) -> Path:
    """An ``ihp-sg13g2`` directory with the IO library's views and KLayout's directory."""
    pdk = root / "IHP-Open-PDK" / "ihp-sg13g2"
    io = pdk / "libs.ref" / "sg13g2_io"
    for sub, text in (("lef", io_lef()), ("gds", ""), ("cdl", CDL)):
        (io / sub).mkdir(parents=True, exist_ok=True)
        (io / sub / f"sg13g2_io.{sub}").write_text(text)
    (pdk / "libs.tech" / "klayout" / "tech").mkdir(parents=True)
    return pdk


CDL = """\
.SUBCKT sg13g2_IOPadIn iovdd iovss pad p2c vdd vss
.ENDS
.SUBCKT sg13g2_IOPadOut4mA c2p iovdd iovss pad vdd vss
.ENDS
.SUBCKT sg13g2_IOPadAnalog iovdd iovss pad padres vdd vss
.ENDS
.SUBCKT sg13g2_IOPadVdd iovdd iovss vdd vss
.ENDS
.SUBCKT sg13g2_IOPadVss iovdd iovss vdd vss
.ENDS
.SUBCKT sg13g2_IOPadIOVdd iovdd iovss vdd vss
.ENDS
.SUBCKT sg13g2_IOPadIOVss iovdd iovss vdd vss
.ENDS
.SUBCKT sg13g2_Corner iovdd iovss vdd vss
.ENDS
"""

# The counter project's chip: 4 pads a side.
COUNTER_CHIP = """\
chip:
  pads:
    south: [clk_i, rst_ni, vdd, vss]
    east:
      - en_i
      - q_o[0]
      - iovdd
      - iovss
    north:
      - q_o[1]
      - q_o[2]
      - q_o[3]
      - null
    west:
      - q_o[4]
      - q_o[5]
      - {analog: vin}
      - null
"""


SRAM = "RM_IHPSG13_1P_64x8_test"


def sram(pdk: Path) -> str:
    """A stand-in SRAM in ``pdk``'s sg13g2_sram library: 100 x 50 um, supply stripes on
    Metal4 across the block, a data bus."""
    lib = pdk / "libs.ref" / "sg13g2_sram"
    for sub in ("lef", "gds", "lib", "cdl"):
        (lib / sub).mkdir(parents=True, exist_ok=True)
    pins = [
        _pin("VDD!", "INOUT", "POWER", [[("Metal4", (10.0, 0.0, 12.0, 50.0))]]),
        _pin("VSS!", "INOUT", "GROUND", [[("Metal4", (20.0, 0.0, 22.0, 50.0))]]),
        *(
            _pin(f"A_DIN[{i}]", "INPUT", "SIGNAL", [[("Metal2", (30.0 + i, 0.0, 30.2 + i, 1.0))]])
            for i in range(8)
        ),
    ]
    (lib / "lef" / f"{SRAM}.lef").write_text(
        "\n".join([f"MACRO {SRAM}", "  CLASS BLOCK ;", "  SIZE 100 BY 50 ;", *pins, f"END {SRAM}"])
        + "\n"
    )
    (lib / "gds" / f"{SRAM}.gds").write_bytes(_gds(SRAM))
    (lib / "lib" / f"{SRAM}_typ_1p20V_25C.lib").write_text(f"library ({SRAM}) {{ }}\n")
    (lib / "cdl" / f"{SRAM}.cdl").write_text(f".SUBCKT {SRAM} VDD! VSS!\n.ENDS\n")
    return SRAM


def _gds(cell: str) -> bytes:
    import struct

    def rec(rtype: int, dtype: int, body: bytes = b"") -> bytes:
        return struct.pack(">HBB", 4 + len(body), rtype, dtype) + body

    name = cell.encode() + b"\0" * (len(cell) % 2)
    units = bytes([0x3E, 0x41, 0x89, 0x37, 0x4B, 0xC6, 0xA7, 0xF0])  # 1e-3
    meters = bytes([0x39, 0x44, 0xB8, 0x2F, 0xA0, 0x9B, 0x5A, 0x54])  # 1e-9
    return (
        rec(0x00, 2, struct.pack(">h", 600))
        + rec(0x01, 2, b"\0" * 24)
        + rec(0x02, 6, b"lib\0")
        + rec(0x03, 5, units + meters)
        + rec(0x05, 2, b"\0" * 24)
        + rec(0x06, 6, name)
        + rec(0x07, 0)
        + rec(0x04, 0)
    )


def sram_1p(
    pdk: Path, words: int, bits: int, size: tuple[float, float], bm: bool = True, mux: int = 2
) -> str:
    """A stand-in for one of IHP's 1P macros, named as the PDK names them, with every view
    and every port of the real one (BIST and bit mask with ``bm``)."""
    from chipleap.macro.sram import CORNERS, Sram, ports

    cell = f"RM_IHPSG13_1P_{words}x{bits}_c{mux}" + ("_bm_bist" if bm else "")
    lib = pdk / "libs.ref" / "sg13g2_sram"
    for sub in ("lef", "gds", "lib", "cdl", "verilog"):
        (lib / sub).mkdir(parents=True, exist_ok=True)
    w, h = size
    pins = [
        _pin("VDD!", "INOUT", "POWER", [[("Metal4", (10.0, 0.0, 12.81, 38.8))]]),
        _pin("VDDARRAY!", "INOUT", "POWER", [[("Metal4", (20.0, 38.8, 22.81, h))]]),
        _pin("VSS!", "INOUT", "GROUND", [[("Metal4", (30.0, 0.0, 32.81, h))]]),
    ]
    x = 40.0
    for name, direction, width in ports(Sram(cell, words, bits, w, h, bm)):
        for bit in range(max(width, 1)):
            pin = f"{name}[{bit}]" if width else name
            rect = ("Metal2", (x, 0.0, x + 0.2, 1.0))
            pins.append(_pin(pin, direction.upper(), "SIGNAL", [[rect]]))
            x += 0.5
    (lib / "lef" / f"{cell}.lef").write_text(
        "\n".join(
            [f"MACRO {cell}", "  CLASS BLOCK ;", f"  SIZE {w} BY {h} ;", *pins, f"END {cell}"]
        )
        + "\n"
    )
    (lib / "gds" / f"{cell}.gds").write_bytes(_gds(cell))
    for corner in CORNERS.values():
        (lib / "lib" / f"{cell}_{corner}.lib").write_text(f"library ({cell}_{corner}) {{ }}\n")
    (lib / "cdl" / f"{cell}.cdl").write_text(f".SUBCKT {cell} A_CLK VDD! VSS!\n.ENDS\n")
    (lib / "verilog" / f"{cell}.v").write_text(f"module {cell} ();\nendmodule\n")
    return cell
