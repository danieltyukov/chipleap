# ChipLeap's chip finishing, run by KLayout:
#   klayout -b -r finish.py -rd plan=PLAN.json
# Bond pads (IHP's bondpad pcell) at the centres the plan gives, IHP's seal ring around
# the die and a PR boundary on the die when the layout has none; the plan comes from
# ChipLeap, and every value is checked again here before it reaches the layout.
import json
import math
import re
import sys

import pya

with open(globals()["plan"]) as fh:
    plan = json.load(fh)

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
LENGTH = re.compile(r"[0-9]{1,5}(\.[0-9]{1,3})?u")


def number(v):
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or abs(v) > 1e5
    ):
        raise SystemExit(f"chipleap finish: {v!r} is not a coordinate")
    return float(v)


top_name = plan["top"]
if not NAME.fullmatch(top_name):
    raise SystemExit("chipleap finish: the top cell's name is not a plain name")
bond = plan["bond_size"]
seal_w, seal_l = plan["seal_w"], plan["seal_l"]
for v in (bond, seal_w, seal_l):
    if not isinstance(v, str) or not LENGTH.fullmatch(v):
        raise SystemExit(f"chipleap finish: {v!r} is not a length")

for p in (plan["pdk_python"] + "/pycell4klayout-api/source/python", plan["pdk_python"]):
    if p not in sys.path:
        sys.path.insert(0, p)
import sg13g2_pycell_lib  # noqa: E402, F401  (registers the SG13_dev library)

ly = pya.Layout()
ly.read(plan["input"])
top = ly.cell(top_name)
if top is None:
    raise SystemExit(f"chipleap finish: no cell {top_name}")
um = 1.0 / ly.dbu


def at(x, y):
    return pya.Trans(pya.Vector(round(number(x) * um), round(number(y) * um)))


# The pcells build only from string parameters: given floats, their code never returns.
pad = ly.create_cell("bondpad", "SG13_dev", {"shape": "square", "diameter": bond})
for x, y in plan["bond_centers"]:
    top.insert(pya.CellInstArray(pad.cell_index(), at(x, y)))
seal = ly.create_cell("sealring", "SG13_dev", {"w": seal_w, "l": seal_l})
top.insert(pya.CellInstArray(seal.cell_index(), at(*plan["seal_origin"])))

x0, y0, x1, y1 = (round(number(v) * um) for v in plan["boundary"])
boundary = ly.layer(189, 4)
if top.shapes(boundary).is_empty():
    top.shapes(boundary).insert(pya.Box(x0, y0, x1, y1))

opts = pya.SaveLayoutOptions()
opts.write_context_info = False
ly.write(plan["output"], opts)
print("chipleap finish:", top.dbbox(), "bond pads", len(plan["bond_centers"]))
