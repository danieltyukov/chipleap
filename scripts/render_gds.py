"""Render a GDS layout to PNG with KLayout, in the PDK's own layer colours.

    klayout -zz -r scripts/render_gds.py -rd gds=final.gds -rd lyp=sg13g2.lyp \
        -rd out=layout.png [-rd width=2400] [-rd layers=Metal1,Metal2,...] [-rd bg=#0d1512] \
        [-rd box=x0,y0,x1,y1]

``box`` is the area to draw, in um; without it, the whole layout with KLayout's margin.
The website's chip picture passes the chip's outline, so a point of the layout maps to a
known place in the image and the callouts land on what they name.

The website shows the placed and routed accelerator this way: a picture of the actual
layout LibreLane produced, not an illustration.
"""

import pya  # provided by KLayout

gds = globals()["gds"]
lyp = globals()["lyp"]
out = globals()["out"]
width = int(globals().get("width", 2400))
keep = [s for s in str(globals().get("layers", "")).split(",") if s]
bg = str(globals().get("bg", "#ffffff"))

view = pya.LayoutView()
view.load_layout(gds, True)
view.load_layer_props(lyp)
view.max_hier()
view.set_config("background-color", bg)
view.set_config("grid-visible", "false")
view.set_config("text-visible", "false")
view.set_config("cell-box-visible", "false")
if keep:
    it = view.begin_layers()
    while not it.at_end():
        node = it.current()
        name = node.source_name if hasattr(node, "source_name") else ""
        label = node.name or name
        node.visible = any(k == label or label.startswith(k + ".") for k in keep)
        it.next()
given = str(globals().get("box", ""))
if given:
    x0, y0, x1, y1 = (float(v) for v in given.split(","))
    box = pya.DBox(x0, y0, x1, y1)
    view.zoom_box(box)
else:
    view.zoom_fit()
    box = view.box()
height = round(width * box.height() / box.width())
view.save_image_with_options(out, width, height, 0, 2, 0, box, False)
print(f"rendered {out} ({width}x{height})")
