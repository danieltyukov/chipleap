"""Render a ChipLeap floorplan as a packaged die with Blender (Cycles).

    blender -b -P scripts/render_die.py -- floorplan.json out.png [width height samples]
    blender -b -P scripts/render_die.py -- floorplan.json out.json W H project

The second form renders nothing: it writes where each block's centre lands in the image
(left and top as fractions), which the website uses to place its callouts.

floorplan.json is chipleap.report.floorplan.Floorplan.to_dict() plus an optional
"rows"/"cols" for the PE array. The die is drawn to scale from it: SRAM banks with a
bit-cell grid, logic as standard-cell rows, the PE array as tiles, the memory interface as
I/O slices, a top-metal power grid, a pad ring and gold bond wires to the package.
"""

from __future__ import annotations

import json
import math
import random
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1 :]
with open(argv[0]) as fh:
    fp = json.load(fh)
OUT = argv[1]
W = int(argv[2]) if len(argv) > 2 else 2000
H = int(argv[3]) if len(argv) > 3 else 1400
SAMPLES = int(argv[4]) if len(argv) > 4 and argv[4] != "project" else 128
PROJECT = len(argv) > 4 and argv[4] == "project"
rng = random.Random(7)

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
side = fp["side"]
S = 1.0 / side * 3.0  # scale the die to 3 scene units
DIE_T = 0.12
TOP = DIE_T


