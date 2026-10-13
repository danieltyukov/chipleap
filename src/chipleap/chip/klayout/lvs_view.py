# ChipLeap's LVS view of a chip, run by KLayout:
#   klayout -b -r lvs_view.py -rd plan=PLAN.json
# The chip without its IO cells, bond pads and seal ring, which IHP's LVS deck cannot
# compare; each IO cell's core-side pins stay as metal labelled with their nets, so
# every route into the ring still ends on a net the netlist names.
import json
import math
import re

import pya

with open(globals()["plan"]) as fh:
    plan = json.load(fh)

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NET = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def number(v):
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or abs(v) > 1e5
    ):
        raise SystemExit(f"chipleap lvs view: {v!r} is not a coordinate")
    return float(v)


if not NAME.fullmatch(plan["top"]):
    raise SystemExit("chipleap lvs view: the top cell's name is not a plain name")
ly = pya.Layout()
ly.read(plan["input"])
top = ly.cell(plan["top"])
if top is None:
    raise SystemExit(f"chipleap lvs view: no cell {plan['top']}")
um = 1.0 / ly.dbu
prefixes = tuple(plan["drop_prefixes"])

for inst in list(top.each_inst()):
    if inst.cell.name.startswith(prefixes):
        inst.delete()

# Top-level shapes outside the IO ring's core-side edge: the routes to the pads' bond
# sides and the chip's pins there.
x0, y0, x1, y1 = (round(number(v) * um) for v in plan["keep_box"])
keep = pya.Box(x0, y0, x1, y1)
for li in ly.layer_indices():
    shapes = top.shapes(li)
    gone = [s for s in shapes.each() if not s.bbox().overlaps(keep)]
    for s in gone:
        shapes.erase(s)

for pin in plan["pins"]:
    if not NET.fullmatch(pin["net"]):
        raise SystemExit(f"chipleap lvs view: {pin['net']!r} is not a net name")
    for layer, rect in pin["rects"]:
        if not isinstance(layer, int) or not 0 < layer < 256:
            raise SystemExit(f"chipleap lvs view: {layer!r} is not a layer")
        a, b, c, d = (round(number(v) * um) for v in rect)
        box = pya.Box(a, b, c, d)
        top.shapes(ly.layer(layer, 0)).insert(box)
        top.shapes(ly.layer(layer, 25)).insert(pya.Text(pin["net"], pya.Trans(box.center())))

ly.cleanup()
opts = pya.SaveLayoutOptions()
opts.write_context_info = False
ly.write(plan["output"], opts)
print("chipleap lvs view:", top.dbbox(), "pins", len(plan["pins"]))
