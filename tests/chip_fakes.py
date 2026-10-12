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