def principled(name, color, metallic=0.0, rough=0.4, film=0.0, film_ior=1.45, coat=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = rough
    if film and "Thin Film Thickness" in bsdf.inputs:
        bsdf.inputs["Thin Film Thickness"].default_value = film
        bsdf.inputs["Thin Film IOR"].default_value = film_ior
    if coat and "Coat Weight" in bsdf.inputs:
        bsdf.inputs["Coat Weight"].default_value = coat
    return m, bsdf


def textured(name, kind, base, accent, scale, metallic=0.6, rough=0.35, film=0.0):
    """A material whose colour follows a procedural pattern in object space."""
    m, bsdf = principled(name, base, metallic, rough, film)
    nt = m.node_tree
    coord = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = scale
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = (*base, 1)
    ramp.color_ramp.elements[1].color = (*accent, 1)
    if kind == "bricks":  # bit cells
        tex = nt.nodes.new("ShaderNodeTexBrick")
        tex.inputs["Scale"].default_value = 1.0  # the mapping node sets the frequency
        tex.inputs["Mortar Size"].default_value = 0.08
        tex.inputs["Brick Width"].default_value = 0.5
        tex.inputs["Row Height"].default_value = 0.25
        tex.offset = 0.0
        nt.links.new(mapping.outputs["Vector"], tex.inputs["Vector"])
        nt.links.new(tex.outputs["Fac"], ramp.inputs["Fac"])
    elif kind == "rows":  # standard-cell rows: bands broken up by noise
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.wave_type = "BANDS"
        wave.bands_direction = "Y"
        wave.inputs["Scale"].default_value = 1.0
        wave.inputs["Distortion"].default_value = 0.0
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 6.0
        mix = nt.nodes.new("ShaderNodeMath")
        mix.operation = "MULTIPLY"
        nt.links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
        nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
        nt.links.new(wave.outputs["Fac"], mix.inputs[0])
        nt.links.new(noise.outputs["Fac"], mix.inputs[1])
        nt.links.new(mix.outputs["Value"], ramp.inputs["Fac"])
    elif kind == "stripes":  # I/O slices
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.wave_type = "BANDS"
        wave.bands_direction = "X"
        wave.inputs["Scale"].default_value = 1.0
        nt.links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
        nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.35
    bump.inputs["Distance"].default_value = 0.002
    nt.links.new(ramp.outputs["Color"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def box(name, x, y, z, w, d, h, mat):
    bpy.ops.mesh.primitive_cube_add(size=1, location=(x + w / 2, y + d / 2, z + h / 2))
    o = bpy.context.active_object
    o.name = name
    o.scale = (w, d, h)
    bpy.ops.object.transform_apply(scale=True)
    o.data.materials.append(mat)
    return o


def bevel(o, width, segments=3):
    mod = o.modifiers.new("bevel", "BEVEL")
    mod.width = width
    mod.segments = segments
    return o


# Materials
silicon, _ = principled("silicon", (0.05, 0.06, 0.08), metallic=0.55, rough=0.22, film=420)
substrate, _ = principled("substrate", (0.62, 0.64, 0.63), rough=0.42, coat=0.3)
substrate_edge, _ = principled("substrate-edge", (0.035, 0.30, 0.20), rough=0.45, coat=0.4)
gold, _ = principled("gold", (1.0, 0.76, 0.33), metallic=1.0, rough=0.22)
alu, _ = principled("aluminium", (0.80, 0.81, 0.83), metallic=1.0, rough=0.3)
strap_m, _ = principled("strap", (0.55, 0.56, 0.58), metallic=1.0, rough=0.45)
copper, _ = principled("copper", (0.95, 0.55, 0.36), metallic=1.0, rough=0.28)
mats = {
    "sram": textured(
        "sram",
        "bricks",
        (0.08, 0.09, 0.13),
        (0.30, 0.34, 0.44),
        (26, 26, 1),
        metallic=0.75,
        rough=0.24,
        film=300,
    ),
    "buffer": textured(
        "buffer",
        "bricks",
        (0.07, 0.11, 0.10),
        (0.26, 0.40, 0.36),
        (34, 34, 1),
        metallic=0.75,
        rough=0.24,
        film=360,
    ),
    "soc": textured(
        "soc",
        "rows",
        (0.09, 0.09, 0.08),
        (0.36, 0.37, 0.32),
        (22, 22, 1),
        metallic=0.55,
        rough=0.32,
        film=520,
    ),
    "cpu": textured(
        "cpu",
        "rows",
        (0.13, 0.08, 0.05),
        (0.52, 0.34, 0.20),
        (26, 26, 1),
        metallic=0.7,
        rough=0.3,
        film=480,
    ),
    "phy": textured(
        "phy",
        "stripes",
        (0.07, 0.08, 0.10),
        (0.28, 0.30, 0.37),
        (30, 30, 1),
        metallic=0.65,
        rough=0.3,
        film=380,
    ),
}
pe_mat = textured(
    "pe",
    "bricks",
    (0.04, 0.16, 0.11),
    (0.18, 0.48, 0.34),
    (60, 60, 1),
    metallic=0.75,
    rough=0.25,
    film=600,
)
pe_gap, _ = principled("pe-gap", (0.05, 0.12, 0.10), metallic=0.4, rough=0.4)

# Package and die
pkg = 5.0
# A two-layer package: a solder-mask green laminate under a light top layer.
box("laminate", -pkg / 2, -pkg / 2, -0.62, pkg, pkg, 0.42, substrate_edge)
bevel(bpy.context.active_object, 0.05)
box("package", -pkg / 2 + 0.04, -pkg / 2 + 0.04, -0.2, pkg - 0.08, pkg - 0.08, 0.2, substrate)
bevel(bpy.context.active_object, 0.06)
die_w = side * S
off = -die_w / 2
die = box("die", off, off, 0.0, die_w, die_w, DIE_T, silicon)
bevel(die, 0.01, 2)

# Blocks (scene x, y from floorplan mm)
for b in fp["blocks"]:
    x, y, w, d = off + b["x"] * S, off + b["y"] * S, b["w"] * S, b["h"] * S
    inset = 0.012
    if b["kind"] == "accel":
        rows, cols = fp.get("rows", 4), fp.get("cols", 4)
        box("pe-bg", x + inset, y + inset, TOP, w - 2 * inset, d - 2 * inset, 0.004, pe_gap)
        tw, td = (w - 2 * inset) / cols, (d - 2 * inset) / rows
        for i in range(rows):
            for j in range(cols):
                box(
                    f"pe{i}_{j}",
                    x + inset + j * tw + tw * 0.08,
                    y + inset + i * td + td * 0.08,
                    TOP + 0.004,
                    tw * 0.84,
                    td * 0.84,
                    0.006,
                    pe_mat,
                )
    else:
        box(
            b["id"], x + inset, y + inset, TOP, w - 2 * inset, d - 2 * inset, 0.006, mats[b["kind"]]
        )

# Top-metal power grid over the core
pad_band = fp.get("pad_ring", 0.06) * die_w
core0, core1 = off + pad_band, off + die_w - pad_band
# Top metal: dense thin straps in one direction, a coarser layer across them, and a
# power ring around the core, the way real power grids look from above.
fine = 34
for i in range(1, fine):
    t = core0 + (core1 - core0) * i / fine
    box(f"strap-v{i}", t - 0.0025, core0, TOP + 0.011, 0.005, core1 - core0, 0.002, strap_m)
coarse = 7
for i in range(1, coarse):
    t = core0 + (core1 - core0) * i / coarse
    box(f"strap-h{i}", core0, t - 0.005, TOP + 0.014, core1 - core0, 0.01, 0.002, strap_m)
for name, (rx, ry, rw, rd) in {
    "ring-s": (core0, core0, core1 - core0, 0.016),
    "ring-n": (core0, core1 - 0.016, core1 - core0, 0.016),
    "ring-w": (core0, core0, 0.016, core1 - core0),
    "ring-e": (core1 - 0.016, core0, 0.016, core1 - core0),
}.items():
    box(name, rx, ry, TOP + 0.011, rw, rd, 0.004, strap_m)
# Seal ring at the die edge.
seal = 0.012
for name, (rx, ry, rw, rd) in {
    "seal-s": (off + 0.01, off + 0.01, die_w - 0.02, seal),
    "seal-n": (off + 0.01, off + die_w - 0.01 - seal, die_w - 0.02, seal),
    "seal-w": (off + 0.01, off + 0.01, seal, die_w - 0.02),
    "seal-e": (off + die_w - 0.01 - seal, off + 0.01, seal, die_w - 0.02),
}.items():
    box(name, rx, ry, TOP, rw, rd, 0.004, alu)

# Pad ring and bond wires
n_pads = max(8, int(die_w / 0.2))
pitch = (die_w - 2 * pad_band) / n_pads
finger_r = die_w / 2 + 0.75
wires = bpy.data.curves.new("wires", "CURVE")
wires.dimensions = "3D"
wires.bevel_depth = 0.0075
wires.bevel_resolution = 3
for sidx in range(4):
    for k in range(n_pads):
        u = core0 + pitch * (k + 0.5)
        local = Vector((u, off + pad_band * 0.5, 0))
        ang = sidx * math.pi / 2
        rot = Vector(
            (
                local.x * math.cos(ang) - local.y * math.sin(ang),
                local.x * math.sin(ang) + local.y * math.cos(ang),
                0,
            )
        )
        ps = pad_band * 0.55
        box(f"pad{sidx}_{k}", rot.x - ps / 2, rot.y - ps / 2, TOP, ps, ps, 0.008, alu)
        out = rot.normalized() * 0 + rot
        direction = Vector((math.cos(ang - math.pi / 2), math.sin(ang - math.pi / 2), 0))
        finger = rot + direction * (finger_r - die_w / 2 + pad_band * 0.5)
        box(f"finger{sidx}_{k}", finger.x - 0.05, finger.y - 0.05, 0.0, 0.1, 0.1, 0.012, gold)
        sp = wires.splines.new("BEZIER")
        sp.bezier_points.add(1)
        p0, p1 = sp.bezier_points
        p0.co = (out.x, out.y, TOP + 0.01)
        p1.co = (finger.x, finger.y, 0.012)
        lift = 0.26 + rng.uniform(-0.02, 0.02)
        p0.handle_left_type = p0.handle_right_type = "FREE"
        p1.handle_left_type = p1.handle_right_type = "FREE"
        p0.handle_right = (out.x + direction.x * 0.15, out.y + direction.y * 0.15, TOP + lift)
        p0.handle_left = p0.co
        p1.handle_left = (finger.x - direction.x * 0.35, finger.y - direction.y * 0.35, 0.12)
        p1.handle_right = p1.co
wobj = bpy.data.objects.new("wires", wires)
wobj.data.materials.append(gold)
scene.collection.objects.link(wobj)

# Laser marking on the package
mark_m, _ = principled("marking", (0.32, 0.33, 0.33), rough=0.6)
for i, (txt, size, dy) in enumerate(
    ((fp.get("mark", "ChipLeap"), 0.30, 0.0), (fp.get("submark", ""), 0.14, -0.30))
):
    if not txt:
        continue
    curve = bpy.data.curves.new(f"mark{i}", "FONT")
    curve.body = txt
    curve.size = size
    curve.extrude = 0.002
    curve.align_x = "LEFT"
    obj = bpy.data.objects.new(f"mark{i}", curve)
    obj.location = (-pkg / 2 + 0.35, -pkg / 2 + 0.55 + dy + 0.3, 0.001)
    obj.data.materials.append(mark_m)
    scene.collection.objects.link(obj)

# Shadow catcher floor
bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 0, -0.62))
floor = bpy.context.active_object
floor.is_shadow_catcher = True


# Lights
def area_light(name, loc, energy, size, color=(1, 1, 1)):
    data = bpy.data.lights.new(name, "AREA")
    data.energy = energy
    data.size = size
    data.color = color
    o = bpy.data.objects.new(name, data)
    o.location = loc
    scene.collection.objects.link(o)
    direction = Vector((0, 0, 0)) - Vector(loc)
    o.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return o


area_light("key", (-6, -5, 9), 2600, 7)
area_light("fill", (7, -3, 5), 700, 6, (0.85, 0.92, 1.0))
area_light("rim", (2, 8, 4), 1400, 4, (1.0, 0.93, 0.85))

world = bpy.data.worlds.new("world")
scene.world = world
world.use_nodes = True
bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
bg.inputs["Color"].default_value = (0.95, 0.96, 0.95, 1)
bg.inputs["Strength"].default_value = 0.35

# Camera
cam_data = bpy.data.cameras.new("cam")
cam_data.lens = 70
cam_data.dof.use_dof = True
cam_data.dof.aperture_fstop = 5.6
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
cam.location = (7.4, -10.6, 9.4)
target = Vector((0.35, -0.3, -0.1))
cam.rotation_euler = (target - cam.location).to_track_quat("-Z", "Y").to_euler()
cam_data.dof.focus_distance = (target - cam.location).length
scene.camera = cam

# Where each block lands in the image, for the website's callouts.
if PROJECT:
    from bpy_extras.object_utils import world_to_camera_view

    scene.render.resolution_x = W
    scene.render.resolution_y = H
    bpy.context.view_layer.update()  # refresh the camera's world matrix
    out = {}
    for b in fp["blocks"]:
        cx = off + (b["x"] + b["w"] / 2) * S
        cy = off + (b["y"] + b["h"] / 2) * S
        v = world_to_camera_view(scene, cam, Vector((cx, cy, TOP + 0.01)))
        out[b["id"]] = [round(v.x, 4), round(1 - v.y, 4)]
    with open(OUT, "w") as fh:
        json.dump(out, fh, indent=1)
    print("projected", OUT)
    raise SystemExit(0)

# Render settings
scene.render.engine = "CYCLES"
scene.cycles.samples = SAMPLES
scene.cycles.use_denoising = True
scene.render.film_transparent = True
scene.render.resolution_x = W
scene.render.resolution_y = H
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA"
scene.view_settings.view_transform = "AgX"
scene.view_settings.look = (
    "AgX - Medium High Contrast"
    if "AgX - Medium High Contrast"
    in [i.identifier for i in scene.view_settings.bl_rna.properties["look"].enum_items]
    else "None"
)
scene.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print("rendered", OUT)
