# ChipLeap's GDS precheck, run by KLayout:
#   klayout -b -r precheck.py -rd plan=PLAN.json
# Facts about the final GDS that a shuttle's precheck looks at: the top cells, every
# layer used, cell names, shapes without area, the PR boundary and the outline's grid.
# Writes them as JSON; ChipLeap decides what passes.
import json

import pya

with open(globals()["plan"]) as fh:
    plan = json.load(fh)

ly = pya.Layout()
ly.read(plan["input"])
tops = [c.name for c in ly.top_cells()]
layers = sorted(
    {
        (ly.get_info(li).layer, ly.get_info(li).datatype)
        for li in ly.layer_indices()
        if not all(c.shapes(li).is_empty() for c in ly.each_cell())
    }
)
names = [c.name for c in ly.each_cell()]
zero = 0
for li in ly.layer_indices():
    for cell in ly.each_cell():
        for s in cell.shapes(li).each():
            if (s.is_box() or s.is_path() or s.is_polygon()) and s.area() == 0:
                zero += 1
top = ly.top_cells()[0] if tops else None
boundary = ly.find_layer(189, 4)
pr = []
if top is not None and boundary is not None:
    for s in top.shapes(boundary).each():
        b = s.dbbox()
        pr.append([b.left, b.bottom, b.right, b.top])
bbox = top.bbox() if top is not None else pya.Box()
report = {
    "top_cells": tops[:10],
    "layers": [list(x) for x in layers],
    "cells": len(names),
    "bad_cell_names": [n for n in names if "#" in n or "/" in n][:20],
    "zero_area_shapes": zero,
    "pr_boundary": pr[:4],
    "bbox_dbu": [bbox.left, bbox.bottom, bbox.right, bbox.top],
    "dbu_um": ly.dbu,
}
with open(plan["output"], "w") as fh:
    json.dump(report, fh)
print("chipleap precheck:", len(layers), "layers,", len(names), "cells")
